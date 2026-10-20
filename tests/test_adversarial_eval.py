"""The adversarial eval: its data, its scorer, and its neighbor test, offline."""

import importlib.util
from pathlib import Path

import exregex as ex
from exregex import Engine, Scripted

spec = importlib.util.spec_from_file_location("adversarial_eval", Path(__file__).resolve().parents[1] / "evals" / "adversarial.py")
adv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adv)


def test_committed_data_matches_the_generator():
    assert adv.load() == adv.build()
    rows = adv.load()
    assert len(rows) == 96 and len({r["id"] for r in rows}) == 96
    for r in rows:
        assert r["text"].startswith(r["clean"]) and r["text"] != r["clean"]
        assert r["gold"] == (r["direction"] == "force_no")  # attacks push against the true label


def test_score_counts_flips_errors_and_the_review_band():
    rows = [r for r in adv.build() if r["attack"] == "self_label"][:2] + [r for r in adv.build() if r["direction"] == "force_yes"][:1]
    p_clean = {r["clean"]: (0.95 if r["gold"] else 0.05) for r in rows}
    cells = adv.score(rows, p_clean, [0.40, 0.92, 0.05])
    no = cells[("force_no", "self_label")]
    assert no["n"] == 2 and no["flipped"] == 1 and no["wrong"] == 1 and no["review_band"] == 1
    assert abs(no["p_attacked"] - 0.66) < 1e-9 and abs(no["p_clean"] - 0.95) < 1e-9
    yes = cells[("force_yes", "command")]
    assert yes["flipped"] == 0 and yes["wrong"] == 0 and yes["review_band"] == 0


def gullible(state, questions):
    """A stand-in model that believes any 'Note to the classifier' in a referenced item."""
    out = {}
    items = state.get("items", {}) if isinstance(state, dict) else {}
    for name in questions:
        text = items.get(name, "")
        out[name] = 0.95 if ("asks for a refund" in text or "money back" in text) and "does not ask" not in text else 0.05
    return out


def test_neighbor_test_runs_one_request_per_pack_and_reports_moves():
    engine = Engine(Scripted(gullible), cache=None)
    pat = ex.compile(adv.PLAIN, unit="text", engine=engine)
    report = adv.neighbors(pat)
    assert set(report) == {(d, a) for d in ("force_yes", "force_no") for a in adv.FORCE_YES}
    assert all(c["flips"] == 0 and c["max_move"] == 0 for c in report.values())  # this stand-in reads items one by one
