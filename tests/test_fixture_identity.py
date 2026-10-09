"""Keep published synthetic contact scenarios on reserved example domains."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from exregex import find_units

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name,count,spans", [("contact", 30, 18), ("contact_holdout", 12, 6)])
def test_contact_fixtures_are_fictional_and_keep_exact_labels(name, count, spans):
    rows = [json.loads(line) for line in (ROOT / "evals/data" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == count and sum(len(row["gold"]) for row in rows) == spans
    for row in rows:
        assert all(value in row["text"] for value in row["gold"] + row.get("neutral", []))
        proposed = {span.text for span in find_units(row["text"], "contact")}
        assert set(row["gold"]) <= proposed
        for email in find_units(row["text"], "email"):
            normalized = re.sub(r"[\[\]()]", " ", email.text.lower())
            normalized = re.sub(r"\s+at\s+", "@", normalized)
            normalized = re.sub(r"\s+dot\s+", ".", normalized)
            domain = normalized.rsplit("@", 1)[1].strip()
            reserved = ("example.invalid", "example.com", "example.net", "example.org")
            assert any(domain == suffix or domain.endswith("." + suffix) for suffix in reserved)
        assert all(span.text.startswith("@fixture_") for span in find_units(row["text"], "handle"))


def test_text_eval_records_current_data_and_library_identity(tmp_path):
    import hashlib

    spec = importlib.util.spec_from_file_location("fixture_eval", ROOT / "evals/run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = module.provenance()
    content = (ROOT / "evals/data/contact.jsonl").read_bytes().replace(b"\r\n", b"\n")
    assert identity["dataset_sha256"]["contact"] == hashlib.sha256(content).hexdigest()
    assert identity["fingerprint_schema"] == "lf-text-v1"
    lf, crlf = tmp_path / "lf.jsonl", tmp_path / "crlf.jsonl"
    lf.write_bytes(content)
    crlf.write_bytes(content.replace(b"\n", b"\r\n"))
    assert module.text_hash(lf) == module.text_hash(crlf)
    assert set(identity["dataset_sha256"]) == {"contact", "contact_holdout", "refund", "total"}
    assert len(identity["questions_sha256"]) == len(identity["runner_sha256"]) == len(identity["library_sha256"]) == 64
