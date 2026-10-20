"""A decision's identity includes the order the request is sent in (key version 2, 0.2.0a2)."""

import json

import pytest

import exregex as ex
from exregex import CacheMiss, Choice, Engine, Noul, Scripted
from exregex.engine import KEY_VERSION, Cache


def first_option(state, questions):
    """A stand-in for a model that leans on order: it always favors the first option."""
    return {n: {"type": "choice", "probabilities": {k: (0.9 if i == 0 else 0.1) for i, k in enumerate(q.criteria)}}
            for n, q in questions.items()}


def test_option_order_is_part_of_the_key():
    backend = Scripted(first_option)
    a = Cache.key(backend, "x", {"q": Choice("Which?", {"red": None, "blue": None})})
    b = Cache.key(backend, "x", {"q": Choice("Which?", {"blue": None, "red": None})})
    again = Cache.key(backend, "x", {"q": Choice("Which?", {"red": None, "blue": None})})
    assert a != b and a == again


def test_state_field_order_is_part_of_the_key():
    backend = Scripted(first_option)
    q = {"q": Noul("Is it?")}
    assert Cache.key(backend, {"a": 1, "b": 2}, q) != Cache.key(backend, {"b": 2, "a": 1}, q)


def test_a_lockfile_never_replays_another_orders_answer(tmp_path):
    lock = tmp_path / "decisions.jsonl"
    rec = Engine(Scripted(first_option), cache=str(lock))
    assert rec.ask("x", {"q": Choice("Which?", {"red": None, "blue": None})})["q"].choice == "red"
    assert rec.ask("x", {"q": Choice("Which?", {"blue": None, "red": None})})["q"].choice == "blue"
    assert rec.stats.cached == 0
    replay = Engine(Scripted(first_option), cache=str(lock), replay=True)
    assert replay.ask("x", {"q": Choice("Which?", {"blue": None, "red": None})})["q"].choice == "blue"
    assert replay.ask("x", {"q": Choice("Which?", {"red": None, "blue": None})})["q"].choice == "red"


def test_new_lockfile_rows_carry_the_key_version(tmp_path):
    lock = tmp_path / "decisions.jsonl"
    Engine(Scripted(lambda s, qs: dict.fromkeys(qs, 0.9)), cache=str(lock)).ask("x", {"q": Noul("Is it?")})
    rows = [json.loads(line) for line in lock.read_text(encoding="utf-8").splitlines()]
    assert rows and all(r["key_version"] == KEY_VERSION == 2 for r in rows)


def test_replaying_an_old_lockfile_says_to_re_record(tmp_path):
    lock = tmp_path / "decisions.jsonl"
    old = {"key": "0" * 64, "model": "scripted", "answers": {"q": {"type": "noul", "noul": 0.9}}, "asked": {"q": "Is it?"}}
    lock.write_text(json.dumps(old) + "\n", encoding="utf-8")
    replay = Engine(Scripted(lambda s, qs: dict.fromkeys(qs, 0.9)), cache=str(lock), replay=True)
    with pytest.raises(CacheMiss, match=r"before ex-regex 0.2.0a2"):
        replay.ask("x", {"q": Noul("Is it?")})


def test_a_current_lockfile_miss_has_no_upgrade_hint(tmp_path):
    lock = tmp_path / "decisions.jsonl"
    Engine(Scripted(lambda s, qs: dict.fromkeys(qs, 0.9)), cache=str(lock)).ask("x", {"q": Noul("Is it?")})
    replay = Engine(Scripted(lambda s, qs: dict.fromkeys(qs, 0.9)), cache=str(lock), replay=True)
    with pytest.raises(CacheMiss) as err:
        replay.ask("y", {"q": Noul("Is it?")})
    assert "0.2.0a2" not in str(err.value)


def test_patterns_still_replay_their_own_recordings(tmp_path):
    """findall packs spans in a stable order, so a recording replays exactly."""
    lock = tmp_path / "decisions.jsonl"
    text = "I want my money back. The lamp is lovely. Refund me now please."
    handler = lambda s, qs: {n: 0.95 if "money" in json.dumps(s) else 0.05 for n in qs}  # noqa: E731
    live = ex.findall("asks for a refund", text, engine=Engine(Scripted(handler), cache=str(lock)))
    replayed = ex.findall("asks for a refund", text, engine=Engine(Scripted(handler), cache=str(lock), replay=True))
    assert [m.text for m in live] == [m.text for m in replayed]
