"""Experimental semantic expressions and bounded record matching.

Definitions, binding, and planning are local. Engine.evaluate is the I/O boundary.
"""

from .definitions import CaptureRef, CaptureRelation, Expr, Field, Predicate, Relation, Step, field, ref
from .errors import (
    CandidateLimitExceeded,
    DeadlineExceeded,
    DefinitionError,
    EvaluationFailed,
    ExecutionFailed,
    InputError,
    PlanMismatch,
    PlanningLimitExceeded,
    RequestLimitExceeded,
    ResultLimitExceeded,
    UnsupportedCapability,
)
from .planner import Plan
from .results import (
    NO,
    UNKNOWN,
    YES,
    Band,
    Coverage,
    DefinitionIdentity,
    Evaluation,
    LeafObservation,
    Limits,
    MatchReport,
    Outcome,
    RecordSnapshot,
    SemanticMatch,
    Trace,
)
from .sequence import Gap, SequencePattern, gap, sequence

__all__ = [
    "Predicate",
    "Relation",
    "Expr",
    "Field",
    "field",
    "Step",
    "CaptureRef",
    "CaptureRelation",
    "ref",
    "sequence",
    "gap",
    "SequencePattern",
    "Gap",
    "Band",
    "Limits",
    "Plan",
    "YES",
    "NO",
    "UNKNOWN",
    "Outcome",
    "Evaluation",
    "EvaluationFailed",
    "DefinitionIdentity",
    "LeafObservation",
    "MatchReport",
    "SemanticMatch",
    "RecordSnapshot",
    "Trace",
    "Coverage",
    "DefinitionError",
    "InputError",
    "PlanMismatch",
    "PlanningLimitExceeded",
    "CandidateLimitExceeded",
    "RequestLimitExceeded",
    "ResultLimitExceeded",
    "DeadlineExceeded",
    "ExecutionFailed",
    "UnsupportedCapability",
]
