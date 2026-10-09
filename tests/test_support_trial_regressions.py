"""Independent support-trial QA: synthetic fixtures and development provenance only."""

import hashlib
import importlib.util
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from exregex import Engine, Scripted
from exregex import semantic as sx
from exregex.errors import BackendError

ROOT = Path(__file__).resolve().parents[1]
BAND = sx.Band(0.15, 0.85)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    """Exercise real preprocessing without downloading or bundling the local pilot."""
    archive = tmp_path / "synthetic-source.zip"
    conversations, utterances = {}, []
    for index in range(200):
        cid = f"synthetic-{index:03}"
        conversations[cid] = {"role_A": cid + "-customer", "role_B": cid + "-agent", "friction": "must not leak"}
        for turn in range(40 if index < 150 else 39):
            utterances.append(
                {
                    "id": f"{cid}-{turn}",
                    "conversation_id": cid,
                    "text": f"Synthetic conversation {index}, turn {turn}.",
                    "timestamp": (datetime(2020, 1, 1) + timedelta(minutes=turn)).isoformat(),
                    "speaker": conversations[cid]["role_A" if turn % 2 == 0 else "role_B"],
                    "meta": {"time_elapsed": float("nan") if index < 24 and turn == 0 else turn, "model_label": "excluded"},
                }
            )
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("ubuntu-chat-logs/conversations.json", json.dumps(conversations))
        zipped.writestr("ubuntu-chat-logs/utterances.jsonl", "\n".join(json.dumps(row) for row in utterances))
    monkeypatch.setattr(preprocessing, "SOURCE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest())
    monkeypatch.setattr(preprocessing, "SOURCE", "synthetic fixture; no external corpus")
    output = tmp_path / "prepared"
    preprocessing.prepare(archive, output)
    return output, archive


def module(name):
    spec = importlib.util.spec_from_file_location("qa_" + name, ROOT / "evals" / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


compare = module("support_compare")
scoring = module("support_score")
preprocessing = module("support_data")


def fixture_rows(*, twice=False):
    stamp = datetime(2026, 10, 8, tzinfo=timezone.utc)
    rows = []
    for ticket in ("one", "two") if twice else ("one",):
        for index, (role, text) in enumerate(
            (("customer", "The login fails."), ("agent", "Try clearing cookies."), ("customer", "I tried that and it still fails."))
        ):
            rows.append(compare.Message(ticket + str(index), "v1", ticket, role, text, stamp + timedelta(minutes=index)))
    return compare.attach_context(rows)


def run_both(tmp_path, rows, unary, relation):
    def handler(state, qs):
        return dict.fromkeys(qs, unary if "item" in state else relation)

    direct_backend = Scripted(handler)
    calls = compare.DirectCalls(direct_backend, tmp_path / "direct")
    direct = compare.direct(rows, calls, BAND)
    engine = Engine(Scripted(handler), cache=False)
    compiled = compare.compiled(rows, engine, BAND, sx.Limits())
    return direct, compiled


@pytest.mark.parametrize("unary,relation", [(0.95, 0.95), (0.5, 0.95), (0.95, 0.5), (0.95, 0.01), (0.01, 0.95)])
def test_direct_compiled_decisions_and_captures_match(tmp_path, unary, relation):
    direct, compiled = run_both(tmp_path, fixture_rows(), unary, relation)
    for key in ("complete", "outcome", "matches", "uncertain", "candidates"):
        assert direct[key] == compiled[key]
    assert {scoring.atomic_key(x): x["p"] for x in direct["observations"]} == {
        scoring.atomic_key(x): x["p"] for x in compiled["observations"]
    }


def test_deduplicated_compiled_observations_keep_each_source_atomic_identity(tmp_path):
    direct, compiled = run_both(tmp_path, fixture_rows(twice=True), 0.95, 0.95)
    assert compiled["matches"] == direct["matches"]
    direct_atoms = {scoring.atomic_key(x): x["p"] for x in direct["observations"]}
    compiled_atoms = {scoring.atomic_key(x): x["p"] for x in compiled["observations"]}
    assert len(direct_atoms) == 10
    assert compiled_atoms == direct_atoms


def label(cid="x", presence="yes", **extra):
    row = {
        "id": cid,
        "input_hash": "sha",
        "status": "single_annotator",
        "reviewer": "synthetic QA fixture",
        "presence": presence,
        "captures": [[["a", 1], ["b", 1], ["c", 1]]] if presence == "yes" else [],
        "ambiguous_captures": [],
        "atomic_labels": [],
        "families": [],
    }
    row.update(extra)
    return row


def prediction(cid="x", outcome="yes", **extra):
    row = {
        "id": cid,
        "input_hash": "sha",
        "complete": True,
        "outcome": outcome,
        "matches": [[["a", 1], ["b", 1], ["c", 1]]] if outcome == "yes" else [],
        "uncertain": [],
        "wall_ms": 1,
        "observations": [],
    }
    row.update(extra)
    return row


def test_failures_and_abstentions_stay_in_coverage_and_recall_denominator():
    labels = {n: label(n) for n in ("yes", "unknown", "failed")}
    predictions = {
        "yes": prediction("yes"),
        "unknown": prediction("unknown", "unknown", uncertain=[[["a", 1], ["b", 1], ["c", 1]]]),
        "failed": prediction("failed", None, complete=False),
    }
    report = scoring.score(predictions, labels)
    assert report["decision_coverage"] == pytest.approx(1 / 3)
    assert report["fully_resolved_coverage"] == pytest.approx(1 / 3)
    assert report["captures"]["precision"] == 1
    assert report["captures"]["recall"] == pytest.approx(1 / 3)
    assert report["counts"]["execution_failures"] == 1
    assert report["counts"]["semantic_abstentions"] == 1


def test_broad_unknown_is_not_fully_resolved():
    report = scoring.score({"x": prediction(outcome="unknown", matches=None, uncertain=None)}, {"x": label()}, broad=True)
    assert report["decision_coverage"] == 0
    assert report["fully_resolved_coverage"] == 0


@pytest.mark.parametrize("change", [{"status": "pending"}, {"reviewer": None}, {"input_hash": "different"}])
def test_pending_unattributed_or_mismatched_labels_rejected(change):
    with pytest.raises(ValueError):
        scoring.score({"x": prediction()}, {"x": label(**change)})


def test_generated_pending_labels_cannot_be_scored(prepared):
    labels = scoring.load(prepared[0] / "dev.labels-a.jsonl")
    predictions = {cid: prediction(cid, "no", input_hash=row["input_hash"]) for cid, row in labels.items()}
    with pytest.raises(ValueError, match=r"pending|reviewer"):
        scoring.score(predictions, labels)


def test_complete_none_outcome_is_invalid():
    with pytest.raises(ValueError):
        scoring.score({"x": prediction(outcome=None)}, {"x": label(presence="no")})


def test_direct_replay_exact_payload_readonly(tmp_path):
    rows = fixture_rows()
    backend = Scripted(lambda state, qs: dict.fromkeys(qs, 0.95))
    original = compare.direct(rows, compare.DirectCalls(backend, tmp_path), BAND)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.glob("*.json")}
    replay = compare.DirectCalls(Scripted(lambda *args: pytest.fail("replay sent a call")), tmp_path, replay=True)
    actual = compare.direct(rows, replay, BAND)
    assert actual == original and replay.cached == 5 and not replay.attempts
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.glob("*.json")} == before
    changed = list(rows)
    from dataclasses import replace

    changed[0] = replace(changed[0], text="Different problem.")
    with pytest.raises(FileNotFoundError):
        compare.direct(changed, replay, BAND)


def test_direct_failed_attempt_never_returns_negative(tmp_path):
    def fails(state, qs):
        raise BackendError("synthetic failed request")

    calls = compare.DirectCalls(Scripted(fails), tmp_path)
    with pytest.raises(BackendError):
        compare.direct(fixture_rows(), calls, BAND)
    assert calls.attempts and all(x["reported_cost_usd"] is None for x in calls.attempts)


def args(mode="live"):
    return SimpleNamespace(
        mode=mode,
        model="typesafe/jev-1.13",
        expected_model="typesafe/jev-1.13-20260917",
        no=0.15,
        yes=0.85,
        budget=0.1,
        arms=["keyword", "broad", "direct", "compiled"],
        reference_labels=None,
    )


def test_profiles_match_live_replay_and_change_for_band_or_model():
    manifest = {"files": {"dev.jsonl": "devhash", "test.jsonl": "testhash"}}
    original = compare.profile(args(), manifest)
    assert compare.profile(args("replay"), manifest) == original
    for field, value in (("yes", 0.9), ("model", "different-model"), ("expected_model", "different-returned-model"), ("budget", 0.2)):
        changed = args()
        setattr(changed, field, value)
        assert compare.profile(changed, manifest) != original


def test_profile_pins_runtime_source_changes(monkeypatch):
    manifest = {"files": {"dev.jsonl": "devhash", "test.jsonl": "testhash"}}
    original = compare.profile(args(), manifest)
    old_read = Path.read_bytes

    def changed_read(path):
        content = old_read(path)
        return (
            content + b"\n# simulated runtime change\n"
            if path.resolve() == (Path(compare.exregex.__file__).resolve().parent / "semantic/runtime.py")
            else content
        )

    monkeypatch.setattr(Path, "read_bytes", changed_read)
    assert compare.profile(args(), manifest) != original


def test_synthetic_overlap_groups_remain_together():
    groups = [["a", "b"], ["c", "d", "e"], ["f"], ["g", "h", "i", "j"]]
    chosen = preprocessing.split_groups(groups, target=5)
    assert len(chosen) == 5
    assert all(not (set(group) & chosen) or set(group) <= chosen for group in groups)
    assert preprocessing.split_groups(reversed(groups), target=5) == chosen
    with pytest.raises(ValueError):
        preprocessing.split_groups([["a", "b"], ["c", "d"]], target=3)


def test_generated_manifest_and_raw_source_provenance(prepared):
    output, archive = prepared
    manifest = json.loads((output / "manifest.json").read_bytes())
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["source_sha256"] == preprocessing.SOURCE_SHA256
    rawdev = (output / "dev.jsonl").read_bytes()
    assert hashlib.sha256(rawdev).hexdigest() == manifest["files"]["dev.jsonl"]
    dev = [json.loads(line) for line in rawdev.splitlines()]
    assert len(dev) == 100 and sum(len(x["messages"]) for x in dev) == manifest["counts"]["dev"]["messages"]
    # Restrict provenance inspection to the already-designated development rows.
    dev_ids = {x["id"] for x in dev}
    with zipfile.ZipFile(archive) as zipped:
        source = [json.loads(line) for line in zipped.read("ubuntu-chat-logs/utterances.jsonl").splitlines()]
    by_id = {x["id"]: x for x in source if x["conversation_id"] in dev_ids}
    assert len(by_id) == manifest["counts"]["dev"]["messages"]
    for item in dev:
        assert item["input_hash"] == preprocessing.sha(item["messages"])
        first = datetime.fromisoformat(item["messages"][0]["source_timestamp"])
        for row in item["messages"]:
            raw = by_id[row["id"]]
            assert row["text"] == raw["text"]
            assert row["source_timestamp"] == raw["timestamp"] and row["source_speaker"] == raw["speaker"]
            elapsed = (datetime.fromisoformat(raw["timestamp"]) - first).total_seconds() / 60
            assert row["revision"] == preprocessing.sha([row["text"], elapsed, raw["speaker"]])
            assert datetime.fromisoformat(row["sent_at"]) == datetime(2000, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=elapsed)
            assert set(row) == {"id", "revision", "ticket_id", "role", "text", "sent_at", "source_timestamp", "source_speaker"}
    assert manifest["nonfinite_supplied_elapsed"] == 24
    assert "timezone is unknown" in manifest["preprocessing"]


def test_preparation_rejects_unreviewed_archive_without_writing(tmp_path):
    archive = tmp_path / "unreviewed.zip"
    archive.write_bytes(b"not the reviewed source")
    output = tmp_path / "prepared"
    with pytest.raises(ValueError, match="reviewed source"):
        preprocessing.prepare(archive, output)
    assert not output.exists()
    output.mkdir()
    marker = output / "human-work.txt"
    marker.write_text("must survive")
    with pytest.raises(ValueError, match="output exists"):
        preprocessing.prepare(archive, output)
    assert marker.read_text() == "must survive"


def test_observed_atomic_calibration_exposes_missing_answers():
    atoms = [
        {"name": "support.problem", "records": [["a", 1]], "label": 1},
        {"name": "support.problem", "records": [["b", 1]], "label": 0},
        {"name": "support.problem", "records": [["c", 1]], "label": 1},
    ]
    observed = [
        {"name": "support.problem", "records": [["a", 1]], "p": 0.9},
        {"name": "support.problem", "records": [["b", 1]], "p": 0.8},
    ]
    report = scoring.score({"x": prediction(observations=observed)}, {"x": label(atomic_labels=atoms)})
    assert report["counts"]["unobserved_labeled_atoms"] == 1
    curve = report["calibration_observed_atoms_only"]["support.problem"]
    assert curve["n"] == 2 and curve["brier"] == pytest.approx(0.325)
    assert sum(b["n"] for b in curve["bins"]) == 2


def test_synthetic_development_cli_runs_all_arms_and_blocks_quality_claim_by_default(tmp_path, monkeypatch, capsys):
    from dataclasses import asdict

    dataset, output = tmp_path / "synthetic-data", tmp_path / "synthetic-run"
    dataset.mkdir()
    rows = []
    for message in fixture_rows():
        row = asdict(message)
        row["sent_at"] = message.sent_at.isoformat()
        rows.append(row)
    item = {"id": "synthetic", "input_hash": compare.hashed(rows), "messages": rows}
    raw = compare.data(item) + b"\n"
    (dataset / "dev.jsonl").write_bytes(raw)
    (dataset / "manifest.json").write_bytes(
        compare.data({"files": {"dev.jsonl": hashlib.sha256(raw).hexdigest(), "test.jsonl": "unused-synthetic-placeholder"}})
    )
    monkeypatch.setattr(sys, "argv", ["support_compare.py", str(dataset), str(output), "--limit", "1"])
    compare.main()
    capsys.readouterr()
    for arm in ("keyword", "broad", "direct", "compiled"):
        results = compare.load_lines(output / (arm + ".jsonl"))
        assert len(results) == 1 and results[0]["complete"] and results[0]["outcome"] == "yes"
        assert results[0]["input_hash"] == item["input_hash"]
    reference = label("synthetic", input_hash=item["input_hash"], captures=compare.load_lines(output / "compiled.jsonl")[0]["matches"])
    label_path = tmp_path / "synthetic-labels.jsonl"
    label_path.write_bytes(compare.data(reference) + b"\n")
    monkeypatch.setattr(sys, "argv", ["support_score.py", str(output), str(label_path)])
    with pytest.raises(SystemExit) as refused:
        scoring.main()
    assert refused.value.code == 2
    assert "Scripted" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["support_score.py", str(output), str(label_path), "--allow-scripted"])
    scoring.main()
    result = json.loads(capsys.readouterr().out)
    assert result["evidence"]["mode"] == "scripted"
    assert result["reports"]["compiled"]["captures"]["f1"] == 1
    assert result["reports"]["broad"]["captures"] is None


def duplicate_dataset(path):
    """Same projected messages in separate conversations expose cache contamination."""
    from dataclasses import asdict

    path.mkdir()
    rows = [{**asdict(m), "sent_at": m.sent_at.isoformat()} for m in fixture_rows()]
    items = [{"id": cid, "input_hash": compare.hashed(rows), "messages": rows} for cid in ("case-a", "case-b")]
    raw = b"".join(compare.data(item) + b"\n" for item in items)
    (path / "dev.jsonl").write_bytes(raw)
    (path / "manifest.json").write_bytes(
        compare.data({"files": {"dev.jsonl": hashlib.sha256(raw).hexdigest(), "test.jsonl": "unused-synthetic-placeholder"}})
    )
    return items


def invoke_compare(monkeypatch, dataset, output, *extra):
    monkeypatch.setattr(
        sys, "argv", ["support_compare.py", str(dataset), str(output), "--limit", "2", "--arms", "direct", "compiled", *extra]
    )
    compare.main()


def tree_bytes(path):
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns) for p in path.rglob("*") if p.is_file()}


def test_cli_cold_isolation_readonly_replay_and_scripted_provenance(tmp_path, monkeypatch, capsys):
    dataset = tmp_path / "duplicate-data"
    duplicate_dataset(dataset)
    first, second, replay = (tmp_path / name for name in ("first", "second", "replay"))
    invoke_compare(monkeypatch, dataset, first)
    invoke_compare(monkeypatch, dataset, second)
    for run in (first, second):
        for arm in ("direct", "compiled"):
            results = compare.load_lines(run / (arm + ".jsonl"))
            assert len(results) == 2 and all(r["complete"] for r in results)
            assert [r["stats"]["requests"] for r in results] == [5, 5]
            assert [r["stats"]["cached"] for r in results] == [0, 0]
            assert all(r["stats"]["dispatched_questions"] == 5 and r["stats"]["dispatched_payload_bytes"] > 0 for r in results)
            assert all(r["returned_models"] == ["scripted"] and r["source_records"] == 3 for r in results)
            assert len(list((run / "observations" / arm).iterdir())) == 2
    assert not (dataset / "observations").exists()
    original = tree_bytes(first)
    invoke_compare(monkeypatch, dataset, replay, "--mode", "replay", "--replay-from", str(first))
    assert tree_bytes(first) == original
    assert not (replay / "observations").exists()
    environment = json.loads((replay / "environment.json").read_bytes())
    assert environment["mode"] == "replay" and environment["backend"] == "scripted"
    assert environment["replay_from"] == str(first.resolve())
    for arm in ("direct", "compiled"):
        results = compare.load_lines(replay / (arm + ".jsonl"))
        cold = compare.load_lines(first / (arm + ".jsonl"))
        assert all(r["complete"] for r in results)
        assert [r["matches"] for r in results] == [r["matches"] for r in cold]
        assert all(
            r["stats"]["requests"] == r["stats"]["dispatched_questions"] == r["stats"]["dispatched_payload_bytes"] == 0 for r in results
        )
        assert [r["stats"]["cached"] for r in results] == [5, 5]
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["support_score.py", str(replay), str(tmp_path / "intentionally-unused-labels")])
    with pytest.raises(SystemExit) as refused:
        scoring.main()
    assert refused.value.code == 2 and "Scripted" in capsys.readouterr().err


@pytest.mark.parametrize("charge", ["reported", "unknown"])
def test_cli_arm_budget_continues_across_case_engines(tmp_path, monkeypatch, charge):
    from dataclasses import replace

    from exregex.semantic.transport import AttemptResponse

    dataset, output = tmp_path / "priced-data", tmp_path / "priced-run"
    duplicate_dataset(dataset)
    attempts_by_backend = []

    def priced_fixture(handler):
        backend = Scripted(handler)
        original_caps = backend.capabilities
        backend.capabilities = lambda: replace(original_caps(), adapter="qa-priced", price_per_mtok=1000.0)
        attempts = []
        attempts_by_backend.append(attempts)

        def attempt(request, *, timeout_s, cancel):
            cancel.check()
            attempts.append(request)
            payload = {"model": "scripted", "answers": {q.name: {"type": "noul", "noul": 0.95} for q in request.questions}}
            if charge == "reported":
                payload["usage"] = {"cost": 0.6}
            return AttemptResponse(200, compare.data(payload), 0, True)

        backend.attempt = attempt
        return backend

    monkeypatch.setattr(compare, "Scripted", priced_fixture)
    invoke_compare(monkeypatch, dataset, output, "--budget", "3.3")
    for arm, requests in zip(("direct", "compiled"), attempts_by_backend, strict=True):
        results = compare.load_lines(output / (arm + ".jsonl"))
        assert results[0]["complete"], results[0]
        assert not results[1]["complete"] and results[1]["outcome"] is None, results[1]
        assert sum(r["stats"]["requests"] for r in results) == len(requests) < 10
        assert sum(r["stats"]["dispatched_questions"] for r in results) == sum(len(r.questions) for r in requests)
        assert sum(r["stats"]["dispatched_payload_bytes"] for r in results) == sum(len(r.body) for r in requests)
        if charge == "reported":
            assert results[0]["stats"]["cost_usd"] == pytest.approx(3.0)
            assert results[1]["stats"]["requests"] == 0
        else:
            total_estimate = sum((len(r.body) + 32 * len(r.questions)) * 0.001 for r in requests)
            assert total_estimate <= 3.3
            assert results[0].get("trace", results[0]["stats"])["unknown_charge_attempts"] == 5


def test_cli_replay_requires_source_and_exact_profile(tmp_path, monkeypatch):
    dataset = tmp_path / "source-data"
    duplicate_dataset(dataset)
    original = tmp_path / "original"
    invoke_compare(monkeypatch, dataset, original)
    snapshot = tree_bytes(original)
    missing_source = tmp_path / "missing-source-output"
    with pytest.raises(SystemExit):
        invoke_compare(monkeypatch, dataset, missing_source, "--mode", "replay")
    assert not missing_source.exists()
    changed = tmp_path / "changed-band-output"
    with pytest.raises(SystemExit):
        invoke_compare(monkeypatch, dataset, changed, "--mode", "replay", "--replay-from", str(original), "--yes", "0.9")
    assert not changed.exists() and tree_bytes(original) == snapshot


def test_direct_missing_replay_never_creates_observation_directory(tmp_path):
    missing = tmp_path / "missing-recording"
    replay = compare.DirectCalls(Scripted(lambda *args: pytest.fail("replay dispatch")), missing, replay=True)
    assert not missing.exists()
    with pytest.raises(FileNotFoundError):
        compare.direct(fixture_rows(), replay, BAND)
    assert not missing.exists()
