"""Semantic contracts: snapshots, truth tables, bounded matching, replay and failures."""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from exregex import Engine, Scripted
from exregex import semantic as sx
from exregex.errors import BackendError, CacheMiss, RefusedError
from exregex.semantic.results import conjunction, disjunction, negate

BAND = sx.Band(0.2, 0.8)


def predicate(name="meaning", *, fields=None, **kwargs):
    return sx.Predicate(name, question=f"Does item.text satisfy {name}?", fields=fields or {"text": ("text",)}, **kwargs)


def engine(value=0.95, **kwargs):
    return Engine(Scripted(lambda state, qs: dict.fromkeys(qs, value)), **kwargs)


@pytest.mark.parametrize("a,b", list(itertools.product((sx.YES, sx.NO, sx.UNKNOWN), repeat=2)))
def test_truth_tables(a, b):
    e = Engine(
        Scripted(
            lambda state, qs: {
                k: {"a": {sx.YES: 1, sx.NO: 0, sx.UNKNOWN: 0.5}[a], "b": {sx.YES: 1, sx.NO: 0, sx.UNKNOWN: 0.5}[b]}[
                    q.instructions.split()[-1][:-1]
                ]
                for k, q in qs.items()
            }
        )
    )
    p, q = predicate("a"), predicate("b")
    for expr, expected in ((p & q, conjunction(a, b)), (p | q, disjunction(a, b)), (~p, negate(a))):
        assert e.evaluate(expr.bind({"text": "x"}, key="x"), band=BAND).outcome is expected


def test_unknown_is_not_excluded_middle_or_failed_execution():
    p, e = predicate(), engine(0.5)
    report = e.evaluate((p | ~p).bind({"text": "x"}, key=1), band=BAND)
    assert report.outcome is sx.UNKNOWN and report.complete
    assert len(report.leaves) == 1
    for value in (p, report, report.outcome, p.bind({"text": "x"}, key=1), e.plan(p.bind({"text": "x"}, key=1), band=BAND)):
        with pytest.raises(TypeError, match="truthiness"):
            bool(value)


def test_snapshot_nested_mutation_and_projection():
    p = predicate(fields={"text": ("nested", "body")})
    original = {"nested": {"body": ["old", {"value": 2}]}, "secret": object()}
    query = p.bind(original, key="a", revision=3)
    original["nested"]["body"][1]["value"] = 9
    e = engine()
    plan = e.plan(query, band=BAND)
    assert e.stats.requests == 0
    assert json.loads(plan.requests[0].state) == {"item": {"text": ["old", {"value": 2}]}, "context": {}}
    result = e.evaluate(plan)
    assert result.leaves[0].records[0].data["nested"]["body"][1]["value"] == 2
    with pytest.raises(TypeError):
        result.leaves[0].records[0].data["nested"]["body"][1]["value"] = 3
    assert "old" not in json.dumps(plan.describe())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), b"binary", object(), datetime(2026, 1, 1)])
def test_unsupported_values(value):
    with pytest.raises(sx.InputError):
        predicate().bind({"text": value}, key=1)


def test_cycle_rejected_unselected_cycle_ignored():
    cycle = []
    cycle.append(cycle)
    with pytest.raises(sx.InputError):
        predicate().bind({"text": cycle}, key=1)
    predicate().bind({"text": "x", "ignored": cycle}, key=1)


def test_dataclass_slots_and_descriptors():
    @dataclass(slots=True)
    class Record:
        text: str

    assert engine().evaluate(predicate().bind(Record("x"), key=1), band=BAND).outcome is sx.YES

    @dataclass
    class Trap:
        text: str

    Trap.text = property(lambda self: pytest.fail("property accessed"))
    value = object.__new__(Trap)
    value.__dict__["text"] = "x"
    with pytest.raises(sx.InputError, match="descriptor"):
        predicate().bind(value, key=1)


def test_missing_null_guards_and_or_pruning():
    e = engine()
    with pytest.raises(sx.InputError):
        (sx.field("role").eq("a") & predicate()).bind({"role": "b"}, key=1)
    assert e.evaluate(sx.field("missing").exists().bind({}, key=1), band=BAND).outcome is sx.NO
    assert e.evaluate(sx.field("x").eq(None).bind({"x": None}, key=1), band=BAND).outcome is sx.YES
    expr = sx.field("role").eq("a") & predicate()
    assert e.evaluate(expr.bind({"role": "b", "text": "x"}, key=1), band=BAND).outcome is sx.NO
    assert not e.backend.calls
    expr = sx.field("role").eq("a") | predicate()
    assert e.evaluate(expr.bind({"role": "b", "text": "x"}, key=1), band=BAND).outcome is sx.YES
    assert len(e.backend.calls) == 1


@pytest.mark.parametrize("key", [True, None, 1.0, (), []])
def test_keys_are_explicit_and_typed(key):
    with pytest.raises(sx.InputError):
        predicate().bind({"text": "x"}, key=key)


def test_definition_conflict_and_copies():
    fields, criteria = {"text": ("text",)}, {"true": "good", "false": "bad"}
    p = predicate(fields=fields, criteria=criteria)
    fields["text"] = ("secret",)
    criteria["true"] = "changed"
    e = engine()
    assert json.loads(e.plan(p.bind({"text": "x"}, key=1), band=BAND).requests[0].state)["item"] == {"text": "x"}
    other = sx.Predicate("meaning", question="Different?", fields={"text": ("text",)})
    with pytest.raises(sx.DefinitionError, match="conflicting"):
        e.plan((p | other).bind({"text": "x"}, key=1), band=BAND)


def test_type_strict_equality_and_aware_comparison():
    e = engine()
    for a, b in ((1, True), (1, 1.0), ("1", 1)):
        assert e.evaluate(sx.field("x").eq(a).bind({"x": b}, key=1), band=BAND).outcome is sx.NO
    assert e.evaluate(sx.field("x").lt(2.0).bind({"x": 1}, key=1), band=BAND).outcome is sx.YES
    with pytest.raises(sx.InputError):
        sx.field("x").lt(2).bind({"x": True}, key=1)
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert (
        e.evaluate(sx.field("x").eq(stamp).bind({"x": stamp.astimezone(timezone(timedelta(hours=5)))}, key=1), band=BAND).outcome is sx.YES
    )


def test_sequence_group_gaps_original_indices_and_time():
    expr = sx.sequence(sx.field("role").eq("a").capture("a"), sx.field("role").eq("b").capture("b"))
    expr = expr.group_by("group").within(timedelta(0), timestamp="time")
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        {"id": 1, "group": "x", "role": "a", "time": stamp},
        {"id": 2, "group": "y", "role": "a", "time": stamp},
        {"id": 3, "group": "x", "role": "b", "time": stamp},
    ]
    result = engine().evaluate(expr.bind(rows, key=("id",)), band=BAND)
    assert result.outcome is sx.YES and result.complete
    assert [(m["a"].index, m["b"].index) for m in result.matches] == [(0, 2)]
    assert result.coverage.input_groups == 2
    rows[2]["time"] -= timedelta(seconds=1)
    with pytest.raises(sx.InputError, match="nondecreasing"):
        expr.bind(rows, key=("id",))


@pytest.mark.parametrize("roles", list(itertools.product("ab", repeat=5)))
@pytest.mark.parametrize("gap", [0, 1, 3])
def test_enumeration_matches_independent_brute_force(roles, gap):
    rows = [{"id": i, "role": role} for i, role in enumerate(roles)]
    pattern = sx.sequence(sx.field("role").eq("a").capture("a"), sx.gap(max_items=gap), sx.field("role").eq("b").capture("b"))
    result = engine().evaluate(pattern.bind(rows, key=("id",)), band=BAND)
    expected = [(i, j) for i in range(5) for j in range(i + 1, 5) if j - i - 1 <= gap and roles[i] == "a" and roles[j] == "b"]
    assert [(m["a"].key, m["b"].key) for m in result.matches] == expected
    assert result.complete


def test_relations_preserve_unknown_and_context():
    p = predicate()
    rel = sx.Relation(
        "targets",
        question="Related?",
        left_fields={"text": ("text",)},
        right_fields={"text": ("text",)},
        context_fields={"original": ("first", "text")},
    )
    pattern = sx.sequence(p.capture("a"), p.capture("b")).require(rel.between("a", "b", context={"first": sx.ref("a")}))
    rows = [{"id": "a", "text": "problem"}, {"id": "b", "text": "remedy"}]
    e = Engine(Scripted(lambda s, qs: dict.fromkeys(qs, 0.5 if "item" in s else 0.99)))
    result = e.evaluate(pattern.bind(rows, key=("id",)), band=BAND)
    assert result.outcome is sx.UNKNOWN and result.complete
    assert len(result.uncertain_matches) == 1 and len(result.leaves) == 3
    assert e.backend.calls[-1][0]["context"] == {"original": "problem"}


def test_duplicates_distinct_text_and_empty():
    pattern = sx.sequence(predicate().capture("x"))
    e = engine()
    rows = [{"id": 1, "text": "same"}, {"id": "1", "text": "same"}]
    report = e.evaluate(pattern.bind(rows, key=("id",)), band=BAND)
    assert len(report.matches) == 2 and len(report.leaves) == 1 and e.stats.requests == 1
    with pytest.raises(sx.InputError, match="unique"):
        pattern.bind([*rows, rows[0]], key=("id",))
    empty = e.evaluate(pattern.bind([], key=("id",)), band=BAND)
    assert empty.complete and empty.outcome is sx.NO


def test_caps_and_no_complete_prefix_work():
    e = engine()
    pattern = sx.sequence(predicate().capture("x"))
    rows = [{"id": i, "text": "x"} for i in range(3)]
    query = pattern.bind(rows, key=("id",))
    with pytest.raises(sx.CandidateLimitExceeded):
        e.plan(query, band=BAND, limits=sx.Limits(max_candidates=2))
    report = e.evaluate(query, band=BAND, limits=sx.Limits(max_results=2), errors="collect")
    assert not report.complete and report.outcome is None and report.truncated
    assert len(report.matches) == 2
    assert e.evaluate(query, band=BAND, limits=sx.Limits(max_results=3)).complete
    parts = []
    for i in range(5):
        if parts:
            parts.append(sx.gap(max_items=100))
        parts.append(sx.field("text").eq("never" if i == 4 else "x").capture(str(i)))
    impossible = sx.sequence(*parts).bind([{"id": i, "text": "x"} for i in range(30)], key=("id",))
    before = len(e.backend.calls)
    with pytest.raises(sx.PlanningLimitExceeded):
        e.plan(impossible, band=BAND, limits=sx.Limits(max_planning_steps=1000))
    assert len(e.backend.calls) == before


@pytest.mark.parametrize("suffix", [".jsonl", ".sqlite"])
def test_replay_lazy_keyless_and_validates_rows(tmp_path, suffix):
    path = tmp_path / "sub" / ("decisions" + suffix)
    query = predicate().bind({"text": "x"}, key=1)
    live = engine(cache=path)
    plan = live.plan(query, band=BAND)
    assert not path.parent.exists()
    result = live.evaluate(plan)
    assert result.outcome is sx.YES
    replay = Engine(Scripted(lambda *args: pytest.fail("replay sent a request")), cache=path, replay=True)
    again = replay.evaluate(query, band=BAND)
    assert again.outcome is sx.YES and again.leaves[0].cached
    assert replay.stats.requests == 0
    with pytest.raises(sx.EvaluationFailed) as failure:
        replay.evaluate(predicate().bind({"text": "changed"}, key=1), band=BAND)
    assert isinstance(failure.value.causes[0], CacheMiss)
    live.close()
    replay.close()


def test_plan_mismatch_and_threshold_reuse():
    e = engine(0.5)
    query = predicate().bind({"text": "x"}, key=1)
    plan = e.plan(query, band=BAND)
    assert e.evaluate(plan).outcome is sx.UNKNOWN
    assert e.evaluate(query, band=sx.Band(0.1, 0.4)).outcome is sx.YES
    assert e.stats.requests == 1
    e.backend.model = "different"
    with pytest.raises(sx.PlanMismatch):
        e.evaluate(plan)


def test_missing_retries_same_payload_and_limits():
    calls = []

    def handler(state, qs):
        calls.append(tuple(qs))
        return {next(iter(qs)): 0.9} if len(calls) == 1 else dict.fromkeys(qs, 0.9)

    e = Engine(Scripted(handler))
    query = (predicate("a") & predicate("b")).bind({"text": "x"}, key=1)
    result = e.evaluate(query, band=BAND)
    assert result.complete and len(calls) == 2 and calls[0] == calls[1]
    assert len(result.trace.attempts) == 2
    e = Engine(Scripted(lambda state, qs: {}))
    result = e.evaluate(query, band=BAND, limits=sx.Limits(max_requests=1), errors="collect")
    assert not result.complete and result.outcome is None
    assert result.errors[0].code == "RequestLimitExceeded" and e.stats.requests == 1


@pytest.mark.parametrize(
    "answer,error",
    [
        ({"type": "noul", "noul": 2}, BackendError),
        ({"type": "noul", "noul": float("inf")}, BackendError),
        ({"type": "noul", "noul": True}, BackendError),
        ({"type": "refusal", "reason": "private text"}, RefusedError),
    ],
)
def test_strict_failures_never_become_no_or_unknown(answer, error):
    e = Engine(Scripted(lambda state, qs: dict.fromkeys(qs, answer)))
    with pytest.raises(sx.EvaluationFailed) as info:
        e.evaluate(predicate().bind({"text": "x"}, key=1), band=BAND)
    assert isinstance(info.value.causes[0], error)
    assert info.value.partial_result.outcome is None
    assert len(info.value.trace.attempts) == 1 and not info.value.partial_result.leaves
    assert "private text" not in str(info.value)


def test_deadline_late_completion_not_success():
    def handler(state, qs):
        time.sleep(0.02)
        return dict.fromkeys(qs, 0.9)

    e = Engine(Scripted(handler))
    result = e.evaluate(predicate().bind({"text": "x"}, key=1), band=BAND, limits=sx.Limits(deadline_s=0.005), errors="collect")
    assert not result.complete and result.errors[0].code == "DeadlineExceeded"
    assert len(result.trace.attempts) == 1


def test_async_cancellation_stops_queued_dispatch():
    entered = threading.Event()
    release = threading.Event()

    def handler(state, qs):
        entered.set()
        assert release.wait(2)
        return dict.fromkeys(qs, 0.9)

    e = Engine(Scripted(handler), concurrency=1)
    pattern = sx.sequence(predicate().capture("x"))
    query = pattern.bind([{"id": i, "text": str(i)} for i in range(3)], key=("id",))

    async def exercise():
        task = asyncio.create_task(e.aevaluate(query, band=BAND))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert len(e.backend.calls) == 1 and e.stats.requests == 1
