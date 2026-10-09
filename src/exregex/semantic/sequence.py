"""Finite sequence grammar. Enumeration lives in the bounded planner."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Generic

if TYPE_CHECKING:
    from .binding import BoundSequence

from .definitions import CaptureRelation, Step, T
from .errors import DefinitionError
from .results import NoBool
from .snapshot import Path, path


@dataclass(frozen=True)
class Gap(NoBool):
    max_items: int

    def __post_init__(self) -> None:
        if type(self.max_items) is not int or not 0 <= self.max_items <= 100:
            raise DefinitionError("gap max_items must be an integer from 0 through 100")


def gap(*, max_items: int) -> Gap:
    return Gap(max_items)


@dataclass(frozen=True)
class SequencePattern(NoBool, Generic[T]):
    parts: tuple[Step[T] | Gap, ...]
    group_path: Path | None = None
    timestamp_path: Path | None = None
    duration: timedelta | None = None
    relations: tuple[CaptureRelation, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        object.__setattr__(self, "relations", tuple(self.relations))
        for name in ("group_path", "timestamp_path"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, path(value))
        if (self.timestamp_path is None) != (self.duration is None):
            raise DefinitionError("timestamp and duration must be configured together")
        if self.duration is not None and (not isinstance(self.duration, timedelta) or self.duration < timedelta(0)):
            raise DefinitionError("duration must be nonnegative")
        if not self.parts or not isinstance(self.parts[0], Step) or not isinstance(self.parts[-1], Step):
            raise DefinitionError("sequence must start and end with a captured step")
        names: set[str] = set()
        prev_gap = False
        for part in self.parts:
            if isinstance(part, Step):
                if part.name in names:
                    raise DefinitionError("capture names must be unique")
                names.add(part.name)
            elif not isinstance(part, Gap) or prev_gap:
                raise DefinitionError("sequence accepts steps separated by at most one gap")
            prev_gap = isinstance(part, Gap)
        if not 1 <= len(names) <= 16:
            raise DefinitionError("sequence requires 1 through 16 steps")
        for rel in self.relations:
            if not isinstance(rel, CaptureRelation) or not {rel.left, rel.right, *(v.name for v in rel.context.values())} <= names:
                raise DefinitionError("relations must reference existing captures")

    @property
    def steps(self) -> tuple[Step[T], ...]:
        return tuple(p for p in self.parts if isinstance(p, Step))

    @property
    def gaps(self) -> tuple[int, ...]:
        result: list[int] = []
        previous = 0
        for part in self.parts[1:]:
            if isinstance(part, Gap):
                previous = part.max_items
            else:
                result.append(previous)
                previous = 0
        return tuple(result)

    def group_by(self, value: str | Path) -> SequencePattern[T]:
        return replace(self, group_path=path(value, shorthand=True))

    def within(self, duration: timedelta, *, timestamp: str | Path) -> SequencePattern[T]:
        if not isinstance(duration, timedelta) or duration < timedelta(0):
            raise DefinitionError("within requires a nonnegative timedelta")
        return replace(self, duration=duration, timestamp_path=path(timestamp, shorthand=True))

    def require(self, *relations: CaptureRelation) -> SequencePattern[T]:
        return replace(self, relations=self.relations + tuple(relations))

    def bind(
        self, items: Sequence[T], *, key: Path, revision: Path | None = None, context: Mapping | None = None, retain: tuple[Path, ...] = ()
    ) -> BoundSequence[T]:
        from .binding import bind_sequence

        return bind_sequence(self, items, key, revision, context, retain)


def sequence(*parts: Step[T] | Gap) -> SequencePattern[T]:
    return SequencePattern(tuple(parts))
