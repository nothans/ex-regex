"""The claim-checker example, offline: Markdown handling, line numbers, and verdict routing."""

import importlib.util
import sys
from pathlib import Path

from exregex import Engine, Scripted
from exregex.testing import referenced_text

spec = importlib.util.spec_from_file_location("claim_check", Path(__file__).resolve().parents[1] / "examples" / "claim_check.py")
cc = importlib.util.module_from_spec(spec)
sys.modules["claim_check"] = cc  # dataclasses look their module up while the class is built
spec.loader.exec_module(cc)

DRAFT = """---
title: test
---
# A heading that states 99% of nothing

Acme raised $40 million in a seed round last month.
According to [the filing](https://example.com/f), Acme has 12 employees. They plan to hire 30 more.
I ran the benchmark myself and it finished in 2.3 seconds on my laptop.
Analysts at Example Research reported that 61% of buyers switched.
This sentence is an opinion about how lovely the weather is today.

```
print("Acme raised $1 billion")  # code is not prose
```
"""


def judge(state, questions):
    """Keyword stand-in for the three questions: wiring, not meaning."""
    out = {}
    for name, q in questions.items():
        text = referenced_text(state, q.instructions)
        sentence = text.split("Sentence:", 1)[-1]
        if "checkable fact" in q.instructions:
            out[name] = 0.05 if "opinion" in sentence else 0.9
        elif "where its fact came from" in q.instructions:
            out[name] = 0.95 if "Analysts at" in sentence else 0.05
        else:  # first-hand
            out[name] = 0.95 if "I ran" in sentence else 0.05
    return out


def run():
    return cc.check(DRAFT, engine=Engine(Scripted(judge), cache=False))


def test_skips_front_matter_headings_and_code_and_keeps_line_numbers():
    claims = run()
    lines = {c.line: c for c in claims}
    assert set(lines) == {6, 7, 8, 9}
    assert all("billion" not in c.sentence and "heading" not in c.sentence for c in claims)
    assert lines[6].figures == ["$40 million", "last month"]


def test_verdicts_route_by_link_source_and_first_hand():
    v = {c.line: c.verdict for c in run()}
    assert v[6] == "unsourced"  # a figure with no link and no named source
    assert v[8] == "first-hand"
    assert v[9] == "sourced"
    # line 7 holds two sentences: the linked one, and one whose previous sentence carries the link
    assert [c.verdict for c in run() if c.line == 7] == ["linked", "linked"]


def test_report_lists_unsourced_first_and_strict_exit(tmp_path, monkeypatch, capsys):
    text = cc.report("draft.md", run())
    assert text.splitlines()[0].startswith("draft.md: 5 factual claims (1 unsourced")
    assert "    6: Acme raised $40 million" in text
    lock = tmp_path / "claims.jsonl"
    draft = tmp_path / "d.md"
    draft.write_text(DRAFT, encoding="utf-8")
    monkeypatch.setattr("exregex.engine.from_env", lambda: Scripted(judge))
    monkeypatch.setattr("exregex.cli.load_dotenv", lambda *a, **k: None)
    assert cc.main([str(draft), "--cache", str(lock), "--strict"]) == 1
    assert "1 unsourced" in capsys.readouterr().out
