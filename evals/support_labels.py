"""Validate human annotations and compare independent reviews without exposing model predictions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from support_compare import capture, load_lines, messages, structural


def validate(items, labels):
    sources = {row["id"]: row for row in items}
    if len(labels) != len(sources) or {r["id"] for r in labels} != set(sources):
        raise ValueError("labels must cover every source conversation exactly once")
    problems = []
    for row in labels:
        item = sources[row["id"]]
        if row["input_hash"] != item["input_hash"]:
            raise ValueError(f"{row['id']}: input hash changed")
        if row.get("status") not in ("adjudicated", "single_annotator", "reviewed") or not row.get("reviewer"):
            problems.append({"id": row["id"], "issue": "human review pending"})
            continue
        if row.get("presence") not in ("yes", "no", "ambiguous"):
            raise ValueError(f"{row['id']}: invalid presence label")
        rows = messages(item)
        allowed = {json.dumps(capture(rows, t)) for t in structural(rows)}
        definite = {json.dumps(t) for t in row.get("captures", [])}
        ambiguous = {json.dumps(t) for t in row.get("ambiguous_captures", [])}
        if len(definite) != len(row.get("captures", [])) or definite & ambiguous:
            raise ValueError(f"{row['id']}: duplicate/conflicting capture labels")
        if (definite | ambiguous) - allowed:
            raise ValueError(f"{row['id']}: capture has wrong identity/revision, role, order, gap or time bounds")
        if (row["presence"] == "yes") != bool(definite):
            raise ValueError(f"{row['id']}: presence and definite captures disagree")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("labels_a", type=Path)
    parser.add_argument("--labels-b", type=Path)
    args = parser.parse_args()
    items, first = load_lines(args.data), load_lines(args.labels_a)
    report = {"review_a": validate(items, first)}
    if args.labels_b:
        second = load_lines(args.labels_b)
        report["review_b"] = validate(items, second)
        by_id = {r["id"]: r for r in second}
        disagreements = []
        for a in first:
            b = by_id[a["id"]]
            if a.get("reviewer") and a.get("reviewer") == b.get("reviewer"):
                raise ValueError("independent review requires two different named reviewers")
            for key in ("presence", "captures", "ambiguous_captures", "atomic_labels"):
                left, right = a.get(key), b.get(key)
                if isinstance(left, list) and isinstance(right, list):
                    left, right = sorted(map(json.dumps, left)), sorted(map(json.dumps, right))
                if left != right:
                    disagreements.append({"id": a["id"], "field": key})
        report["disagreements_requiring_human_adjudication"] = disagreements
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
