"""Independent comparison and scoring semantics, using synthetic fixtures only."""

import importlib.util
import json
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exregex import Engine, Scripted
from exregex import semantic as sx


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "evals" / f"{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


compare = module("support_compare")
scorer = module("support_score")
preparation = module("support_data")


@pytest.mark.parametrize("unary", [0.95, 0.5, 0.05])
@pytest.mark.parametrize("relationship", [0.95, 0.5, 0.05])
def test_direct_and_compiled_agree_on_three_valued_fixtures(tmp_path, unary, relationship):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = compare.attach_context([
        compare.Message("p", "1", "ticket", "customer", "It fails", start),
        compare.Message("r", "2", "ticket", "agent", "Try this", start + timedelta(minutes=1)),
        compare.Message("f", "3", "ticket", "customer", "Still fails", start + timedelta(minutes=2)),
    ])
    backend = Scripted(lambda state, qs: dict.fromkeys(qs, unary if "item" in state else relationship))
    band = sx.Band(no=0.15, yes=0.85)
    calls = compare.DirectCalls(backend, tmp_path)
    direct = compare.direct(rows, calls, band)
    compiled = compare.compiled(rows, Engine(backend), band, sx.Limits())
    assert (direct["matches"], direct["uncertain"], direct["outcome"]) == (
        compiled["matches"], compiled["uncertain"], compiled["outcome"]
    )
    assert direct["candidates"] == compiled["candidates"] == 1
    direct_atoms = {scorer.atomic_key(a): a["p"] for a in direct["observations"]}
    compiled_atoms = {scorer.atomic_key(a): a["p"] for a in compiled["observations"]}
    assert direct_atoms == compiled_atoms


def reference(cid, presence="yes"):
    return {
        "id": cid, "input_hash": cid, "presence": presence, "status": "single_annotator",
        "reviewer": "synthetic test fixture", "captures": [[["p", "1"], ["r", "2"], ["f", "3"]]] if presence == "yes" else [],
    }


def prediction(cid, **kwargs):
    return {"input_hash": cid, "complete": True, "outcome": "no", "matches": [], "uncertain": [], "wall_ms": 10, **kwargs}


def test_scoring_includes_failure_recall_duplicate_false_positives_and_ambiguity():
    labels = {cid: reference(cid, presence) for cid, presence in (("a", "yes"), ("b", "yes"), ("c", "ambiguous"))}
    triple = labels["a"]["captures"][0]
    predictions = {
        "a": prediction("a", outcome="yes", matches=[triple, triple]),
        "b": prediction("b", complete=False, outcome=None),
        "c": prediction("c", outcome="yes", matches=[triple]),
    }
    result = scorer.score(predictions, labels)
    assert result["counts"]["tp"] == result["counts"]["fp"] == result["counts"]["fn"] == 1
    assert result["captures"]["precision"] == result["captures"]["recall"] == 0.5
    assert result["captures"]["conservative_precision"] == pytest.approx(1 / 3)
    assert result["decision_coverage"] == 0.5 and result["counts"]["execution_failures"] == 1
    assert result["counts"]["ambiguous_accepted_presence"] == 1


def test_pending_labels_and_changed_inputs_cannot_be_scored():
    label = reference("a")
    label["status"] = "pending"
    with pytest.raises(ValueError, match="pending"):
        scorer.score({"a": prediction("a")}, {"a": label})
    label["status"] = "single_annotator"
    with pytest.raises(ValueError, match="hash"):
        scorer.score({"a": prediction("changed")}, {"a": label})


def test_broad_abstention_is_not_resolved_and_has_no_capture_metric():
    result = scorer.score({"a": prediction("a", outcome="unknown", matches=None, uncertain=None)}, {"a": reference("a")}, broad=True)
    assert result["fully_resolved_coverage"] == 0 and result["captures"] is None
    assert result["counts"]["semantic_abstentions"] == 1


def test_split_keeps_connected_source_groups_together():
    groups = [["a", "b"], ["c"], ["d"], ["e", "f"]]
    split = preparation.split_groups(groups, target=3)
    assert len(split) == 3
    for group in groups:
        assert not (set(group) & split) or set(group) <= split
    assert split == preparation.split_groups(reversed(groups), target=3)


def test_cold_runs_isolate_conversations_and_replay_is_readonly(tmp_path, monkeypatch, capsys):
    configured_concurrency = []

    def observed_engine(*args, **kwargs):
        configured_concurrency.append(kwargs.get("concurrency"))
        return Engine(*args, **kwargs)

    monkeypatch.setattr(compare, "Engine", observed_engine)
    dataset = tmp_path / "data"
    dataset.mkdir()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    items = []
    for cid in ("one", "two"):
        rows = [asdict(compare.Message(f"{cid}-{i}", "v1", cid, role, text, start + timedelta(minutes=i)))
                for i, (role, text) in enumerate((("customer", "It fails"), ("agent", "Try this"), ("customer", "Still fails")))]
        for row in rows:
            row["sent_at"] = row["sent_at"].isoformat()
        items.append({"id": cid, "messages": rows, "input_hash": compare.hashed(rows)})
    raw = b"".join(compare.data(item) + b"\n" for item in items)
    (dataset / "dev.jsonl").write_bytes(raw)
    (dataset / "manifest.json").write_bytes(compare.data({"files": {"dev.jsonl": compare.hashlib.sha256(raw).hexdigest()}}))

    def run(output, *flags):
        monkeypatch.setattr(sys, "argv", ["support_compare.py", str(dataset), str(output), "--limit", "2", *flags])
        compare.main()
        capsys.readouterr()

    cold = tmp_path / "cold"
    run(cold)
    for item in compare.load_lines(cold / "compiled.jsonl"):
        assert item["stats"]["requests"] == item["stats"]["dispatched_questions"] == 5
        assert item["stats"]["cached"] == 0
        assert item["returned_models"] == ["scripted"]
    second = tmp_path / "cold-again"
    run(second)
    assert all(row["stats"]["cached"] == 0 for row in compare.load_lines(second / "compiled.jsonl"))
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in cold.rglob("*") if p.is_file()}
    warm = tmp_path / "replay"
    run(warm, "--mode", "replay", "--replay-from", str(cold))
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in cold.rglob("*") if p.is_file()}
    assert before == after
    assert not (warm / "observations").exists()
    for arm in ("broad", "direct", "compiled"):
        for original, replayed in zip(compare.load_lines(cold / f"{arm}.jsonl"), compare.load_lines(warm / f"{arm}.jsonl"), strict=True):
            assert replayed["matches"] == original["matches"]
            assert replayed["stats"]["requests"] == replayed["stats"]["dispatched_questions"] == 0
            assert replayed["stats"]["cached"] > 0
    assert json.loads((warm / "environment.json").read_bytes())["backend"] == "scripted"
    monkeypatch.setattr(sys, "argv", ["support_score.py", str(warm), str(tmp_path / "unused-labels")])
    with pytest.raises(SystemExit) as error:
        scorer.main()
    assert error.value.code == 2 and "Scripted" in capsys.readouterr().err
    assert configured_concurrency and set(configured_concurrency) == {4}


def test_direct_missing_replay_does_not_create_directories(tmp_path):
    missing = tmp_path / "does-not-exist"
    calls = compare.DirectCalls(Scripted(lambda *_: pytest.fail("replay dispatched")), missing, replay=True)
    with pytest.raises(FileNotFoundError):
        calls.request({"item": {"text": "failure"}}, [compare.problem])
    assert not missing.exists()
