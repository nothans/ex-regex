import asyncio

import pytest

import exregex as ex
from exregex import Choice, Engine, LimitError, Noul, RefusedError, Scripted, SystemOne
from exregex._types import ChoiceAnswer, Refusal, ScoreAnswer
from exregex.testing import keyword_engine

from .conftest import keyword_answers, no_sleep

TICKET = """Hi, I'm Quasar Marmot. My blender broke after two days.
I would like my money back, please.
You can reach me at fixture_quasar_7k9@example.invalid or (415) 555-0177.
For anything else, our office is support@comet-tools.example.invalid.
Thanks!"""


@pytest.fixture
def engine():
    return keyword_engine(
        {"refund": ["money back", "refund"], "contact": ["fixture_quasar_7k9@", "555-0177"], "heading": ["#"], "thanks": ["thanks"]}
    )


def test_findall_returns_verbatim_spans(engine):
    hits = ex.findall("asks for a refund", TICKET, engine=engine)
    assert [m.text for m in hits] == ["I would like my money back, please."]
    m = hits[0]
    assert TICKET[m.start : m.end] == m.text and m.span() == (m.start, m.end) and m.group() == m.text and str(m) == m.text
    assert m.p == pytest.approx(0.95) and m.band == "yes" and m.unit == "sentence"
    with pytest.raises(IndexError):
        m.group(1)


def test_scan_returns_everything_with_p(engine):
    scanned = ex.scan("asks for a refund", TICKET, engine=engine)
    assert len(scanned) == len(ex.find_units(TICKET, "sentence"))
    assert sum(m.p >= 0.5 for m in scanned) == 1


def test_sub_with_candidate_unit_keeps_shared_address(engine):
    out = ex.sub("a way to contact a specific person", "[redacted]", TICKET, unit="contact", engine=engine)
    assert "fixture_quasar_7k9@example.invalid" not in out and "555-0177" not in out
    assert "support@comet-tools.example.invalid" in out
    assert out.count("[redacted]") == 2


def test_subn_count_and_callable(engine):
    out, n = ex.subn("a way to contact a specific person", lambda m: f"<{m.unit}>", TICKET, unit="contact", engine=engine)
    assert n == 2 and "<email>" in out and "<phone>" in out
    out1, n1 = ex.subn("a way to contact a specific person", "X", TICKET, count=1, unit="contact", engine=engine)
    assert n1 == 1 and "555-0177" in out1


def test_split_and_keep(engine):
    doc = "# Intro\nhello\nworld\n# Usage\nrun it\n"
    assert ex.split("a section heading", doc, unit="line", engine=engine) == ["hello\nworld", "run it"]
    assert ex.split("a section heading", doc, unit="line", keep=True, engine=engine) == ["# Intro", "hello\nworld", "# Usage", "run it"]


def test_test_and_verdict(engine):
    v = ex.test("asks for a refund", TICKET, engine=engine)
    assert v and float(v) == pytest.approx(0.95) and v.band == "yes"
    v2 = ex.test("asks for a refund", "The weather is nice.", engine=engine)
    assert not v2 and v2.band == "no"
    assert ex.Verdict(0.5).band == "review" and ex.Verdict(0.5, threshold=0.6).__bool__() is False


def test_threshold_controls_matching():
    e = Engine(Scripted(lambda s, qs: {k: 0.6 for k in qs}))
    assert len(ex.findall("x", "One. Two.", engine=e)) == 2
    assert ex.findall("x", "One. Two.", threshold=0.7, engine=e) == []


def test_filter_and_rank_keep_order(engine):
    items = ["thanks a lot", "refund now please", "money back!", "no idea"]
    assert ex.filter("asks for a refund", items, engine=engine) == ["refund now please", "money back!"]
    ranked = ex.rank("asks for a refund", items, engine=engine)
    assert {t for t, p in ranked[:2]} == {"refund now please", "money back!"} and ranked[-1][1] < 0.5


def test_packing_respects_max_questions():
    calls = []

    def handler(state, qs):
        calls.append(len(qs))
        return {k: 0.1 for k in qs}

    e = Engine(Scripted(handler), max_questions=32)
    text = " ".join(f"Sentence number {i}." for i in range(70))
    assert len(ex.scan("x", text, engine=e)) == 70
    assert sorted(calls) == [6, 32, 32]


def test_packing_respects_state_size():
    calls = []

    def handler(state, qs):
        calls.append(sum(len(v) for v in state["segments"].values()))
        return {k: 0.1 for k in qs}

    e = Engine(Scripted(handler, max_state_chars=2000))
    text = "\n".join(f"{i:03d}" + "y" * 300 for i in range(20))  # distinct, so the cache cannot hide requests
    ex.scan("x", text, unit="line", engine=e)
    assert len(calls) > 1 and max(calls) <= 2000


def test_one_huge_span_raises_a_clear_error():
    e = Engine(Scripted(lambda s, q: {k: 0.1 for k in q}, max_state_chars=1000))
    with pytest.raises(LimitError, match="Split the text first"):
        ex.test("x", "z" * 5000, engine=e)


def test_search_uses_choice_plus_exists(server):
    server.handler = keyword_answers({"refund": ["money back"]})
    e = Engine(SystemOne(server.url, model="m", sleep=no_sleep))
    m = ex.search("asks for a refund", TICKET, engine=e)
    assert m is not None and m.text == "I would like my money back, please." and m.confidence == pytest.approx(0.9)
    body = server.requests[-1]
    assert set(body["questions"]) == {"exists", "best"}
    assert body["questions"]["best"]["type"] == "choice" and len(body["questions"]["best"]["criteria"]) == len(
        ex.find_units(TICKET, "sentence")
    )


def test_search_returns_none_when_nothing_is_there(engine):
    assert ex.search("asks for a refund", "Nice weather. Lovely day.", engine=engine) is None


def test_search_over_many_windows():
    def handler(state, qs):
        segs = state["segments"]
        hit = next((k for k, v in segs.items() if "needle" in v), None)
        out = {"exists": 0.95 if hit else 0.05}
        if "best" in qs:
            opts = list(qs["best"].criteria)
            pick = hit or opts[0]
            out["best"] = ChoiceAnswer(pick, {o: (0.9 if o == pick else 0.0) for o in opts}, 0.9)
        return out

    e = Engine(Scripted(handler))
    lines = [f"hay {i}" for i in range(450)]
    lines[333] = "the needle"
    m = ex.search("the needle", "\n".join(lines), unit="line", engine=e)
    assert m is not None and m.text == "the needle"


def test_search_with_candidate_unit(engine):
    m = ex.search("a way to contact a specific person", TICKET, unit="contact", engine=engine)
    assert m is not None and m.text in ("fixture_quasar_7k9@example.invalid", "(415) 555-0177")


def test_extract_picks_a_verbatim_candidate():
    def handler(state, qs):
        opts = list(qs["pick"].criteria)
        assert opts[-1] == "(none of these)" and "$1,315.50" in opts
        return {"pick": ChoiceAnswer("$1,315.50", {o: (0.93 if o == "$1,315.50" else 0.07 / (len(opts) - 1)) for o in opts}, 0.9)}

    e = Engine(Scripted(handler))
    text = "Subtotal $1,365.50. Credit $50.00. Total due: $1,315.50."
    m = ex.extract("the total amount due", text, unit="money", engine=e)
    assert m is not None and m.text == "$1,315.50" and text[m.start : m.end] == "$1,315.50" and m.p == pytest.approx(0.93)


def test_extract_none_and_threshold():
    def none(state, qs):
        opts = list(qs["pick"].criteria)
        return {"pick": ChoiceAnswer("(none of these)", {o: (0.8 if o.startswith("(none") else 0.2 / (len(opts) - 1)) for o in opts}, 0.8)}

    assert ex.extract("the total", "It was $5.", unit="money", engine=Engine(Scripted(none))) is None

    def weak(state, qs):
        opts = list(qs["pick"].criteria)
        return {"pick": ChoiceAnswer(opts[0], {o: 1 / len(opts) for o in opts}, 0.3)}

    assert ex.extract("the total", "It was $5 or $6.", unit="money", threshold=0.5, engine=Engine(Scripted(weak))) is None
    assert ex.extract("the total", "no numbers here", unit="money", engine=Engine(Scripted(weak))) is None


def test_classify_and_rate():
    canned = {
        "pick": ChoiceAnswer("billing", {"billing": 0.8, "bug": 0.2}, 0.75),
        "rate": ScoreAnswer(1.6, {0: 0.0, 1: 0.4, 2: 0.6}, 0.6, {0: "Calm", 1: "Annoyed", 2: "Furious"}),
    }
    e = Engine(Scripted(lambda s, q: {k: canned[k] for k in q}))
    pick = ex.classify("charged twice", {"billing": "money", "bug": "errors"}, engine=e)
    assert pick.label == "billing" and str(pick) == "billing" and pick.p == 0.8 and pick.top(1) == [("billing", 0.8)]
    r = ex.rate("charged twice!!", "How angry?", ["Calm", "Annoyed", "Furious"], engine=e)
    assert r.level == 2 and r.label == "Furious" and float(r) == 1.6


def test_classify_list_options_sent_as_criteria(server):
    e = Engine(SystemOne(server.url, model="m", sleep=no_sleep))
    ex.classify("x", ["a", "b", "c"], engine=e)
    assert server.requests[-1]["questions"]["pick"]["criteria"] == {"a": None, "b": None, "c": None}


def test_refusal_raises():
    e = Engine(Scripted(lambda s, qs: {k: Refusal("policy") for k in qs}))
    with pytest.raises(RefusedError, match="policy"):
        ex.findall("x", "One. Two.", engine=e)
    with pytest.raises(RefusedError):
        ex.classify("x", ["a", "b"], engine=e)


def test_custom_question_wording():
    seen = []

    def handler(state, qs):
        seen.extend(q.instructions for q in qs.values())
        return {k: 0.9 for k in qs}

    e = Engine(Scripted(handler))
    ex.findall("ignored", "One. Two.", question="Does {ref} name a fruit?", engine=e)
    assert seen == ["Does `segments.S001` name a fruit?", "Does `segments.S002` name a fruit?"]
    with pytest.raises(ValueError, match="ref"):
        ex.compile("x", question="no placeholder here")


def test_context_rides_along():
    states = []
    e = Engine(Scripted(lambda s, qs: (states.append(s), {k: 0.1 for k in qs})[1]))
    ex.findall("x", "One. Two.", context="These are product reviews.", engine=e)
    assert states[0]["context"] == "These are product reviews."


def test_candidate_state_carries_value_and_neighborhood():
    states = []
    e = Engine(Scripted(lambda s, qs: (states.append(s), {k: 0.1 for k in qs})[1]))
    ex.findall("x", "Mail fixture_quasar_7k9@example.invalid today.", unit="email", engine=e)
    c = states[0]["candidates"]["C001"]
    assert c["value"] == "fixture_quasar_7k9@example.invalid" and "⟦fixture_quasar_7k9@example.invalid⟧" in c["in_context"]
    assert states[0]["text"] == "Mail fixture_quasar_7k9@example.invalid today."


def test_compiled_pattern_reuse_and_kwargs_guard(engine):
    pat = ex.compile("asks for a refund", engine=engine)
    assert repr(pat).startswith("exregex.compile(")
    assert ex.findall(pat, TICKET)
    with pytest.raises(TypeError):
        ex.findall(pat, TICKET, unit="line")
    with pytest.raises(ValueError):
        ex.compile("")
    with pytest.raises(ValueError):
        ex.compile("x", threshold=2)
    with pytest.raises(ValueError):
        ex.compile("x", unit="nope")


def test_ask_and_typed_questions_directly():
    ex.set_engine(Engine(Scripted(lambda s, qs: {"a": 0.3, "b": ChoiceAnswer("y", {"x": 0.1, "y": 0.9}, 0.9)})))
    out = ex.ask({"t": 1}, {"a": Noul("A?"), "b": Choice("B?", {"x": None, "y": None})})
    assert out["a"].p == 0.3 and out["b"].choice == "y"
    assert ex.stats().requests == 1


def test_async_twins(engine):
    async def main():
        a = await ex.afindall("asks for a refund", TICKET, engine=engine)
        b = await ex.compile("asks for a refund", engine=engine).afindall(TICKET)
        c = await ex.asub("asks for a refund", "[x]", TICKET, engine=engine)
        return a, b, c

    a, b, c = asyncio.run(main())
    assert a == b and "[x]" in c and ex.afindall.__name__ == "afindall"


def test_empty_inputs(engine):
    assert ex.findall("x", "", engine=engine) == []
    assert ex.search("x", "", engine=engine) is None
    assert ex.filter("x", [], engine=engine) == []
    assert ex.sub("x", "y", "", engine=engine) == ""
    assert ex.split("x", "   ", engine=engine) == []


def test_wrong_answer_types_become_backend_errors():
    noul_as_score = Engine(Scripted(lambda s, qs: {k: ScoreAnswer(1.0, {0: 0.0, 1: 1.0}, 1.0) for k in qs}))
    with pytest.raises(ex.BackendError, match="noul"):
        ex.findall("x", "One. Two.", engine=noul_as_score)
    off_menu = Engine(Scripted(lambda s, qs: {k: ChoiceAnswer("zebra", {"zebra": 1.0}, 1.0) for k in qs}))
    with pytest.raises(ex.BackendError, match="not one of the options"):
        ex.classify("x", ["a", "b"], engine=off_menu)


def test_search_survives_a_pick_that_is_not_an_id():
    def handler(state, qs):
        out = {}
        for k in qs:
            if k == "exists":
                out[k] = 0.95
            elif k == "best":
                out[k] = ChoiceAnswer("none", {"none": 1.0}, 1.0)
            else:
                out[k] = 0.95 if "needle" in state["segments"][k] else 0.05
        return out

    class Loose(Scripted):  # a backend that answers off the menu, as a buggy server might
        def decide(self, state, questions):
            return super().decide(state, questions)

    e = Engine(Loose(handler))
    import exregex.engine as eng

    original = eng.check_answers
    eng.check_answers = lambda q, a: None  # let the off-menu pick through to search's own guard
    try:
        m = ex.search("the needle", "hay one\nthe needle\nhay two", unit="line", engine=e)
    finally:
        eng.check_answers = original
    assert m is not None and m.text == "the needle"


def test_small_api_guards():
    e = Engine(Scripted(lambda s, qs: {k: 0.9 for k in qs}))
    with pytest.raises(ValueError):
        ex.subn("x", "y", "One. Two.", count=-1, engine=e)
    with pytest.raises(TypeError):
        ex.classify("x", "ab", engine=e)
    calls = []

    def unit(t):
        calls.append(t)
        return [(0, len(t))]

    ex.compile("x", unit=unit, engine=e)
    assert calls == []  # not called at compile time


# ------------------------------------------------------------------ extraction tournament (review 2026-10-07)


def _pick_by_context(marker):
    """A fake judge for the tournament: picks the option whose neighborhood contains `marker`,
    else the first real option. Options are occurrence ids; their values are the descriptions."""

    def handler(state, qs):
        q = qs["pick"] if "pick" in qs else qs["which"]
        opts = list(q.criteria)
        ctx = state.get("candidates_in_context", {})
        real = [o for o in opts if not o.startswith("(none")]
        pick = next((o for o in real if marker in ctx.get(o, "")), real[0])
        name = "pick" if "pick" in qs else "which"
        return {name: ChoiceAnswer(pick, {o: (0.9 if o == pick else 0.1 / max(1, len(opts) - 1)) for o in opts}, 0.9)}

    return handler


def test_extract_long_text_runs_a_tournament_over_occurrences():
    text = " ".join(f"Item {i} costs ${i}.00." for i in range(1, 300)) + " The grand total is $999.99."
    seen = []

    def handler(state, qs):
        import json as _json

        seen.append((len(_json.dumps(state, ensure_ascii=False)), len(qs["pick"].criteria)))
        return _pick_by_context("grand total is ⟦")(state, qs)

    m = ex.extract("the grand total", text, unit="money", engine=Engine(Scripted(handler, max_state_chars=4000), cache=False))
    assert m is not None and m.text == "$999.99" and m.start == text.index("$999.99")
    assert len(seen) > 2 and all(size <= 4000 for size, _ in seen) and all(n <= 255 for _, n in seen)


def test_extract_cannot_loop_when_every_group_is_a_singleton():
    calls = []

    def handler(state, qs):
        calls.append(1)
        assert len(calls) < 50, "the tournament is not shrinking"
        return _pick_by_context("Final ⟦")(state, qs)

    text = "Item $10 here. " + "x" * 900 + " Other $20 there. " + "y" * 900 + " Final $30."
    m = ex.extract("the total", text, unit="money", engine=Engine(Scripted(handler, max_state_chars=1000), cache=False))
    assert m is not None and m.text == "$30" and len(calls) <= 5


def test_extract_raises_instead_of_looping_when_nothing_can_share_a_request():
    def huge(text):
        return [(0, 600), (700, 1300)]

    text = "a" * 600 + " " * 100 + "b" * 600
    with pytest.raises(LimitError, match="too large to share"):
        ex.extract("the total", text, unit=huge, engine=Engine(Scripted(_pick_by_context("zzz"), max_state_chars=1000), cache=False))


def test_extract_long_text_sees_every_occurrence_context():
    text = "Line item: $10.00. " + "z" * 3000 + " Total due: $10.00"
    m = ex.extract(
        "the total due", text, unit="money", engine=Engine(Scripted(_pick_by_context("Total due: ⟦"), max_state_chars=2000), cache=False)
    )
    assert m is not None and m.start == text.rindex("$10.00")


def test_extract_short_text_picks_the_meant_occurrence():
    def handler(state, qs):
        if "pick" in qs and "text" in state:  # the short path: values as options
            opts = list(qs["pick"].criteria)
            return {"pick": ChoiceAnswer("$10.00", {o: (0.9 if o == "$10.00" else 0.1 / (len(opts) - 1)) for o in opts}, 0.9)}
        return _pick_by_context("Total due: ⟦")(state, qs)

    text = "Subtotal: $10.00\nTotal due: $10.00"
    m = ex.extract("the total due", text, unit="money", engine=Engine(Scripted(handler), cache=False))
    assert m is not None and m.start == text.rindex("$10.00")


def test_extract_occurrence_selection_has_no_254_ceiling():
    text = " ".join(f"Row {i}: $10.00." for i in range(260)) + " Total due: $10.00"

    def handler(state, qs):
        if "text" in state:
            opts = list(qs["pick"].criteria)
            return {"pick": ChoiceAnswer("$10.00", {o: (0.9 if o == "$10.00" else 0.1 / (len(opts) - 1)) for o in opts}, 0.9)}
        return _pick_by_context("Total due: ⟦")(state, qs)

    m = ex.extract("the total due", text, unit="money", engine=Engine(Scripted(handler, max_state_chars=60_000), cache=False))
    assert m is not None and m.start == text.rindex("$10.00")


# ------------------------------------------------------------------ search options, replay identity, concurrency


def test_search_respects_prefilter_and_custom_question():
    def handler(state, qs):
        out = {}
        for k, q in qs.items():
            if k == "exists":
                out[k] = 0.95
            elif k == "best":
                out[k] = ChoiceAnswer(next(iter(q.criteria)), {o: 1 / len(q.criteria) for o in q.criteria}, 0.5)
            else:
                seg = state["segments"][k]
                out[k] = (
                    0.95 if ("fruit" in q.instructions and "apple" in seg) or ("fruit" not in q.instructions and "refund" in seg) else 0.03
                )
        return out

    e = Engine(Scripted(handler), cache=False)
    text = "refund now\napple pie\nhello"
    assert ex.search("refund", text, unit="line", prefilter=lambda t: False, engine=e) is None
    m = ex.search("refund", text, unit="line", question="Does {ref} name a fruit?", engine=e)
    assert m is not None and m.text == "apple pie"
    one = ex.search("refund", text, unit="line", prefilter=lambda t: t.startswith("refund"), engine=e)
    assert one is not None and one.text == "refund now"


def test_python_replay_needs_no_credentials(tmp_path, monkeypatch):
    lock = tmp_path / "d.jsonl"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-record")
    rec = Engine(cache=lock)
    key = __import__("exregex.engine", fromlist=["Cache"]).Cache.key(rec.backend, "x", {"q": Noul("Is it?")})
    import json as _json

    row = {"key": key, "model": "m", "answers": {"q": {"type": "noul", "noul": 0.7}}}
    lock.write_text(_json.dumps(row) + "\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY")
    e = Engine(cache=lock, replay=True)  # no key anywhere in the environment
    assert e.decide("x", {"q": Noul("Is it?")}).answers["q"].p == 0.7
    with pytest.raises(ex.CacheMiss):
        e.decide("new", {"q": Noul("Is it?")})


def test_concurrency_is_bounded_across_callers():
    import threading
    import time

    live, peak, lock = [0], [0], threading.Lock()

    def handler(state, qs):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.03)
        with lock:
            live[0] -= 1
        return {k: 0.5 for k in qs}

    e = Engine(Scripted(handler), concurrency=1, cache=False)
    threads = [threading.Thread(target=lambda i=i: e.decide(i, {"q": Noul("Is it?")})) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1
