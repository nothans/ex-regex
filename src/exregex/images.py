"""Pictures in the state, for models that read them (Jev 1.13 and 1.14 through OpenRouter).

    photo = ex.image("kitchen.jpg")
    ex.ask({"photo": photo}, {"mug": ex.Noul("`photo` shows a coffee mug"), "where": ex.Box("the coffee mug in `photo`")})

`image()` returns a Chat Completions image part, the shape OpenRouter's Decisions endpoint
reads at any depth in the state. TypeSafe's own API takes pictures in a separate `assets` map
instead; the `typesafe()` backend translates (see `to_assets`), so the same state works on both.

The size is checked before sending, because one bad picture fails the whole request. The
documented range is 256 to 1,568 pixels a side; measured through OpenRouter on 2026-10-15, every
side from 256 to 279 failed, so the floor here is 280. There is no resizing: ex-regex has no
dependencies, so shrink or enlarge the file yourself (Pillow, ffmpeg) when `LimitError` says so.

Put one picture in a request. Measured on 2026-10-15, a request with several pictures sometimes
answered about one item from another item's picture.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
from pathlib import Path
from typing import Any, Union

from .errors import LimitError

MIN_SIDE = 280
MAX_SIDE = 1568
MIN_PIXELS = 78_400
MAX_PIXELS = 2_458_624

_MIME = {"png": "image/png", "jpeg": "image/jpeg"}


def image_size(data: bytes) -> tuple[str, int, int]:
    """(format, width, height) of a PNG or JPEG, read from its header."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7 or marker == 0xFF:
                i += 1 if marker == 0xFF else 2
                continue
            (length,) = struct.unpack(">H", data[i + 2 : i + 4])
            # Start-of-frame markers carry the size; C4, C8, and CC are tables, not frames.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[i + 5 : i + 9])
                return "jpeg", width, height
            i += 2 + length
        raise LimitError("image: a JPEG without a readable size")
    raise LimitError("image: only PNG and JPEG are accepted")


def image(source: Union[str, os.PathLike[str], bytes], *, check: bool = True) -> dict:
    """A PNG or JPEG as an image part for the state. `source` is a path or the file's bytes."""
    data = source if isinstance(source, bytes) else Path(os.fspath(source)).read_bytes()
    kind, width, height = image_size(data)
    if check:
        problems = []
        if min(width, height) < MIN_SIDE:
            problems.append(f"a side under {MIN_SIDE} px")
        if max(width, height) > MAX_SIDE:
            problems.append(f"a side over {MAX_SIDE} px")
        if not problems and not (MIN_PIXELS <= width * height <= MAX_PIXELS):
            problems.append(f"{width * height:,} pixels, outside {MIN_PIXELS:,}-{MAX_PIXELS:,}")
        if problems:
            raise LimitError(
                f"image: {width}x{height} has {' and '.join(problems)}; resize it to {MIN_SIDE}-{MAX_SIDE} px a side "
                "(check=False sends it anyway)"
            )
    url = f"data:{_MIME[kind]};base64,{base64.b64encode(data).decode('ascii')}"
    return {"type": "image_url", "image_url": {"url": url}}


_FORMATS = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg"}


def is_image(value: Any) -> bool:
    return isinstance(value, dict) and value.get("type") == "image_url" and isinstance(value.get("image_url"), dict)


def has_images(value: Any) -> bool:
    if is_image(value):
        return True
    if isinstance(value, dict):
        return any(has_images(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_images(v) for v in value)
    return False


def to_assets(value: Any, assets: dict) -> Any:
    """TypeSafe's shape: each image part becomes a reference string, and the image goes in
    `assets` under that reference. The reference is derived from the image's bytes, so the same
    picture always gets the same reference (and a request's bytes, and its cache key, are stable).
    """
    if is_image(value):
        url = value["image_url"].get("url", "")
        head, _, data = url.partition(",")
        mime = head.removeprefix("data:").split(";")[0].lower()
        if not head.startswith("data:") or ";base64" not in head or mime not in _FORMATS:
            raise LimitError("image: TypeSafe takes PNG or JPEG data, as built by exregex.image()")
        ref = "img-" + hashlib.sha256(data.encode("ascii")).hexdigest()[:16]
        assets[ref] = {"type": "image", "format": _FORMATS[mime], "data_base64": data}
        return ref
    if isinstance(value, dict):
        return {k: to_assets(v, assets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_assets(v, assets) for v in value]
    return value
