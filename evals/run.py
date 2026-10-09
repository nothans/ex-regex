"""ex-regex against common regex baselines on authored synthetic development fixtures.

    python evals/run.py            # live: needs OPENROUTER_API_KEY (or another backend), about 1 cent
    python evals/run.py --backend local --model kev-4b

Three tasks, each scored against explicit fixture labels:

  contact   redact every way to contact a specific person (not shared, company, or no-reply
            addresses). Baseline: the usual email and phone regexes of a PII gate.
  refund    which support-ticket lines ask for a refund. Baseline: a keyword alternation.
  total     the total amount due on an invoice or receipt (or none). Baseline: a labeled-total regex.

Each ex-regex task runs two wordings: the plain one a developer would write first, and a more
specific one. The gap between them is the cost of question design, measured.
Writes evals/results/report-<date>.md (and raw JSONL beside it).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import exregex as ex  # noqa: E402
from exregex.cli import load_dotenv  # noqa: E402

# ------------------------------------------------------------------ baselines

RX_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RX_PHONE = re.compile(r"(?:\+\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}\b")
RX_REFUND = re.compile(r"\b(refund\w*|money back|reimburs\w*|charge ?back)\b", re.I)
RX_TOTAL = re.compile(
    r"(?:total(?:\s+(?:due|payable|amount))?|amount\s+(?:due|charged)|balance(?:\s+(?:due|owing))?)\s*[:.]*\s*"
    r"([$€£]\s?[\d.,]*\d|[\d.,]*\d\s?(?:USD|EUR|GBP))",
    re.I,
)

CONTACT_WORDINGS = {
    "plain": "a way to contact a specific person",
    "specific": "a way to reach one specific individual person directly (a shared team, company, sales, support, or no-reply address does not count)",
}
REFUND_WORDINGS = {
    "plain": "asks for a refund",
    "specific": "the writer is asking, now, to get money back for a purchase or charge (a refund, reimbursement, reversal, or chargeback); asking about refund policy, thanking for a past refund, or declining a refund does not count",
}
TOTAL_WORDINGS = {
    "plain": "the total amount due",
    "specific": "the final amount the customer still has to pay or was charged in total, after credits, payments, tax, and tips",
}


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (HERE / "data" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]


def locate(text: str, needle: str) -> tuple[int, int]:
    i = text.find(needle)
    if i < 0:
        raise ValueError(f"label {needle!r} not found in {text!r}")
    return i, i + len(needle)


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def covered(gold: tuple[int, int], pred: list[tuple[int, int]]) -> bool:
    """Every character of the gold span is inside some predicted span. For redaction, anything
    less leaks: blanking one character of an address still shows the address."""
    a, b = gold
    return all(any(p0 <= i < p1 for p0, p1 in pred) for i in range(a, b))


def score_spans(items: list[dict], predicted: dict[str, list[tuple[int, int]]]) -> dict:
    """Span scoring for redaction.

    recall      gold spans fully covered by the predictions (a partial cover counts as a miss)
    exact       gold spans predicted with exactly the right boundaries
    precision   predicted spans that land on a gold (or neutral) span, over all predicted spans
    leaked      snippets with at least one gold span not fully covered
    """
    covered_n = exact = fp = leaked = landed = 0
    n_gold = 0
    misses = []
    for it in items:
        gold = [locate(it["text"], g) for g in it["gold"]]
        neutral = [locate(it["text"], g) for g in it.get("neutral", [])]
        pred = predicted[it["id"]]
        n_gold += len(gold)
        full = [g for g in gold if covered(g, pred)]
        covered_n += len(full)
        exact += sum(1 for g in gold if g in pred)
        on_target = [p for p in pred if any(overlaps(p, g) for g in gold)]
        extra = [p for p in pred if not any(overlaps(p, g) for g in gold + neutral)]
        landed += len(on_target)
        fp += len(extra)
        if len(full) < len(gold):
            leaked += 1
        if len(full) < len(gold) or extra:
            misses.append(
                {
                    "id": it["id"],
                    "missed": [it["text"][a:b] for a, b in gold if (a, b) not in full],
                    "partial": [it["text"][a:b] for a, b in gold if (a, b) not in full and any(overlaps((a, b), p) for p in pred)],
                    "false": [it["text"][a:b] for a, b in extra],
                }
            )
    precision = landed / (landed + fp) if landed + fp else 1.0
    recall = covered_n / n_gold if n_gold else 1.0
    return {
        "precision": precision,
        "recall": recall,
        "exact": exact,
        "covered": covered_n,
        "n_gold": n_gold,
        "fp": fp,
        "fn": n_gold - covered_n,
        "leaked_items": leaked,
        "misses": misses,
    }


def score_labels(items: list[dict], predicted: dict[str, bool]) -> dict:
    tp = sum(1 for it in items if it["gold"] and predicted[it["id"]])
    fp = sum(1 for it in items if not it["gold"] and predicted[it["id"]])
    fn = sum(1 for it in items if it["gold"] and not predicted[it["id"]])
    correct = sum(1 for it in items if it["gold"] == predicted[it["id"]])
    misses = [{"id": it["id"], "gold": it["gold"], "text": it["text"]} for it in items if it["gold"] != predicted[it["id"]]]
    return {
        "accuracy": correct / len(items),
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "misses": misses,
    }


def score_values(items: list[dict], predicted: dict[str, str | None]) -> dict:
    def norm(v: str | None) -> str | None:
        return None if v is None else re.sub(r"\s+", "", v)

    correct = sum(1 for it in items if norm(it["gold"]) == norm(predicted[it["id"]]))
    misses = [
        {"id": it["id"], "gold": it["gold"], "got": predicted[it["id"]]} for it in items if norm(it["gold"]) != norm(predicted[it["id"]])
    ]
    return {"accuracy": correct / len(items), "misses": misses}


# ------------------------------------------------------------------ runs


def run_contact(engine: ex.Engine, dataset: str = "contact") -> dict:
    items = load(dataset)
    out: dict = {"n_items": len(items), "n_gold": sum(len(it["gold"]) for it in items)}
    base = {it["id"]: [m.span() for m in RX_EMAIL.finditer(it["text"])] + [m.span() for m in RX_PHONE.finditer(it["text"])] for it in items}
    out["regex"] = score_spans(items, base)
    for name, meaning in CONTACT_WORDINGS.items():
        pat = ex.compile(meaning, unit="contact", engine=engine)
        pred = {it["id"]: [m.span() for m in pat.findall(it["text"])] for it in items}
        out[f"ex-regex ({name})"] = score_spans(items, pred)
    # recall ceiling: what the loose finders propose at all, before Jev judges anything
    cand = {it["id"]: [(s.start, s.end) for s in ex.find_units(it["text"], "contact")] for it in items}
    out["finder ceiling"] = score_spans(items, cand)
    return out


def run_refund(engine: ex.Engine) -> dict:
    items = load("refund")
    texts = [it["text"] for it in items]
    out: dict = {"n_items": len(items), "n_gold": sum(it["gold"] for it in items)}
    out["regex"] = score_labels(items, {it["id"]: bool(RX_REFUND.search(it["text"])) for it in items})
    for name, meaning in REFUND_WORDINGS.items():
        ps = ex.compile(meaning, engine=engine).filter_p(texts)
        out[f"ex-regex ({name})"] = score_labels(items, {it["id"]: p >= 0.5 for it, p in zip(items, ps, strict=True)})
        out[f"ex-regex ({name})"]["p"] = {it["id"]: round(p, 3) for it, p in zip(items, ps, strict=True)}
    return out


def run_total(engine: ex.Engine) -> dict:
    items = load("total")
    out: dict = {"n_items": len(items)}
    base = {}
    for it in items:
        m = RX_TOTAL.search(it["text"])
        base[it["id"]] = m.group(1) if m else None
    out["regex"] = score_values(items, base)
    for name, what in TOTAL_WORDINGS.items():
        pred = {}
        for it in items:
            m = ex.extract(what, it["text"], unit="money", engine=engine)
            pred[it["id"]] = m.text if m else None
        out[f"ex-regex ({name})"] = score_values(items, pred)
    return out


def pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def text_hash(path: Path) -> str:
    """Hash UTF-8 source/data with CRLF normalized to LF for portable checkouts."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def provenance() -> dict:
    library = Path(ex.__file__).resolve().parent
    sources = {p.relative_to(library).as_posix(): text_hash(p) for p in sorted(library.rglob("*.py"))}
    questions = {"contact": CONTACT_WORDINGS, "refund": REFUND_WORDINGS, "total": TOTAL_WORDINGS}
    return {
        "package_version": ex.__version__,
        "fingerprint_schema": "lf-text-v1",
        "data_kind": "authored synthetic development fixtures; no independent human-label validation",
        "dataset_sha256": {
            name: text_hash(HERE / "data" / f"{name}.jsonl")
            for name in ("contact", "contact_holdout", "refund", "total")
        },
        "questions_sha256": hashlib.sha256(json.dumps(questions, sort_keys=True).encode()).hexdigest(),
        "runner_sha256": text_hash(Path(__file__)),
        "library_sha256": hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest(),
    }


def report(results: dict, engine: ex.Engine, seconds: float) -> str:
    b = engine.backend
    lines = [
        f"# ex-regex eval, {dt.date.today().isoformat()}",
        "",
        f"Backend: {b.name}, model `{b.model}`.",
        f"Spend: ${engine.stats.cost_usd:.4f} over {engine.stats.requests} requests ({engine.stats.cached} cached), {seconds:.1f} s wall time.",
        "Authored synthetic development fixtures and labels are in `evals/data/`; they are not an independent human-labeled benchmark.",
        "Redaction recall counts a span only when every character of it is covered; a partial cover is a miss and a leak.",
        "",
        "## Contact redaction",
        "",
        f"{results['contact']['n_items']} snippets, {results['contact']['n_gold']} spans that are a way to contact a specific person.",
        "",
        "| Method | Precision | Recall (full cover) | Exact boundaries | Spans not fully covered | Snippets that leak |",
        "|---|---|---|---|---|---|",
    ]
    for k, v in results["contact"].items():
        if isinstance(v, dict):
            lines.append(
                f"| {k} | {pct(v['precision'])} | {pct(v['recall'])} | {v['exact']}/{v['n_gold']} | {v['fn']} | {v['leaked_items']} |"
            )
    if "contact_holdout" in results:
        h = results["contact_holdout"]
        lines += [
            "",
            "### Additional contact cases",
            "",
            f"{h['n_items']} synthetic snippets, {h['n_gold']} spans. This is a regression set, not a blind holdout.",
            "",
            "| Method | Precision | Recall (full cover) | Exact boundaries | Spans not fully covered | Snippets that leak |",
            "|---|---|---|---|---|---|",
        ]
        for k, v in h.items():
            if isinstance(v, dict):
                lines.append(
                    f"| {k} | {pct(v['precision'])} | {pct(v['recall'])} | {v['exact']}/{v['n_gold']} | {v['fn']} | {v['leaked_items']} |"
                )
    lines += [
        "",
        "## Refund requests (grep by meaning)",
        "",
        f"{results['refund']['n_items']} ticket lines, {results['refund']['n_gold']} that ask for a refund.",
        "",
        "| Method | Accuracy | Precision | Recall |",
        "|---|---|---|---|",
    ]
    for k, v in results["refund"].items():
        if isinstance(v, dict):
            lines.append(f"| {k} | {pct(v['accuracy'])} | {pct(v['precision'])} | {pct(v['recall'])} |")
    lines += [
        "",
        "## Total due (extraction)",
        "",
        f"{results['total']['n_items']} invoices and receipts; one has nothing due and should return none.",
        "",
        "| Method | Exact value |",
        "|---|---|",
    ]
    for k, v in results["total"].items():
        if isinstance(v, dict):
            lines.append(f"| {k} | {pct(v['accuracy'])} |")
    lines += ["", "## Misses", ""]
    for task in [t for t in ("contact", "contact_holdout", "refund", "total") if t in results]:
        for k, v in results[task].items():
            if isinstance(v, dict) and v.get("misses"):
                lines.append(f"**{task}, {k}:**")
                for m in v["misses"]:
                    lines.append(f"- `{json.dumps({kk: vv for kk, vv in m.items() if kk != 'text'}, ensure_ascii=False)}`")
                lines.append("")
    if "provenance" in results:
        lines += ["## Reproduction", "", "```json", json.dumps(results["provenance"], indent=2), "```", ""]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend")
    ap.add_argument("--model")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    load_dotenv(HERE)
    if args.backend:
        os.environ["EXREGEX_BACKEND"] = args.backend
    if args.model:
        os.environ["EXREGEX_MODEL"] = args.model
    engine = ex.Engine(cache=False if args.no_cache else HERE / ".cache" / "answers.sqlite", max_cost_usd=0.25)
    t0 = time.perf_counter()
    results = {
        "provenance": provenance(),
        "contact": run_contact(engine),
        "contact_holdout": run_contact(engine, "contact_holdout"),
        "refund": run_refund(engine),
        "total": run_total(engine),
    }
    seconds = time.perf_counter() - t0
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = dt.date.today().isoformat()
    tag = f"{engine.backend.name}-{engine.backend.model}".replace("/", "_")
    md = report(results, engine, seconds)
    (out_dir / f"report-{stamp}-{tag}.md").write_text(md, encoding="utf-8")
    (out_dir / f"raw-{stamp}-{tag}.jsonl").write_text(json.dumps(results, ensure_ascii=False) + "\n", encoding="utf-8")
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
