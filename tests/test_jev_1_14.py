"""Jev 1.14 support: Box questions, pictures in the state, and long-context requests."""

import base64
import struct
import zlib

import pytest

import exregex as ex
from exregex import BackendError, Box, BoxAnswer, Engine, LimitError, Noul, Scripted
from exregex._types import answer_to_wire, parse_answer
from exregex.backends import OpenAIDecisions, SystemOne
from exregex.engine import IMAGE_TOKENS, estimate_tokens


def png(width: int, height: int) -> bytes:
    """A real, decodable grayscale PNG of the given size."""
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def jpeg_header(width: int, height: int, *, sof: int = 0xC0) -> bytes:
    """The start of a JPEG: SOI, an APP0 segment, a DHT table (C4, not a frame), then the frame."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    dht = b"\xff\xc4" + struct.pack(">H", 5) + b"\x00\x00\x00"
    frame = bytes([0xFF, sof]) + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + dht + frame + b"\xff\xd9"


# ------------------------------------------------------------------ Box


def test_box_wire_and_instructions():
    assert Box("the mug in `photo`").to_wire() == {"type": "box", "instructions": "the mug in `photo`"}
    with pytest.raises(LimitError):
        Box("  ")


def test_box_answer_parse_round_trip_and_helpers():
    raw = {"type": "box", "box": {"top": 0.25, "bottom": 0.82, "left": 0.44, "right": 0.84}, "confidence": 0.92}
    a = parse_answer(raw)
    assert a == BoxAnswer(0.44, 0.25, 0.84, 0.82, 0.92)
    assert parse_answer(answer_to_wire(a)) == a
    assert a.pixels(1000, 500) == (440, 125, 840, 410)
    assert a.area == pytest.approx(0.4 * 0.57)
    padded = BoxAnswer(0.02, 0.1, 0.99, 0.5, 0.3).pad(0.05)
    assert (padded.left, padded.top, padded.right, padded.bottom) == (0.0, pytest.approx(0.05), 1.0, pytest.approx(0.55))


def test_box_answer_rejects_missing_and_crossed_edges():
    with pytest.raises(BackendError):
        parse_answer({"type": "box", "confidence": 0.5})
    with pytest.raises(BackendError):
        parse_answer({"type": "box", "box": {"top": 0.6, "bottom": 0.2, "left": 0.1, "right": 0.3}, "confidence": 0.5})
    with pytest.raises(BackendError):
        parse_answer({"type": "box", "box": {"top": 0.1, "bottom": 0.2, "left": "x", "right": 0.3}, "confidence": 0.5})


def test_engine_checks_box_answers_against_box_questions():
    box = {"type": "box", "box": {"top": 0.1, "bottom": 0.4, "left": 0.2, "right": 0.6}, "confidence": 0.8}
    engine = Engine(Scripted(lambda state, qs: {"where": box, "there": 0.97}), cache=None)
    out = engine.ask({"photo": "x"}, {"where": Box("the mug in `photo`"), "there": Noul("`photo` shows a mug")})
    assert isinstance(out["where"], BoxAnswer) and out["where"].confidence == 0.8
    wrong = Engine(Scripted(lambda state, qs: {"where": 0.9}), cache=None)
    with pytest.raises(BackendError):
        wrong.ask({"photo": "x"}, {"where": Box("the mug in `photo`")})


def test_openai_backend_refuses_box_before_sending():
    with pytest.raises(LimitError):
        OpenAIDecisions._question("where", Box("the mug"))


# ------------------------------------------------------------------ images


def test_image_size_reads_png_and_jpeg_headers():
    assert ex.image_size(png(300, 420)) == ("png", 300, 420)
    assert ex.image_size(jpeg_header(1024, 768)) == ("jpeg", 1024, 768)
    assert ex.image_size(jpeg_header(640, 480, sof=0xC2)) == ("jpeg", 640, 480)  # progressive
    with pytest.raises(LimitError):
        ex.image_size(b"GIF89a....")
    with pytest.raises(LimitError):
        ex.image_size(b"\xff\xd8\xff\xe0\x00\x10JFIF")  # no frame


def test_image_builds_a_data_url_part(tmp_path):
    data = png(320, 300)
    path = tmp_path / "pic.png"
    path.write_bytes(data)
    part = ex.image(path)
    assert part["type"] == "image_url"
    url = part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,") and base64.b64decode(url.split(",", 1)[1]) == data
    assert ex.image(data) == part


@pytest.mark.parametrize("size", [(279, 400), (256, 1000), (1600, 400)])
def test_image_rejects_sizes_jev_refuses(size):
    with pytest.raises(LimitError, match="resize"):
        ex.image(png(*size))
    assert ex.image(png(*size), check=False)["type"] == "image_url"


def test_image_accepts_the_measured_edges():
    for size in [(280, 280), (1568, 280), (280, 1568)]:
        ex.image(png(*size))


def test_estimate_counts_pictures_by_count_not_base64_length():
    part = ex.image(png(1000, 1000))
    with_picture = estimate_tokens({"photo": part, "note": "hi"}, {"q": Noul("Is it?")})
    without = estimate_tokens({"photo": "[image]", "note": "hi"}, {"q": Noul("Is it?")})
    assert with_picture == without + IMAGE_TOKENS
    assert estimate_tokens([part, part], {"q": Noul("Is it?")}) >= 2 * IMAGE_TOKENS


def test_picture_reaches_the_server_untouched(server):
    part = ex.image(png(300, 300))
    server.handler = lambda body: {"q": {"type": "noul", "noul": 0.9}}
    engine = Engine(SystemOne(server.url, model="m"), cache=None)
    engine.ask({"photo": part}, {"q": Noul("`photo` shows a gray square")})
    assert server.requests[0]["state"]["photo"] == part


# ------------------------------------------------------------------ long context


def test_long_context_sends_header_and_lifts_the_state_limit(server):
    server.handler = lambda body: {"q": {"type": "noul", "noul": 0.6}}
    plain = SystemOne(server.url, model="m")
    long = SystemOne(server.url, model="m", long_context=True)
    assert plain.max_state_chars == 60_000 and long.max_state_chars == 600_000
    assert SystemOne(server.url, model="m", long_context=True, max_state_chars=100_000).max_state_chars == 100_000
    Engine(long, cache=None).ask("x", {"q": Noul("Is it?")})
    Engine(plain, cache=None).ask("x", {"q": Noul("Is it?")})
    sent = [{k.lower(): v for k, v in h.items()} for h in server.headers]
    assert sent[0].get("typesafe-danger-zone-long-context") == "true"
    assert "typesafe-danger-zone-long-context" not in sent[1]


def test_long_context_lets_a_long_text_be_judged_whole(server):
    server.handler = lambda body: {name: {"type": "noul", "noul": 0.9} for name in body["questions"]}
    book = "It was a dark and stormy night. " * 4000  # 128,000 characters
    with pytest.raises(LimitError):
        ex.test("a story", book, unit="text", engine=Engine(SystemOne(server.url, model="m"), cache=None))
    assert ex.test("a story", book, unit="text", engine=Engine(SystemOne(server.url, model="m", long_context=True), cache=None))
