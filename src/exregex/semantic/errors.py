"""Validation and execution failures for semantic programs."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from ..errors import ExRegexError, LimitError

if TYPE_CHECKING:
    from .results import Evaluation


class EvaluationFailed(ExRegexError):
    """An execution-incomplete evaluation, with the report and original causes."""

    def __init__(self, partial_result: Evaluation, causes: tuple[ExRegexError, ...]):
        self.partial_result = partial_result
        self.trace = partial_result.trace
        self.causes = tuple(causes)
        counts = ", ".join(f"{code}: {count}" for code, count in self.cause_counts.items())
        super().__init__(f"semantic evaluation failed ({counts}); inspect .partial_result and .causes")

    @property
    def cause_counts(self) -> dict[str, int]:
        """Detached counts by error type name; full per-failure evidence stays in .causes."""
        return dict(sorted(Counter(type(cause).__name__ for cause in self.causes).items()))


class DefinitionError(ExRegexError, ValueError):
    pass


class InputError(ExRegexError, ValueError):
    pass


class PlanMismatch(ExRegexError, ValueError):
    pass


class PlanningLimitExceeded(LimitError):
    pass


class CandidateLimitExceeded(LimitError):
    pass


class RequestLimitExceeded(LimitError):
    pass


class ResultLimitExceeded(LimitError):
    pass


class DeadlineExceeded(ExRegexError):
    pass


class ExecutionFailed(ExRegexError):
    pass


class UnsupportedCapability(ExRegexError):
    pass
