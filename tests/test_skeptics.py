"""The features for people who trust regex: diff against your regex, a decision lockfile with
replay for CI, and a regex prefilter so only plausible spans cost anything."""

import json

import pytest

import exregex as ex
from exregex import CacheMiss, Engine, Noul, Scripted
from exregex.testing import keyword_engine

from .conftest import keyword_answers

TICKETS = "\n".join(
    [
        "I want a refund for the lamp.",
        "Please don't refund me, send a replacement.",  # the regex matches, the meaning should not
        "Give me my money back, it broke.",  # the meaning matches, the regex misses
        "Where is my package?",
    ]
)


def refund_engine():
    def handler(state, qs):
        out = {}
        for k in qs:
            seg = (state.get("segments") or state.get("items") or {}).get(k, "")
            yes = ("refund for" in seg) or ("money back" in seg)
            out[k] = 0.95 if yes else 0.04
        return out

    return Engine(Scripted(handler))


def test_diff_reports_only_disagreements():
    d = ex.diff("asks for a refund", r"(?i)\brefund\b", TICKETS, unit="line", engine=refund_engine())
    assert [m.text for m in d.regex_only] == ["Please don't refund me, send a replacement."]
    assert [m.text for m in d.meaning_only] == ["Give me my money back, it broke."]
    assert d.agree == 2 and not d.clean and str(d) == "agree 2, regex only 1, meaning only 1"


def test_diff_with_a_candidate_unit_judges_regex_matches_the_finders_missed():
    seen = []

    def handler(state, qs):
        seen.extend(c["value"] for c in state["candidates"].values())
        return {k: (0.95 if "fixture_quasar_7k9" in state["candidates"][k]["value"] else 0.03) for k in qs}

    text = "Mail fixture_quasar_7k9@example.invalid or the team at support@comet-tools.example.invalid, ticket TCK-555-0199."
    d = ex.diff("a way to contact a specific person", r"[\w.]+@[\w.]+|\d{3}-\d{4}", text, unit="email", engine=Engine(Scripted(handler)))
    assert "555-0199" in seen  # a regex-only span the email finder never proposed still got judged
    assert [m.text for m in d.regex_only] == ["support@comet-tools.example.invalid", "555-0199"]
    assert d.meaning_only == [] and d.agree == 1


def test_lockfile_records_readable_lines_and_replays_offline(tmp_path):
    lock = tmp_path / "decisions.jsonl"
    calls = []

    def handler(state, qs):
        calls.append(1)
        return {k: 0.9 for k in qs}

    with Engine(Scripted(handler), cache=lock) as e:
        ex.findall("asks for a refund", "Refund me. Thanks.", engine=e)
    rows = [json.loads(x) for x in lock.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["model"] == "scripted" and "asked" in rows[0]
    assert "asks for a refund" in json.dumps(rows[0]["asked"])  # a reviewer can read what was asked

    def boom(state, qs):
        raise AssertionError("replay must not call the backend")

    with Engine(Scripted(boom), cache=lock, replay=True) as e:
        assert [m.text for m in ex.findall("asks for a refund", "Refund me. Thanks.", engine=e)] == ["Refund me.", "Thanks."]
        with pytest.raises(CacheMiss, match="not recorded"):
            ex.findall("asks for a refund", "Something new entirely.", engine=e)
    assert len(calls) == 1


def test_lockfile_is_append_only_and_deduplicated(tmp_path):
    lock = tmp_path / "d.jsonl"
    with Engine(Scripted(lambda s, q: {k: 0.5 for k in q}), cache=lock) as e:
        for _ in range(3):
            e.decide("x", {"q": Noul("Is it?")})
        e.decide("y", {"q": Noul("Is it?")})
    assert len(lock.read_text(encoding="utf-8").splitlines()) == 2


def test_corrupt_lockfile_is_a_clear_error(tmp_path):
    lock = tmp_path / "bad.jsonl"
    lock.write_text('{"key": "a", "model": "m", "answers": {}}\nnot json\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad.jsonl:2"):
        Engine(Scripted(lambda s, q: {}), cache=lock)


def test_replay_needs_a_cache():
    with pytest.raises(ValueError, match="replay"):
        Engine(Scripted(lambda s, q: {}), cache=False, replay=True)


def test_prefilter_skips_spans_and_costs_nothing_for_them():
    asked = []

    def handler(state, qs):
        asked.extend(state["segments"].values())
        return {k: 0.95 for k in qs}

    log = "\n".join(["INFO ok"] * 50 + ["ERROR refund failed for order 7"] + ["INFO ok 2"] * 50)
    hits = ex.findall("a failed refund", log, unit="line", prefilter=r"ERROR|WARN", engine=Engine(Scripted(handler)))
    assert [m.text for m in hits] == ["ERROR refund failed for order 7"] and asked == ["ERROR refund failed for order 7"]
    scanned = ex.scan("a failed refund", log, unit="line", prefilter=r"ERROR", engine=Engine(Scripted(handler)))
    assert len(scanned) == 101 and sum(m.p > 0 for m in scanned) == 1


def test_prefilter_applies_to_filter_and_takes_a_callable():
    e = keyword_engine({"refund": ["refund"]})
    items = ["refund now", "REFUND NOW", "hello"]
    assert ex.filter("asks for a refund", items, prefilter=lambda t: t.islower(), engine=e) == ["refund now"]


def test_cli_diff_and_replay(server, monkeypatch, tmp_path, capsys):
    from exregex import cli

    monkeypatch.setenv("EXREGEX_BACKEND", server.url)
    monkeypatch.chdir(tmp_path)
    server.handler = keyword_answers({"refund": ["refund for", "money back"]})
    f = tmp_path / "t.txt"
    f.write_text(TICKETS, encoding="utf-8")
    lock = str(tmp_path / "lock.jsonl")
    assert cli.main(["diff", "asks for a refund", r"(?i)\brefund\b", str(f), "--cache", lock]) == 1
    out = capsys.readouterr()
    assert out.out.splitlines() == [
        "- 2:  p=0.02  Please don't refund me, send a replacement.",
        "+ 3:  p=0.97  Give me my money back, it broke.",
    ]
    assert "agree 2, regex only 1, meaning only 1" in out.err
    n = len(server.requests)
    # CI: no key, no network, the same answer from the lockfile
    monkeypatch.delenv("EXREGEX_BACKEND")
    monkeypatch.setenv("EXREGEX_BACKEND", server.url)
    assert cli.main(["diff", "asks for a refund", r"(?i)\brefund\b", str(f), "--cache", lock, "--replay", "-q"]) == 1
    assert len(server.requests) == n
    capsys.readouterr()
    assert cli.main(["diff", "asks for a refund", r"(?i)\brefund\b", "--text", "brand new line", "--cache", lock, "--replay", "-q"]) == 2
    assert "not recorded" in capsys.readouterr().err
