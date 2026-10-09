"""Bounded deterministic planning, with no backend or cache I/O."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass
from datetime import timezone
from types import MappingProxyType
from typing import Generic, TypeVar

from .._types import Noul
from ..errors import BudgetExceeded
from .binding import BoundQuery
from .definitions import Composite, Guard, Predicate, Relation, definition_digest, walk
from .errors import CandidateLimitExceeded, DefinitionError, PlanningLimitExceeded, RequestLimitExceeded, UnsupportedCapability
from .results import NO, YES, Band, Limits, NoBool, Outcome, RecordSnapshot, conjunction, disjunction, negate
from .sequence import SequencePattern
from .snapshot import aliases, canonical, digest, freeze, group_identity, read
from .transport import BackendCapabilities, NamedQuestion, PreparedRequest

QueryT = TypeVar("QueryT", bound=BoundQuery)
PlanQueryT = TypeVar("PlanQueryT", bound=BoundQuery, covariant=True)


@dataclass(frozen=True)
class Leaf:
    id: str
    definition: Predicate | Relation
    definition_digest: str
    input_digest: str
    state: Mapping
    records: tuple[RecordSnapshot, ...]


@dataclass(frozen=True)
class Request:
    id: str
    prepared: PreparedRequest
    leaf_ids: tuple[str, ...]
    estimated_tokens: int
    estimated_cost_usd: float

    @property
    def state(self):
        return self.prepared.state_utf8.decode("utf-8")

    @property
    def questions(self):
        return self.prepared.questions


@dataclass(frozen=True)
class Candidate:
    indices: tuple[int, ...]
    expressions: tuple[tuple, ...]
    relation_ids: tuple[str, ...]


@dataclass(frozen=True)
class Plan(NoBool, Generic[PlanQueryT]):
    id: str
    query: PlanQueryT
    band: Band
    limits: Limits
    engine_id: int
    fingerprint: str
    capabilities: BackendCapabilities
    leaves: Mapping[str, Leaf]
    requests: tuple[Request, ...]
    candidates: tuple[Candidate, ...]
    expression: tuple | None
    input_groups: int
    planning_steps: int
    request_upper_bound: int
    pruned_nodes: tuple[str, ...]
    cache_path: str | None

    def describe(self) -> dict:
        empty_fields = []
        projected: Counter[tuple[str, str]] = Counter()
        empty: Counter[tuple[str, str]] = Counter()
        for item in sorted(self.leaves.values(), key=lambda item: item.id):
            for scope, projection in item.state.items():
                for name, value in projection.items():
                    projected[scope, name] += 1
                    if (
                        value is None
                        or (isinstance(value, str) and not value.strip())
                        or (isinstance(value, (tuple, Mapping)) and len(value) == 0)
                    ):
                        empty[scope, name] += 1
                        empty_fields.append(
                            {
                                "code": "empty_projected_field",
                                "leaf_id": item.id,
                                "definition": {"name": item.definition.name, "version": item.definition.version},
                                "scope": scope,
                                "field": name,
                                "record_indices": sorted(r.index for r in item.records),
                            }
                        )
        return {
            "id": self.id,
            "experimental": True,
            "backend": self.capabilities.adapter,
            "model": self.capabilities.model,
            "input_records": len(self.query.records),
            "groups": self.input_groups,
            "candidates": len(self.candidates),
            "leaf_questions": len(self.leaves),
            "initial_requests": len(self.requests),
            "request_upper_bound": self.request_upper_bound,
            "planning_steps": self.planning_steps,
            "limits": asdict(self.limits),
            "band": asdict(self.band),
            "diagnostics": empty_fields,
            "diagnostic_summary": [
                {"scope": scope, "field": name, "empty_leaves": count, "projected_leaves": projected[scope, name]}
                for (scope, name), count in sorted(empty.items())
            ],
            "cache_path": self.cache_path,
            "token_estimator": "UTF-8 bytes plus per-question allowance, version 1",
            "requests": [
                {
                    "id": r.id,
                    "questions": len(r.leaf_ids),
                    "estimated_tokens": r.estimated_tokens,
                    "estimated_cost_usd": r.estimated_cost_usd,
                }
                for r in self.requests
            ],
        }


class Work:
    def __init__(self, limits: Limits):
        self.limits = limits
        self.used = 0

    def charge(self, stage: str, count: int = 1):
        if self.used + count > self.limits.max_planning_steps:
            raise PlanningLimitExceeded(f"planning limit at {stage}: {self.used} units used; {count} more required")
        self.used += count


def validate_prices(caps) -> None:
    prices = (caps.price_per_mtok, caps.output_price_per_mtok)
    if any(type(p) not in (int, float) or not math.isfinite(p) or p < 0 for p in prices):
        raise UnsupportedCapability("semantic budgets require an explicit backend price estimate")
    if caps.output_price_per_mtok != 0:
        raise UnsupportedCapability("semantic budgets currently require zero output-token pricing")


def engine_fingerprint(engine) -> str:
    if any(not callable(getattr(engine.backend, op, None)) for op in ("capabilities", "prepare", "attempt", "parse_strict")):
        raise UnsupportedCapability("backend requires the semantic single-attempt protocol")
    caps = engine.backend.capabilities()
    validate_prices(caps)
    return digest(
        {
            "capabilities": asdict(caps),
            "questions": engine.max_questions,
            "state_chars": engine.max_state_chars,
            "packing": "identical-state-v1",
        }
    )


def leaf(definition, records, context, work: Work, leaves: dict[str, Leaf], *, context_records=()) -> str:
    work.charge("leaf")
    if isinstance(definition, Predicate):
        state = {"item": aliases(records[0].data, definition.fields)}
    else:
        state = {"left": aliases(records[0].data, definition.left_fields), "right": aliases(records[1].data, definition.right_fields)}
    state["context"] = aliases(context, definition.context_fields)
    state = freeze(state)
    dd, sd = definition_digest(definition), digest(state)
    ident = digest((dd, sd))
    # Context capture identities are evidence, never additions to model state or
    # request identity. Keep all sources when projected inputs are deduplicated.
    records = tuple({(r.index, type(r.key), r.key, r.revision): r for r in (*records, *context_records)}.values())
    if ident in leaves:
        old = leaves[ident]
        identities = {(r.index, type(r.key), r.key, r.revision) for r in old.records}
        records = old.records + tuple(r for r in records if (r.index, type(r.key), r.key, r.revision) not in identities)
    leaves[ident] = Leaf(ident, definition, dd, sd, state, tuple(records))
    return ident


def expression(expr, record, context, work, leaves, pruned) -> tuple:
    stack = [(expr, False)]
    done: dict[int, tuple] = {}
    while stack:
        node, after = stack.pop()
        work.charge("expression")
        if isinstance(node, Composite):
            if not after:
                stack.append((node, True))
                stack.extend((child, False) for child in reversed(node.children))
                continue
            children = [done[id(c)] for c in node.children]
            constants = [c[0][1] if len(c) == 1 and c[0][0] == "const" else None for c in children]
            decisive = (node.op == "and" and NO in constants) or (node.op == "or" and YES in constants)
            if decisive:
                outcome = NO if node.op == "and" else YES
                done[id(node)] = (("const", outcome),)
                pruned.append(digest((record.digest, node.op, len(pruned))))
            elif all(c is not None for c in constants):
                a = constants[0]
                assert isinstance(a, Outcome)
                if node.op == "not":
                    outcome = negate(a)
                else:
                    b = constants[1]
                    assert isinstance(b, Outcome)
                    outcome = (conjunction if node.op == "and" else disjunction)(a, b)
                done[id(node)] = (("const", outcome),)
            else:
                done[id(node)] = (*tuple(token for child in children for token in child), (node.op, None))
        elif isinstance(node, Guard):
            work.charge("guard")
            done[id(node)] = (("const", node.evaluate(record.data)),)
        elif isinstance(node, Predicate):
            ident = leaf(node, (record,), context, work, leaves)
            done[id(node)] = (("leaf", ident),)
        else:
            raise DefinitionError("unsupported expression node")
    return done[id(expr)]


def evaluate_expression(tokens: tuple, outcomes: Mapping[str, Outcome]) -> Outcome:
    values = []
    for op, value in tokens:
        if op == "const":
            values.append(value)
        elif op == "leaf":
            values.append(outcomes[value])
        elif op == "not":
            values.append(negate(values.pop()))
        else:
            right, left = values.pop(), values.pop()
            values.append((conjunction if op == "and" else disjunction)(left, right))
    return values[0]


def leaf_ids(tokens) -> tuple[str, ...]:
    return tuple(v for op, v in tokens if op == "leaf")


def pack(engine, leaves: Mapping[str, Leaf]) -> tuple[Request, ...]:
    if not leaves:
        return ()
    caps = engine.backend.capabilities()
    if "noul" not in caps.answer_types:
        raise UnsupportedCapability("backend does not support required Noul answers")
    validate_prices(caps)
    assert caps.price_per_mtok is not None
    groups: dict[bytes, list[Leaf]] = {}
    for item in leaves.values():
        groups.setdefault(canonical(item.state), []).append(item)
    requests: list[Request] = []

    def make(state, batch):
        questions = tuple(NamedQuestion(item.id, Noul(item.definition.question, item.definition.criteria)) for item in batch)
        prepared = engine.backend.prepare(state, questions)
        tokens = len(prepared.body) + 32 * len(questions)
        largest = len(state) + max((len(canonical(q.question.to_wire())) for q in questions), default=0) + 128
        if (
            len(state.decode("utf-8")) > min(engine.max_state_chars, caps.max_state_chars)
            or (caps.max_input_tokens is not None and tokens > caps.max_input_tokens)
            or (caps.max_state_question_tokens is not None and largest > caps.max_state_question_tokens)
        ):
            raise RequestLimitExceeded("serialized state and questions exceed backend limits")
        # Full body plus caps pins ordered questions/options and nonsecret behavior settings.
        ident = digest(("semantic-observation-v1", "packing-v1", asdict(caps), prepared.body.hex()))
        return Request(ident, prepared, tuple(item.id for item in batch), tokens, tokens * caps.price_per_mtok / 1e6)

    for state, items in sorted(groups.items()):
        items.sort(key=lambda item: (item.definition_digest, item.id))
        batch: list[Leaf] = []
        for item in items:
            if len(batch) >= min(engine.max_questions, caps.max_questions):
                requests.append(make(state, batch))
                batch = []
            try:
                make(state, [*batch, item])
            except RequestLimitExceeded:
                if not batch:
                    raise
                requests.append(make(state, batch))
                batch = []
                make(state, [item])
            batch.append(item)
        if batch:
            requests.append(make(state, batch))
    return tuple(requests)


DEFAULT_LIMITS = Limits()


def plan(engine, query: QueryT, *, band: Band, limits: Limits = DEFAULT_LIMITS) -> Plan[QueryT]:
    if not isinstance(query, BoundQuery) or not isinstance(band, Band) or not isinstance(limits, Limits):
        raise DefinitionError("plan requires a bound query, explicit Band, and Limits")
    fingerprint = engine_fingerprint(engine)
    caps = engine.backend.capabilities()
    work = Work(limits)
    leaves: dict[str, Leaf] = {}
    pruned: list[str] = []
    definition = query.definition
    names: dict[tuple[str, int], str] = {}
    definitions: list[Predicate | Relation] = []
    expressions = (
        [s.expr for s in definition.steps]
        if isinstance(definition, SequencePattern)
        else []
        if isinstance(definition, Relation)
        else [definition]
    )
    for expr in expressions:
        for node in walk(expr):
            work.charge("definition")
            if isinstance(node, Predicate):
                definitions.append(node)
    if isinstance(definition, Relation):
        definitions.append(definition)
    elif isinstance(definition, SequencePattern):
        definitions.extend(r.relation for r in definition.relations)
    for obj in definitions:
        dd = definition_digest(obj)
        key = (obj.name, obj.version)
        if key in names and names[key] != dd:
            raise DefinitionError("a name/version pair has conflicting definitions")
        names[key] = dd
    candidates: list[Candidate] = []
    unary_ids: set[str] = set()
    relation_ids: set[str] = set()
    scalar_expr = None
    groups: dict[str, list[int]] = {}
    if isinstance(definition, SequencePattern):
        planned = [
            [expression(step.expr, record, query.context, work, leaves, pruned) for record in query.records] for step in definition.steps
        ]
        for record in query.records:
            group = None if definition.group_path is None else read(record.data, definition.group_path)
            groups.setdefault(group_identity(group), []).append(record.index)
        for indices in groups.values():
            # An iterator per prefix keeps memory proportional to pattern depth.
            stack: list[tuple[tuple[int, ...], Iterator[int]]] = [((), iter(range(len(indices))))]
            while stack:
                prefix, choices = stack[-1]
                try:
                    position = next(choices)
                except StopIteration:
                    stack.pop()
                    continue
                work.charge("prefix")
                index = indices[position]
                step_expr = planned[len(prefix)][index]
                if len(step_expr) == 1 and step_expr[0] == ("const", NO):
                    continue
                current = (*prefix, position)
                if definition.timestamp_path is not None:
                    first = read(query.records[indices[current[0]]].data, definition.timestamp_path)
                    last = read(query.records[index].data, definition.timestamp_path)
                    if last.astimezone(timezone.utc) - first.astimezone(timezone.utc) > definition.duration:
                        continue
                if len(current) < len(definition.steps):
                    stop = min(len(indices), position + definition.gaps[len(current) - 1] + 2)
                    stack.append((current, iter(range(position + 1, stop))))
                    continue
                if len(candidates) >= limits.max_candidates:
                    raise CandidateLimitExceeded("exact candidates exceed max_candidates")
                chosen = tuple(indices[i] for i in current)
                exprs = tuple(planned[s][i] for s, i in enumerate(chosen))
                unary_ids.update(ident for expr in exprs for ident in leaf_ids(expr))
                captured = {step.name: query.records[i] for step, i in zip(definition.steps, chosen, strict=True)}
                rels = []
                for cr in definition.relations:
                    context = {**query.context, **{root: captured[ref.name].data for root, ref in cr.context.items()}}
                    assert cr.relation.context_fields is not None
                    context_names = {cr.context[path[0]].name for path in cr.relation.context_fields.values() if path[0] in cr.context}
                    context_records = tuple(sorted((captured[name] for name in context_names), key=lambda r: r.index))
                    ident = leaf(
                        cr.relation, (captured[cr.left], captured[cr.right]), context, work, leaves, context_records=context_records
                    )
                    rels.append(ident)
                    relation_ids.add(ident)
                candidates.append(Candidate(chosen, exprs, tuple(rels)))
        candidates.sort(key=lambda c: c.indices)
    elif isinstance(definition, Relation):
        ident = leaf(definition, query.records, query.context, work, leaves)
        scalar_expr = (("leaf", ident),)
        unary_ids.add(ident)
    else:
        scalar_expr = expression(definition, query.records[0], query.context, work, leaves, pruned)
        unary_ids.update(leaf_ids(scalar_expr))
    leaves = {ident: leaves[ident] for ident in sorted(unary_ids | relation_ids)}
    if len(leaves) > limits.max_leaf_questions:
        raise PlanningLimitExceeded("logical questions exceed max_leaf_questions")
    requests = pack(engine, {ident: leaves[ident] for ident in unary_ids})
    # Any survivor subset can repack relations differently; singleton packing is the safe upper bound.
    future = [pack(engine, {ident: leaves[ident]})[0] for ident in sorted(relation_ids)]
    work.charge("relation-materialization-reservation", len(future))
    upper = len(requests) + len(future)
    if upper > limits.max_requests:
        raise RequestLimitExceeded("nominal request upper bound exceeds max_requests")
    if sum(r.estimated_cost_usd for r in (*requests, *future)) > limits.max_estimated_cost_usd:
        raise BudgetExceeded("nominal request cost estimate exceeds evaluation budget")

    def expr_identity(expr):
        return [(op, value.value if isinstance(value, Outcome) else value) for op, value in expr]

    identity = {
        "fingerprint": fingerprint,
        "band": asdict(band),
        "limits": asdict(limits),
        "records": [(r.key, r.revision, r.index, r.digest) for r in query.records],
        "context": query.context,
        "candidates": [(c.indices, [expr_identity(e) for e in c.expressions], c.relation_ids) for c in candidates],
        "expression": None if scalar_expr is None else expr_identity(scalar_expr),
        "leaves": sorted(leaves),
    }
    return Plan(
        digest(identity),
        query,
        band,
        limits,
        id(engine),
        fingerprint,
        caps,
        MappingProxyType(leaves),
        requests,
        tuple(candidates),
        scalar_expr,
        len(groups),
        work.used,
        upper,
        tuple(pruned),
        engine._semantic_store.path,
    )
