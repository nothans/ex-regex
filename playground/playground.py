"""The ex-regex playground's Python side.

One function, run(feature, params, engine), drives every station of the playground and returns
plain JSON. The same module runs in CPython when the demo answers are recorded and in Pyodide
when they are replayed, so a recorded call and a browser call ask exactly the same questions.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

import exregex as ex
from exregex import semantic as sx

VERSION = ex.__version__


# ------------------------------------------------------------------ shapes


def _match(m: Any) -> dict:
    return {"text": m.text, "start": m.start, "end": m.end, "p": round(m.p, 4), "band": m.band, "unit": m.unit}


def _span(s: Any) -> dict:
    return {"text": s.text, "start": s.start, "end": s.end, "unit": s.unit}


def _finder(regex: str) -> Callable[[str], list]:
    """A unit from a regex: your pattern proposes the spans, the meaning decides."""
    compiled = re.compile(regex)

    def spans(text: str) -> list:
        return [m.span() for m in compiled.finditer(text)]

    spans.__name__ = "your_regex"
    return spans


def _unit(params: dict) -> Any:
    if params.get("finder"):
        return _finder(params["finder"])
    unit = params.get("unit") or "sentence"
    if isinstance(unit, list):
        return tuple(unit)
    return unit


def _options(params: dict) -> dict:
    """Pattern options the stations share; only the ones that are set are passed."""
    out: dict = {"unit": _unit(params)}
    if params.get("threshold") is not None:
        out["threshold"] = float(params["threshold"])
    for key in ("context", "question", "prefilter"):
        if params.get(key):
            out[key] = params[key]
    return out


# ------------------------------------------------------------------ stations


def _test(p: dict, engine: Any) -> dict:
    kwargs = {k: v for k, v in _options(p).items() if k != "unit"}
    verdict = ex.test(p["meaning"], p["text"], engine=engine, **kwargs)
    return {"p": round(verdict.p, 4), "band": verdict.band, "match": bool(verdict), "threshold": verdict.threshold}


def _find(p: dict, engine: Any) -> dict:
    mode = p.get("mode", "findall")
    pattern = ex.compile(p["meaning"], engine=engine, **_options(p))
    if mode == "search":
        m = pattern.search(p["text"])
        matches = [_match(m)] if m else []
    else:
        matches = [_match(m) for m in pattern.findall(p["text"])]
    return {"matches": matches, "candidates": [_span(s) for s in ex.find_units(p["text"], pattern.unit)]}


def _scan(p: dict, engine: Any) -> dict:
    pattern = ex.compile(p["meaning"], engine=engine, **_options(p))
    return {"spans": [_match(m) for m in pattern.scan(p["text"])]}


def _sub(p: dict, engine: Any) -> dict:
    pattern = ex.compile(p["meaning"], engine=engine, **_options(p))
    repl: Any = p.get("repl", "[redacted]")
    if p.get("repl_mode") == "unit":
        repl = lambda m: f"[{m.unit}]"  # noqa: E731
    text, count = pattern.subn(repl, p["text"])
    return {"text": text, "count": count, "matches": [_match(m) for m in pattern.findall(p["text"])]}


def _split(p: dict, engine: Any) -> dict:
    pattern = ex.compile(p["meaning"], engine=engine, **_options(p))
    return {"pieces": pattern.split(p["text"], keep=bool(p.get("keep"))), "matches": [_match(m) for m in pattern.findall(p["text"])]}


def _extract(p: dict, engine: Any) -> dict:
    unit = _unit(p)
    kwargs = {"context": p["context"]} if p.get("context") else {}
    if p.get("all"):
        found = ex.extractall(p["what"], p["text"], unit=unit, engine=engine, **kwargs)
        return {"matches": [_match(m) for m in found], "candidates": [_span(s) for s in ex.find_units(p["text"], unit)]}
    m = ex.extract(p["what"], p["text"], unit=unit, engine=engine, **kwargs)
    return {"matches": [_match(m)] if m else [], "candidates": [_span(s) for s in ex.find_units(p["text"], unit)]}


def _units(p: dict, engine: Any) -> dict:
    unit = _unit(p)
    return {"spans": [_span(s) for s in ex.find_units(p["text"], unit)]}


def _classify(p: dict, engine: Any) -> dict:
    options = {o["label"]: (o.get("description") or None) for o in p["options"]}
    kwargs = {"context": p["context"]} if p.get("context") else {}
    pick = ex.classify(p["text"], options, engine=engine, **kwargs)
    return {"label": pick.label, "p": round(pick.p, 4), "probabilities": {k: round(v, 4) for k, v in pick.probabilities.items()}}


def _rate(p: dict, engine: Any) -> dict:
    rating = ex.rate(p["text"], p["question"], p["levels"], engine=engine)
    return {
        "score": round(rating.score, 4),
        "level": rating.level,
        "label": rating.label,
        "probabilities": {str(k): round(v, 4) for k, v in rating.probabilities.items()},
    }


def _many(p: dict, engine: Any) -> dict:
    pattern = ex.compile(p["meaning"], engine=engine, **{k: v for k, v in _options(p).items() if k != "unit"})
    ranked = pattern.rank(p["texts"])
    keep = set(pattern.filter(p["texts"]))
    return {"ranked": [{"text": t, "p": round(v, 4), "kept": t in keep} for t, v in ranked]}


def _diff(p: dict, engine: Any) -> dict:
    d = ex.diff(p["meaning"], p["regex"], p["text"], unit=_unit(p), engine=engine)
    return {
        "regex_only": [_match(m) for m in d.regex_only],
        "meaning_only": [_match(m) for m in d.meaning_only],
        "agree": d.agree,
        "clean": d.clean,
    }


def _classic(p: dict, engine: Any) -> dict:
    """One famous regex problem, asked both ways over the same cases."""
    regex = re.compile(p["regex"])
    rows = []
    for case in p["cases"]:
        text, truth = case["text"], bool(case["truth"])
        by_regex = bool(regex.search(text)) if p.get("regex_mode") == "search" else bool(regex.fullmatch(text))
        if p.get("regex_means") == "reject":
            by_regex = not by_regex
        verdict = ex.test(p["meaning"], text, engine=engine)
        rows.append({
            "text": text, "truth": truth, "regex": by_regex,
            "meaning": bool(verdict), "p": round(verdict.p, 4), "band": verdict.band,
        })
    score = lambda key: sum(r[key] == r["truth"] for r in rows)  # noqa: E731
    return {"rows": rows, "regex_right": score("regex"), "meaning_right": score("meaning"), "total": len(rows)}


def _semantic(p: dict, engine: Any) -> dict:
    """A predicate, an exact field guard, and a band, evaluated per record."""
    asks = sx.Predicate[dict](
        p["name"],
        version=1,
        question=p["question"],
        fields={"body": ("text",)},
    )
    expression = sx.field("status").eq(p["status_required"]) & asks if p.get("guard") else asks
    band = sx.Band(no=float(p["band_no"]), yes=float(p["band_yes"]))
    rows = []
    for record in p["records"]:
        query = expression.bind(record, key=record["id"])
        result = engine.evaluate(query, band=band)
        leaves = []
        for leaf in result.leaves:
            leaves.append({"definition": leaf.definition.name, "p": round(_leaf_p(leaf), 4), "outcome": leaf.outcome.name})
        outcome = result.outcome.name if result.outcome is not None else "INCOMPLETE"
        rows.append({
            "id": record["id"], "status": record["status"], "text": record["text"],
            "outcome": outcome, "complete": result.complete,
            "pruned": len(result.trace.pruned_nodes), "leaves": leaves,
        })
    return {"rows": rows}


def _leaf_p(leaf: Any) -> float:
    answer = getattr(leaf, "answer", None)
    for name in ("p", "probability", "yes"):
        value = getattr(answer, name, None)
        if isinstance(value, (int, float)):
            return float(value)
    value = getattr(leaf, "p", None)
    return float(value) if isinstance(value, (int, float)) else float("nan")


STATIONS: dict[str, Callable[[dict, Any], dict]] = {
    "test": _test,
    "find": _find,
    "scan": _scan,
    "sub": _sub,
    "split": _split,
    "extract": _extract,
    "units": _units,
    "classify": _classify,
    "rate": _rate,
    "many": _many,
    "diff": _diff,
    "classic": _classic,
    "semantic": _semantic,
}

# Stations that never ask a model.
OFFLINE = {"units"}


def run(station: str, params: dict, engine: Any) -> dict:
    t0 = time.perf_counter()
    result = STATIONS[station](params, engine)
    result["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return result


def run_json(station: str, params_json: str, engine: Any) -> str:
    return json.dumps(run(station, json.loads(params_json), engine), ensure_ascii=False)
