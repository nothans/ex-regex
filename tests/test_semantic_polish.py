"""Operator summaries and the four public, offline first-use scenarios."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from exregex import Engine, Scripted
from exregex import semantic as sx
from exregex.errors import BackendError, CacheMiss

BAND = sx.Band(0.15, 0.85)


def test_failure_counts_preserve_distinct_original_errors():
    causes = (BackendError("private outage A"), CacheMiss("private missing row"), BackendError("private outage B"))
    predicate = sx.Predicate("p", question="Qualifies?", fields={"position": ("position",)})
    bound = sx.sequence(predicate.capture("x")).bind([{"position": i} for i in range(3)], key=("position",))

    def fail(state, _):
        raise causes[state["item"]["position"]]

    with Engine(Scripted(fail), concurrency=1) as engine, pytest.raises(sx.EvaluationFailed) as caught:
        engine.evaluate(bound, band=BAND)
    failure = caught.value
    assert failure.causes == causes and failure.__cause__ is causes[0]
    counts = failure.cause_counts
    assert list(counts.items()) == [("BackendError", 2), ("CacheMiss", 1)]
    counts["BackendError"] = 999
    assert failure.cause_counts == {"BackendError": 2, "CacheMiss": 1}
    assert "BackendError: 2, CacheMiss: 1" in str(failure)
    assert "private" not in str(failure) + json.dumps(failure.cause_counts)
    assert len({issue.request_id for issue in failure.partial_result.errors}) == 3


def test_empty_summary_aggregates_definitions_without_hiding_first_turn(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "examples/support"))
    from demo import fixture
    from support_context import attach_context
    from support_patterns import failed_remedy

    def bound(messages):
        return failed_remedy.bind(messages, key=("id",), revision=("revision",))

    with Engine(Scripted(lambda *_: pytest.fail("planning dispatched"))) as engine:
        plan = engine.plan(bound(attach_context(fixture())), band=BAND)
        before = [(request.id, request.prepared.body) for request in plan.requests]
        description = plan.describe()
        assert description["diagnostic_summary"] == [
            {"scope": "item", "field": "prior_turns", "empty_leaves": 1, "projected_leaves": 3}
        ]
        assert len(description["diagnostics"]) == 1
        assert description["diagnostics"][0]["record_indices"] == [0]
        description["diagnostic_summary"][0]["empty_leaves"] = 999
        assert plan.describe()["diagnostic_summary"][0]["empty_leaves"] == 1
        assert [(request.id, request.prepared.body) for request in plan.requests] == before
        assert engine.plan(bound(attach_context(fixture())), band=BAND).id == plan.id
        missing = engine.plan(bound(fixture()), band=BAND).describe()
        assert missing["diagnostic_summary"] == [
            {"scope": "item", "field": "prior_turns", "empty_leaves": 3, "projected_leaves": 3}
        ]
        assert "printer" not in json.dumps(description["diagnostic_summary"] + description["diagnostics"])
        assert engine.stats.requests == 0


def test_summary_counts_deduplicated_leaves_and_keeps_scopes_separate():
    fields = {name: (name,) for name in ("null", "text", "list", "mapping", "count", "flag")}
    p = sx.Predicate("p", question="Qualifies?", fields=fields, context_fields={"text": ("text",)})
    rows = [{"id": i, "null": None, "text": " \t", "list": [], "mapping": {}, "count": 0, "flag": False} for i in range(3)]
    with Engine(Scripted(lambda *_: pytest.fail("planning dispatched"))) as engine:
        plan = engine.plan(sx.sequence(p.capture("x")).bind(rows, key=("id",), context={"text": ""}), band=BAND)
    summary = plan.describe()["diagnostic_summary"]
    assert [(row["scope"], row["field"]) for row in summary] == [
        ("context", "text"), ("item", "list"), ("item", "mapping"), ("item", "null"), ("item", "text")
    ]
    assert all(row["empty_leaves"] == row["projected_leaves"] == 1 for row in summary)
    assert all(row["record_indices"] == [0, 1, 2] for row in plan.describe()["diagnostics"])


@pytest.mark.parametrize(
    "options,status",
    [([], "reported_failed_remedy"), (["--answer", "no"], "no_match_within_declared_pattern"),
     (["--answer", "unknown"], "uncertain"), (["--outage"], "evaluation_failed")],
)
def test_support_demo_outcomes(options, status):
    root = Path(__file__).resolve().parents[1]
    run = subprocess.run(
        [sys.executable, "examples/support/demo.py", *options], cwd=root, capture_output=True, text=True, check=True, timeout=20,
    )
    output = json.loads(run.stdout)
    assert output["service"]["status"] == status
    assert output["worker"] == [output["service"]]
    if status == "evaluation_failed":
        assert output["service"]["errors"] == {"BackendError": 3}
    else:
        assert output["service"]["near_edge"] == 0
