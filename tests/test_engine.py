import threading
import time

import pytest

import exregex as ex
from exregex import BudgetExceeded, Engine, LimitError, Noul, Scripted
from exregex._types import NoulAnswer


def counting_backend(p=0.5, price=0.0, delay=0.0):
    calls = []

    def handler(state, questions):
        calls.append(len(questions))
        if delay:
            time.sleep(delay)
        return {k: p for k in questions}

    b = Scripted(handler)
    b.price_per_mtok = price
    return b, calls


def test_cache_in_memory():
    b, calls = counting_backend()
    e = Engine(b)
    q = {"q": Noul("Is `x` y?")}
    assert not e.decide({"x": 1}, q).cached
    assert e.decide({"x": 1}, q).cached
    assert e.decide({"x": 2}, q).cached is False
    assert len(calls) == 2 and e.stats.cached == 1 and e.stats.requests == 2


def test_cache_off():
    b, calls = counting_backend()
    e = Engine(b, cache=False)
    for _ in range(3):
        e.decide("x", {"q": Noul("Is it?")})
    assert len(calls) == 3


def test_sqlite_cache_survives_a_new_engine(tmp_path):
    path = tmp_path / "c" / "answers.sqlite"
    b, calls = counting_backend(0.7)
    with Engine(b, cache=path) as e:
        e.decide("x", {"q": Noul("Is it?")})
    with Engine(b, cache=path) as e2:
        d = e2.decide("x", {"q": Noul("Is it?")})
    assert d.cached and d.answers["q"] == NoulAnswer(0.7) and len(calls) == 1


def test_cache_key_separates_models():
    b1, c1 = counting_backend()
    b2, c2 = counting_backend()
    b2.model = "other"
    e = Engine(b1)
    e.decide("x", {"q": Noul("Is it?")})
    e.backend = b2
    e.decide("x", {"q": Noul("Is it?")})
    assert len(c1) == 1 and len(c2) == 1


def test_question_limit_and_ask_splits():
    b, calls = counting_backend()
    e = Engine(b, max_questions=4)
    qs = {f"q{i}": Noul(f"Question {i}?") for i in range(10)}
    with pytest.raises(LimitError):
        e.decide("x", qs)
    answers = e.ask("x", qs)
    assert sorted(answers) == sorted(qs) and sorted(calls) == [2, 4, 4]


def test_ask_many_keeps_order_and_collects_errors():
    def handler(state, questions):
        if state == "bad":
            raise ex.BackendError("boom")
        time.sleep(0.01 * (5 - state))
        return {k: state / 10 for k in questions}

    e = Engine(Scripted(handler), concurrency=4)
    reqs = [(i, {"q": Noul("Is it?")}) for i in range(5)] + [("bad", {"q": Noul("Is it?")})]
    out = e.ask_many(reqs, raise_errors=False)
    assert [r.answers["q"].p for r in out[:5]] == [0.0, 0.1, 0.2, 0.3, 0.4]
    assert isinstance(out[5], ex.BackendError) and e.stats.failures == 1
    with pytest.raises(ex.BackendError):
        e.ask_many(reqs)


def test_concurrency_is_bounded():
    live = []
    peak = [0]
    lock = threading.Lock()

    def handler(state, questions):
        with lock:
            live.append(1)
            peak[0] = max(peak[0], len(live))
        time.sleep(0.02)
        with lock:
            live.pop()
        return {k: 0.5 for k in questions}

    e = Engine(Scripted(handler), concurrency=3, cache=False)
    e.ask_many([(i, {"q": Noul("Is it?")}) for i in range(12)])
    assert 1 < peak[0] <= 3


def test_budget_stops_before_spending():
    b, calls = counting_backend(price=1_000_000.0)  # a dollar per token, so the estimate is large
    e = Engine(b, max_cost_usd=1.0)
    with pytest.raises(BudgetExceeded):
        e.decide("x" * 100, {"q": Noul("Is it?")})
    assert calls == []


def test_budget_allows_cheap_requests_and_counts_actual_cost():
    def handler(state, questions):
        return {k: 0.5 for k in questions}

    class Priced(Scripted):
        price_per_mtok = 0.042

        def decide(self, state, questions):
            d = super().decide(state, questions)
            from dataclasses import replace

            return replace(d, cost_usd=0.001, input_tokens=50, latency_ms=12.0)

    e = Engine(Priced(handler), max_cost_usd=0.002, cache=False)
    e.decide("a", {"q": Noul("Is it?")})
    e.decide("b", {"q": Noul("Is it?")})
    with pytest.raises(BudgetExceeded):
        e.decide("c", {"q": Noul("Is it?")})
    s = e.stats.as_dict()
    assert s["requests"] == 2 and s["cost_usd"] == 0.002 and s["input_tokens"] == 100 and s["latency_ms"]["p50"] == 12.0
    assert "2 request(s)" in e.stats.line()


def test_default_engine_reads_env_lazily(monkeypatch):
    with pytest.raises(ex.ConfigError):
        ex.get_engine()
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert ex.get_engine().backend.name == "openrouter"
    custom = Engine(Scripted(lambda s, q: {}))
    ex.set_engine(custom)
    assert ex.get_engine() is custom


def test_cache_path_expands_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    b, _ = counting_backend()
    with Engine(b, cache="~/sub/answers.sqlite") as e:
        e.decide("x", {"q": Noul("Is it?")})
    assert (tmp_path / "sub" / "answers.sqlite").exists()
