"""Snapshot all required fields at binding, before exact pruning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Generic

from .definitions import Expr, Guard, L, Predicate, R, Relation, T, walk
from .errors import DefinitionError, InputError
from .results import NoBool, RecordSnapshot
from .sequence import SequencePattern
from .snapshot import MAX_BYTES, Path, canonical, digest, group_identity, identity_key, path, project, read, tagged


@dataclass(frozen=True)
class BoundQuery(NoBool):
    definition: Expr | Relation | SequencePattern
    records: tuple[RecordSnapshot, ...]
    context: Mapping[str, Any]

    def __post_init__(self):
        size = len(canonical(tagged(self.context)))
        for record in self.records:
            size += len(canonical(tagged((record.key, record.revision, record.data))))
            if size > MAX_BYTES:
                raise InputError("bound query exceeds 4 MiB across records and context")


class BoundPredicate(BoundQuery, Generic[T]):
    definition: Expr[T]


class BoundRelation(BoundQuery, Generic[L, R]):
    definition: Relation[L, R]


class BoundSequence(BoundQuery, Generic[T]):
    definition: SequencePattern[T]


def _expr_paths(expr: Expr):
    required: set[Path] = set()
    optional: set[Path] = set()
    context: set[Path] = set()
    for node in walk(expr):
        if isinstance(node, Predicate):
            required.update(node.fields.values())
            assert node.context_fields is not None
            context.update(node.context_fields.values())
        elif isinstance(node, Guard):
            (optional if node.op == "exists" else required).add(node.path)
    return required, optional, context


def _record(item, required, optional, key, revision, index):
    data = project(item, required, optional)
    return RecordSnapshot(identity_key(key), identity_key(revision, revision=True), index, data, digest(data))


def bind_scalar(expr, item, *, key, revision, context):
    required, optional, ctx = _expr_paths(expr)
    record = _record(item, required, optional, key, revision, 0)
    # Validate comparisons even when a different branch would later prune them.
    for node in walk(expr):
        if isinstance(node, Guard):
            node.evaluate(record.data)
    return BoundPredicate(expr, (record,), project({} if context is None else context, ctx))


def bind_relation(rel, left, right, left_key, right_key, left_revision, right_revision, context):
    records = (
        _record(left, set(rel.left_fields.values()), set(), left_key, left_revision, 0),
        _record(right, set(rel.right_fields.values()), set(), right_key, right_revision, 0),
    )
    return BoundRelation(rel, records, project({} if context is None else context, set(rel.context_fields.values())))


def bind_sequence(pattern, items, key, revision, context, retain):
    key = path(key)
    revision = None if revision is None else path(revision)
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or len(items) > 1000:
        raise InputError("bind requires a materialized sequence of at most 1,000 records")
    if not isinstance(retain, tuple):
        raise DefinitionError("retain requires a tuple of paths")
    required = {key, *(path(p) for p in retain)}
    if revision is not None:
        required.add(revision)
    optional: set[Path] = set()
    ctx: set[Path] = set()
    for step in pattern.steps:
        r, o, c = _expr_paths(step.expr)
        required.update(r)
        optional.update(o)
        ctx.update(c)
    for p in (pattern.group_path, pattern.timestamp_path):
        if p is not None:
            required.add(p)
    for capture_rel in pattern.relations:
        rel = capture_rel.relation
        required.update(rel.left_fields.values())
        required.update(rel.right_fields.values())
        if context is not None and any(root in context for root in capture_rel.context):
            raise InputError("shared and capture context roots cannot overlap")
        for p in rel.context_fields.values():
            if p[0] in capture_rel.context:
                if len(p) == 1:
                    raise DefinitionError("capture context projections must select fields below the capture root")
                required.add(p[1:])
            else:
                ctx.add(p)
    shared = project({} if context is None else context, ctx)
    size = len(canonical(tagged(shared)))
    records = []
    keys = set()
    times: dict[str, datetime] = {}
    for index, item in enumerate(items):
        rec = _record(item, required, optional, read(item, key), None if revision is None else read(item, revision), index)
        ident = (type(rec.key), rec.key)
        if ident in keys:
            raise InputError("sequence record keys must be unique across groups")
        keys.add(ident)
        size += len(canonical(tagged(rec.data)))
        if size > MAX_BYTES:
            raise InputError("bound sequence exceeds 4 MiB")
        for step in pattern.steps:
            for node in walk(step.expr):
                if isinstance(node, Guard):
                    node.evaluate(rec.data)
        group = None if pattern.group_path is None else read(rec.data, pattern.group_path)
        if group is not None and not (type(group) in (str, bool, int, float) or isinstance(group, datetime)):
            raise InputError("group keys must be scalars")
        group_id = group_identity(group)
        if pattern.timestamp_path is not None:
            stamp = read(rec.data, pattern.timestamp_path)
            if not isinstance(stamp, datetime):
                raise InputError("within timestamp field must contain aware datetimes")
            stamp = stamp.astimezone(timezone.utc)
            if group_id in times and stamp < times[group_id]:
                raise InputError("timestamps must be nondecreasing within each group")
            times[group_id] = stamp
        records.append(rec)
    return BoundSequence(pattern, tuple(records), shared)
