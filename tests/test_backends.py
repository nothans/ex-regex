import json

import pytest

import exregex as ex
from exregex import BackendError, Choice, ConfigError, Noul, Score
from exregex._types import ChoiceAnswer, NoulAnswer, Refusal, ScoreAnswer
from exregex.backends import OpenAIDecisions, SystemOne, from_env

from .conftest import keyword_answers, no_sleep


def backend(server, **kw):
    return SystemOne(server.url, api_key="sk-test", model="jev-test", sleep=no_sleep, **kw)


def test_wire_format_and_auth(server):
    server.handler = keyword_answers({"refund": ["refund"]})
    d = backend(server).decide({"text": "refund please"}, {"q": Noul("Does `text` mention a refund?")})
    body = server.requests[0]
    assert body == {
        "model": "jev-test",
        "state": {"text": "refund please"},
        "questions": {"q": {"type": "noul", "instructions": "Does `text` mention a refund?"}},
    }
    assert server.headers[0]["Authorization"] == "Bearer sk-test"
    assert server.headers[0]["User-Agent"].startswith("ex-regex/")
    assert d.answers["q"] == NoulAnswer(0.97)
    assert d.input_tokens == 100 and d.model == "jev-test"


def test_url_gets_systemone_path(server):
    assert backend(server).url == server.url + "/v1/systemone"
    assert SystemOne("https://openrouter.ai/api/alpha/decisions", model="m").url.endswith("/alpha/decisions")


def test_cost_from_usage_or_price(server):
    b = backend(server, price_per_mtok=0.042)
    d = b.decide("x", {"q": Noul("Is `x`?")})
    assert d.cost_usd == pytest.approx(100 * 0.042 / 1e6)
    server.reply_override = lambda body, reply: {**reply, "usage": {"input_tokens": 10, "cost": 0.5}}
    assert b.decide("y", {"q": Noul("Is it?")}).cost_usd == 0.5


def test_retries_on_overload_then_succeeds(server):
    server.failures = [(529, "0", "overloaded"), (520, None, ""), (429, "0", "{}")]
    d = backend(server).decide("x", {"q": Noul("Is it?")})
    assert d.retries == 3 and len(server.requests) == 4


def test_no_retry_on_422(server):
    server.failures = [(422, None, json.dumps({"detail": "questions.q.criteria: too few"}))]
    with pytest.raises(BackendError) as e:
        backend(server).decide("x", {"q": Noul("Is it?")})
    assert e.value.status == 422 and not e.value.retryable and "too few" in str(e.value)
    assert len(server.requests) == 1


def test_gives_up_after_max_retries(server):
    server.failures = [(503, "0", "")] * 3
    with pytest.raises(BackendError) as e:
        backend(server, max_retries=2).decide("x", {"q": Noul("Is it?")})
    assert e.value.status == 503 and e.value.retryable


def test_skipped_answers_are_reasked_then_fail(server):
    calls = []

    def skip_first(body, reply):
        calls.append(sorted(body["questions"]))
        if len(calls) == 1:  # the first reply drops one packed question
            reply = {**reply, "answers": {k: v for k, v in reply["answers"].items() if k != "b"}}
        return reply

    server.reply_override = skip_first
    e = ex.Engine(backend(server), cache=False)
    out = e.decide("x", {"a": Noul("A?"), "b": Noul("B?")})
    assert sorted(out.answers) == ["a", "b"] and calls == [["a", "b"], ["b"]]  # only the missing one was re-asked

    server.reply_override = lambda body, reply: {**reply, "answers": {}}
    with pytest.raises(BackendError, match="no answer"):
        ex.Engine(backend(server, max_retries=0), cache=False).decide("y", {"q": Noul("Is it?")})


def test_unwraps_cloudflare_result(server):
    server.reply_override = lambda body, reply: {"result": reply, "success": True}
    d = backend(server).decide("x", {"q": Noul("Is it?")})
    assert isinstance(d.answers["q"], NoulAnswer)


def test_unreachable_server_message():
    b = SystemOne("http://127.0.0.1:9", model="m", max_retries=0, timeout=2)
    with pytest.raises(BackendError, match="could not reach"):
        b.decide("x", {"q": Noul("Is it?")})


def test_repr_hides_key(server):
    assert "sk-test" not in repr(backend(server))


# ------------------------------------------------------------------ OpenAI translation


def test_openai_translates_both_ways(server):
    def reply(body, _):
        assert body["model"] == "gpt-6-luna"
        assert isinstance(body["input"], str) and json.loads(body["input"]) == {"text": "hi"}
        qs = {q["name"]: q for q in body["questions"]}
        assert qs["yn"]["type"] == "predicate" and "True means: yes it is" in qs["yn"]["instructions"]
        assert qs["pick"]["choices"] == [{"value": "a", "description": "first"}, {"value": "b", "description": "b"}]
        assert qs["lvl"]["levels"][1] == {"label": "1", "description": "high"}
        return {
            "answers": [
                {"type": "predicate", "name": "yn", "probability": 0.8},
                {
                    "type": "choice",
                    "name": "pick",
                    "choice": "a",
                    "probabilities": [{"value": "a", "probability": 0.7}, {"value": "b", "probability": 0.3}],
                    "confidence": 0.6,
                },
                {
                    "type": "score",
                    "name": "lvl",
                    "score": 0.9,
                    "probabilities": [{"value": 0, "label": "0", "probability": 0.1}, {"value": 1, "label": "1", "probability": 0.9}],
                    "confidence": 0.8,
                },
                {"type": "refusal", "name": "nope", "refusal": "cannot help"},
            ],
            "usage": {"input_tokens": 1000},
        }

    server.reply_override = reply
    b = OpenAIDecisions(api_key="k", url=server.url + "/v1/decisions", sleep=no_sleep)
    d = b.decide(
        {"text": "hi"},
        {
            "yn": Noul("Is `text` a greeting?", {"true": "yes it is", "false": "no"}),
            "pick": Choice("Which?", {"a": "first", "b": None}),
            "lvl": Score("How?", ["low", "high"]),
            "nope": Noul("Something refused?"),
        },
    )
    assert d.answers["yn"] == NoulAnswer(0.8)
    assert d.answers["pick"] == ChoiceAnswer("a", {"a": 0.7, "b": 0.3}, 0.6)
    s = d.answers["lvl"]
    assert isinstance(s, ScoreAnswer) and s.level == 1 and s.legend[1] == "high"
    assert d.answers["nope"] == Refusal("cannot help")
    assert d.cost_usd == pytest.approx(1000 * 0.10 / 1e6)


# ------------------------------------------------------------------ from_env


def test_from_env_prefers_typesafe_then_openrouter(monkeypatch):
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        from_env()
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")
    assert from_env().name == "openrouter" and from_env().model == "typesafe/jev-1.13"
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts")
    b = from_env()
    assert b.name == "typesafe" and b.model == "jev-1.13.0" and b.url == "https://api.typesafe.ai/v1/systemone"


def test_from_env_explicit_choice_and_url(monkeypatch):
    monkeypatch.setenv("EXREGEX_BACKEND", "http://127.0.0.1:8123")
    monkeypatch.setenv("EXREGEX_MODEL", "kev-4b")
    b = from_env()
    assert b.name == "local" and b.model == "kev-4b" and b.url == "http://127.0.0.1:8123/v1/systemone" and b.max_questions == 8
    monkeypatch.setenv("EXREGEX_BACKEND", "nonsense")
    with pytest.raises(ConfigError, match="nonsense"):
        from_env()
    monkeypatch.setenv("EXREGEX_BACKEND", "openai")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        from_env()


def test_scripted_accepts_shorthand():
    b = ex.Scripted(lambda s, q: {"a": 0.4, "b": {"type": "noul", "noul": 0.6}})
    d = b.decide("x", {"a": Noul("A?"), "b": Noul("B?")})
    assert d.answers == {"a": NoulAnswer(0.4), "b": NoulAnswer(0.6)}
    with pytest.raises(BackendError):
        ex.Scripted(lambda s, q: {}).decide("x", {"a": Noul("A?")})


def test_truncated_reply_is_a_network_error_not_a_crash(server):
    import socket
    import threading

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    port = srv.getsockname()[1]

    def serve():
        for _ in range(2):
            conn, _ = srv.accept()
            conn.recv(65536)
            conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n{"answers":')
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(BackendError) as e:
        SystemOne(f"http://127.0.0.1:{port}", model="m", max_retries=1, sleep=no_sleep, timeout=5).decide("x", {"q": Noul("Is it?")})
    assert e.value.retryable
    srv.close()


def test_redirects_are_not_followed_with_the_key(server):
    server.failures = [(307, None, "")]
    with pytest.raises(BackendError, match="redirect") as e:
        backend(server, max_retries=0).decide("x", {"q": Noul("Is it?")})
    assert e.value.status == 307 and len(server.requests) == 1


def test_retry_after_is_never_shortened(server):
    waits = []
    server.failures = [(429, "2", "{}")]
    SystemOne(server.url, model="m", sleep=waits.append).decide("x", {"q": Noul("Is it?")})
    assert waits and waits[0] >= 2.0


def test_url_backend_takes_a_key_from_the_environment(monkeypatch, server):
    monkeypatch.setenv("EXREGEX_BACKEND", server.url)
    monkeypatch.setenv("EXREGEX_API_KEY", "gw-key")
    from_env().decide("x", {"q": Noul("Is it?")})
    assert server.headers[-1]["Authorization"] == "Bearer gw-key"
