"""Record the playground's demo answers: every preset, asked live, kept in a decision lockfile.

    uv venv .venv-playground && uv pip install --python .venv-playground "ex-regex==0.1.0a1"
    .venv-playground/Scripts/python playground/record.py

Needs OPENROUTER_API_KEY. The lockfile (decisions.jsonl) is what the playground replays offline,
so every built-in example shows a real Jev answer without a key. Run it again after changing a
preset; recorded decisions are reused and only new questions cost anything.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import exregex as ex  # noqa: E402
import playground  # noqa: E402

MODEL = "typesafe/jev-1.13"


def main() -> int:
    if ex.__version__ != "0.1.0a1":
        print(f"record with the published 0.1.0a1, not {ex.__version__}: the playground installs that release", file=sys.stderr)
        return 2
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    lockfile = HERE / "decisions.jsonl"
    presets = json.loads((HERE / "presets.json").read_text(encoding="utf-8"))
    engine = ex.Engine(ex.openrouter(model=MODEL), cache=str(lockfile), max_cost_usd=0.05)
    failures = 0
    for station, items in presets.items():
        if station in playground.OFFLINE:
            continue
        for item in items:
            try:
                result = playground.run(station, item["params"], engine)
                print(f"{station:9} {item['title']:40} ok  {summary(result)}")
            except Exception as error:  # report every preset, then fail
                failures += 1
                print(f"{station:9} {item['title']:40} ERROR {type(error).__name__}: {error}")
    # The console snippets ask real questions too; run them so they replay offline.
    for snippet in json.loads((HERE / "snippets.json").read_text(encoding="utf-8")):
        try:
            exec(snippet["code"], {"ex": ex, "engine": engine})
            print(f"{'console':9} {snippet['title']:40} ok")
        except Exception as error:
            failures += 1
            print(f"{'console':9} {snippet['title']:40} ERROR {type(error).__name__}: {error}")
    engine.close()
    print(json.dumps(engine.stats.as_dict()))
    return 1 if failures else 0


def summary(result: dict) -> str:
    if "p" in result:
        return f"p={result['p']} {result.get('band', result.get('label', ''))}"
    if "matches" in result:
        return ", ".join(f"{m['text'][:28]!r}@{m['p']}" for m in result["matches"]) or "no matches"
    if "rows" in result and "regex_right" in result:
        return f"regex {result['regex_right']}/{result['total']}, meaning {result['meaning_right']}/{result['total']}"
    if "rows" in result:
        return ", ".join(f"{r['id']}={r['outcome']}" for r in result["rows"])
    if "ranked" in result:
        return ", ".join(f"{r['p']}" for r in result["ranked"])
    if "spans" in result:
        return ", ".join(f"{s['p']}" for s in result["spans"])
    if "label" in result:
        return f"{result['label']} {result.get('p', result.get('score'))}"
    return ""


if __name__ == "__main__":
    sys.exit(main())
