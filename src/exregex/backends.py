"""Backends: where the decisions come from.

Every backend takes a state and named questions and returns a Decision. ex-regex ships:

    SystemOne        anything that speaks TypeSafe's /v1/systemone shape: TypeSafe itself,
                     OpenRouter's Decisions endpoint, Vercel's gateway, and open servers that
                     copy the shape (Kev, Laya, razorback16/openjev, Cloudflare's Clef)
    OpenAIDecisions  OpenAI's /v1/decisions (public beta since 2026-10-06), translated to
                     and from the System One shape
    Scripted         a function you write, for tests and offline development

Presets: typesafe(), openrouter(), local(url), openai(). from_env() picks one from the
environment. Retries, backoff, and error mapping live in HTTPBackend so every backend
behaves the same way under load.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Optional

from ._types import Answer, ChoiceAnswer, Decision, NoulAnswer, Question, Refusal, Score, ScoreAnswer, parse_answer
from ._version import __version__
from .errors import BackendError, ConfigError

# Statuses worth another attempt: rate limits, overload (TypeSafe's 529), gateway hiccups
# (OpenRouter returns 520 under load), and server errors. Anything else is a caller mistake.
RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524, 529})

USER_AGENT = f"ex-regex/{__version__} (+https://pypi.org/project/ex-regex/)"


class Backend:
    """The interface. Subclass it, or pass anything with these attributes and a decide()."""

    name: str = "backend"
    model: str = ""
    price_per_mtok: float = 0.0  # USD per million input tokens; output is free on every current backend
    max_questions: int = 32  # questions per request ex-regex will pack
    max_state_chars: int = 60_000  # about 15k tokens: half of Jev's 32k state-plus-question window

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name} ({self.model})"


class HTTPBackend(Backend):
    """POST JSON with retries. Subclasses build the body and parse the reply."""

    def __init__(
        self,
        url: str,
        *,
        api_key: Optional[str],
        model: str,
        timeout: float = 30.0,
        max_retries: int = 4,
        headers: Optional[Mapping[str, str]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.url = url
        self.model = model
        self.timeout = timeout
        self.max_retries = max_retries
        self._api_key = api_key
        self._headers = dict(headers or {})
        self._sleep = sleep

    def __repr__(self) -> str:  # never show the key
        return f"{type(self).__name__}(url={self.url!r}, model={self.model!r})"

    def capabilities(self):
        from .semantic.transport import capabilities
        return capabilities(self)

    def prepare(self, state_utf8, questions):
        from .semantic.transport import prepare
        return prepare(self, state_utf8, questions)

    def attempt(self, request, *, timeout_s, cancel):
        from .semantic.transport import attempt
        return attempt(self, request, timeout_s=timeout_s, cancel=cancel)

    def parse_strict(self, response, request):
        from .semantic.transport import parse_strict
        return parse_strict(response, request)

    def _post(self, body: dict) -> tuple[dict, int, float]:
        """POST with retries. Returns (json, retries used, latency of the final attempt in ms)."""
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, **self._headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        attempt = 0
        while True:
            t0 = time.perf_counter()
            status, text, retry_after = self._send(data, headers)
            latency = (time.perf_counter() - t0) * 1000
            payload: Any = None
            if text:
                try:
                    payload = json.loads(text)
                except ValueError:
                    payload = None
            ok = status is not None and 200 <= status < 300 and isinstance(payload, dict)
            if ok:
                return payload, attempt, latency
            retryable = status is None or status in RETRYABLE or (status is not None and 200 <= status < 300)
            if retryable and attempt < self.max_retries:
                attempt += 1
                if retry_after is not None:
                    self._sleep(retry_after + 0.25 * random.random())  # never earlier than the server asked
                else:
                    self._sleep(min(20.0, 0.5 * 2 ** (attempt - 1)) * (0.8 + 0.4 * random.random()))
                continue
            raise BackendError(_error_message(self.name, status, payload, text), status=status, body=payload or text, retryable=retryable)

    def _send(self, data: bytes, headers: dict) -> tuple[Optional[int], str, Optional[float]]:
        req = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with _OPENER.open(req, timeout=self.timeout) as res:
                return res.status, res.read().decode("utf-8", "replace"), None
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace") if e.fp else ""
            except (http.client.HTTPException, OSError):
                body = ""
            return e.code, body, _retry_after(e.headers.get("retry-after") if e.headers else None)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, ConnectionError, OSError):
            return None, "", None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the API key to wherever the server points, and turn a POST into a
    GET. Refuse: the 3xx comes back as an error that names the new location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return max(0.0, min(60.0, float(value)))
    except ValueError:
        return None


def _error_message(name: str, status: Optional[int], payload: Any, text: str) -> str:
    if status is None:
        return f"{name}: could not reach the server"
    detail = ""
    if isinstance(payload, dict):
        err = payload.get("error", payload.get("detail", payload.get("message")))
        if isinstance(err, dict):
            detail = str(err.get("message") or err)
        elif err:
            detail = str(err)
    if not detail:
        detail = (text or "").strip()[:300]
    hint = {
        401: " (check the API key)",
        403: " (the key is not allowed to use this endpoint)",
        422: " (a question or the state was rejected)",
        **dict.fromkeys((301, 302, 303, 307, 308), " (a redirect; ex-regex never sends your key on to another URL, so set the final URL)"),
    }.get(status, "")
    if 200 <= status < 300:
        return f"{name}: the reply had no answers: {detail}"
    return f"{name}: HTTP {status}{hint}: {detail}"


class SystemOne(HTTPBackend):
    """Any server that speaks TypeSafe's System One shape.

    `url` is the full endpoint (https://api.typesafe.ai/v1/systemone), or a server root, in
    which case /v1/systemone is added.
    """

    def __init__(
        self,
        url: str,
        *,
        api_key: Optional[str] = None,
        model: str,
        name: str = "systemone",
        price_per_mtok: Optional[float] = None,
        max_questions: int = 32,
        max_state_chars: int = 60_000,
        **kwargs: Any,
    ):
        url = url.rstrip("/")
        if not (url.endswith("/systemone") or url.endswith("/decisions") or "/ai/run/" in url):
            url += "/v1/systemone"
        super().__init__(url, api_key=api_key, model=model, **kwargs)
        self.name = name
        self.price_per_mtok = price_per_mtok if price_per_mtok is not None else 0.0
        self._semantic_price = price_per_mtok
        self.max_questions = max_questions
        self.max_state_chars = max_state_chars

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        body = {"model": self.model, "state": state, "questions": {k: q.to_wire() for k, q in questions.items()}}
        payload, retries, latency = self._post(body)
        if "answers" not in payload and isinstance(payload.get("result"), dict):
            payload = payload["result"]  # Cloudflare Workers AI wraps replies in {"result": ...}
        raw = payload.get("answers")
        if not isinstance(raw, dict):
            raise BackendError(f"{self.name}: the reply had no answers", body=payload)
        answers = {k: parse_answer(v) for k, v in raw.items() if k in questions}  # missing ones are re-asked by the Engine
        usage = payload.get("usage") or {}
        tokens = int(usage.get("input_tokens") or 0)
        cost = usage.get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) else tokens * self.price_per_mtok / 1e6
        return Decision(answers, str(payload.get("model") or self.model), tokens, cost, latency, False, retries, payload.get("id"))


class OpenAIDecisions(HTTPBackend):
    """OpenAI's Decisions API (POST /v1/decisions, public beta since 2026-10-06).

    The shapes differ from System One: questions are a list with a `name`, a yes/no is a
    `predicate`, choices are `{value, description}`, levels are `{label, description}`, the
    state goes in `input` as text, and an answer may be a `refusal`. This class translates.
    Built from OpenAI's published guide; see the README for what has been run live.
    """

    name = "openai"

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model: str = "gpt-6-luna",
        url: str = "https://api.openai.com/v1/decisions",
        price_per_mtok: float = 0.10,
        **kwargs: Any,
    ):
        super().__init__(url, api_key=api_key, model=model, **kwargs)
        self.price_per_mtok = price_per_mtok
        self.max_questions = 32

    @staticmethod
    def _question(name: str, q: Question) -> dict:
        wire = q.to_wire()
        text = wire["instructions"] if isinstance(wire["instructions"], str) else json.dumps(wire["instructions"], ensure_ascii=False)
        if wire["type"] == "noul":
            crit = wire.get("criteria") or {}
            if crit:
                text += "".join(f"\n{k.capitalize()} means: {v}" for k, v in crit.items())
            return {"type": "predicate", "name": name, "instructions": text}
        if wire["type"] == "choice":
            choices = [{"value": k, "description": v if isinstance(v, str) and v else k} for k, v in wire["criteria"].items()]
            return {"type": "choice", "name": name, "instructions": text, "choices": choices}
        levels = [{"label": str(i), "description": d} for i, d in enumerate(wire["criteria"])]
        return {"type": "score", "name": name, "instructions": text, "levels": levels}

    @staticmethod
    def _answer(raw: Mapping[str, Any], legend: Mapping[int, str]) -> Answer:
        kind = raw.get("type")
        if kind == "predicate":
            return parse_answer({"type": "noul", "noul": raw.get("probability")})
        if kind == "choice":
            probs = {str(p.get("value")): p.get("probability") for p in raw.get("probabilities") or []}
            return parse_answer(
                {"type": "choice", "choice": raw.get("choice"), "probabilities": probs, "confidence": raw.get("confidence")}
            )
        if kind == "score":
            levels: dict[int, Any] = {}
            for i, p in enumerate(raw.get("probabilities") or []):
                key = p.get("value", p.get("label", i))
                try:
                    levels[int(key)] = p.get("probability")
                except (TypeError, ValueError):
                    levels[i] = p.get("probability")
            return parse_answer(
                {"type": "score", "score": raw.get("score"), "probabilities": levels, "confidence": raw.get("confidence"), "legend": legend}
            )
        if kind == "refusal":
            return Refusal(raw.get("refusal") or raw.get("reason"))
        raise BackendError(f"openai: unknown answer type {kind!r}")

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, indent=1)
        body = {"model": self.model, "input": text, "questions": [self._question(k, q) for k, q in questions.items()]}
        payload, retries, latency = self._post(body)
        raw = payload.get("answers")
        if not isinstance(raw, list):
            raise BackendError("openai: the reply had no answers", body=payload)
        answers: dict[str, Answer] = {}
        for item in raw:
            name = item.get("name")
            q = questions.get(name)
            if q is None:
                continue
            legend = dict(enumerate(q.criteria)) if isinstance(q, Score) else {}
            answers[name] = self._answer(item, legend)
        usage = payload.get("usage") or {}
        tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
        return Decision(
            answers,
            str(payload.get("model") or self.model),
            tokens,
            tokens * self.price_per_mtok / 1e6,
            latency,
            False,
            retries,
            payload.get("id"),
        )


class Scripted(Backend):
    """Answers come from a function: handler(state, questions) -> {name: Answer}.

    For tests and offline work. The handler may return Answer objects or raw System One
    answer dicts, and may return a float for a noul as shorthand.
    """

    name = "scripted"

    capabilities = HTTPBackend.capabilities
    prepare = HTTPBackend.prepare
    attempt = HTTPBackend.attempt
    parse_strict = HTTPBackend.parse_strict

    def __init__(
        self,
        handler: Callable[[Any, Mapping[str, Question]], Mapping[str, Any]],
        *,
        model: str = "scripted",
        max_questions: int = 32,
        max_state_chars: int = 60_000,
    ):
        self.handler = handler
        self.model = model
        self.max_questions = max_questions
        self.max_state_chars = max_state_chars
        self.calls: list[tuple[Any, dict]] = []

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        self.calls.append((state, dict(questions)))
        out = self.handler(state, questions)
        answers: dict[str, Answer] = {}
        for name in questions:
            if name not in out:
                raise BackendError(f"scripted: no answer for {name!r}")
            a = out[name]
            if isinstance(a, (int, float)):
                a = NoulAnswer(float(a))
            elif isinstance(a, Mapping):
                a = parse_answer(a)
            if not isinstance(a, (NoulAnswer, ChoiceAnswer, ScoreAnswer, Refusal)):
                raise BackendError(f"scripted: {name!r} is not an answer: {a!r}")
            answers[name] = a
        return Decision(answers, self.model)


# ------------------------------------------------------------------ presets


def typesafe(api_key: Optional[str] = None, *, model: str = "jev-1.13.0", **kwargs: Any) -> SystemOne:
    """TypeSafe directly. Model pinned to jev-1.13.0: thresholds tuned on one model do not move."""
    key = api_key or os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise ConfigError("typesafe: set TYPESAFE_API_KEY or pass api_key=")
    url = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai")
    return SystemOne(url, api_key=key, model=model, name="typesafe", price_per_mtok=0.042, **kwargs)


def openrouter(api_key: Optional[str] = None, *, model: str = "typesafe/jev-1.13", **kwargs: Any) -> SystemOne:
    """Jev through OpenRouter's Decisions endpoint. No TypeSafe account needed."""
    key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ConfigError("openrouter: set OPENROUTER_API_KEY or pass api_key=")
    return SystemOne(
        "https://openrouter.ai/api/alpha/decisions", api_key=key, model=model, name="openrouter", price_per_mtok=0.042, **kwargs
    )


def local(url: str = "http://127.0.0.1:8000", *, model: str = "local", api_key: Optional[str] = None, **kwargs: Any) -> SystemOne:
    """An open model on your own machine behind /v1/systemone (Kev, Laya, openjev, Clef weights).

    Eight questions per request and a long timeout by default: local servers are slower, and
    some skip questions when packed (the Engine re-asks those). Thresholds tuned on Jev do not
    transfer to another model.
    """
    kwargs.setdefault("timeout", 300.0)
    kwargs.setdefault("max_questions", 8)
    return SystemOne(url, api_key=api_key, model=model, name="local", price_per_mtok=0.0, **kwargs)


def openai(api_key: Optional[str] = None, *, model: str = "gpt-6-luna", **kwargs: Any) -> OpenAIDecisions:
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ConfigError("openai: set OPENAI_API_KEY or pass api_key=")
    return OpenAIDecisions(api_key=key, model=model, **kwargs)


def identity_from_env() -> Backend:
    """The backend from_env() would choose, without requiring its API key.

    Replay needs only the backend's identity (its URL and model) to find recorded decisions, so
    CI can replay a lockfile with no key at all. The placeholder key below is never sent: an
    Engine in replay mode raises CacheMiss instead of calling the backend. Set EXREGEX_BACKEND
    (and EXREGEX_MODEL) to match how the lockfile was recorded.
    """
    try:
        return from_env()
    except ConfigError:
        pass
    choice = (os.environ.get("EXREGEX_BACKEND") or "openrouter").strip()
    model = (os.environ.get("EXREGEX_MODEL") or "").strip()
    kw: dict[str, Any] = {"model": model} if model else {}
    if choice.startswith(("http://", "https://")):
        return local(choice, **kw)
    if choice not in PRESETS:
        raise ConfigError(f"EXREGEX_BACKEND={choice!r}: expected one of {sorted(PRESETS)} or a URL")
    return PRESETS[choice](api_key="replay-only-never-sent", **kw)


PRESETS: dict[str, Callable[..., Backend]] = {"typesafe": typesafe, "openrouter": openrouter, "local": local, "openai": openai}


def from_env() -> Backend:
    """Pick a backend from the environment.

    EXREGEX_BACKEND   typesafe | openrouter | openai | local | an http(s) URL of a System One server
    EXREGEX_MODEL     override the preset's model
    EXREGEX_API_KEY   the bearer key for a URL backend (a hosted System One gateway)
    Otherwise: TYPESAFE_API_KEY, then OPENROUTER_API_KEY.
    """
    choice = (os.environ.get("EXREGEX_BACKEND") or "").strip()
    model = (os.environ.get("EXREGEX_MODEL") or "").strip()
    kw: dict[str, Any] = {"model": model} if model else {}
    if choice.startswith(("http://", "https://")):
        return local(choice, api_key=os.environ.get("EXREGEX_API_KEY") or None, **kw)
    if choice:
        if choice not in PRESETS:
            raise ConfigError(f"EXREGEX_BACKEND={choice!r}: expected one of {sorted(PRESETS)} or a URL")
        return PRESETS[choice](**kw)
    if os.environ.get("TYPESAFE_API_KEY"):
        return typesafe(**kw)
    if os.environ.get("OPENROUTER_API_KEY"):
        return openrouter(**kw)
    raise ConfigError(
        "no decision backend configured. Set OPENROUTER_API_KEY (Jev via OpenRouter) or "
        "TYPESAFE_API_KEY (Jev direct), or EXREGEX_BACKEND=<url> for a local System One server, "
        "or pass engine=Engine(backend) explicitly."
    )
