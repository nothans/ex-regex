"""Freeze a provenance-bearing, overlap-grouped support corpus without importing source labels."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = "https://zissou.infosci.cornell.edu/convokit/datasets/ubuntu-chat-logs/ubuntu-chat-logs.zip"
CARD = "https://convokit.cornell.edu/documentation/chatlogs.html"
SOURCE_SHA256 = "03fe5f7a8e8f3d8a7c4cca083a7fc85433ca163d0ba8672469eab37e9139e9a2"
VERSION = "ubuntu-failed-remedy-v1"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def jsonl(path, rows):
    path.write_bytes(b"".join(encoded(row) + b"\n" for row in rows))


def split_groups(groups, *, target=100):
    """Deterministic subset sum keeps shared-source-message components together."""
    groups = sorted((sorted(g) for g in groups), key=lambda g: sha([VERSION, g]))
    possible = {0: []}
    for group in groups:
        for size, chosen in sorted(list(possible.items()), reverse=True):
            if size + len(group) <= target:
                possible.setdefault(size + len(group), [*chosen, *group])
    if target not in possible:
        raise ValueError("cannot form the required split without breaking source-overlap groups")
    return set(possible[target])


def prepare(archive: Path, output: Path):
    if output.exists():
        raise ValueError("output exists; use a new directory to avoid replacing frozen inputs or annotations")
    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    if archive_hash != SOURCE_SHA256:
        raise ValueError("archive differs from the reviewed source; inspect and version preprocessing first")
    with zipfile.ZipFile(archive) as z:
        # Read exactly two bounded members, never extract archive paths.
        names = ("ubuntu-chat-logs/conversations.json", "ubuntu-chat-logs/utterances.jsonl")
        if any(z.getinfo(name).file_size > 20_000_000 for name in names):
            raise ValueError("unexpected source size")
        conversations = json.loads(z.read(names[0]))
        source = [json.loads(line) for line in z.read(names[1]).splitlines()]
    if len(conversations) != 200 or len(source) != 7950:
        raise ValueError("source dimensions changed")
    groups = defaultdict(list)
    parent = {cid: cid for cid in conversations}

    def root(cid):
        while parent[cid] != cid:
            cid = parent[cid]
        return cid

    source_messages = {}
    ids = set()
    for row in source:
        cid = row["conversation_id"]
        if row["id"] in ids or cid not in conversations:
            raise ValueError("duplicate ID or unknown conversation")
        ids.add(row["id"])
        groups[cid].append(row)
        identity = (row["speaker"], row["timestamp"], row["text"])
        if identity in source_messages:
            parent[root(cid)] = root(source_messages[identity])
        source_messages[identity] = cid
    components = defaultdict(list)
    for cid in conversations:
        components[root(cid)].append(cid)
    dev = split_groups(components.values())
    anchor = datetime(2000, 1, 1, tzinfo=timezone.utc)
    normalized = {"dev": [], "test": []}
    nonfinite_elapsed = 0
    elapsed_disagreements = 0
    by_split_speakers = {"dev": set(), "test": set()}
    for cid in sorted(conversations):
        meta = conversations[cid]
        messages = []
        previous = -1.0
        origin = datetime.fromisoformat(groups[cid][0]["timestamp"])
        for row in groups[cid]:
            elapsed = (datetime.fromisoformat(row["timestamp"]) - origin).total_seconds() / 60
            supplied_elapsed = row["meta"]["time_elapsed"]
            if not math.isfinite(supplied_elapsed):
                nonfinite_elapsed += 1
            elif abs(supplied_elapsed - elapsed) > 1e-6:
                elapsed_disagreements += 1
            if elapsed < previous:
                raise ValueError("nonmonotonic source timestamps")
            previous = elapsed
            speaker = row["speaker"]
            if speaker not in (meta["role_A"], meta["role_B"]):
                raise ValueError("unknown role")
            messages.append({
                "id": row["id"], "revision": sha([row["text"], elapsed, speaker]),
                "ticket_id": cid, "role": "customer" if speaker == meta["role_A"] else "agent",
                "text": row["text"], "sent_at": (anchor + timedelta(minutes=elapsed)).isoformat(),
                "source_timestamp": row["timestamp"], "source_speaker": speaker,
            })
        split = "dev" if cid in dev else "test"
        by_split_speakers[split].update((meta["role_A"], meta["role_B"]))
        normalized[split].append({"id": cid, "source": "ubuntu-chat-logs", "messages": messages, "input_hash": sha(messages)})
    output.mkdir(parents=True)
    files = {}
    for split, items in normalized.items():
        path = output / f"{split}.jsonl"
        jsonl(path, items)
        files[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        for reviewer in ("a", "b"):
            jsonl(output / f"{split}.labels-{reviewer}.jsonl", [{
                "id": item["id"], "input_hash": item["input_hash"], "reviewer": None,
                "status": "pending", "presence": None, "captures": [], "ambiguous_captures": [],
                "atomic_labels": [], "families": [], "notes": "",
            } for item in items])
    manifest = {
        "schema": VERSION, "source": SOURCE, "source_sha256": archive_hash, "card": CARD,
        "license": "CC BY 4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": "Sarkar, Srikanth, Hudson, Rudinger, Bonial, Resnik (2025), arXiv:2503.12370; ConvoKit conversion by Axel Bax",
        "preprocessing": "Preserve file order and text. A=customer, B=agent. Exclude all friction/success/model annotations. Compute elapsed minutes from naive source timestamps and anchor at synthetic 2000-01-01 UTC; retain source timestamps for provenance. Original timezone is unknown.",
        "source_card_discrepancy": "Downloaded timestamps are naive date-time strings, not the sequence indices described by the card. Use differences within each conversation; do not claim observed UTC times.",
        "nonfinite_supplied_elapsed": nonfinite_elapsed,
        "supplied_elapsed_disagreements": elapsed_disagreements,
        "split": "100 development / 100 test, SHA-256 ordered component subset sum; conversations sharing source speaker+timestamp+text stay together",
        "overlap_components": sorted([sorted(g) for g in components.values() if len(g) > 1]),
        "speaker_overlap_count": len(by_split_speakers["dev"] & by_split_speakers["test"]),
        "limitation": "One source/domain; not a source-held-out or necessarily speaker-disjoint trial. Task labels remain pending human review.",
        "counts": {s: {"conversations": len(xs), "messages": sum(len(x["messages"]) for x in xs)} for s, xs in normalized.items()},
        "files": files,
    }
    (output / "manifest.json").write_bytes(encoded(manifest) + b"\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.archive, args.output), indent=2))


if __name__ == "__main__":
    main()
