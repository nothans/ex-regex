# The Python API

ex-regex has the shape of Python's `re` module.

## Next to `re`

| `re` | `exregex` | Returns |
|---|---|---|
| `re.fullmatch(p, s)` | `ex.test(meaning, text)` (alias `fullmatch`) | a `Verdict`, truthy when `p >= threshold` |
| `re.search(p, s)` | `ex.search(meaning, text)` | the best-fitting `Match`, or `None` |
| `re.findall(p, s)` | `ex.findall(meaning, text)` | a list of `Match` (not strings) |
| `re.finditer(p, s)` | `ex.finditer(meaning, text)` | an iterator of `Match` |
| `re.sub(p, r, s)` | `ex.sub(meaning, repl, text)` | a string; `repl` may be a function of the `Match` |
| `re.subn(p, r, s)` | `ex.subn(meaning, repl, text)` | `(string, count)` |
| `re.split(p, s)` | `ex.split(meaning, text, keep=False)` | the pieces between matches |
| `re.compile(p)` | `ex.compile(meaning, unit=..., threshold=...)` | a reusable `Pattern` |
| | `ex.scan(meaning, text)` | every span with its probability, matching or not |
| | `ex.extract(what, text, unit=...)` | the one span that is `what`, or `None` |
| | `ex.filter(meaning, texts)`, `ex.rank(meaning, texts)` | many texts, judged in packed requests |
| | `ex.classify(text, options)` | a `Pick`: label, probability, every label's probability |
| | `ex.rate(text, question, levels)` | a `Rating` on an ordered scale of 2-10 levels |
| | `ex.diff(meaning, regex, text)` | a `Diff` with `regex_only`, `meaning_only`, and `agree` |

Every function takes `engine=`.
The span functions also take:

- `unit=` - what counts as a span (below).
- `threshold=` - default 0.5.
- `context=` - a sentence about the text, sent with every request.
- `question=` - your own wording, with `{ref}` where the span goes.
- `prefilter=` - a regex or function a span must pass before it is asked; spans that fail score 0 and cost nothing.

Async versions: `atest`, `asearch`, `afindall`, `asub`, `asplit`, `afilter`, `aextract`, `aclassify`, `arate`.

`Pattern.filter_p(texts)` returns one probability per text, in order, for use as a table column.

```python
df["refund_p"] = pat.filter_p(df["text"].tolist())
```

A `Match` has `.text`, `.start`, `.end`, `.span()`, `.group()`, `.p`, `.unit`, and `.band`.
`.band` is `"yes"` at 0.9 or above, `"no"` at 0.1 or below, and `"review"` in between.

## Units: what counts as a span

| Unit | Finds |
|---|---|
| `sentence` (default), `line`, `paragraph`, `text` | segments that cover the text |
| `email`, `phone`, `url`, `handle` | candidates, including spelled-out addresses and spoken numbers |
| `contact` | email, phone, url, and handle together |
| `money`, `number`, `percent`, `date`, `time`, `ipv4` | candidates |
| `name`, `quote` | capitalized runs and quoted passages |

A unit can also be a tuple of names (their union), or any function from text to spans or `(start, end)` pairs.
`ex.find_units(text, unit)` shows what a unit finds without sending a request.

The built-in finders only propose spans, so they over-collect on purpose; the meaning decides.
Use a strict regex as the finder when the shape must be exact.

```python
STRICT = re.compile(r"^((25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$")

def valid_ips(text):
    return [m.span() for m in re.finditer(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text) if STRICT.match(m.group())]

ex.findall("the server affected by the outage", "The outage hit 10.0.0.12 first; 192.168.1.1 is my home router.", unit=valid_ips)
# [Match(text='10.0.0.12', ...)]
```

## Examples

**Pull the total due, not the subtotal.**

```python
invoice = "Subtotal: $1,250.00. Tax: $65.50. Total due: $1,315.50."

re.search(r"(?i)total\D*([\d,.]+\d)", invoice).group(1)        # '1,250.00'
ex.extract("the total amount due", invoice, unit="money").text  # '$1,315.50'
```

**Sort text into categories, or place it on a scale.**

```python
ex.classify("I was charged twice this month.", {"billing": "Payments, refunds, invoices", "bug": "Errors, crashes", "other": None})
# Pick(label='billing', ...)

ex.rate("The site is down and customers can't check out.", "How urgent is `text`?", ["Can wait", "This week", "Today"])
# Rating(label='Today', ...)
```

**Split a document by what its lines are.**

```python
doc = "Introduction\nWe built a thing.\nIt works.\nInstallation steps\nRun pip install.\nThen import it."
ex.split("a section heading (a short title line, not a sentence)", doc, unit="line", keep=True)
# ['Introduction', 'We built a thing.\nIt works.', 'Installation steps', 'Run pip install.\nThen import it.']
```

**Route a queue, and send the unsure middle to a person.**

```python
pat = ex.compile("the writer asks, now, to get money back", context="Customer support tickets.")
for text, p in pat.rank(tickets):
    route(text, ex.Verdict(p).band)   # "yes" -> automation, "review" -> a human, "no" -> leave it
```

**Spend a language model call only when a message needs one.**

```python
needs_action = ex.compile("asks for help, reports a problem, or complains (a thank-you, praise, or an FYI needs no action)")
if needs_action.test(message):
    draft = llm.reply(message)
```

These outputs were checked live on Jev 1.14 on 2026-10-20.

More worked examples:

- [`examples/claim_check.py`](../examples/claim_check.py) finds the unsourced claims in a Markdown draft by asking three narrow questions per sentence: is it a checkable fact, does it say where the fact came from, and is it the writer's own result.
- [`examples/support/`](../examples/support/README.md) is an offline application example with scripted answers.
  Run `python examples/support/demo.py` from a checkout.
- A commit hook that flags breaking changes: `git log -1 --format=%B | exregex test "the commit message describes a breaking change to a public API" -q && echo "bump the major version"`.
- [Semantic programs](semantic.md) compose predicates and record patterns with exact guards.

## Building an application around it

```python
import exregex as ex
from exregex import Engine, openrouter

engine = Engine(
    openrouter(model="typesafe/jev-1.14"),   # pinned: thresholds tuned on one model do not move
    cache="~/.cache/myapp/decisions.sqlite",  # recorded answers keyed by model, state, and questions
    max_cost_usd=0.50,                       # refuse any request that would pass this
    concurrency=8,                           # requests in flight, shared by every caller of this engine
)
redact = ex.compile(
    "a way to reach one specific individual person directly "
    "(a shared team, company, sales, support, or no-reply address does not count)",
    unit="contact",
    engine=engine,
)

clean = redact.sub(lambda m: f"[{m.unit}]", message)   # 'Text me at [phone] or write [email]. ...'
print(engine.stats.as_dict())   # requests, cached, retries, failures, input_tokens, cost_usd, latency_ms
```

- **Typed errors.** All of them are subclasses of `ExRegexError`.
  - `ConfigError`: no key.
  - `LimitError`: a documented limit was broken, caught before sending.
  - `BudgetExceeded`: raised before spending.
  - `BackendError`: carries `.status` and `.retryable`.
  - `RefusedError`: a backend declined.
- **Automatic retries.** Rate limits, overloads (TypeSafe's 529), and gateway errors (OpenRouter's 520) are retried with backoff, honoring `retry-after`.
  A 4xx validation error is never retried.
- **Partial replies.** When a server skips a question in a packed request, the engine asks again for only the missing ones, twice, before raising `BackendError`.
  An answer of the wrong type, or a pick that is not an option, also raises `BackendError`.
- **Keys stay with the backend you chose.** Redirects are refused rather than followed with the `Authorization` header.
- **Raw primitives.** `ex.ask(state, {"name": ex.Noul(...), "pick": ex.Choice(...), "level": ex.Score(...)})` returns typed answers for any questions you write.
  Jev 1.14 adds `ex.Box` and pictures; see [Backends, pictures, and long texts](backends.md).

## Lockfiles: pin every decision

Point the cache at a `.jsonl` file to record every decision as one readable line: the question asked, the model, and the answers.
Commit the file, diff it in review, and run CI with `Engine(replay=True)` (or `--replay` on the command line).
Replay never touches the network and needs no API key.
A decision that was not recorded raises `CacheMiss` instead of calling out.

A decision's identity is the request as sent, including the order of a choice's options, because the model reads them in order.
Lockfiles recorded before 0.2.0a2 ignored that order and need one re-record; replay says so when it meets one.

## Testing without a key

```python
from exregex.testing import keyword_engine

engine = keyword_engine({"refund": ["refund", "money back"]})   # real request shapes, keyword answers
assert ex.findall("asks for a refund", "I want my money back.", engine=engine)
```

`exregex.Scripted(handler)` takes a function from `(state, questions)` to answers, for tests that need exact probabilities or error paths.

## How the calls work

`search` asks one choice question over up to 200 segments plus one "is it here at all?" question, following TypeSafe's line-by-line search cookbook.
`extract` asks one choice question over the candidates plus a "none of these" option, following the pre-parsed value extraction cookbook, and on long texts runs a first round per neighborhood and a final round over the winners.
Jev can only pick a span a finder found, so it cannot transpose a digit or return a value that was not in the input.

Up to 32 questions share one request, so a 70-sentence document is three requests.
Eight requests run at a time by default (`concurrency=8`).
