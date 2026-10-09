"""Single-attempt transport contract, kept separate from permissive legacy calls."""

from __future__ import annotations

import asyncio
import http.client
import json
import math
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType

from .._types import Choice, ChoiceAnswer, Noul, NoulAnswer, Question, Refusal, Score, ScoreAnswer, answer_to_wire
from ..errors import BackendError, RefusedError
from .errors import UnsupportedCapability


@dataclass(frozen=True)
class BackendCapabilities:
    adapter: str
    version: str
    endpoint: str
    model: str
    answer_types: tuple[str, ...]
    max_questions: int
    max_state_chars: int
    max_input_tokens: int | None
    max_state_question_tokens: int | None
    price_per_mtok: float | None
    output_price_per_mtok: float | None = 0.0
    tokenizer_id: str | None = None
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class NamedQuestion:
    name: str
    question: Question


@dataclass(frozen=True)
class PreparedRequest:
    capabilities: BackendCapabilities
    state_utf8: bytes
    questions: tuple[NamedQuestion, ...]
    body: bytes


@dataclass(frozen=True)
class AttemptResponse:
    status: int | None
    body: bytes
    elapsed_ms: float
    dispatched: bool = True
    retry_after_s: float | None = None


@dataclass(frozen=True)
class ValidatedObservation:
    answers: Mapping
    model: str
    input_tokens: int | None
    cost_usd: float | None
    request_id: str | None


class CancelToken:
    def __init__(self):
        self.event = threading.Event()

    def check(self):
        if self.event.is_set():
            raise asyncio.CancelledError()

    def cancel(self):
        self.event.set()


def capabilities(backend) -> BackendCapabilities:
    from ..backends import OpenAIDecisions, Scripted, SystemOne

    if not isinstance(backend, (SystemOne, OpenAIDecisions, Scripted)):
        raise UnsupportedCapability("backend must implement the explicit semantic attempt protocol")
    scripted = isinstance(backend, Scripted)
    adapter = "scripted" if scripted else "openai-decisions" if isinstance(backend, OpenAIDecisions) else "systemone"
    headers = getattr(backend, "_headers", {})
    # Credentials are dispatch-only and never appear in plans or cache identities.
    safe_headers = tuple(sorted((k, v) for k, v in headers.items() if k.lower() not in ("authorization", "api-key", "x-api-key")))
    jev = "jev" in backend.model
    price = 0.0 if scripted else getattr(backend, "_semantic_price", backend.price_per_mtok)
    return BackendCapabilities(
        adapter,
        "1",
        getattr(backend, "url", "scripted"),
        backend.model,
        ("noul", "choice", "score"),
        backend.max_questions,
        backend.max_state_chars,
        64_000 if jev else None,
        32_000 if jev else None,
        price,
        headers=safe_headers,
    )


def prepare(backend, state_utf8: bytes, questions: tuple[NamedQuestion, ...]) -> PreparedRequest:
    caps = backend.capabilities()
    qs = {}
    for named in questions:
        if named.name in qs:
            raise BackendError("duplicate question ID")
        qs[named.name] = named.question
    state = json.loads(state_utf8)
    if caps.adapter == "openai-decisions":
        body = {"model": caps.model, "input": state_utf8.decode("utf-8"), "questions": [backend._question(k, q) for k, q in qs.items()]}
    else:
        body = {"model": caps.model, "state": state, "questions": {k: q.to_wire() for k, q in qs.items()}}
    # Preserve option/level ordering; only unordered state was canonicalized beforehand.
    data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return PreparedRequest(caps, state_utf8, questions, data)


def retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None


def attempt(backend, request: PreparedRequest, *, timeout_s: float, cancel: CancelToken) -> AttemptResponse:
    from ..backends import _OPENER, USER_AGENT

    cancel.check()
    started = time.monotonic()
    if request.capabilities.adapter == "scripted":
        state = json.loads(request.state_utf8)
        qs = {q.name: q.question for q in request.questions}
        backend.calls.append((state, qs.copy()))
        out = backend.handler(state, MappingProxyType(qs))
        if not isinstance(out, Mapping):
            raise BackendError("scripted handler must return an answer mapping")
        raw = {}
        for name, answer in out.items():
            if isinstance(answer, (NoulAnswer, ChoiceAnswer, ScoreAnswer, Refusal)):
                answer = answer_to_wire(answer)
            elif type(answer) in (float, int):
                answer = {"type": "noul", "noul": answer}
            raw[name] = answer
        # allow_nan here so strict parsing, not the serializer, rejects malformed test values.
        body = json.dumps(
            {"model": request.capabilities.model, "answers": raw, "usage": {"input_tokens": 0, "cost": 0}}, ensure_ascii=False
        ).encode("utf-8")
        return AttemptResponse(200, body, (time.monotonic() - started) * 1000)
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT, **dict(request.capabilities.headers)}
    for name, value in getattr(backend, "_headers", {}).items():
        if name.lower() in ("authorization", "api-key", "x-api-key"):
            headers[name] = value
    if backend._api_key:
        headers["Authorization"] = f"Bearer {backend._api_key}"
    req = urllib.request.Request(request.capabilities.endpoint, data=request.body, headers=headers, method="POST")
    cancel.check()
    try:
        with _OPENER.open(req, timeout=timeout_s) as response:
            status, body, retry = response.status, response.read(), None
    except urllib.error.HTTPError as error:
        status, body = error.code, error.read() if error.fp else b""
        retry = retry_after(error.headers.get("retry-after") if error.headers else None)
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        status, body, retry = None, b"", None
    return AttemptResponse(status, body, (time.monotonic() - started) * 1000, retry_after_s=retry)


def _number(value, *, high=1.0, low=0.0):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise BackendError("malformed numeric answer")
    return value


def _distribution(raw, expected):
    if not isinstance(raw, Mapping) or set(raw) != set(expected):
        raise BackendError("answer distribution does not match the declared options")
    values = {key: _number(value) for key, value in raw.items()}
    if abs(sum(values.values()) - 1) > 1e-4:
        raise BackendError("answer probabilities do not sum to one")
    return MappingProxyType(values)


def strict_answer(raw, question: Question):
    if not isinstance(raw, Mapping):
        raise BackendError("malformed typed answer")
    kind = raw.get("type")
    if kind == "refusal":
        raise RefusedError("backend refused a required question")
    if isinstance(question, Noul) and kind == "noul":
        return NoulAnswer(_number(raw.get("noul")))
    if isinstance(question, Choice) and kind == "choice":
        if not isinstance(raw.get("choice"), str) or raw["choice"] not in question.criteria:
            raise BackendError("selected option was not declared")
        return ChoiceAnswer(raw["choice"], _distribution(raw.get("probabilities"), question.criteria), _number(raw.get("confidence")))
    if isinstance(question, Score) and kind == "score":
        probs = _distribution(raw.get("probabilities"), tuple(str(i) for i in range(len(question.criteria))))
        return ScoreAnswer(
            _number(raw.get("score"), high=len(question.criteria) - 1),
            MappingProxyType({int(k): v for k, v in probs.items()}),
            _number(raw.get("confidence")),
            MappingProxyType(dict(enumerate(question.criteria))),
        )
    raise BackendError("answer type does not match question")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackendError("duplicate key in provider JSON")
        result[key] = value
    return result


def _openai_answer(raw):
    kind = raw.get("type")
    if kind == "predicate":
        return {"type": "noul", "noul": raw.get("probability")}
    if kind in ("choice", "score"):
        rows = raw.get("probabilities")
        if not isinstance(rows, list):
            raise BackendError("malformed provider distribution")
        pairs = []
        for row in rows:
            if not isinstance(row, dict):
                raise BackendError("malformed probability row")
            key = row.get("value", row.get("label"))
            if not isinstance(key, str):
                raise BackendError("provider option labels must be strings")
            pairs.append((key, row.get("probability")))
        return {"type": kind, kind: raw.get(kind), "probabilities": _object(pairs), "confidence": raw.get("confidence")}
    return raw


def parse_strict(response: AttemptResponse, request: PreparedRequest) -> ValidatedObservation:
    try:
        payload = json.loads(response.body, object_pairs_hook=_object)
    except (ValueError, UnicodeError):
        raise BackendError("provider response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise BackendError("provider response must be an object")
    if "answers" not in payload and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    raw = payload.get("answers")
    if request.capabilities.adapter == "openai-decisions":
        if not isinstance(raw, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in raw):
            raise BackendError("provider answers must be named objects")
        raw = _object([(row["name"], _openai_answer(row)) for row in raw])
    if not isinstance(raw, dict):
        raise BackendError("provider answers must be an object")
    expected = {q.name: q.question for q in request.questions}
    if set(raw) - set(expected):
        raise BackendError("provider returned undeclared answer IDs")
    # Validate present answers before deciding missing IDs are retryable.
    answers = {name: strict_answer(value, expected[name]) for name, value in raw.items()}
    if set(raw) != set(expected):
        raise BackendError("provider omitted required answers", retryable=True)
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise BackendError("provider omitted its model identity")
    usage = payload.get("usage", {})
    if not isinstance(usage, dict):
        raise BackendError("malformed usage metadata")
    tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    if tokens is not None and (type(tokens) is not int or tokens < 0):
        raise BackendError("malformed input token count")
    cost = usage.get("cost")
    if cost is not None:
        cost = _number(cost, high=float("inf"))
    # Usage-derived estimates are not provider-reported billing. Leave the cost
    # unknown so the runtime retains its conservative reservation as a liability.
    rid = payload.get("id")
    if rid is not None and not isinstance(rid, str):
        raise BackendError("malformed provider request ID")
    return ValidatedObservation(MappingProxyType(answers), model, tokens, cost, rid)
