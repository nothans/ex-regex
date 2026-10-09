"""Reusable immutable definitions. Constructing expressions never evaluates them."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Generic, TypeVar

if TYPE_CHECKING:
    from .binding import BoundPredicate, BoundRelation

from .errors import DefinitionError, InputError
from .results import NO, YES, NoBool, Outcome
from .snapshot import MISSING, Path, digest, freeze, path, read, tagged

T = TypeVar("T")
L = TypeVar("L")
R = TypeVar("R")


def _name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DefinitionError("names and questions must be nonempty strings")
    return value


def _projection(value: Any, *, empty: bool = False) -> Mapping[str, Path]:
    if not isinstance(value, Mapping) or (not value and not empty):
        raise DefinitionError("projections must be nonempty mappings")
    if any(not isinstance(k, str) or not k.isidentifier() for k in value):
        raise DefinitionError("projection aliases must be identifiers")
    return MappingProxyType({k: path(v) for k, v in value.items()})


class Expr(NoBool, Generic[T]):
    def __and__(self, other: Expr[T]) -> Expr[T]:
        return Composite("and", (self, other))

    def __or__(self, other: Expr[T]) -> Expr[T]:
        return Composite("or", (self, other))

    def __invert__(self) -> Expr[T]:
        return Composite("not", (self,))

    def capture(self, name: str) -> Step[T]:
        return Step(self, _name(name))

    def bind(self, item: T, *, key: str | int, revision: str | int | None = None, context: Mapping | None = None) -> BoundPredicate[T]:
        from .binding import bind_scalar

        return bind_scalar(self, item, key=key, revision=revision, context=context)


@dataclass(frozen=True)
class Composite(Expr[T]):
    op: str
    children: tuple[Expr[T], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "children", tuple(self.children))
        if self.op not in ("and", "or", "not") or len(self.children) != (1 if self.op == "not" else 2):
            raise DefinitionError("invalid expression operator")
        if any(not isinstance(c, Expr) for c in self.children):
            raise DefinitionError("compose only semantic expressions with &, |, and ~")


@dataclass(frozen=True)
class Predicate(Expr[T]):
    name: str
    question: str
    fields: Mapping[str, Path]
    version: int = 1
    criteria: Mapping[str, str] | None = None
    context_fields: Mapping[str, Path] | None = None

    def __post_init__(self) -> None:
        _definition(self)
        object.__setattr__(self, "fields", _projection(self.fields))


def _definition(obj: Any) -> None:
    _name(obj.name)
    _name(obj.question)
    if type(obj.version) is not int or obj.version < 1:
        raise DefinitionError("definition version must be a positive integer")
    if obj.criteria is not None:
        if not isinstance(obj.criteria, Mapping) or set(obj.criteria) - {"true", "false"}:
            raise DefinitionError("criteria may describe only true and false")
        if any(not isinstance(v, str) for v in obj.criteria.values()):
            raise DefinitionError("criteria descriptions must be strings")
        object.__setattr__(obj, "criteria", MappingProxyType(dict(obj.criteria)))
    object.__setattr__(obj, "context_fields", _projection({} if obj.context_fields is None else obj.context_fields, empty=True))


@dataclass(frozen=True)
class Relation(NoBool, Generic[L, R]):
    name: str
    question: str
    left_fields: Mapping[str, Path]
    right_fields: Mapping[str, Path]
    version: int = 1
    criteria: Mapping[str, str] | None = None
    context_fields: Mapping[str, Path] | None = None

    def __post_init__(self) -> None:
        _definition(self)
        object.__setattr__(self, "left_fields", _projection(self.left_fields))
        object.__setattr__(self, "right_fields", _projection(self.right_fields))

    def bind(
        self,
        left: L,
        right: R,
        *,
        left_key: str | int,
        right_key: str | int,
        left_revision: str | int | None = None,
        right_revision: str | int | None = None,
        context: Mapping | None = None,
    ) -> BoundRelation[L, R]:
        from .binding import bind_relation

        return bind_relation(self, left, right, left_key, right_key, left_revision, right_revision, context)

    def between(self, left_capture: str, right_capture: str, *, context: Mapping[str, CaptureRef] | None = None) -> CaptureRelation:
        return CaptureRelation(self, _name(left_capture), _name(right_capture), MappingProxyType(dict(context or {})))


@dataclass(frozen=True)
class CaptureRef(NoBool):
    name: str

    def __post_init__(self) -> None:
        _name(self.name)


def ref(name: str) -> CaptureRef:
    return CaptureRef(_name(name))


@dataclass(frozen=True)
class CaptureRelation(NoBool):
    relation: Relation
    left: str
    right: str
    context: Mapping[str, CaptureRef]

    def __post_init__(self) -> None:
        if any(not isinstance(k, str) or not k.isidentifier() or not isinstance(v, CaptureRef) for k, v in self.context.items()):
            raise DefinitionError("capture context maps identifiers to sx.ref values")
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))


@dataclass(frozen=True)
class Step(NoBool, Generic[T]):
    expr: Expr[T]
    name: str

    def __post_init__(self) -> None:
        _name(self.name)
        if not isinstance(self.expr, Expr):
            raise DefinitionError("steps capture semantic expressions")


def scalar(value: Any) -> Any:
    if not (value is None or type(value) in (str, bool, int, float) or isinstance(value, datetime)):
        raise DefinitionError("exact guard values must be scalars")
    try:
        return freeze(value)
    except InputError as error:
        raise DefinitionError(str(error)) from error


@dataclass(frozen=True)
class Guard(Expr[T]):
    path: Path
    op: str
    value: Any = None

    def evaluate(self, data: Mapping) -> Outcome:
        actual = read(data, self.path, optional=self.op == "exists")
        if self.op == "exists":
            return NO if actual is MISSING else YES
        try:
            scalar(actual)
        except DefinitionError as error:
            raise InputError("exact guard field must contain a supported scalar") from error
        if self.op in ("eq", "one_of"):
            values = (self.value,) if self.op == "eq" else self.value
            matched = any(tagged(actual) == tagged(v) for v in values)
        else:
            numeric = type(actual) in (int, float) and type(self.value) in (int, float)
            temporal = isinstance(actual, datetime) and isinstance(self.value, datetime)
            if not numeric and not temporal:
                raise InputError("ordered guard requires compatible numeric or datetime operands")
            expected = self.value
            if temporal:
                actual = actual.astimezone(timezone.utc)
                expected = expected.astimezone(timezone.utc)
            matched = {
                "lt": lambda: actual < expected,
                "le": lambda: actual <= expected,
                "gt": lambda: actual > expected,
                "ge": lambda: actual >= expected,
            }[self.op]()
        return YES if matched else NO


@dataclass(frozen=True)
class Field(NoBool):
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", path(self.path))

    def eq(self, value: Any) -> Guard:
        return Guard(self.path, "eq", scalar(value))

    def one_of(self, values: Sequence) -> Guard:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise DefinitionError("one_of requires a sequence of scalars")
        return Guard(self.path, "one_of", tuple(scalar(v) for v in values))

    def exists(self) -> Guard:
        return Guard(self.path, "exists")

    def _order(self, op: str, value: Any) -> Guard:
        value = scalar(value)
        if not (type(value) in (int, float) or isinstance(value, datetime)):
            raise DefinitionError("ordered guards require a number or aware datetime")
        return Guard(self.path, op, value)

    def lt(self, value: Any) -> Guard:
        return self._order("lt", value)

    def le(self, value: Any) -> Guard:
        return self._order("le", value)

    def gt(self, value: Any) -> Guard:
        return self._order("gt", value)

    def ge(self, value: Any) -> Guard:
        return self._order("ge", value)


def field(*parts: str) -> Field:
    return Field(path(parts))


def walk(expr: Expr):
    stack = [expr]
    visited = 0
    while stack:
        node = stack.pop()
        visited += 1
        if visited > 10_000:
            raise DefinitionError("expression exceeds 10,000 nodes")
        yield node
        if isinstance(node, Composite):
            stack.extend(reversed(node.children))


def definition_data(obj: Predicate | Relation) -> Mapping:
    names: tuple[str, ...] = ("name", "question", "version", "criteria", "context_fields")
    names += ("fields",) if isinstance(obj, Predicate) else ("left_fields", "right_fields")
    return {"type": type(obj).__name__, **{n: getattr(obj, n) for n in names}}


def definition_digest(obj: Predicate | Relation) -> str:
    return digest(definition_data(obj))
