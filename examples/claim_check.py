"""Find the unsourced factual claims in a Markdown draft.

    python examples/claim_check.py post.md
    python examples/claim_check.py post.md --cache post.claims.jsonl      # record decisions
    python examples/claim_check.py post.md --cache post.claims.jsonl --replay   # offline, same answers
    python examples/claim_check.py post.md --json --strict                 # machine output; exit 1 if any

Exact work stays in code: skipping code blocks and headings, finding links, line numbers, and
pulling figures out of a sentence. Jev answers three narrow questions about each sentence:
does it state a checkable fact about the world; if it has no link, does it say where the fact
came from; and is it the writer's own first-hand result? The source question also reads the
sentence before it, where attributions usually sit. The first-hand question reads the whole
paragraph, because "I ran this" is often said two or three sentences earlier.

Every decision comes back with a probability. Sentences in the uncertain middle are listed as
"check" rather than silently passed or flagged.

What "sourced" means here: the sentence (or the one before it) says who reported, measured, or
said the fact, or links somewhere. A named company counts ("OpenRouter added it"). Nothing checks
that the source exists or says what the sentence claims; that part is still yours.

Needs ex-regex (pip install --pre ex-regex) and OPENROUTER_API_KEY, or --replay with a recorded
--cache file. A 1,200-word draft takes about five requests and a tenth of a cent.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import exregex as ex
except ImportError:  # running from a checkout without installing
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import exregex as ex

CLAIM = (
    "states a specific, checkable fact about the world, such as a statistic, measurement, price, date, "
    "ranking, quotation, or an event that happened (an opinion, joke, question, instruction, definition, "
    "or the author's own plans or first-hand experience does not count)"
)
SOURCED = (
    "Does the Sentence in {ref} say where its fact came from: who reported, measured, published, announced, "
    "or said it (a named person, company, paper, article, benchmark, or dataset)? "
    "A source named in the Previous sentence counts. A vague attribution such as 'studies show' or "
    "'people say' does not count."
)
FIRST_HAND = (
    "Is the Sentence in {ref} the writer describing a result from their own work or experiment, "
    "including work done by the writer's own tools, scripts, or AI agents, "
    "rather than reporting a fact about other people, companies, products, prices, or news events? "
    "Use the Paragraph to see whose work it is."
)
PARAGRAPH_CHARS = 900
# Bands for the source question, set by reading two real drafts (a small sample: tune on yours).
# Clearly unsourced facts scored 0.06 or lower; named attributions ("Zach Lloyd gave a talk",
# "Elon Musk posted") scored 0.43-0.89. Between the two edges the sentence is listed to check.
SOURCED_YES = 0.5
SOURCED_NO = 0.2
FIGURES = ("money", "percent", "date", "number")

_FENCE = re.compile(r"^\s*(```|~~~)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://|/|\.)[^)]*\)|<https?://[^>]+>|https?://\S+")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.S)


def visible_text(markdown: str) -> str:
    """Blank out what is not prose (front matter, code, headings, images, comments, tables)
    with spaces, keeping every newline, so offsets still map to the original line numbers."""

    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group())

    text = _FRONT_MATTER.sub(blank, markdown)
    text = _HTML_COMMENT.sub(blank, text)
    text = _IMAGE.sub(blank, text)
    out, fenced = [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            out.append(" " * len(line))
            continue
        skip = fenced or line.lstrip().startswith(("#", "|", ">"))
        out.append(" " * len(line) if skip else line)
    return "\n".join(out)


def readable(sentence: str) -> str:
    """What the model reads: link text kept, URLs replaced by a marker, Markdown emphasis dropped."""
    s = _LINK.sub(lambda m: (m.group(1) or "") + " [link]", sentence)
    return re.sub(r"[*_`]+", "", s).strip()


@dataclass
class Claim:
    line: int
    sentence: str
    p_claim: float
    linked: bool
    p_sourced: float | None = None
    p_first_hand: float | None = None
    figures: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """linked, sourced, first-hand, unsourced, or check (the uncertain middle)."""
        if self.linked:
            return "linked"
        assert self.p_sourced is not None and self.p_first_hand is not None
        if self.p_sourced >= SOURCED_YES:
            return "sourced"
        if self.p_first_hand >= 0.5:
            return "first-hand"
        return "unsourced" if self.p_sourced <= SOURCED_NO else "check"


def check(markdown: str, *, engine: ex.Engine | None = None, threshold: float = 0.5) -> list[Claim]:
    text = visible_text(markdown)
    spans = [s for s in ex.find_units(text, "sentence") if len(s.text.split()) >= 5]
    if not spans:
        return []
    sentences = [readable(s.text) for s in spans]
    paragraphs = ex.find_units(text, "paragraph")
    p_claims = ex.compile(CLAIM, context="Sentences from a blog post draft.", engine=engine).filter_p(sentences)

    claims: list[Claim] = []
    need_source: list[tuple[int, str, str]] = []
    for i, (span, sent, p) in enumerate(zip(spans, sentences, p_claims, strict=True)):
        if p < threshold:
            continue
        previous = spans[i - 1].text if i else ""
        linked = bool(_LINK.search(span.text)) or bool(_LINK.search(previous))
        claim = Claim(
            line=text.count("\n", 0, span.start) + 1,
            sentence=sent,
            p_claim=p,
            linked=linked,
            figures=[f.text for f in ex.find_units(sent, FIGURES)],
        )
        claims.append(claim)
        if not linked:
            para = next((q.text for q in paragraphs if q.start <= span.start < q.end), span.text)
            need_source.append(
                (
                    len(claims) - 1,
                    f"Previous sentence: {readable(previous) or '(none)'}\nSentence: {sent}",
                    f"Paragraph: {readable(para)[:PARAGRAPH_CHARS]}\nSentence: {sent}",
                )
            )

    if need_source:
        sourced = ex.compile("names its source", question=SOURCED, engine=engine).filter_p([t for _, t, _ in need_source])
        first_hand = ex.compile("first-hand result", question=FIRST_HAND, engine=engine).filter_p([t for _, _, t in need_source])
        for (k, _, _), ps, pf in zip(need_source, sourced, first_hand, strict=True):
            claims[k].p_sourced, claims[k].p_first_hand = ps, pf
    return claims


def report(path: str, claims: list[Claim]) -> str:
    groups = {v: [c for c in claims if c.verdict == v] for v in ("unsourced", "check", "first-hand", "sourced", "linked")}
    counts = ", ".join(f"{len(g)} {v}" for v, g in groups.items())
    lines = [f"{path}: {len(claims)} factual claims ({counts})"]
    titles = {
        "unsourced": "Unsourced: add a link or say who reported it",
        "check": "Check: the source question was uncertain",
        "first-hand": "First-hand: your own results, no citation needed (listed so a wrong call is visible)",
    }
    for verdict, title in titles.items():
        if not groups[verdict]:
            continue
        lines.append(f"\n{title}")
        for c in sorted(groups[verdict], key=lambda c: (not c.figures, c.line)):  # claims with figures first
            figs = f"  [{', '.join(c.figures)}]" if c.figures else ""
            lines.append(f"  {c.line:>4}: {c.sentence}{figs}")
            if verdict != "first-hand":
                lines.append(f"        claim {c.p_claim:.2f}, sourced {c.p_sourced:.2f}, first-hand {c.p_first_hand:.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("draft", help="a Markdown file")
    ap.add_argument("--cache", metavar="PATH", help="decision lockfile (.jsonl) or SQLite path")
    ap.add_argument("--replay", action="store_true", help="answer only from --cache; no network, no key")
    ap.add_argument("--threshold", type=float, default=0.5, help="p at which a sentence counts as a claim")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("--strict", action="store_true", help="exit 1 when any claim is unsourced")
    ap.add_argument("--max-cost", type=float, default=0.05, metavar="USD", help="refuse to spend more (default $0.05)")
    args = ap.parse_args(argv)

    from exregex.cli import load_dotenv  # the CLI's own loader: API keys only

    load_dotenv()
    markdown = Path(args.draft).read_text(encoding="utf-8")
    backend = ex.backends.identity_from_env() if args.replay else None
    with ex.Engine(backend, cache=args.cache or True, replay=args.replay, max_cost_usd=args.max_cost) as engine:
        claims = check(markdown, engine=engine, threshold=args.threshold)
        stats = engine.stats.line()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    if args.json:
        print(json.dumps([c.__dict__ | {"verdict": c.verdict} for c in claims], indent=1, ensure_ascii=False))
    else:
        print(report(args.draft, claims))
    print(f"[{engine.backend.describe()}] {stats}", file=sys.stderr)
    return 1 if args.strict and any(c.verdict == "unsourced" for c in claims) else 0


if __name__ == "__main__":
    sys.exit(main())
