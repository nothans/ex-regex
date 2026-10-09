"""Score exact capture identities with honest abstention and incomplete-run denominators.

Rejects pending/unattributed labels. Scores Scripted output only when explicitly requested,
and marks such output as a mechanics exercise rather than a model-quality result.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path


def load(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("duplicate conversation IDs")
    return {row["id"]: row for row in rows}


def tuples(rows):
    result = []
    for triple in rows:
        if not isinstance(triple, list) or len(triple) != 3 or any(not isinstance(pair, list) or len(pair) != 2 for pair in triple):
            raise ValueError("capture must be three ordered [record_id, revision] pairs")
        result.append(tuple(tuple(pair) for pair in triple))
    return Counter(result)


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1)]


def f1(tp, fp, fn):
    return ratio(2 * tp, 2 * tp + fp + fn)


def bootstrap(counts, *, rounds=1000):
    if not counts:
        return None
    rng = random.Random(20261008)
    scores = []
    for _ in range(rounds):
        selected = [rng.choice(counts) for _ in counts]
        score = f1(*(sum(c[i] for c in selected) for i in range(3)))
        if score is not None:
            scores.append(score)
    return [percentile(scores, 0.025), percentile(scores, 0.975)]


def atomic_key(row):
    return json.dumps([row["name"], row["records"]], separators=(",", ":"))


def score(predictions, labels, *, broad=False):
    if set(predictions) != set(labels):
        raise ValueError("prediction/label IDs differ; missing runs must be recorded as failures")
    confusion = Counter()
    totals = Counter()
    capture_counts = []
    calibration = defaultdict(list)
    families = Counter()
    review_modes = Counter()
    for cid, label in labels.items():
        if label.get("status") not in ("adjudicated", "single_annotator") or not label.get("reviewer"):
            raise ValueError(f"{cid}: human labels are pending or lack reviewer provenance")
        review_modes[label["status"]] += 1
        if label.get("presence") not in ("yes", "no", "ambiguous"):
            raise ValueError(f"{cid}: invalid reference presence")
        pred = predictions[cid]
        if pred["input_hash"] != label["input_hash"]:
            raise ValueError(f"{cid}: label input hash differs")
        if pred.get("outcome") not in ("yes", "no", "unknown", None) or type(pred.get("complete")) is not bool:
            raise ValueError(f"{cid}: invalid execution result")
        if not pred["complete"] and pred["outcome"] is not None:
            raise ValueError(f"{cid}: incomplete execution must not assert a complete outcome")
        if pred["complete"] and pred["outcome"] is None:
            raise ValueError(f"{cid}: complete execution must have a semantic outcome")
        families.update(label.get("families", []))
        totals["conversations"] += 1
        if not pred["complete"]:
            totals["execution_failures"] += 1
        elif pred["outcome"] in ("yes", "no") and not pred.get("uncertain"):
            totals["fully_resolved"] += 1
        reference = label["presence"]
        if reference == "ambiguous":
            totals["ambiguous_references"] += 1
            totals["ambiguous_accepted_presence"] += pred["outcome"] == "yes"
        else:
            totals["evaluable"] += 1
            if pred["complete"] and pred["outcome"] in ("yes", "no"):
                confusion[f"reference_{reference}/prediction_{pred['outcome']}"] += 1
                totals["definitive"] += 1
            elif pred["complete"]:
                totals["semantic_abstentions"] += 1
        if not broad:
            gold, ambiguous, accepted = tuples(label["captures"]), tuples(label.get("ambiguous_captures", [])), tuples(pred["matches"])
            if any(n != 1 for n in gold.values()) or gold.keys() & ambiguous.keys():
                raise ValueError(f"{cid}: duplicate or conflicting reference captures")
            if (reference == "no" and gold) or (reference == "yes" and not gold):
                raise ValueError(f"{cid}: reference presence conflicts with capture labels")
            true = sum((gold & accepted).values())
            ambiguous_accepted = sum((ambiguous & (accepted - gold)).values())
            # An entirely ambiguous reference has no trustworthy negatives either.
            if reference == "ambiguous":
                totals["accepted_on_ambiguous_conversations"] += sum(accepted.values())
            else:
                false = sum(accepted.values()) - true - ambiguous_accepted
                missed = sum(gold.values()) - true
                totals.update(tp=true, fp=false, fn=missed, accepted_ambiguous=ambiguous_accepted)
                capture_counts.append((true, false, missed))
        atomic = {atomic_key(a): a["label"] for a in label.get("atomic_labels", []) if a.get("label") in (0, 1)}
        observations = {atomic_key(a): a for a in pred.get("observations", [])}
        for key, truth in atomic.items():
            if key in observations:
                observation = observations[key]
                p = observation["p"]
                if type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1:
                    raise ValueError("invalid probability in observation")
                calibration[observation["name"]].append((p, truth))
            else:
                totals["unobserved_labeled_atoms"] += 1
    calibration_report = {}
    for name, pairs in calibration.items():
        bins = []
        for i in range(10):
            chosen = [(p, y) for p, y in pairs if min(int(p * 10), 9) == i]
            bins.append({"range": [i / 10, (i + 1) / 10], "n": len(chosen), "mean_p": ratio(sum(p for p, _ in chosen), len(chosen)), "frequency": ratio(sum(y for _, y in chosen), len(chosen))})
        curve = []
        for threshold in (0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.99):
            chosen = [(p, y) for p, y in pairs if p >= threshold or p <= 1 - threshold]
            curve.append({"certainty": threshold, "coverage": len(chosen) / len(pairs), "error_rate": ratio(sum((p >= 0.5) != y for p, y in chosen), len(chosen))})
        calibration_report[name] = {"n": len(pairs), "brier": sum((p - y) ** 2 for p, y in pairs) / len(pairs), "bins": bins, "atomic_risk_coverage": curve}
    precision = ratio(totals["tp"], totals["tp"] + totals["fp"])
    recall = ratio(totals["tp"], totals["tp"] + totals["fn"])
    performance = {"wall_ms": {"p50": percentile([p["wall_ms"] for p in predictions.values()], 0.5), "p95": percentile([p["wall_ms"] for p in predictions.values()], 0.95)}}
    for metric in ("requests", "cached", "retries", "input_tokens", "cost_usd", "dispatched_questions", "dispatched_payload_bytes"):
        performance[metric] = sum(p.get("stats", {}).get(metric, 0) for p in predictions.values())
    performance["peak_candidates"] = max((p.get("candidates", 0) for p in predictions.values()), default=0)
    performance["accepted_results"] = sum(len(p.get("matches") or []) for p in predictions.values())
    performance["uncertain_results"] = sum(len(p.get("uncertain") or []) for p in predictions.values())
    performance["source_records"] = sum(p.get("source_records", 0) for p in predictions.values())
    performance["source_utf8_bytes"] = sum(p.get("source_utf8_bytes", 0) for p in predictions.values())
    performance["returned_models"] = sorted({m for p in predictions.values() for m in p.get("returned_models", [])})
    performance["observed_peak_concurrency"] = max((p.get("observed_peak_concurrency", 0) for p in predictions.values()), default=0)
    performance["unknown_charge_attempts"] = sum(p.get("trace", p.get("stats", {})).get("unknown_charge_attempts", 0) for p in predictions.values())
    conservative_denominator = totals["tp"] + totals["fp"] + totals["accepted_ambiguous"] + totals["accepted_on_ambiguous_conversations"]
    return {
        "counts": dict(totals), "review_modes": dict(review_modes), "families": dict(families), "confusion": dict(confusion),
        "decision_coverage": ratio(totals["definitive"], totals["evaluable"]),
        "fully_resolved_coverage": ratio(totals["fully_resolved"], totals["conversations"]),
        "captures": None if broad else {"precision": precision, "recall": recall, "f1": f1(totals["tp"], totals["fp"], totals["fn"]),
                                         "conservative_precision": ratio(totals["tp"], conservative_denominator), "f1_bootstrap_95pct_by_conversation": bootstrap(capture_counts)},
        "calibration_observed_atoms_only": calibration_report, "performance": performance,
        "caveats": ["Calibration excludes unevaluated atoms; missing-label and unobserved counts must accompany curves.", "Do not tune a band from test risk/coverage curves.", "Single-annotator results are pilots, not adjudicated reference truth.", "Broad arm has no capture score; its fully resolved coverage means presence resolution only."],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("labels", type=Path)
    parser.add_argument("--allow-scripted", action="store_true")
    args = parser.parse_args()
    environment = json.loads((args.run / "environment.json").read_text(encoding="utf-8"))
    if (environment["mode"] == "scripted" or environment.get("backend") == "scripted") and not args.allow_scripted:
        parser.error("Scripted answers cannot establish model quality; --allow-scripted is only for scorer testing")
    labels = load(args.labels)
    reports = {p.stem: score(load(p), labels, broad=p.stem == "broad") for p in sorted(args.run.glob("*.jsonl"))}
    print(json.dumps({"evidence": environment, "reports": reports}, indent=2))


if __name__ == "__main__":
    main()
