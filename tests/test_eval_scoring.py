"""The public eval's scorer: redaction counts only when every character is covered."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("public_eval", Path(__file__).resolve().parents[1] / "evals" / "run.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)

ADDRESS = "fixture_quasar_7k9@example.invalid"
ITEMS = [{"id": "x", "text": f"mail {ADDRESS} now", "gold": [ADDRESS]}]
START = ITEMS[0]["text"].index(ADDRESS)
END = START + len(ADDRESS)


def score(pred):
    return pub.score_spans(ITEMS, {"x": pred})


def test_partial_cover_is_a_leak():
    r = score([(START, START + 1)])
    assert r["recall"] == 0 and r["leaked_items"] == 1 and r["exact"] == 0 and r["misses"][0]["partial"] == [ADDRESS]


def test_exact_and_full_covers():
    assert score([(START, END)])["recall"] == 1 and score([(START, END)])["exact"] == 1
    wide = score([(0, len(ITEMS[0]["text"]))])
    assert wide["recall"] == 1 and wide["exact"] == 0 and wide["leaked_items"] == 0
    split = score([(START, START + 7), (START + 7, END)])
    assert split["recall"] == 1 and split["leaked_items"] == 0


def test_false_positives_count_against_precision():
    office = "support@comet-tools.example.invalid"
    text = f"call {office} or {ADDRESS}"
    items = [{"id": "y", "text": text, "gold": [ADDRESS]}]
    spans = [(text.index(value), text.index(value) + len(value)) for value in (office, ADDRESS)]
    r = pub.score_spans(items, {"y": spans})
    assert r["recall"] == 1 and r["fp"] == 1 and r["precision"] == 0.5
