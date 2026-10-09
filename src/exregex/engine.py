"""The engine: the part of ex-regex that talks to a backend.

It owns everything a decision call needs to be a dependable software component:

- limits checked before anything is sent (2-255 options, 2-10 levels, state size)
- many questions over one state split across requests and run concurrently (`ask`)
- many independent requests run concurrently, in order (`ask_many`)
- a cache, in memory by default or in SQLite, keyed by backend, model, state, and questions
- a spending cap (`max_cost_usd`) checked before each request
- a running tally of requests, retries, tokens, cost, and latency
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional, Union, overload

if TYPE_CHECKING:
    from .semantic.binding import BoundQuery, BoundSequence
    from .semantic.planner import Plan, QueryT
    from .semantic.results import Band, Evaluation, Limits, MatchReport

from ._types import (
    Answer,
    Choice,
    ChoiceAnswer,
    Decision,
    Noul,
    NoulAnswer,
    Question,
    Refusal,
    Score,
    ScoreAnswer,
    answer_to_wire,
    parse_answer,
)
from .backends import Backend, from_env, identity_from_env
from .errors import BackendError, BudgetExceeded, CacheMiss, LimitError

BYTES_PER_TOKEN = 3.0  # conservative: English runs about 4 bytes a token, CJK about 3
MISSING_RETRIES = 2  # re-ask questions a server skipped in a packed request


def estimate_tokens(state: Any, questions: Mapping[str, Question]) -> int:
    """A deliberately high estimate. UTF-8 bytes, not characters: a CJK character is about one token."""
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
    q = json.dumps({k: v.to_wire() for k, v in questions.items()}, ensure_ascii=False)
    return int((len(text.encode("utf-8")) + len(q.encode("utf-8"))) / BYTES_PER_TOKEN) + 8 * len(questions)


_EXPECTED = {Noul: NoulAnswer, Choice: ChoiceAnswer, Score: ScoreAnswer}


def check_answers(questions: Mapping[str, Question], answers: Mapping[str, Answer]) -> None:
    """Every answer must be the type its question asked for (or a refusal), and a pick must be an option."""
    for name, a in answers.items():
        q = questions.get(name)
        if q is None or isinstance(a, Refusal):
            continue
        want = _EXPECTED[type(q)]
        if not isinstance(a, want):
            raise BackendError(f"question {name!r} is a {type(q).__name__.lower()} but the answer was {a!r}")
        if isinstance(a, ChoiceAnswer) and isinstance(q, Choice) and a.choice not in q.criteria:
            raise BackendError(f"question {name!r}: the pick {a.choice!r} is not one of the options")


@dataclass
class Stats:
    requests: int = 0
    cached: int = 0
    retries: int = 0
    failures: int = 0
    input_tokens: int = 0
    cost_usd: float = 0.0
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=10_000))  # the most recent requests
    started: float = field(default_factory=time.time)

    def percentile(self, q: float) -> Optional[float]:
        if not self.latencies_ms:
            return None
        xs = sorted(self.latencies_ms)
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "cached": self.cached,
            "retries": self.retries,
            "failures": self.failures,
            "input_tokens": self.input_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "latency_ms": {"p50": self.percentile(0.5), "p95": self.percentile(0.95)},
        }

    def line(self) -> str:
        p50 = self.percentile(0.5)
        lat = f", p50 {p50:.0f} ms" if p50 is not None else ""
        return f"{self.requests} request(s), {self.cached} cached, {self.retries} retries, ${self.cost_usd:.6f}{lat}"


def _short(instructions: Any, n: int = 160) -> str:
    text = instructions if isinstance(instructions, str) else json.dumps(instructions, ensure_ascii=False)
    return text if len(text) <= n else text[: n - 1] + "…"


class Cache:
    """In-memory LRU, optionally backed by a file. Thread-safe.

    A path ending in .jsonl is a decision lockfile: one readable JSON line per decision, append
    only, sorted keys, every entry kept. Commit it next to your code and diff it in review; with
    Engine(replay=True) your tests run from it and never touch the network. Any other path is a
    SQLite file, better for large private caches.
    """

    def __init__(self, path: Optional[Union[str, Path]] = None, max_items: int = 20_000, *, read_only: bool = False):
        self._mem: OrderedDict[str, dict] = OrderedDict()
        self._max = max_items
        self._lock = threading.Lock()
        self._db: Optional[sqlite3.Connection] = None
        self._lockfile: Optional[Path] = None
        self._pinned: dict[str, dict] = {}
        self._path: Optional[Path] = None
        self._read_only = read_only
        if path is not None and str(path).endswith(".jsonl"):
            self._lockfile = Path(path).expanduser()
            if self._lockfile.exists():
                for n, line in enumerate(self._lockfile.read_text(encoding="utf-8").splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                        self._pinned[row["key"]] = {"model": row["model"], "answers": row["answers"]}
                    except (ValueError, KeyError, TypeError):
                        raise ValueError(f"{self._lockfile}:{n}: not a decision lockfile line") from None
        elif path is not None:
            self._path = Path(path).expanduser()

    def _open_db(self, *, write=False) -> None:
        if self._db is not None or self._path is None or (not write and not self._path.exists()):
            return
        if self._read_only:
            self._db = sqlite3.connect(self._path.absolute().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=30)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self._path), check_same_thread=False, timeout=30)
            self._db.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL)")
            self._db.commit()

    @staticmethod
    def key(backend: Backend, state: Any, questions: Mapping[str, Question]) -> str:
        blob = json.dumps(
            [
                type(backend).__name__,
                getattr(backend, "url", backend.name),
                backend.model,
                state,
                {k: v.to_wire() for k, v in questions.items()},
            ],
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            if key in self._pinned:
                return self._pinned[key]
            if key in self._mem:
                self._mem.move_to_end(key)
                return self._mem[key]
            self._open_db()
            if self._db is None:
                return None
            if not self._db.execute("SELECT name FROM sqlite_master WHERE name='answers'").fetchone():
                return None
            row = self._db.execute("SELECT value FROM answers WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row[0])
            except ValueError:
                return None
            self._remember(key, value)
            return value

    def put(self, key: str, value: dict, about: Optional[dict] = None) -> None:
        if self._read_only:
            return
        with self._lock:
            if self._lockfile is not None:
                if key not in self._pinned:
                    self._pinned[key] = value
                    row = {"key": key, **value, **(about or {})}
                    self._lockfile.parent.mkdir(parents=True, exist_ok=True)
                    with self._lockfile.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                return
            self._remember(key, value)
            self._open_db(write=True)
            if self._db is not None:
                self._db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?)", (key, json.dumps(value), time.time()))
                self._db.commit()

    def _remember(self, key: str, value: dict) -> None:
        self._mem[key] = value
        self._mem.move_to_end(key)
        while len(self._mem) > self._max:
            self._mem.popitem(last=False)

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None


class Engine:
    """Runs decisions against a backend.

    backend           a Backend; default: exregex.backends.from_env()
    cache             True (memory), False (none), or a path to a SQLite file
    concurrency       requests in flight at once
    max_questions     questions packed into one request (default: the backend's)
    max_cost_usd      refuse any request that would take spending past this
    """

    def __init__(
        self,
        backend: Optional[Backend] = None,
        *,
        cache: Union[bool, str, Path] = True,
        replay: bool = False,
        concurrency: int = 8,
        max_questions: Optional[int] = None,
        max_cost_usd: Optional[float] = None,
    ):
        if backend is None:
            backend = identity_from_env() if replay else from_env()
        self.backend = backend
        if cache is True:
            self.cache: Optional[Cache] = Cache()
        elif cache is False or cache is None:
            self.cache = None
        else:
            self.cache = Cache(cache, read_only=replay)
        if replay and self.cache is None:
            raise ValueError("replay=True needs a cache to replay from (a .jsonl lockfile or a SQLite path)")
        self.replay = replay
        self.concurrency = max(1, int(concurrency))
        # One limit per engine, enforced where requests leave: every caller, thread, and async
        # twin sharing this engine shares these slots.
        self._slots = threading.BoundedSemaphore(self.concurrency)
        per_request = max_questions if max_questions is not None else getattr(self.backend, "max_questions", None)
        per_request = per_request if isinstance(per_request, int) else 32
        self.max_questions = max(1, int(per_request))
        self.max_state_chars = int(getattr(self.backend, "max_state_chars", 60_000))
        self.max_cost_usd = max_cost_usd
        self.stats = Stats()
        self._lock = threading.Lock()
        self._reserved = 0.0
        self._semantic_liability = 0.0
        from .semantic.observations import ObservationStore

        self._semantic_store = ObservationStore(cache, replay=replay)

    def plan(self, query: QueryT, *, band: Band, limits: Optional[Limits] = None) -> Plan[QueryT]:
        """Freeze an inspectable semantic execution plan, without I/O."""
        from .semantic.planner import plan

        kwargs = {} if limits is None else {"limits": limits}
        return plan(self, query, band=band, **kwargs)

    @overload
    def evaluate(
        self,
        query_or_plan: Union[BoundSequence, Plan[BoundSequence]],
        *,
        band: Optional[Band] = None,
        limits: Optional[Limits] = None,
        errors: Literal["raise", "collect"] = "raise",
    ) -> MatchReport: ...

    @overload
    def evaluate(
        self,
        query_or_plan: Union[BoundQuery, Plan],
        *,
        band: Optional[Band] = None,
        limits: Optional[Limits] = None,
        errors: Literal["raise", "collect"] = "raise",
    ) -> Evaluation: ...

    def evaluate(self, query_or_plan, *, band=None, limits=None, errors="raise") -> Evaluation:
        """Explicitly evaluate a semantic query; uncertainty is retained in typed results."""
        from .semantic.runtime import evaluate

        return evaluate(self, query_or_plan, band=band, limits=limits, errors=errors)

    @overload
    async def aevaluate(
        self,
        query_or_plan: Union[BoundSequence, Plan[BoundSequence]],
        *,
        band: Optional[Band] = None,
        limits: Optional[Limits] = None,
        errors: Literal["raise", "collect"] = "raise",
    ) -> MatchReport: ...

    @overload
    async def aevaluate(
        self,
        query_or_plan: Union[BoundQuery, Plan],
        *,
        band: Optional[Band] = None,
        limits: Optional[Limits] = None,
        errors: Literal["raise", "collect"] = "raise",
    ) -> Evaluation: ...

    async def aevaluate(self, query_or_plan, *, band=None, limits=None, errors="raise") -> Evaluation:
        """Evaluate asynchronously; cancellation stops dispatch and drains in-flight attempts."""
        from .semantic.runtime import aevaluate

        return await aevaluate(self, query_or_plan, band=band, limits=limits, errors=errors)

    def __repr__(self) -> str:
        return f"Engine({self.backend.describe()}, {self.stats.line()})"

    # ------------------------------------------------------------ one request

    def decide(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        """One request: a state and up to max_questions questions."""
        if not questions:
            raise LimitError("decide() needs at least one question")
        if len(questions) > self.max_questions:
            raise LimitError(f"{len(questions)} questions in one request; the limit here is {self.max_questions} (use ask() to split)")
        key = Cache.key(self.backend, state, questions) if self.cache is not None else None
        if key is not None:
            hit = self.cache.get(key)  # type: ignore[union-attr]
            if hit is not None:
                with self._lock:
                    self.stats.cached += 1
                return Decision({k: parse_answer(v) for k, v in hit["answers"].items()}, hit["model"], cached=True)
        if self.replay:
            raise CacheMiss(
                "replay mode: this decision is not recorded. Re-run once without replay to record it "
                f"({len(questions)} question(s), model {self.backend.model})"
            )
        decision = self._send(state, questions)
        answers = dict(decision.answers)
        missing = [n for n in questions if n not in answers]
        for _ in range(MISSING_RETRIES):
            if not missing:
                break
            again = self._send(state, {n: questions[n] for n in missing})
            answers.update({n: a for n, a in again.answers.items() if n in questions})
            missing = [n for n in questions if n not in answers]
        if missing:
            with self._lock:
                self.stats.failures += 1
            raise BackendError(f"{self.backend.name}: no answer for {sorted(missing)} after {MISSING_RETRIES} re-asks")
        answers = {n: answers[n] for n in questions}
        check_answers(questions, answers)
        decision = Decision(
            answers,
            decision.model,
            decision.input_tokens,
            decision.cost_usd,
            decision.latency_ms,
            False,
            decision.retries,
            decision.request_id,
        )
        if key is not None:
            about = {"asked": {k: _short(q.instructions) for k, q in questions.items()}}
            self.cache.put(key, {"model": decision.model, "answers": {k: answer_to_wire(a) for k, a in decision.answers.items()}}, about)  # type: ignore[union-attr]
        return decision

    def _send(self, state: Any, questions: Mapping[str, Question]) -> Decision:
        estimate = estimate_tokens(state, questions) * getattr(self.backend, "price_per_mtok", 0.0) / 1e6
        self._reserve(estimate)
        try:
            with self._slots:
                decision = self.backend.decide(state, questions)
        except Exception:
            with self._lock:
                self._reserved -= estimate
                self.stats.failures += 1
            raise
        with self._lock:
            self._reserved -= estimate
            self.stats.requests += 1
            self.stats.retries += decision.retries
            self.stats.input_tokens += decision.input_tokens
            self.stats.cost_usd += decision.cost_usd
            if decision.latency_ms:
                self.stats.latencies_ms.append(decision.latency_ms)
        return decision

    def _reserve(self, estimate: float) -> None:
        with self._lock:
            if (
                self.max_cost_usd is not None
                and self.stats.cost_usd + self._reserved + self._semantic_liability + estimate > self.max_cost_usd
            ):
                raise BudgetExceeded(
                    f"the next request (about ${estimate:.6f}) would pass max_cost_usd=${self.max_cost_usd} "
                    f"(spent ${self.stats.cost_usd:.6f})"
                )
            self._reserved += estimate

    # ------------------------------------------------------------ many questions, one state

    def ask(self, state: Any, questions: Mapping[str, Question]) -> dict[str, Answer]:
        """Any number of questions about one state. Split into requests and run concurrently."""
        names = list(questions)
        if not names:
            return {}
        groups = [names[i : i + self.max_questions] for i in range(0, len(names), self.max_questions)]
        decisions = self.ask_many([(state, {n: questions[n] for n in g}) for g in groups], raise_errors=True)
        out: dict[str, Answer] = {}
        for d in decisions:
            out.update(d.answers)
        return out

    # ------------------------------------------------------------ many requests

    def ask_many(self, requests: Sequence[tuple[Any, Mapping[str, Question]]], *, raise_errors: bool = True) -> list:
        """Run requests concurrently. Results come back in order.

        With raise_errors=False a failed request yields its exception in place of a Decision.
        """
        if not requests:
            return []
        if len(requests) == 1 or self.concurrency == 1:
            results: list[Any] = []
            for state, qs in requests:
                try:
                    results.append(self.decide(state, qs))
                except Exception as e:
                    if raise_errors:
                        raise
                    results.append(e)
            return results

        def run(req: tuple[Any, Mapping[str, Question]]) -> Any:
            try:
                return self.decide(*req)
            except Exception as e:  # collected, re-raised below if asked
                return e

        with ThreadPoolExecutor(max_workers=min(self.concurrency, len(requests))) as pool:
            results = list(pool.map(run, requests))
        if raise_errors:
            for r in results:
                if isinstance(r, Exception):
                    raise r
        return results

    def close(self) -> None:
        if self.cache is not None:
            self.cache.close()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# ------------------------------------------------------------------ the default engine

_default: Optional[Engine] = None
_default_lock = threading.Lock()


def get_engine() -> Engine:
    """The engine module-level functions use when none is passed. Created on first use."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Engine()
        return _default


def set_engine(engine: Optional[Engine]) -> None:
    """Replace the default engine (None resets it, so the next call reads the environment again)."""
    global _default
    with _default_lock:
        _default = engine
