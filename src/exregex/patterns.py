"""Patterns by meaning: the re-shaped API.

    import exregex as ex

    ex.test("asks for a refund", ticket)                    # Verdict, truthy at p >= threshold
    ex.search("who owns uploaded code", terms, unit="line")  # the best-fitting Match, or None
    ex.findall("a complaint about shipping", review)        # every sentence that fits
    ex.sub("a way to contact a specific person", "[redacted]", text, unit="contact")
    ex.split("a section heading", doc, unit="line")
    ex.extract("the total amount due", invoice, unit="money")
    ex.classify(ticket, {"billing": "...", "bug": "..."})
    ex.rate(ticket, "How urgent is this?", ["Can wait", "This week", "Today", "Now"])

Every Match is a verbatim slice of the input with its offsets. Jev only chooses among spans a
unit found; it never writes the span, so a result can be wrong but never invented.
"""

from __future__ import annotations

import asyncio
import functools
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ._types import Choice, ChoiceAnswer, Noul, NoulAnswer, Question, Refusal, Score, ScoreAnswer
from .engine import Engine, get_engine
from .errors import BackendError, LimitError, RefusedError
from .units import Span, UnitLike, find, is_segment

DEFAULT_THRESHOLD = 0.5
REVIEW_BAND = (0.1, 0.9)
NEAR_CHARS = 160  # context shown around a candidate on each side
FULL_TEXT_CHARS = 6000  # below this, a candidate request also carries the whole text
SEARCH_WINDOW = 200  # segments per choice question in search (the hard limit is 255 options)

# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class Verdict:
    """A yes/no judgment. Truthy when p >= threshold."""

    p: float
    threshold: float = DEFAULT_THRESHOLD

    def __bool__(self) -> bool:
        return self.p >= self.threshold

    def __float__(self) -> float:
        return self.p

    @property
    def band(self) -> str:
        """'yes', 'no', or 'review' (the uncertain middle, which deserves a human look)."""
        low, high = REVIEW_BAND
        return "yes" if self.p >= high else "no" if self.p <= low else "review"


@dataclass(frozen=True)
class Match:
    """A span of the input that fits the meaning. `text` is a verbatim slice of the input."""

    text: str
    start: int
    end: int
    p: float
    unit: str
    confidence: Optional[float] = None

    def group(self, index: int = 0) -> str:
        if index != 0:
            raise IndexError("ex-regex matches have one group: the span itself")
        return self.text

    def span(self) -> tuple[int, int]:
        return (self.start, self.end)

    def __str__(self) -> str:
        return self.text

    @property
    def band(self) -> str:
        return Verdict(self.p).band


@dataclass(frozen=True)
class Pick:
    """A classification: the chosen label and every label's probability."""

    label: str
    p: float
    confidence: float
    probabilities: Mapping[str, float] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.label

    def top(self, n: int = 3) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: -kv[1])[:n]


@dataclass(frozen=True)
class Rating:
    """A place on an ordered scale. `score` is probability-weighted and can sit between levels."""

    score: float
    level: int
    label: str
    confidence: float
    probabilities: Mapping[int, float] = field(default_factory=dict)

    def __float__(self) -> float:
        return self.score


# ------------------------------------------------------------------ wording


def _fit_question(ref: str, meaning: str, question: Optional[str]) -> Noul:
    if question:
        if "{ref}" not in question:
            raise ValueError("a custom question must contain {ref}, where the span goes, e.g. 'Does {ref} ask for a refund?'")
        return Noul(question.replace("{ref}", ref))
    return Noul(
        f"Does {ref} fit this description: {meaning}",
        {"true": f"{ref} fits the description.", "false": f"{ref} does not fit the description."},
    )


def _with_context(state: dict, context: Optional[str]) -> dict:
    return {"context": context, **state} if context else state


def _p(answer: Any) -> float:
    if isinstance(answer, Refusal):
        raise RefusedError(f"the backend refused a question: {answer.reason or 'no reason given'}")
    if isinstance(answer, NoulAnswer):
        return answer.p
    raise BackendError(f"expected a yes/no answer, got {answer!r}")


def _choice(answer: Any) -> ChoiceAnswer:
    if isinstance(answer, Refusal):
        raise RefusedError(f"the backend refused a question: {answer.reason or 'no reason given'}")
    if isinstance(answer, ChoiceAnswer):
        return answer
    raise BackendError(f"expected a choice answer, got {answer!r}")


def _check_size(
    engine: Engine, text: str, what: str, hint: str = "Use a smaller unit (paragraph, sentence, line) so ex-regex can split it."
) -> None:
    if len(text) > engine.max_state_chars:
        raise LimitError(f"{what} is {len(text):,} characters; one request holds about {engine.max_state_chars:,}. {hint}")


def _question_cost(meaning: str, question: Optional[str]) -> int:
    """Characters one packed question adds to a request: the wording, the reference, the criteria."""
    return len(question or meaning) + 140


def _budget(engine: Engine, context: Optional[str]) -> int:
    return max(1000, engine.max_state_chars - (len(context) if context else 0))


# ------------------------------------------------------------------ judging spans


def _windows(spans: Sequence[Span], per: int, max_chars: int, overhead: int = 40) -> list[list[int]]:
    """Consecutive groups of span indexes, each at most `per` long and `max_chars` of text."""
    out: list[list[int]] = []
    cur: list[int] = []
    size = 0
    for i, s in enumerate(spans):
        cost = len(s.text) + overhead
        if cur and (len(cur) >= per or size + cost > max_chars):
            out.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += cost
    if cur:
        out.append(cur)
    return out


def _judge_segments(
    engine: Engine, meaning: str, pieces: Sequence[str], *, container: str, context: Optional[str], question: Optional[str]
) -> list[float]:
    """P(fits) for each piece. Pieces travel together, one question each, as Sieve packs them."""
    spans = [Span(0, len(t), t, container) for t in pieces]
    hint = "Split the text first (findall with unit='paragraph'), or raise the backend's max_state_chars." if container == "items" else None
    for t in pieces:
        _check_size(engine, t, "the text" if container == "items" else f"one {container[:-1]}", *([hint] if hint else []))
    requests = []
    windows = _windows(spans, engine.max_questions, _budget(engine, context), overhead=_question_cost(meaning, question))
    for w in windows:
        ids = {i: f"{container[0].upper()}{k + 1:03d}" for k, i in enumerate(w)}
        state = _with_context({container: {ids[i]: pieces[i] for i in w}}, context)
        qs = {ids[i]: _fit_question(f"`{container}.{ids[i]}`", meaning, question) for i in w}
        requests.append((state, qs))
    decisions = engine.ask_many(requests)
    probs = [0.0] * len(pieces)
    for w, d in zip(windows, decisions, strict=True):
        for k, i in enumerate(w):
            probs[i] = _p(d.answers[f"{container[0].upper()}{k + 1:03d}"])
    return probs


def _near(text: str, span: Span, chars: int = NEAR_CHARS) -> str:
    a = max(0, span.start - chars)
    b = min(len(text), span.end + chars)
    pre = ("…" if a > 0 else "") + text[a : span.start]
    post = text[span.end : b] + ("…" if b < len(text) else "")
    return f"{pre}⟦{span.text}⟧{post}"


def _judge_candidates(
    engine: Engine, meaning: str, text: str, spans: Sequence[Span], *, context: Optional[str], question: Optional[str]
) -> list[float]:
    """P(fits) for each candidate occurrence, judged where it sits in the text."""
    if not spans:
        return []
    carry_text = len(text) <= min(FULL_TEXT_CHARS, engine.max_state_chars // 2)
    budget = _budget(engine, context) - (len(text) if carry_text else 0)
    sized = [Span(s.start, s.end, _near(text, s), s.unit) for s in spans]
    windows = _windows(sized, engine.max_questions, max(budget, 2000), overhead=_question_cost(meaning, question) + 40)
    requests = []
    for w in windows:
        cands = {f"C{k + 1:03d}": {"value": spans[i].text, "in_context": sized[i].text} for k, i in enumerate(w)}
        state: dict = {"candidates": cands}
        if carry_text:
            state = {"text": text, **state}
        state = _with_context(state, context)
        qs = {cid: _fit_question(f"`candidates.{cid}.value`", meaning, question) for cid in cands}
        requests.append((state, qs))
    decisions = engine.ask_many(requests)
    probs = [0.0] * len(spans)
    for w, d in zip(windows, decisions, strict=True):
        for k, i in enumerate(w):
            probs[i] = _p(d.answers[f"C{k + 1:03d}"])
    return probs


Prefilter = Union[str, "re.Pattern[str]", Callable[[str], bool], None]


def _prefilter_fn(prefilter: Prefilter) -> Optional[Callable[[str], bool]]:
    if prefilter is None:
        return None
    if isinstance(prefilter, str):
        prefilter = re.compile(prefilter)
    if isinstance(prefilter, re.Pattern):
        rx = prefilter
        return lambda t: rx.search(t) is not None
    return prefilter


def _judge_spans(
    engine: Engine, meaning: str, text: str, spans: Sequence[Span], *, segments: bool, context: Optional[str], question: Optional[str]
) -> list[float]:
    if not spans:
        return []
    if segments:
        return _judge_segments(engine, meaning, [s.text for s in spans], container="segments", context=context, question=question)
    return _judge_candidates(engine, meaning, text, spans, context=context, question=question)


def _scan(
    engine: Engine,
    meaning: str,
    text: str,
    unit: UnitLike,
    context: Optional[str],
    question: Optional[str],
    prefilter: Optional[Callable[[str], bool]] = None,
) -> list[Match]:
    spans = find(text, unit)
    if not spans:
        return []
    # A prefilter is a cheap, local test a span must pass before anyone is asked. Spans that fail
    # come back with p = 0 and cost nothing: regex narrows, Jev decides.
    asked = [i for i, s in enumerate(spans) if prefilter is None or prefilter(s.text)]
    probs = [0.0] * len(spans)
    for i, p in zip(
        asked,
        _judge_spans(engine, meaning, text, [spans[i] for i in asked], segments=is_segment(unit), context=context, question=question),
        strict=True,
    ):
        probs[i] = p
    return [Match(s.text, s.start, s.end, p, s.unit) for s, p in zip(spans, probs, strict=True)]


@dataclass(frozen=True)
class Diff:
    """Where a regex and a meaning disagree about the same text.

    regex_only    spans the regex matched and the meaning rejected (likely regex false positives)
    meaning_only  spans the meaning matched and the regex missed (likely regex false negatives)
    agree         how many spans both called the same way
    Only the disagreements need a human look, so you can audit a regex without labeling anything.
    """

    regex_only: list
    meaning_only: list
    agree: int

    @property
    def clean(self) -> bool:
        return not self.regex_only and not self.meaning_only

    def __str__(self) -> str:
        return f"agree {self.agree}, regex only {len(self.regex_only)}, meaning only {len(self.meaning_only)}"


# ------------------------------------------------------------------ the Pattern


class Pattern:
    """A compiled meaning. Reuse one across many texts; it holds no per-text state.

    meaning     plain-English description of what to find ("asks for a refund")
    unit        what counts as a span: a segment (line, sentence, paragraph, text), a candidate
                kind (email, phone, money, date, name, contact, ...), a tuple of those, or a callable
    threshold   p at or above which a span matches
    context     a sentence about the text, sent with every request ("These are support tickets.")
    question    full custom wording with {ref} where the span goes; replaces the default
    engine      an Engine; default: exregex.get_engine()
    """

    def __init__(
        self,
        meaning: str,
        *,
        unit: UnitLike = "sentence",
        threshold: float = DEFAULT_THRESHOLD,
        context: Optional[str] = None,
        question: Optional[str] = None,
        prefilter: Prefilter = None,
        engine: Optional[Engine] = None,
    ):
        if not meaning or not str(meaning).strip():
            raise ValueError("a pattern needs a meaning")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold must be between 0 and 1")
        if question is not None and "{ref}" not in question:
            raise ValueError("a custom question must contain {ref}, where the span goes")
        if not callable(unit):
            find("", unit)  # fail fast on an unknown unit name
        self.meaning = str(meaning).strip()
        self.unit = unit
        self.threshold = threshold
        self.context = context
        self.question = question
        self.prefilter = prefilter
        self._prefilter = _prefilter_fn(prefilter)
        self._engine = engine

    def __repr__(self) -> str:
        return f"exregex.compile({self.meaning!r}, unit={self.unit!r}, threshold={self.threshold})"

    @property
    def engine(self) -> Engine:
        return self._engine if self._engine is not None else get_engine()

    # --- whole text

    def test(self, text: str) -> Verdict:
        """Does the whole text fit the meaning?"""
        return Verdict(self.filter_p([text])[0], self.threshold)

    fullmatch = test

    # --- spans within one text

    def scan(self, text: str) -> list[Match]:
        """Every span of the unit with its probability, matching or not, in document order."""
        return _scan(self.engine, self.meaning, text, self.unit, self.context, self.question, self._prefilter)

    def findall(self, text: str) -> list[Match]:
        """Every span that fits, in document order. (re.findall returns strings; this returns Matches.)"""
        return [m for m in self.scan(text) if m.p >= self.threshold]

    def finditer(self, text: str) -> Iterator[Match]:
        return iter(self.findall(text))

    def search(self, text: str) -> Optional[Match]:
        """The single best-fitting span, or None when nothing fits.

        Unlike re.search this returns the best match, not the first. For segment units it asks
        one choice question over the segments plus one yes/no "is it here at all?" question
        (TypeSafe's line-by-line search cookbook); for candidate units it scans and takes the max.
        A custom `question` is a per-span question, so with one it scans and takes the max too.
        A prefilter applies either way: only spans that pass it are ever asked about.
        """
        if not is_segment(self.unit) or self.question is not None:
            hits = self.findall(text)
            return max(hits, key=lambda m: m.p) if hits else None
        spans = [s for s in find(text, self.unit) if self._prefilter is None or self._prefilter(s.text)]
        if not spans:
            return None
        if len(spans) == 1:
            s = spans[0]
            p = _judge_spans(self.engine, self.meaning, text, spans, segments=True, context=self.context, question=None)[0]
            return Match(s.text, s.start, s.end, p, s.unit) if p >= self.threshold else None
        engine = self.engine
        for s in spans:
            _check_size(engine, s.text, f"one {s.unit}")
        windows = _windows(spans, SEARCH_WINDOW, _budget(engine, self.context), overhead=12)
        requests = []
        all_ids = []
        for w in windows:
            ids = {i: f"S{k + 1:03d}" for k, i in enumerate(w)}
            all_ids.append(ids)
            state = _with_context({"segments": {ids[i]: spans[i].text for i in w}}, self.context)
            exists = Noul(
                f"Does any segment in `segments` fit this description: {self.meaning}",
                {"true": "At least one segment fits the description.", "false": "No segment fits the description."},
            )
            qs: dict[str, Question] = {"exists": exists}
            if len(w) >= 2:
                qs["best"] = Choice(f"Which segment in `segments` best fits this description: {self.meaning}", {ids[i]: None for i in w})
            requests.append((state, qs))
        decisions = engine.ask_many(requests)
        best: Optional[Match] = None
        for w, ids, d in zip(windows, all_ids, decisions, strict=True):
            p_exists = _p(d.answers["exists"])
            if len(w) >= 2:
                pick = _choice(d.answers["best"])
                by_id = {v: i for i, v in ids.items()}
                if pick.choice in by_id:
                    span, conf = spans[by_id[pick.choice]], pick.p()
                else:
                    # The pick is not one of the ids; judge this window's segments one by one instead.
                    ps = _judge_segments(
                        engine, self.meaning, [spans[i].text for i in w], container="segments", context=self.context, question=self.question
                    )
                    k = max(range(len(w)), key=lambda j: ps[j])
                    span, conf, p_exists = spans[w[k]], ps[k], min(p_exists, ps[k])
            else:
                span, conf = spans[w[0]], 1.0
            m = Match(span.text, span.start, span.end, p_exists, span.unit, conf)
            if best is None or (m.p, m.confidence or 0) > (best.p, best.confidence or 0):
                best = m
        return best if best is not None and best.p >= self.threshold else None

    def diff(self, regex: Union[str, re.Pattern[str]], text: str) -> Diff:
        """Run an existing regex and this meaning over the same text; report where they disagree.

        For segment units (line, sentence, ...) a segment counts as a regex hit when the regex
        matches anywhere in it. For candidate units the regex's own matches are judged too, so a
        match the finders never proposed still gets a verdict.
        """
        rx = re.compile(regex) if isinstance(regex, str) else regex
        hits = [(m.start(), m.end()) for m in rx.finditer(text) if m.end() > m.start()]
        spans = find(text, self.unit)
        if not is_segment(self.unit):
            extra = [Span(a, b, text[a:b], "regex") for a, b in hits if not any(a < s.end and s.start < b for s in spans)]
            spans = sorted([*spans, *extra], key=lambda s: s.start)
        probs = _judge_spans(
            self.engine, self.meaning, text, spans, segments=is_segment(self.unit), context=self.context, question=self.question
        )
        regex_only, meaning_only, agree = [], [], 0
        for s, p in zip(spans, probs, strict=True):
            by_regex = any(a < s.end and s.start < b for a, b in hits)
            m = Match(s.text, s.start, s.end, p, s.unit)
            if by_regex and p < self.threshold:
                regex_only.append(m)
            elif p >= self.threshold and not by_regex:
                meaning_only.append(m)
            else:
                agree += 1
        return Diff(regex_only, meaning_only, agree)

    def sub(self, repl: Union[str, Callable[[Match], str]], text: str, count: int = 0) -> str:
        return self.subn(repl, text, count)[0]

    def subn(self, repl: Union[str, Callable[[Match], str]], text: str, count: int = 0) -> tuple[str, int]:
        """Replace every span that fits. `repl` is a string or a function of the Match."""
        if count < 0:
            raise ValueError("count must be 0 (all) or a positive number")
        hits = self.findall(text)
        if count:
            hits = hits[:count]
        out = []
        pos = 0
        for m in hits:
            out.append(text[pos : m.start])
            out.append(repl(m) if callable(repl) else repl)
            pos = m.end
        out.append(text[pos:])
        return "".join(out), len(hits)

    def split(self, text: str, *, keep: bool = False) -> list[str]:
        """Split the text at spans that fit; the spans are the separators.

        Pieces are stripped and empty pieces dropped. keep=True includes each separator as its
        own item, like a capturing group in re.split.
        """
        pieces: list[str] = []
        pos = 0
        for m in self.findall(text):
            before = text[pos : m.start].strip()
            if before:
                pieces.append(before)
            if keep:
                pieces.append(m.text)
            pos = m.end
        rest = text[pos:].strip()
        if rest:
            pieces.append(rest)
        return pieces

    # --- many texts

    def filter_p(self, texts: Sequence[str]) -> list[float]:
        """P(fits) for each whole text, packed several to a request."""
        texts = list(texts)
        if not texts:
            return []
        asked = [i for i, t in enumerate(texts) if self._prefilter is None or self._prefilter(t)]
        probs = [0.0] * len(texts)
        if asked:
            got = _judge_segments(
                self.engine, self.meaning, [texts[i] for i in asked], container="items", context=self.context, question=self.question
            )
            for i, p in zip(asked, got, strict=True):
                probs[i] = p
        return probs

    def filter(self, texts: Iterable[str]) -> list[str]:
        """The texts that fit, in their original order."""
        texts = list(texts)
        return [t for t, p in zip(texts, self.filter_p(texts), strict=True) if p >= self.threshold]

    def rank(self, texts: Iterable[str]) -> list[tuple[str, float]]:
        """Every text with its probability, best first."""
        texts = list(texts)
        return sorted(zip(texts, self.filter_p(texts), strict=True), key=lambda tp: -tp[1])

    # --- async twins (run the blocking call in a worker thread)

    async def atest(self, text: str) -> Verdict:
        return await asyncio.to_thread(self.test, text)

    async def asearch(self, text: str) -> Optional[Match]:
        return await asyncio.to_thread(self.search, text)

    async def afindall(self, text: str) -> list[Match]:
        return await asyncio.to_thread(self.findall, text)

    async def asub(self, repl: Union[str, Callable[[Match], str]], text: str, count: int = 0) -> str:
        return await asyncio.to_thread(self.sub, repl, text, count)

    async def afilter(self, texts: Iterable[str]) -> list[str]:
        return await asyncio.to_thread(self.filter, list(texts))


# ------------------------------------------------------------------ extraction and classification


NONE_OPTION = "(none of these)"


def _extract_question(what: str, values: Sequence[str]) -> Choice:
    criteria: dict[str, Any] = {v: None for v in values}
    criteria[NONE_OPTION] = f"None of these values is {what}."
    return Choice(f"Which of these values is {what}?", criteria)


def _extract_pick(answer: Any, values: Sequence[str]) -> tuple[Optional[str], float, float]:
    a = _choice(answer)
    if a.choice == NONE_OPTION or a.choice not in values:
        return None, a.p(), a.confidence
    return a.choice, a.p(), a.confidence


def _tournament(
    engine: Engine,
    question: str,
    text: str,
    occurrences: Sequence[Span],
    *,
    none: Optional[str],
    context: Optional[str],
    budget: int,
) -> Optional[tuple[Span, float, float]]:
    """Pick one occurrence from any number of them, each judged in its own neighborhood.

    Each round packs occurrences into groups that fit one request, asks one choice question per
    group, and keeps the winners. Every round must shrink the pool: when the groups come out as
    singletons, the neighborhoods are shortened and the round is planned again, and if even a
    bare value cannot share a request, LimitError is raised instead of looping. With `none`, a
    "none of these" option is offered and may end the search with no answer.
    """
    pool = list(occurrences)
    if not pool:
        return None
    near = NEAR_CHARS
    asked_once = False
    per_group = 254 if none is not None else 255
    while True:
        if any(len(o.text) > budget for o in pool):
            raise LimitError("candidates are too large to share the configured request; use smaller spans")
        if len(pool) == 1 and (none is None or asked_once):
            return pool[0], 1.0, 1.0
        sized = [Span(0, 0, _near(text, o, near), "c") for o in pool]
        groups = _windows(sized, per_group, budget - 500, overhead=len(question) + 60)
        if len(pool) > 1 and len(groups) == len(pool):
            if near > 0:
                near = near // 2 if near > 20 else 0
                continue
            raise LimitError(
                f"{len(pool)} candidates are each too large to share a request of {budget:,} characters, so they cannot be compared. "
                "Use a unit with smaller spans, or a backend with a larger max_state_chars."
            )
        requests, plans = [], []
        for g in groups:
            ids = {f"O{k + 1:03d}": pool[i] for k, i in enumerate(g)}
            if len(ids) == 1 and none is None:
                plans.append(("auto", ids))
                continue
            criteria: dict[str, Any] = {k: o.text for k, o in ids.items()}
            if none is not None:
                criteria[NONE_OPTION] = none
            state = _with_context({"candidates_in_context": {k: _near(text, o, near) for k, o in ids.items()}}, context)
            requests.append((state, {"pick": Choice(question, criteria)}))
            plans.append(("ask", ids))
        # The window estimate is not a dispatch check: criteria repeat the candidate text,
        # JSON escapes add bytes, and even a singleton request must fit the configured budget.
        if any(len(json.dumps(state, ensure_ascii=False)) + len(json.dumps(
            {k: q.to_wire() for k, q in qs.items()}, ensure_ascii=False
        )) > budget for state, qs in requests):
            if near > 0:
                near = near // 2 if near > 20 else 0
                continue
            raise LimitError("candidates are too large to share the configured request; use smaller spans")
        decisions = iter(engine.ask_many(requests)) if requests else iter(())
        asked_once = asked_once or bool(requests)
        winners: list[tuple[Span, float, float]] = []
        for kind, ids in plans:
            if kind == "auto":
                winners.append((next(iter(ids.values())), 1.0, 1.0))
                continue
            a = _choice(next(decisions).answers["pick"])
            if a.choice in ids:
                winners.append((ids[a.choice], a.p(), a.confidence))
        if len(groups) == 1:
            return winners[0] if winners else None
        if not winners:
            return None
        pool = [w[0] for w in winners]
        if len(pool) == 1:
            return winners[0]


def extract(
    what: str,
    text: str,
    *,
    unit: UnitLike = ("money", "date", "email", "phone", "url", "percent", "number"),
    threshold: float = DEFAULT_THRESHOLD,
    context: Optional[str] = None,
    engine: Optional[Engine] = None,
) -> Optional[Match]:
    """The one span that is `what` ("the total amount due"), or None.

    A unit finds candidates; Jev picks one of them or "none of these" (TypeSafe's pre-parsed
    value extraction cookbook). The answer is always a verbatim span, so a digit cannot be
    transposed and a value cannot be invented. Name the unit when you know it: fewer candidates,
    better picks.

    When the text fits one request, the choice is among the distinct values with the whole text
    as the state. Otherwise every occurrence is judged in its own neighborhood, in a tournament
    that shrinks each round. When the winning value appears more than once, the same tournament
    picks the occurrence that was meant, so the offsets point at the right place.
    """
    engine = engine or get_engine()
    spans = find(text, unit)
    if not spans:
        return None
    occurrences: dict[str, list[Span]] = {}
    for s in spans:
        occurrences.setdefault(s.text, []).append(s)
    values = list(occurrences)
    budget = _budget(engine, context)
    option_cost = sum(len(v) + 8 for v in values) + len(what) + 120

    if len(text) + option_cost <= budget and len(values) <= 254:
        d = engine.decide(_with_context({"text": text}, context), {"pick": _extract_question(what, values)})
        winner, p, conf = _extract_pick(d.answers["pick"], values)
        if winner is None or p < threshold:
            return None
        occ = occurrences[winner]
        chosen = occ[0]
        if len(occ) > 1:
            q = f"The value {winner} appears more than once. Which occurrence is {what}?"
            picked = _tournament(engine, q, text, occ, none=None, context=context, budget=budget)
            if picked is not None:
                chosen = picked[0]
        return Match(chosen.text, chosen.start, chosen.end, p, chosen.unit, conf)

    q = (
        f"Which of these candidates is {what}? "
        "Each option is a value; its surrounding text is under the same id in `candidates_in_context`."
    )
    picked = _tournament(engine, q, text, spans, none=f"None of these values is {what}.", context=context, budget=budget)
    if picked is None or picked[1] < threshold:
        return None
    s, p, conf = picked
    return Match(s.text, s.start, s.end, p, s.unit, conf)


def extractall(
    what: str,
    text: str,
    *,
    unit: UnitLike,
    threshold: float = DEFAULT_THRESHOLD,
    context: Optional[str] = None,
    engine: Optional[Engine] = None,
) -> list[Match]:
    """Every candidate occurrence that is `what`, judged one by one where it sits."""
    return Pattern(what, unit=unit, threshold=threshold, context=context, engine=engine).findall(text)


def classify(
    text: Any,
    options: Union[Mapping[str, Optional[str]], Sequence[str]],
    *,
    question: str = "Which of these categories does `text` belong to?",
    context: Optional[str] = None,
    engine: Optional[Engine] = None,
) -> Pick:
    """Pick one label. `options` is a list of labels or a {label: description} mapping.

    Describe every option, including the boundary case; Jev reads them literally.
    """
    engine = engine or get_engine()
    if isinstance(options, str):
        raise TypeError("options must be a list of labels or a {label: description} mapping, not a string")
    criteria = dict(options) if isinstance(options, Mapping) else {str(o): None for o in options}
    a = _choice(engine.decide(_with_context({"text": text}, context), {"pick": Choice(question, criteria)}).answers["pick"])
    return Pick(a.choice, a.p(), a.confidence, dict(a.probabilities))


def rate(text: Any, question: str, levels: Sequence[str], *, context: Optional[str] = None, engine: Optional[Engine] = None) -> Rating:
    """Place the text on an ordered scale of 2-10 levels, lowest first. Refer to it as `text`."""
    engine = engine or get_engine()
    a = engine.decide(_with_context({"text": text}, context), {"rate": Score(question, list(levels))}).answers["rate"]
    if isinstance(a, Refusal):
        raise RefusedError(f"the backend refused a question: {a.reason or 'no reason given'}")
    if not isinstance(a, ScoreAnswer):
        raise BackendError(f"expected a score answer, got {a!r}")
    level = a.level
    label = a.legend.get(level) or (levels[level] if 0 <= level < len(levels) else str(level))
    return Rating(a.score, level, label, a.confidence, dict(a.probabilities))


# ------------------------------------------------------------------ module-level shortcuts


def compile(meaning: str, **kwargs: Any) -> Pattern:
    return Pattern(meaning, **kwargs)


def _pattern(meaning: Union[str, Pattern], kwargs: dict) -> Pattern:
    if isinstance(meaning, Pattern):
        if kwargs:
            raise TypeError("pass options to compile(), not alongside a compiled Pattern")
        return meaning
    return Pattern(meaning, **kwargs)


def test(meaning: Union[str, Pattern], text: str, **kwargs: Any) -> Verdict:
    return _pattern(meaning, kwargs).test(text)


fullmatch = test


def search(meaning: Union[str, Pattern], text: str, **kwargs: Any) -> Optional[Match]:
    return _pattern(meaning, kwargs).search(text)


def findall(meaning: Union[str, Pattern], text: str, **kwargs: Any) -> list[Match]:
    return _pattern(meaning, kwargs).findall(text)


def finditer(meaning: Union[str, Pattern], text: str, **kwargs: Any) -> Iterator[Match]:
    return _pattern(meaning, kwargs).finditer(text)


def scan(meaning: Union[str, Pattern], text: str, **kwargs: Any) -> list[Match]:
    return _pattern(meaning, kwargs).scan(text)


def diff(meaning: Union[str, Pattern], regex: Union[str, re.Pattern[str]], text: str, **kwargs: Any) -> Diff:
    """Where an existing regex and a meaning disagree on the same text (see Pattern.diff)."""
    return _pattern(meaning, kwargs).diff(regex, text)


def sub(meaning: Union[str, Pattern], repl: Union[str, Callable[[Match], str]], text: str, count: int = 0, **kwargs: Any) -> str:
    return _pattern(meaning, kwargs).sub(repl, text, count)


def subn(
    meaning: Union[str, Pattern], repl: Union[str, Callable[[Match], str]], text: str, count: int = 0, **kwargs: Any
) -> tuple[str, int]:
    return _pattern(meaning, kwargs).subn(repl, text, count)


def split(meaning: Union[str, Pattern], text: str, *, keep: bool = False, **kwargs: Any) -> list[str]:
    return _pattern(meaning, kwargs).split(text, keep=keep)


def filter(meaning: Union[str, Pattern], texts: Iterable[str], **kwargs: Any) -> list[str]:
    return _pattern(meaning, kwargs).filter(texts)


def rank(meaning: Union[str, Pattern], texts: Iterable[str], **kwargs: Any) -> list[tuple[str, float]]:
    return _pattern(meaning, kwargs).rank(texts)


def _async(fn: Callable) -> Callable:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    wrapper.__name__ = "a" + fn.__name__
    wrapper.__doc__ = f"Async {fn.__name__}: runs in a worker thread so the event loop stays free."
    return wrapper


atest = _async(test)
asearch = _async(search)
afindall = _async(findall)
asub = _async(sub)
asplit = _async(split)
afilter = _async(filter)
aextract = _async(extract)
aclassify = _async(classify)
arate = _async(rate)
