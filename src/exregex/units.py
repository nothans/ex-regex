"""Units: how a text is cut into spans before Jev judges them.

Two kinds:

- **Segments** cover the text: `line`, `sentence`, `paragraph`, `text` (the whole thing).
- **Candidates** are small spans worth a look: `email`, `url`, `phone`, `handle`, `money`,
  `number`, `percent`, `date`, `time`, `ipv4`, `name`, `quote`, and `contact` (email, phone,
  url, and handle together).

The candidate finders are loose on purpose. A regex that has to be right on its own has to be
strict, and strict regexes miss "quasar at example dot invalid" and "five five five, oh one seven seven".
Here a finder only proposes; Jev decides. So a finder should over-collect, and every span it
returns is a verbatim slice of the input, so nothing downstream can invent a value.

A unit can also be a tuple of names (their union), or any callable `text -> iterable of
Span or (start, end)`.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Union


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    text: str
    unit: str


UnitLike = Union[str, Sequence[str], Callable[[str], Iterable]]

# ------------------------------------------------------------------ segments


def lines(text: str) -> list[Span]:
    out = []
    pos = 0
    for raw in text.splitlines(keepends=True):
        body = raw.rstrip("\r\n")
        stripped = body.strip()
        if stripped:
            lead = len(body) - len(body.lstrip())
            out.append(Span(pos + lead, pos + lead + len(stripped), stripped, "line"))
        pos += len(raw)
    return out


_BLANK_LINES = re.compile(r"\r?\n(?:[ \t]*\r?\n)+")


def paragraphs(text: str) -> list[Span]:
    """Blocks separated by one or more blank lines (any line ending)."""
    out = []
    pos = 0
    for sep in [*_BLANK_LINES.finditer(text), None]:
        end = sep.start() if sep else len(text)
        block = text[pos:end]
        s = block.strip()
        if s:
            lead = len(block) - len(block.lstrip())
            out.append(Span(pos + lead, pos + lead + len(s), s, "paragraph"))
        if sep:
            pos = sep.end()
    return out


_ABBREV = {
    "mr",
    "mrs",
    "ms",
    "dr",
    "prof",
    "sr",
    "jr",
    "st",
    "vs",
    "e.g",
    "i.e",
    "inc",
    "ltd",
    "co",
    "corp",
    "fig",
    "approx",
    "dept",
    "est",
    "u.s",
    "u.k",
    "a.m",
    "p.m",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
}
_SENT_END = re.compile(r"(?<![.!?])[.!?]+[\"')\]]*(?=\s+|$)")  # the lookbehind keeps a long run of dots linear
_LAST_WORD = re.compile(r"([A-Za-z.]+)[.!?]*[\"')\]]*$")
_NEXT_WORD = re.compile(r"\s+([A-Za-z]+)")
_DIGIT_NEXT = re.compile(r"\s*\d")  # "No. 5" is an abbreviation; "I said no. She left." is not


def sentences(text: str) -> list[Span]:
    """Sentences within paragraphs; a line break inside a paragraph is not a boundary unless the
    line ends with sentence punctuation or the next line starts a list item."""
    out = []
    for para in paragraphs(text):
        chunk = para.text
        base = para.start
        start = 0
        # list items and headings on their own lines are their own sentences
        breaks = [m.end() for m in re.finditer(r"\n(?=[ \t]*(?:[-*+#>]|\d+[.)])\s)", chunk)]
        for m in _SENT_END.finditer(chunk):
            # The word before the stop, from a bounded window, so the scan stays linear in the paragraph.
            word = _LAST_WORD.search(chunk[max(0, m.start() - 40) : m.end()])
            stem = word.group(1).lower().rstrip(".") if word else ""
            if m.group().startswith(".") and (stem in _ABBREV or (stem == "no" and _DIGIT_NEXT.match(chunk, m.end()))):
                continue
            if word and re.fullmatch(r"[A-Z]", word.group(1).rstrip(".")):
                nxt = _NEXT_WORD.match(chunk, m.end())
                if not (nxt and nxt.group(1).lower() in _COMMON_STARTS):
                    continue  # an initial, as in "J. Smith"; but "Plan B. Then ..." ends a sentence
            breaks.append(m.end())
        for b in sorted(set(breaks)):
            piece = chunk[start:b]
            if piece.strip():
                lead = len(piece) - len(piece.lstrip())
                s = piece.strip()
                out.append(Span(base + start + lead, base + start + lead + len(s), s, "sentence"))
            start = b
        piece = chunk[start:]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            s = piece.strip()
            out.append(Span(base + start + lead, base + start + lead + len(s), s, "sentence"))
    return out


def whole(text: str) -> list[Span]:
    s = text.strip()
    if not s:
        return []
    lead = len(text) - len(text.lstrip())
    return [Span(lead, lead + len(s), s, "text")]


# ------------------------------------------------------------------ candidates (loose on purpose)

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9][\w.+'-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
# Written out to dodge scrapers: "quasar at example dot invalid", "quasar [at] example [dot] invalid", "quasar (at) example.invalid".
_AT_WORD = r"(?:\s*[\[(<{]\s*at\s*[\])>}]\s*|\s+at\s+)"
_DOT_WORD = r"(?:\s*[\[(<{]\s*dot\s*[\])>}]\s*|\s+dot\s+)"
_LABEL = r"[A-Za-z0-9][\w+-]*"
_SEP = rf"(?:{_DOT_WORD}|\.)"
# Candidate discovery is deliberately syntactic, including unfamiliar TLDs. The judge decides
# whether a plausible spoken address is actually contact information. Bounded greedy labels
# consume the whole domain; a continuation guard forbids a truncated match at the bound.
_EMAIL_SPOKEN_WORDS = re.compile(
    rf"(?<![\w.+-]){_LABEL}(?:{_SEP}{_LABEL}){{0,3}}{_AT_WORD}"
    rf"{_LABEL}(?:{_SEP}{_LABEL}){{0,30}}{_SEP}[A-Za-z]{{2,63}}\b(?![@\w]|{_SEP}{_LABEL})",
    re.I,
)


def emails(text: str) -> list[Span]:
    """Real addresses first; a written-out form only where it does not overlap a real one."""
    strict = _finder(_EMAIL, "email")(text)
    spoken = _finder(_EMAIL_SPOKEN_WORDS, "email")(text)
    return strict + [s for s in spoken if not any(s.start < t.end and t.start < s.end for t in strict)]


_URL = re.compile(r"\b(?:https?://|www\.)[^\s<>\"')\]]+[^\s<>\"')\].,;:!?]", re.I)
_HANDLE = re.compile(r"(?<![\w@.])@[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,38}[A-Za-z0-9_])?\b")
_PHONE = re.compile(r"(?<![\w.])(?:\+|00)?\(?\d[\d\s().\-/]{5,}\d(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?![\w])", re.I)
_SPOKEN_DIGIT = r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine)"
_PHONE_EXT = re.compile(r"\b(?:ext\.?|extension)\s*#?\d{2,6}\b", re.I)  # "ext. 4471" on its own
_PHONE_WORDS = re.compile(rf"\b{_SPOKEN_DIGIT}(?:[\s,-]+{_SPOKEN_DIGIT}){{6,14}}\b", re.I)
_CUR = r"(?:[$€£¥₹₩]|USD|EUR|GBP|JPY|CAD|AUD|CHF|INR|US\$|C\$|A\$)"
_AMOUNT = r"\d{1,3}(?:[,.\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
_MONEY = re.compile(
    rf"(?:-\s*)?(?:{_CUR}\s?(?:{_AMOUNT})(?:\s?(?:k|m|bn|million|billion))?|(?<![\d,.])(?:{_AMOUNT})\s?(?:{_CUR}|dollars?|euros?|pounds?|cents?))(?![\w])",
    re.I,
)
_PERCENT = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)?\s?(?:%|percent\b|per cent\b)", re.I)
_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[,.]\d+)*(?![\w])")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_WEEKDAY = r"(?:mon|tue|wed|thu|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?"
_DATE = re.compile(
    r"\b(?:"
    r"(?<![\d.\-/])\d{4}-\d{1,2}-\d{1,2}(?![\d.\-/]\d)"
    r"|(?<![\d.\-/])\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}(?![\d.\-/]\d)"
    rf"|(?:{_WEEKDAY},?\s+)?{_MONTH}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
    rf"|(?:{_WEEKDAY},?\s+)?\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\.?(?:,?\s+\d{{4}})?"
    rf"|{_MONTH}\.?\s+\d{{4}}"
    rf"|(?:next|last|this)\s+(?:{_WEEKDAY}|week|month|year)"
    rf"|{_WEEKDAY}"
    r"|today|tomorrow|yesterday"
    r")\b",
    re.I,
)
_TIME = re.compile(r"\b(?:\d{1,2}:\d{2}(?::\d{2})?\s?(?:[ap]\.?m\.?)?|\d{1,2}\s?[ap]\.?m\.?|noon|midnight)(?![\w])", re.I)
_IPV4 = re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")
_NAME = re.compile(
    r"(?:\b(?:Mr|Mrs|Ms|Mx|Dr|Prof)\.?\s+)?\b[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?:\s+(?:[A-Z]\.|[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?|van|von|de|da|del|di|la|le|bin|al)){0,3}\b"
)
_QUOTE = re.compile(r"\"[^\"\n]{1,400}\"|“[^”\n]{1,400}”|'[^'\n]{2,400}'(?!\w)")


def _finder(pattern: re.Pattern, unit: str, check: Callable[[str], bool] = lambda s: True) -> Callable[[str], list[Span]]:
    def find(text: str) -> list[Span]:
        out = []
        for m in pattern.finditer(text):
            s = m.group().strip()
            if not s or not check(s):
                continue
            lead = m.group().find(s)
            out.append(Span(m.start() + lead, m.start() + lead + len(s), s, unit))
        return out

    find.__name__ = unit
    return find


def _phone_ok(s: str) -> bool:
    digits = sum(c.isdigit() for c in s)
    return 7 <= digits <= 18


def phones(text: str) -> list[Span]:
    return _finder(_PHONE, "phone", _phone_ok)(text) + _finder(_PHONE_WORDS, "phone")(text) + _finder(_PHONE_EXT, "phone")(text)


def names(text: str) -> list[Span]:
    """Capitalized runs. Over-collects (sentence-initial words, places, products); Jev sorts it out."""
    out = []
    for m in _NAME.finditer(text):
        s = m.group()
        if " " not in s and s.lower() in _COMMON_STARTS:
            continue
        out.append(Span(m.start(), m.end(), s, "name"))
    return out


_COMMON_STARTS = {
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "it",
    "i",
    "we",
    "you",
    "he",
    "she",
    "they",
    "my",
    "our",
    "your",
    "his",
    "her",
    "their",
    "if",
    "when",
    "then",
    "so",
    "but",
    "and",
    "or",
    "please",
    "thanks",
    "thank",
    "hi",
    "hello",
    "dear",
    "yes",
    "no",
    "also",
    "after",
    "before",
    "on",
    "in",
    "at",
    "for",
    "to",
    "of",
    "with",
    "as",
    "by",
    "from",
    "can",
    "could",
    "would",
    "should",
    "will",
    "is",
    "are",
    "was",
    "were",
    "do",
    "does",
    "did",
    "have",
    "has",
    "had",
    "not",
    "there",
    "here",
    "what",
    "why",
    "how",
    "who",
    "where",
    "which",
    "best",
    "regards",
    "cheers",
    "sincerely",
    "note",
    "see",
    "call",
    "email",
    "contact",
    "reach",
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "order",
    "total",
    "subtotal",
    "invoice",
    "ping",
    "today",
    "tomorrow",
    "yesterday",
    "ok",
    "okay",
}

FINDERS: dict[str, Callable[[str], list[Span]]] = {
    "line": lines,
    "sentence": sentences,
    "paragraph": paragraphs,
    "text": whole,
    "email": emails,
    "url": _finder(_URL, "url"),
    "handle": _finder(_HANDLE, "handle"),
    "phone": phones,
    "money": _finder(_MONEY, "money"),
    "percent": _finder(_PERCENT, "percent"),
    "number": _finder(_NUMBER, "number"),
    "date": _finder(_DATE, "date"),
    "time": _finder(_TIME, "time"),
    "ipv4": _finder(_IPV4, "ipv4"),
    "name": names,
    "quote": _finder(_QUOTE, "quote"),
}
GROUPS: dict[str, tuple[str, ...]] = {"contact": ("email", "phone", "url", "handle")}
SEGMENTS = frozenset({"line", "sentence", "paragraph", "text"})


def unit_names() -> list[str]:
    return sorted(set(FINDERS) | set(GROUPS))


def is_segment(unit: UnitLike) -> bool:
    return isinstance(unit, str) and unit in SEGMENTS


def _resolve(unit: UnitLike) -> list[tuple[str, Callable[[str], Iterable]]]:
    if callable(unit):
        return [(getattr(unit, "__name__", "custom"), unit)]
    if isinstance(unit, str):
        if unit in GROUPS:
            return [(n, FINDERS[n]) for n in GROUPS[unit]]
        if unit not in FINDERS:
            raise ValueError(f"unknown unit {unit!r}; expected one of {unit_names()} or a callable")
        return [(unit, FINDERS[unit])]
    out = []
    for u in unit:
        out.extend(_resolve(u))
    return out


def find(text: str, unit: UnitLike) -> list[Span]:
    """All spans of `unit` in `text`, in document order, overlaps resolved.

    When finders overlap (a date that also looks like a phone number), the longer span wins,
    and among equal lengths the earlier finder in the unit list wins.
    """
    raw: list[tuple[int, Span]] = []
    for rank, (name, fn) in enumerate(_resolve(unit)):
        for item in fn(text):
            if isinstance(item, Span):
                span = item
            else:
                a, b = item
                span = Span(a, b, text[a:b], name)
            if 0 <= span.start < span.end <= len(text) and text[span.start : span.end] == span.text:
                raw.append((rank, span))
    if not raw:
        return []
    raw.sort(key=lambda rs: (-(rs[1].end - rs[1].start), rs[0], rs[1].start))
    # Accepted spans never overlap, so sorted by start their ends are sorted too: a new span can only
    # collide with its neighbors in that order. bisect keeps this O(n log n) on big numeric tables.
    starts: list[int] = []
    taken: list[Span] = []
    for _, span in raw:
        i = bisect.bisect_left(starts, span.start)
        if i < len(taken) and taken[i].start < span.end:
            continue
        if i > 0 and taken[i - 1].end > span.start:
            continue
        starts.insert(i, span.start)
        taken.insert(i, span)
    return taken
