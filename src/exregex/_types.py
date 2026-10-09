"""Typed questions and answers: the System One wire format, as Python values.

A request is a *state* (any text or JSON) plus named *questions*. Each question is one of
three types, and each comes back as a typed answer with probabilities:

    Noul    yes or no             -> NoulAnswer.p, the probability of yes
    Choice  pick one of 2-255      -> ChoiceAnswer.choice, .probabilities, .confidence
    Score   place on 2-10 levels   -> ScoreAnswer.score, .probabilities, .confidence

The limits are checked here, before a request costs anything.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from .errors import BackendError, LimitError

MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10


def _check_instructions(instructions: Any) -> None:
    if instructions is None or (isinstance(instructions, str) and not instructions.strip()):
        raise LimitError("a question needs non-empty instructions")


@dataclass(frozen=True)
class Noul:
    """A yes-or-no question. `criteria` may describe what counts as true and as false."""

    instructions: Any
    criteria: Optional[Mapping[str, str]] = None

    def __post_init__(self) -> None:
        _check_instructions(self.instructions)
        if self.criteria is not None:
            extra = set(self.criteria) - {"true", "false"}
            if extra:
                raise LimitError(f"noul criteria take only 'true' and 'false', got {sorted(extra)}")

    def to_wire(self) -> dict:
        d: dict = {"type": "noul", "instructions": self.instructions}
        if self.criteria:
            d["criteria"] = dict(self.criteria)
        return d


@dataclass(frozen=True)
class Choice:
    """Pick one option. `criteria` maps each option to a description (or None)."""

    instructions: Any
    criteria: Mapping[str, Any]

    def __post_init__(self) -> None:
        _check_instructions(self.instructions)
        if not isinstance(self.criteria, Mapping):
            raise LimitError("choice criteria must be a mapping of option to description")
        n = len(self.criteria)
        if n < 2 or n > MAX_CHOICE_OPTIONS:
            raise LimitError(f"a choice needs 2-{MAX_CHOICE_OPTIONS} options, got {n}")
        for key in self.criteria:
            if not isinstance(key, str) or not key:
                raise LimitError(f"choice options must be non-empty strings, got {key!r}")

    def to_wire(self) -> dict:
        return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.criteria)}


@dataclass(frozen=True)
class Score:
    """Place the state on an ordered scale. `criteria` lists the levels, lowest first."""

    instructions: Any
    criteria: Sequence[str]

    def __post_init__(self) -> None:
        _check_instructions(self.instructions)
        if isinstance(self.criteria, (str, bytes)) or not isinstance(self.criteria, Sequence):
            raise LimitError("score criteria must be a list of level descriptions, lowest first")
        n = len(self.criteria)
        if n < MIN_SCORE_LEVELS or n > MAX_SCORE_LEVELS:
            raise LimitError(f"a score needs {MIN_SCORE_LEVELS}-{MAX_SCORE_LEVELS} levels, got {n}")

    def to_wire(self) -> dict:
        return {"type": "score", "instructions": self.instructions, "criteria": list(self.criteria)}


Question = Union[Noul, Choice, Score]


@dataclass(frozen=True)
class NoulAnswer:
    p: float
    type: str = field(default="noul", init=False)


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: Mapping[str, float]
    confidence: float
    type: str = field(default="choice", init=False)

    def p(self, option: Optional[str] = None) -> float:
        """The probability of `option` (default: the chosen one)."""
        return float(self.probabilities.get(self.choice if option is None else option, 0.0))


@dataclass(frozen=True)
class ScoreAnswer:
    score: float
    probabilities: Mapping[int, float]
    confidence: float
    legend: Mapping[int, str] = field(default_factory=dict)
    type: str = field(default="score", init=False)

    @property
    def level(self) -> int:
        """The most probable level, as an index (the lowest level is 0)."""
        if not self.probabilities:
            return round(self.score)
        return max(self.probabilities, key=lambda k: self.probabilities[k])


@dataclass(frozen=True)
class Refusal:
    """A backend declined to answer this question (OpenAI's Decisions API can)."""

    reason: Optional[str] = None
    type: str = field(default="refusal", init=False)


Answer = Union[NoulAnswer, ChoiceAnswer, ScoreAnswer, Refusal]


def _prob(value: Any, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise BackendError(f"malformed answer: {what} is {value!r}") from None
    if math.isnan(x):
        raise BackendError(f"malformed answer: {what} is NaN")
    return min(1.0, max(0.0, x))


def _real(value: Any, what: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        raise BackendError(f"malformed answer: {what} is {value!r}") from None
    if math.isnan(x):
        raise BackendError(f"malformed answer: {what} is NaN")
    return x


def parse_answer(raw: Any) -> Answer:
    """Turn one answer in the System One shape into a typed answer."""
    if not isinstance(raw, Mapping):
        raise BackendError(f"malformed answer: {raw!r}")
    kind = raw.get("type")
    if kind == "noul":
        return NoulAnswer(_prob(raw.get("noul"), "noul"))
    if kind == "choice":
        probs = {str(k): _prob(v, "probability") for k, v in (raw.get("probabilities") or {}).items()}
        choice = raw.get("choice")
        if choice is None and probs:
            choice = max(probs, key=lambda k: probs[k])
        if choice is None:
            raise BackendError("malformed answer: a choice without a pick")
        conf = raw.get("confidence", probs.get(str(choice), 0.0))
        return ChoiceAnswer(str(choice), probs, _prob(conf, "confidence"))
    if kind == "score":
        try:
            levels = {int(k): _prob(v, "probability") for k, v in (raw.get("probabilities") or {}).items()}
            legend = {int(k): str(v) for k, v in (raw.get("legend") or {}).items()}
        except ValueError:
            raise BackendError(f"malformed answer: score levels {raw.get('probabilities')!r}") from None
        return ScoreAnswer(_real(raw.get("score"), "score"), levels, _prob(raw.get("confidence", 0.0), "confidence"), legend)
    if kind == "refusal":
        return Refusal(raw.get("reason") or raw.get("refusal"))
    raise BackendError(f"unknown answer type {kind!r}")


def answer_to_wire(answer: Answer) -> dict:
    """The inverse of parse_answer, for the cache."""
    if isinstance(answer, NoulAnswer):
        return {"type": "noul", "noul": answer.p}
    if isinstance(answer, ChoiceAnswer):
        return {
            "type": "choice",
            "choice": answer.choice,
            "probabilities": dict(answer.probabilities),
            "confidence": answer.confidence,
        }
    if isinstance(answer, ScoreAnswer):
        return {
            "type": "score",
            "score": answer.score,
            "probabilities": {str(k): v for k, v in answer.probabilities.items()},
            "confidence": answer.confidence,
            "legend": {str(k): v for k, v in answer.legend.items()},
        }
    return {"type": "refusal", "reason": answer.reason}


@dataclass(frozen=True)
class Decision:
    """One request's answers, plus what it cost."""

    answers: Mapping[str, Answer]
    model: str
    input_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False
    retries: int = 0
    request_id: Optional[str] = None
