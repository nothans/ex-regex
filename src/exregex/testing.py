"""Test your own code without a network or an API key.

    from exregex.testing import keyword_engine

    engine = keyword_engine({"refund": ["refund", "money back"]})
    assert ex.findall("refund", text, engine=engine)

keyword_engine answers every yes/no question by looking for keywords in the span the question
points at (`segments.S001`, `items.I003`, `candidates.C002.value`, or `text`). It is a stand-in
with the right shapes and plumbing, not a model: use it to test wiring, thresholds, and error
paths, and test meaning against a real backend.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional

from ._types import Choice, ChoiceAnswer, Noul, NoulAnswer, Question, Score, ScoreAnswer
from .backends import Scripted
from .engine import Engine

_REF = re.compile(r"`([A-Za-z_][\w]*(?:\.[\w]+)*)`")


def resolve(state: Any, path: str) -> Any:
    """Follow a dotted path such as 'segments.S001' into a state."""
    node = state
    for part in path.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return None
    return node


def referenced_text(state: Any, instructions: Any) -> str:
    """The text a question points at: the first backticked path that resolves, else the state."""
    text = instructions if isinstance(instructions, str) else str(instructions)
    for path in _REF.findall(text):
        value = resolve(state, path)
        if value is not None:
            return value if isinstance(value, str) else str(value)
    return state if isinstance(state, str) else str(state)


def keyword_engine(keywords: Mapping[str, Sequence[str]], *, hit: float = 0.95, miss: float = 0.03, **engine_kwargs: Any) -> Engine:
    """An Engine whose answers come from keyword lookups.

    keywords maps a word that appears in the question (usually the meaning) to the keywords that
    make the span a match. A choice picks the first option whose description or name contains a
    keyword from the matching entry; a score picks the top level on a hit and the bottom on a miss.
    """

    def matches(question_text: str, span: str) -> Optional[bool]:
        q = question_text.lower()
        s = span.lower()
        for trigger, words in keywords.items():
            if trigger.lower() in q:
                return any(w.lower() in s for w in words)
        return None

    def handler(state: Any, questions: Mapping[str, Question]) -> dict:
        out: dict = {}
        for name, q in questions.items():
            instr = q.instructions if isinstance(q.instructions, str) else str(q.instructions)
            if isinstance(q, Noul):
                if "any segment in `segments`" in instr:
                    segs = resolve(state, "segments") or {}
                    found = any(matches(instr, str(v)) for v in segs.values())
                else:
                    found = bool(matches(instr, referenced_text(state, instr)))
                out[name] = NoulAnswer(hit if found else miss)
            elif isinstance(q, Choice):
                options = list(q.criteria)
                pick = options[-1]
                segs = resolve(state, "segments") if "`segments`" in instr else None
                for opt in options:
                    target = str(segs.get(opt, "")) if isinstance(segs, Mapping) else opt + " " + str(q.criteria[opt] or "")
                    if matches(instr, target):
                        pick = opt
                        break
                probs = {o: (0.9 if o == pick else 0.1 / max(1, len(options) - 1)) for o in options}
                out[name] = ChoiceAnswer(pick, probs, 0.9)
            elif isinstance(q, Score):
                n = len(q.criteria)
                top = bool(matches(instr, referenced_text(state, instr)))
                level = n - 1 if top else 0
                out[name] = ScoreAnswer(float(level), {i: (0.9 if i == level else 0.1 / (n - 1)) for i in range(n)}, 0.9)
        return out

    return Engine(Scripted(handler, model="keyword"), **engine_kwargs)
