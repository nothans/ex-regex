"""Public contracts identified by the hands-on agent trial. All judgments are scripted."""

import asyncio
import contextlib
import json
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from exregex import Engine, Scripted
from exregex import semantic as sx
from exregex.errors import BackendError, BudgetExceeded, CacheMiss, RefusedError

BAND = sx.Band(0.15, 0.85)


def predicate(name="p", **kwargs):
    return sx.Predicate(name, question="Does item.text qualify?", fields={"text": ("text",)}, **kwargs)


def query():
    return predicate().bind({"text": "private source"}, key="ticket", revision=3)


@pytest.mark.parametrize("cause", [BackendError("outage"), RefusedError("refused"), BudgetExceeded("limit")])
@pytest.mark.parametrize("mode", ["raise", "collect"])
def test_evaluation_failure_has_one_public_boundary(cause, mode):
    def fail(*_):
        raise cause

    with Engine(Scripted(fail)) as engine:
        if mode == "raise":
            with pytest.raises(sx.EvaluationFailed) as caught:
                engine.evaluate(query(), band=BAND)
            failure = caught.value
            assert failure.causes == (cause,) and failure.__cause__ is cause
            assert failure.trace is failure.partial_result.trace
            result = failure.partial_result
            assert not hasattr(cause, "partial_result")
        else:
            result = engine.evaluate(query(), band=BAND, errors="collect")
        assert not result.complete and result.outcome is None
        assert result.errors[0].code == type(cause).__name__
        assert len(result.trace.attempts) == 1


@pytest.mark.parametrize("cause", [TypeError("bug"), ValueError("bug"), AssertionError("bug"), sx.InputError("bug")])
@pytest.mark.parametrize("mode", ["raise", "collect"])
def test_programming_faults_in_user_backends_are_not_collected(cause, mode):
    def fail(*_):
        raise cause

    with Engine(Scripted(fail)) as engine:
        with pytest.raises(type(cause)) as caught:
            engine.evaluate(query(), band=BAND, errors=mode)
        assert caught.value is cause
        assert engine._reserved == 0
        assert engine._slots.acquire(blocking=False)
        engine._slots.release()


def test_planning_and_validation_errors_keep_their_types():
    with Engine(Scripted(lambda *_: pytest.fail("preflight dispatched"))) as engine:
        with pytest.raises(sx.DefinitionError):
            engine.evaluate(query(), band=BAND, errors="invalid")
        with pytest.raises(sx.RequestLimitExceeded):
            engine.evaluate(
                sx.sequence(predicate().capture("x")).bind([{"id": i, "text": str(i)} for i in range(2)], key=("id",)),
                band=BAND,
                limits=sx.Limits(max_requests=1),
                errors="collect",
            )
        planned = engine.plan(query(), band=BAND)
        engine.backend.model = "changed"
        with pytest.raises(sx.PlanMismatch):
            engine.evaluate(planned, errors="collect")


def test_replay_miss_and_deadline_share_failure_boundary(tmp_path):
    with Engine(Scripted(lambda *_: pytest.fail("replay dispatched")), cache=tmp_path / "missing.sqlite", replay=True) as engine:
        with pytest.raises(sx.EvaluationFailed) as caught:
            engine.evaluate(query(), band=BAND)
        assert isinstance(caught.value.causes[0], CacheMiss)

    def late(_, qs):
        time.sleep(0.02)
        return dict.fromkeys(qs, 0.95)

    with Engine(Scripted(late)) as engine:
        with pytest.raises(sx.EvaluationFailed) as caught:
            engine.evaluate(query(), band=BAND, limits=sx.Limits(deadline_s=0.005))
        assert isinstance(caught.value.causes[0], sx.DeadlineExceeded)


def test_result_cap_wraps_with_successful_partial_evidence():
    bound = sx.sequence(predicate().capture("x")).bind([{"id": i, "text": str(i)} for i in range(2)], key=("id",))
    with Engine(Scripted(lambda _, qs: dict.fromkeys(qs, 0.85))) as engine:
        with pytest.raises(sx.EvaluationFailed) as caught:
            engine.evaluate(bound, band=BAND, limits=sx.Limits(max_results=1))
        report = caught.value.partial_result
        assert isinstance(caught.value.causes[0], sx.ResultLimitExceeded)
        assert report.outcome is None and report.truncated and len(report.matches) == 1
        assert report.near_edge == report.coverage.near_edge == 2
        assert len(report.explain()) == 2


def test_async_failure_has_same_public_contract():
    def fail(*_):
        raise BackendError("offline")

    async def run():
        with Engine(Scripted(fail)) as engine:
            with pytest.raises(sx.EvaluationFailed) as caught:
                await engine.aevaluate(query(), band=BAND)
            assert isinstance(caught.value.causes[0], BackendError)

    asyncio.run(run())


def test_repeated_async_cancellation_still_drains_inflight_transport():
    started, release, exited = threading.Event(), threading.Event(), threading.Event()

    def handler(_, questions):
        started.set()
        try:
            assert release.wait(3), "test did not release the transport"
            return dict.fromkeys(questions, 0.95)
        finally:
            exited.set()

    async def run():
        with Engine(Scripted(handler)) as engine:
            task = asyncio.create_task(engine.aevaluate(query(), band=BAND))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0.02)
                    assert not task.done() and not exited.is_set()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert exited.is_set() and engine.stats.requests == 1
                assert engine._reserved == 0
            finally:
                release.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    asyncio.run(run())


def test_explanations_are_named_ordered_detached_and_source_free(tmp_path):
    pattern = sx.sequence(predicate("named", version=2).capture("x"))
    bound = pattern.bind([{"id": "first", "text": "private zero"}, {"id": 2, "text": "private one"}], key=("id",))
    path = tmp_path / "observations.jsonl"
    with Engine(Scripted(lambda _, qs: dict.fromkeys(qs, 0.1)), cache=path) as engine:
        report = engine.evaluate(bound, band=BAND)
    rows = report.explain()
    assert report.outcome is sx.NO and not report.matches
    assert [row["associated_records"][0]["key"] for row in rows] == ["first", 2]
    assert all(row["definition"] == {"name": "named", "version": 2} for row in rows)
    assert "private" not in json.dumps(rows)
    rows[0]["definition"]["name"] = "changed"
    assert report.leaves[0].definition.name == "named"
    with pytest.raises(FrozenInstanceError):
        report.leaves[0].definition.name = "changed"
    with Engine(Scripted(lambda *_: pytest.fail("replay dispatched")), cache=path, replay=True) as replay:
        again = replay.evaluate(bound, band=BAND)
        assert replay.stats.requests == 0
    assert [dict(row, cached=False) for row in again.explain()] == report.explain()
    assert again.near_edge == report.near_edge == 2


def test_deduplicated_explanation_keeps_associated_ids_and_counts_once():
    bound = sx.sequence(predicate().capture("x")).bind([{"id": i, "text": "same"} for i in range(3)], key=("id",))
    with Engine(Scripted(lambda _, qs: dict.fromkeys(qs, 0.85))) as engine:
        report = engine.evaluate(bound, band=BAND)
    assert report.coverage.near_edge == report.near_edge == 1
    assert len(report.matches) == 3
    assert [row["key"] for row in report.explain()[0]["associated_records"]] == [0, 1, 2]


def test_relation_explanation_includes_capture_context_sources():
    relation = sx.Relation(
        "targets_original",
        question="Does right.text still fail after left.text for context.problem?",
        left_fields={"text": ("text",)},
        right_fields={"text": ("text",)},
        context_fields={"problem": ("original", "text")},
    )
    pattern = sx.sequence(
        sx.field("role").eq("problem").capture("problem"),
        sx.gap(max_items=1),
        sx.field("role").eq("remedy").capture("remedy"),
        sx.field("role").eq("followup").capture("followup"),
    ).require(relation.between("remedy", "followup", context={"original": sx.ref("problem")}))
    rows = [
        {"id": name, "role": role, "text": text}
        for name, role, text in (
            ("printer", "problem", "Printer fails"),
            ("scanner", "problem", "Scanner fails"),
            ("fix", "remedy", "Restart it"),
            ("reply", "followup", "Same problem"),
        )
    ]

    def answers(state, questions):
        return dict.fromkeys(questions, 0.95 if state["context"]["problem"].startswith("Printer") else 0.05)

    with Engine(Scripted(answers)) as engine:
        report = engine.evaluate(pattern.bind(rows, key=("id",)), band=BAND)
    explanation = report.explain()
    assert [r["key"] for r in explanation[0]["associated_records"]] == ["printer", "fix", "reply"]
    assert [r["key"] for r in explanation[1]["associated_records"]] == ["scanner", "fix", "reply"]
    assert [r["outcome"] for r in explanation] == ["yes", "no"]
    assert [m["problem"].key for m in report.matches] == ["printer"]


@pytest.mark.parametrize(
    "p,near",
    [
        (0, False),
        (0.1, True),
        (0.15, True),
        (0.2, True),
        (0.201, False),
        (0.799, False),
        (0.8, True),
        (0.85, True),
        (0.9, True),
        (1, False),
    ],
)
def test_edge_distance_boundaries(p, near):
    with Engine(Scripted(lambda _, qs: dict.fromkeys(qs, p))) as engine:
        report = engine.evaluate(query(), band=BAND)
    assert report.near_edge == int(near)
    assert report.explain()[0]["near_edge"] is near


def test_overlapping_edge_neighborhood_counts_one_leaf():
    with Engine(Scripted(lambda _, qs: dict.fromkeys(qs, 0.5))) as engine:
        report = engine.evaluate(query(), band=sx.Band(0.48, 0.52))
    assert report.near_edge == 1 and report.outcome is sx.UNKNOWN


def test_empty_projection_diagnostics_are_local_and_keep_false_and_zero():
    fields = {name: (name,) for name in ("prior_turns", "blank", "null", "flag", "count")}
    definition = sx.Predicate("contextual", question="Qualifies?", fields=fields, context_fields={"history": ("history",)})
    bound = definition.bind({"prior_turns": [], "blank": "  ", "null": None, "flag": False, "count": 0}, key=1, context={"history": {}})
    with Engine(Scripted(lambda *_: pytest.fail("planning dispatched"))) as engine:
        plan = engine.plan(bound, band=BAND)
        diagnostics = plan.describe()["diagnostics"]
        assert {(d["scope"], d["field"]) for d in diagnostics} == {
            ("item", "prior_turns"),
            ("item", "blank"),
            ("item", "null"),
            ("context", "history"),
        }
        assert engine.stats.requests == 0 and not engine.backend.calls
        assert "value" not in json.dumps(diagnostics)


def test_bounded_archive_preserves_order_and_uses_shared_limit(monkeypatch):
    # Load the public example through the same import paths used by a consumer.
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "examples/support"))
    import archive_worker

    lock, entered = threading.Lock(), threading.Event()
    active = peak = 0

    def handler(state, qs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
        entered.wait(2)
        with lock:
            active -= 1
        return dict.fromkeys(qs, 0.95)

    def inspect(ticket, *, engine, band, limits):
        result = engine.evaluate(predicate().bind({"text": str(ticket)}, key=ticket), band=band, limits=limits)
        return result.leaves[0].records[0].key

    monkeypatch.setattr(archive_worker, "inspect_ticket", inspect)
    consumed = []

    def inputs():
        for i in range(9):
            consumed.append(i)
            yield i

    with Engine(Scripted(handler), concurrency=2, cache=False) as engine:
        output = archive_worker.inspect_archive(inputs(), engine=engine, band=BAND, limits=sx.Limits(), workers=3)
        assert next(output) == 0
        assert len(consumed) == 3
        assert list(output) == list(range(1, 9))
        assert peak == 2 and engine.stats.requests == 9
