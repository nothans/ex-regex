"""Classic regex problems, both directions: where meaning wins, where the pattern wins, where neither should be used.

    python evals/classics.py        # live, well under a cent; free from the cache on a re-run

Two data files, labels made by code wherever the truth is computable:
  data/classics.jsonl        famous examples (Scunthorpe, sarcasm, IPv4, dates, hex colors, brackets, palindromes)
  data/classics_hard.jsonl   random, unfamous strings for the computable ones, so memorization cannot help
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))

import exregex as ex  # noqa: E402

CLASSIC_MEANINGS = {
    "scunthorpe": "contains a swear word, profanity, or a crude insult",
    "sarcasm": "the writer is genuinely pleased (not sarcastic)",
    "ipv4": "a valid IPv4 address: exactly four parts, each a number from 0 to 255, no leading zeros",
    "date": "a real calendar date in YYYY-MM-DD form: the month exists and the day exists in that month and year, counting leap years",
    "hex_color": "a valid CSS hex color: a # followed by exactly 3 or exactly 6 hexadecimal digits",
    "balanced_parens": "a string whose parentheses are balanced: every ( is closed by a later ), and no ) comes before its (",
    "palindrome": "reads the same forwards and backwards once spaces, punctuation, and capitalization are ignored",
}
CLASSIC_REGEX = {
    "scunthorpe": (re.compile(r"(ass|sex|cock|tit|shit|hell|damn|crap|dick|bloody)", re.I), "the naive substring blocklist"),
    "sarcasm": (re.compile(r"\b(great|love|awesome|perfect|fantastic|wonderful|happy|happier)\b", re.I), "positive-keyword sentiment"),
    "ipv4": (re.compile(r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$"), "the textbook strict IPv4 regex"),
    "date": (re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$"), "the common ISO date regex (no month lengths, no leap years)"),
    "hex_color": (re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$"), "the standard hex color regex (also the gold label's definition)"),
    "balanced_parens": (
        re.compile(r"^[^()]*(\([^()]*\)[^()]*)*$"),
        "the usual one-level attempt (regular expressions cannot count nesting)",
    ),
    "palindrome": (None, "no regex exists for arbitrary length; a 3-to-5-letter backreference on lowercased letters"),
}


def _palin_regex(s: str) -> bool:
    t = "".join(c.lower() for c in s if c.isalnum())
    return bool(re.fullmatch(r"(\w)(\w)?\w?\2\1", t))


def load(dataset: str) -> list[dict]:
    return [json.loads(x) for x in (HERE / "data" / f"{dataset}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def suite_classics(engine: ex.Engine, dataset: str = "classics") -> dict:
    by_task: dict = defaultdict(list)
    for it in load(dataset):
        by_task[it["task"]].append(it)
    out = {}
    for task, rows in by_task.items():
        rx, rx_desc = CLASSIC_REGEX[task]
        if rx is None:
            base = [_palin_regex(r["text"]) for r in rows]
        elif task in ("scunthorpe", "sarcasm"):
            base = [bool(rx.search(r["text"])) for r in rows]
        else:
            base = [bool(rx.match(r["text"])) for r in rows]
        ps = ex.compile(CLASSIC_MEANINGS[task], engine=engine).filter_p([r["text"] for r in rows])
        out[task] = {
            "n": len(rows),
            "regex_desc": rx_desc,
            "regex": sum(b == r["gold"] for b, r in zip(base, rows, strict=True)) / len(rows),
            "ex-regex": sum((p >= 0.5) == r["gold"] for p, r in zip(ps, rows, strict=True)) / len(rows),
            "rows": [{"text": r["text"], "gold": r["gold"], "regex": b, "p": round(p, 3)} for r, b, p in zip(rows, base, ps, strict=True)],
        }
    return out


def main() -> int:
    from exregex.cli import load_dotenv

    load_dotenv(HERE)
    engine = ex.Engine(cache=HERE / ".cache" / "answers.sqlite", max_cost_usd=0.05)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    for dataset, title in (("classics", "famous examples"), ("classics_hard", "random, unfamous strings")):
        print(f"\n{title}\n{'problem':16} {'n':>3} {'regex':>6} {'ex-regex':>9}")
        for task, v in suite_classics(engine, dataset).items():
            print(f"{task:16} {v['n']:3} {100 * v['regex']:5.0f}% {100 * v['ex-regex']:8.0f}%")
    print(f"\n{engine.stats.line()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
