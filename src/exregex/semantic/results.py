"""Explicit three-valued results and immutable execution evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .errors import DefinitionError


class NoBool:
    def __bool__(self) -> bool:
        raise TypeError("semantic values have no truthiness; evaluate expressions, inspect .outcome, and compare with is YES/NO/UNKNOWN")


class Outcome(NoBool, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


YES, NO, UNKNOWN = Outcome.YES, Outcome.NO, Outcome.UNKNOWN


def conjunction(a: Outcome, b: Outcome) -> Outcome:
    return NO if a is NO or b is NO else YES if a is YES and b is YES else UNKNOWN


def disjunction(a: Outcome, b: Outcome) -> Outcome:
    return YES if a is YES or b is YES else NO if a is NO and b is NO else UNKNOWN


def negate(a: Outcome) -> Outcome:
    return NO if a is YES else YES if a is NO else UNKNOWN


@dataclass(frozen=True)
class Band:
    no: float
    yes: float

    def __post_init__(self) -> None:
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (self.no, self.yes)) or not 0 <= self.no < self.yes <= 1:
            raise DefinitionError("Band requires finite 0 <= no < yes <= 1")

    def apply(self, p: float) -> Outcome:
        return NO if p <= self.no else YES if p >= self.yes else UNKNOWN


@dataclass(frozen=True)
class RecordSnapshot:
    key: str | int
    revision: str | int | None
    index: int
    data: Mapping[str, Any]
    digest: str


@dataclass(frozen=True)
class ExecutionIssue:
    code: str
    stage: str
    message: str
    request_id: str | None = None
    leaf_ids: tuple[str, ...] = ()
    candidate_ids: tuple[int, ...] = ()
    retryable: bool = False


@dataclass(frozen=True)
class Attempt:
    request_id: str
    number: int
    dispatched: bool
    elapsed_ms: float
    status: int | None
    estimated_cost_usd: float
    reported_cost_usd: float | None
    code: str


@dataclass(frozen=True)
class DefinitionIdentity:
    name: str
    version: int


@dataclass(frozen=True)
class LeafObservation:
    id: str
    definition_digest: str
    input_digest: str
    answer: Any
    outcome: Outcome
    requested_model: str
    returned_model: str
    request_id: str
    cached: bool
    records: tuple[RecordSnapshot, ...]
    definition: DefinitionIdentity
    band: Band

    @property
    def edge_distance(self) -> float:
        """Distance to the nearest threshold, not a confidence interval."""
        return min(abs(self.answer.p - self.band.no), abs(self.answer.p - self.band.yes))

    @property
    def near_edge(self) -> bool:
        return self.edge_distance <= 0.05 or math.isclose(self.edge_distance, 0.05, rel_tol=0, abs_tol=1e-12)


@dataclass(frozen=True)
class Trace(NoBool):
    plan_id: str
    attempts: tuple[Attempt, ...] = ()
    cached_requests: tuple[str, ...] = ()
    pruned_nodes: tuple[str, ...] = ()
    estimated_cost_usd: float = 0.0
    reported_cost_usd: float = 0.0
    unknown_charge_attempts: int = 0
    cache_path: str | None = None


@dataclass(frozen=True)
class Coverage:
    input_records: int
    input_groups: int
    structural_candidates: int
    evaluated_candidates: int
    accepted_candidates: int
    rejected_candidates: int
    unresolved_candidates: int
    unevaluated_candidates: int
    stage_complete: Mapping[str, bool]
    near_edge: int = 0


@dataclass(frozen=True)
class SemanticMatch(NoBool):
    outcome: Outcome
    captures: Mapping[str, RecordSnapshot]
    leaf_ids: tuple[str, ...]

    def __getitem__(self, name: str) -> RecordSnapshot:
        return self.captures[name]


@dataclass(frozen=True)
class Evaluation(NoBool):
    outcome: Outcome | None
    complete: bool
    leaves: tuple[LeafObservation, ...]
    errors: tuple[ExecutionIssue, ...]
    trace: Trace

    @property
    def near_edge(self) -> int:
        """Observed logical leaves within 0.05 of either band edge, counted once."""
        return sum(leaf.near_edge for leaf in self.leaves)

    def explain(self) -> list[dict[str, Any]]:
        """Return source-free rows for observed leaves, including rejection evidence.

        Records are the associated source identities, not directional relation pairs:
        one deduplicated observation may serve several records and candidates.
        Unexecuted/pruned questions are absent; inspect .errors and .trace too.
        """
        return [
            {
                "leaf_id": leaf.id,
                "definition": {"name": leaf.definition.name, "version": leaf.definition.version},
                "associated_records": [{"key": r.key, "revision": r.revision, "index": r.index} for r in leaf.records],
                "p": leaf.answer.p,
                "outcome": leaf.outcome.value,
                "band": {"no": leaf.band.no, "yes": leaf.band.yes},
                "edge_distance": leaf.edge_distance,
                "near_edge": leaf.near_edge,
                "requested_model": leaf.requested_model,
                "returned_model": leaf.returned_model,
                "request_id": leaf.request_id,
                "cached": leaf.cached,
            }
            for leaf in self.leaves
        ]


@dataclass(frozen=True)
class MatchReport(Evaluation):
    matches: tuple[SemanticMatch, ...]
    uncertain_matches: tuple[SemanticMatch, ...]
    truncated: bool
    coverage: Coverage


@dataclass(frozen=True)
class Limits:
    max_planning_steps: int = 1_000_000
    max_candidates: int = 10_000
    max_leaf_questions: int = 10_000
    max_requests: int = 256
    max_results: int = 1000
    max_attempts: int = 3
    request_timeout_s: float = 30
    deadline_s: float = 60
    max_estimated_cost_usd: float = 0.10

    def __post_init__(self) -> None:
        for name in ("max_planning_steps", "max_candidates", "max_leaf_questions", "max_requests", "max_results", "max_attempts"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise DefinitionError(f"{name} must be a positive integer")
        for name in ("request_timeout_s", "deadline_s", "max_estimated_cost_usd"):
            value = getattr(self, name)
            valid_number = type(value) in (int, float) and math.isfinite(value)
            minimum = valid_number and (value >= 0 if name == "max_estimated_cost_usd" else value > 0)
            if not minimum:
                raise DefinitionError(f"{name} must be finite and {'nonnegative' if name == 'max_estimated_cost_usd' else 'positive'}")
