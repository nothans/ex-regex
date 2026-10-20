# ex-regex

![ex-regex: Match meaning in Python.](https://raw.githubusercontent.com/nothans/ex-regex/main/images/readme-banner.jpg)

[![PyPI version](https://img.shields.io/pypi/v/ex-regex?color=e65338)](https://pypi.org/project/ex-regex/)
[![Python versions](https://img.shields.io/pypi/pyversions/ex-regex?color=405866)](https://pypi.org/project/ex-regex/)
[![Package checks](https://github.com/nothans/ex-regex/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/nothans/ex-regex/actions/workflows/ci.yml)
[![MIT license](https://img.shields.io/pypi/l/ex-regex?color=405866)](https://github.com/nothans/ex-regex/blob/main/LICENSE)

**Find, replace, split, and extract text by what it means.**

ex-regex has the shape of Python's `re` module, but a model that reads the text decides each match instead of a pattern that checks its shape.
Every match is a verbatim slice of the input with its offsets, so a result can be wrong but never invented.

```bash
python -m pip install --pre ex-regex
```

Python 3.10 or newer, no runtime dependencies.
Live calls need a key, such as `OPENROUTER_API_KEY` for Jev through OpenRouter.

**[Try it in your browser](https://nothans.github.io/ex-regex/)**, with nothing to install and no key.

## Before and after

Each pair asks the same question: the regex answers from what the text looks like, and ex-regex from what it means.

**Redact a person's contact details, not the support address.**

```python
import re
import exregex as ex

text = "Ping me at quasar.marmot.7k9@example.invalid or the team at support@quasar-tools.example.invalid. Cell: 617-555-0123."

# Before
re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|\b\d{3}-\d{3}-\d{4}\b", "[redacted]", text)
# 'Ping me at [redacted] or the team at [redacted]. Cell: [redacted].'

# After
ex.sub("a way to contact a specific person", "[redacted]", text, unit="contact")
# 'Ping me at [redacted] or the team at support@quasar-tools.example.invalid. Cell: [redacted].'
```

**Find the tickets that ask for a refund.**

```python
tickets = [
    "The refund came through yesterday, thanks!",
    "Give me back what I paid, it's useless.",
    "Please refund my order, it arrived broken.",
]

# Before
[t for t in tickets if re.search(r"(?i)\brefund", t)]
# ['The refund came through yesterday, thanks!', 'Please refund my order, it arrived broken.']

# After
ex.filter("the writer asks, now, to get money back", tickets)
# ["Give me back what I paid, it's useless.", 'Please refund my order, it arrived broken.']
```

**Keep the regex you trust, and let it propose the candidates.**

```python
email = "Orders A-1182 and A-1190 arrived. A-1182 is fine, but the lamp in A-1190 is cracked and I want my money back for it."

def order_ids(text):
    return [m.span() for m in re.finditer(r"\bA-\d{3,}\b", text)]

# Before: the pattern finds both orders and cannot say which one is the problem.
[text[s:e] for s, e in order_ids(email)]
# ['A-1182', 'A-1190', 'A-1182', 'A-1190']

# After
ex.extract("the order the customer wants refunded", email, unit=order_ids).text
# 'A-1190'
```

These outputs come from a live run with Jev 1.14 on 2026-10-20; probabilities can shift between runs and models.

[![The ex-regex playground scanning a support ticket: each sentence gets a probability, and a threshold slider picks the ones that need action.](https://raw.githubusercontent.com/nothans/ex-regex/main/images/playground.png)](https://nothans.github.io/ex-regex/)

## Features

- **The `re` API, by meaning:** `test`, `search`, `findall`, `sub`, `split`, `compile`, plus `scan`, `extract`, `filter`, `rank`, `classify`, and `rate`.
- **A probability on every match:** choose the threshold and send the unsure middle to a person.
- **Units that propose spans:** sentences, lines, contacts, money, dates, or your own regex or function.
- **`exgrep` and `exregex diff`:** grep by meaning, and audit an existing regex by printing only the disagreements.
- **Lockfiles:** record every decision in a readable `.jsonl` file, then replay it in CI with no network and no key.
- **Pictures, boxes, and long texts** with Jev 1.14.
- **Offline test engines** that send the real request shapes, for testing your own code.
- **Cost caps, typed errors, and automatic retries.**

## How it works

1. **A unit proposes spans.** The built-in finders over-collect on purpose and catch forms such as "quasar at example dot invalid" and "five five five, oh one seven seven".
2. **Jev judges each span.** [Jev](https://docs.typesafe.ai/introduction.md), TypeSafe's "System One" model, answers typed questions with probabilities and never writes text.
   Up to 32 spans share one request.
3. **Your code decides.** Every span carries a probability, `p`.
   Jev can only pick a span a finder found, so it cannot transpose a digit or return a value that was not in the input.

Jev reads the meaning literally, so most accuracy comes from the wording ([wording rules](https://github.com/nothans/ex-regex/blob/main/docs/tuning.md#word-the-meaning)).

## Measured results

| Task | Regex | ex-regex |
|---|---|---|
| Personal contact redaction (30 snippets, 18 spans) | 42% precision, 44% recall, 9 snippets leak | 100% precision, 100% recall, 0 leak |
| Current refund requests (30 ticket lines) | 53% accuracy | 97% accuracy |
| Total amount due (15 invoices and receipts) | 47% exact | 100% exact |

- The sets are small and authored, every number was the same on Jev 1.13 and 1.14, and the full evaluation costs about a quarter of a cent.
- On the classic regex problems, ex-regex scored higher on Scunthorpe and sarcasm, the strict regex scored higher on random IPv4 addresses, and counting belongs in code.
- Text written to steer the answer flipped 0 of 96 verdicts when the meaning describes what the writer does.
- Details: [all evaluations](https://github.com/nothans/ex-regex/blob/main/docs/evals.md).

## Documentation

- [The Python API](https://github.com/nothans/ex-regex/blob/main/docs/api.md): every function, units, the engine, lockfiles, and testing without a key.
- [The command line](https://github.com/nothans/ex-regex/blob/main/docs/cli.md): `exgrep`, `exregex diff`, caching, and replay.
- [Tuning and shipping](https://github.com/nothans/ex-regex/blob/main/docs/tuning.md): wording rules, choosing the right tool, text that steers the answer, and a pre-ship checklist.
- [Backends, pictures, and long texts](https://github.com/nothans/ex-regex/blob/main/docs/backends.md): keys, OpenRouter, TypeSafe, local models, and Jev 1.14.
- [Evaluations](https://github.com/nothans/ex-regex/blob/main/docs/evals.md): every measured result and how to reproduce it.
- [Semantic programs](https://github.com/nothans/ex-regex/blob/main/docs/semantic.md): experimental predicates and record patterns with exact guards.

## Limits

- **Use a regex for shape and code for counting.** Well-formed strings, sums, brackets, and whether a date exists are not questions about meaning.
- **Thresholds belong to one model.** Pin the model and measure on your own labels before you trust one.
- **Extraction needs a candidate.** ex-regex returns only spans a unit proposed; write a unit for anything the built-in finders miss.
- **Requests hold about 60,000 characters**, or 600,000 with `long_context=True`, at $0.042 per million input tokens.

## Development

```bash
uv venv && uv pip install -e . pytest ruff mypy
pytest                     # offline: a fake System One server on localhost, no key needed
ruff check src tests evals examples scripts && mypy src examples/support --check-untyped-defs
```

Releases follow the [release workflow](https://github.com/nothans/ex-regex/blob/main/docs/releasing.md).

## Credits

The `search` and `extract` designs follow TypeSafe's line-by-line search and pre-parsed value extraction cookbooks.
The packing approach was first measured in [Sieve](https://github.com/nothans/sieve): 16 notes per request cost no accuracy against hand-filed labels.
