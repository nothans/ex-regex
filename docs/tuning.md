# Tuning and shipping

Jev reads literally, so most accuracy comes from the wording.
Test wording the way you test code.

## Word the meaning

Two wordings for routing support messages that need a person, measured with Jev 1.13 on 2026-10-07:

| Message | "needs a written reply from a person on the support team" | "asks for help, reports a problem, or complains (a thank-you, praise, or an FYI needs no action)" |
|---|---|---|
| Thanks, all good now! | 0.04 | 0.03 |
| Your app deleted my thesis. I need someone to call me today. | **0.35** | 0.99 |
| FYI the docs link in your footer is fixed now. | 0.07 | 0.04 |
| I was charged twice and nobody answers my emails. | 0.83 | 0.98 |
| Just wanted to say the new version is great. | 0.05 | 0.02 |

The urgent message scored 0.35 because it asked for a call and the wording said "written reply".
Jev answered the question as written.

Wording rules:

- **Name the boundary case.** "A way to reach one specific person directly (a shared team, company, or no-reply address does not count)" beat "a way to contact a specific person" on precision.
  On receipts, distinguish the printed total from the amount still payable after a credit or payment.
- **Ask one thing.** If the meaning has an "and", split it into two patterns and combine the results in code.
- **Describe observable things.** "Asks for help, reports a problem, or complains" beats "needs a reply".
  The model can see the first in the text and has to guess the second.
- **Use `context=` for what the text is**, as in "These are support tickets from customers."
  Do not use it for instructions.
- **Never write a meaning the input can redefine.** "Counts as a refund request under the policy in effect" lets a message supply its own policy.
  Describe what the writer does instead (see [text that steers the answer](#text-that-steers-the-answer)).
- **Keep math, counting, and date checks in code.** Use `extract` to get the span, then compute.

## The tuning loop

1. **Write the meaning the way you would explain it to a new colleague.**
   Then name the boundary case in parentheses: what looks close but does not count.
2. **Run `scan` on twenty real examples** and read the probabilities, not only the matches.
   A right answer at 0.6 is a wording problem.
3. **Run `diff` against your existing regex**, if there is one.
   The disagreements are your test cases.
4. **Label about fifty examples per pattern and measure.**
   `evals/run.py` shows the shape: precision, recall, and the misses, for each wording side by side.
5. **Pin the model and record a lockfile**, so the numbers you measured are the numbers you ship.

## Work alongside your regex

- **Audit it.** [`exregex diff`](cli.md#audit-a-regex-you-already-have) prints only the lines where your pattern and the meaning disagree.
- **Let the regex narrow and the meaning decide.** `prefilter=` takes a regex or a function a span must pass before it is asked.
  On a large application log, `prefilter=r"\b(ERROR|WARN)\b"` limits judgment to warning and error lines.
- **Use it as the finder.** Any function that returns spans is a unit, so a strict regex can guarantee the shape while the meaning picks the span ([example](api.md#units-what-counts-as-a-span)).

## Choose the right tool

| Your question | Use | Why |
|---|---|---|
| Is this string well-formed? (an IP, a UUID, a hex color, a log line's fields) | regex | exact, free, microseconds; it beat Jev on random IPv4 strings (100% vs 92%) |
| Does this span mean X? (personal contact, a refund request, the total due, sarcasm) | ex-regex | the failures of a regex here are about meaning, not spelling |
| Count, compare, or compute (balanced brackets, whether a date exists, sums) | plain code | neither a pattern nor Jev counts reliably |
| Millions of lines, a few of which matter | regex `prefilter=` + ex-regex | a Jev request takes 200-500 ms; the pattern throws away what cannot match |
| Write, summarize, rephrase, explain | a language model | Jev never writes text |
| Must run offline and deterministic | regex, a local backend, or ex-regex with `replay=True` | a lockfile makes recorded decisions repeatable |

Extraction can only return spans a unit proposed.
If a value has no finder, write a unit for it (any function returning spans), or use a generative model.

## Text that steers the answer

The text being judged can try to steer the judgment: a fake system command, a note to the classifier, a fake JSON answer, a counterfeit policy.
How far it moves the answer depends on the wording.
[`evals/adversarial.py`](../evals/adversarial.py) measures this on 96 attacked support messages, each against its clean twin.

| Wording | Verdicts flipped, Jev 1.14 | Verdicts flipped, Jev 1.13 |
|---|---|---|
| What the writer does: "the writer asks, now, to get money back" | 0 of 96 | 0 of 96 |
| The same, naming text inside the message as part of the message | 0 of 96 | 0 of 96 |
| Defers to the text: "counts as a refund request under the review policy in effect" | 61 of 96 | 60 of 96 |

- With the plain wording, counterfeit policies still pulled real refund requests from p 0.97 to 0.78 on 1.14, into the review band.
- Naming the boundary case held every attacked line within 0.07 of its clean score.
- One attacked message packed with eight clean ones moved its neighbors by at most 0.06 on the first two wordings.

These are authored synthetic attacks, not a guarantee.
Keep a review band, and pair any gate that untrusted text can reach with a rule or a person.
Details: [evaluations](evals.md#text-that-steers-the-answer).

## Before you ship it

- **Pin the model.** The presets already do (`typesafe/jev-1.14`, `jev-1.14.0`).
  An alias like `jev-latest` can move under thresholds you measured.
- **Measure on your own labels.** Choose the threshold from the misses on your labeled set, not from these pages.
- **Decide what the review band does.** Probabilities between 0.1 and 0.9 are where the model is unsure.
  Send them to a person, a second wording, or a stricter rule, instead of rounding them.
- **Record a lockfile for your tests**, and run CI with `replay=True`.
- **Set `max_cost_usd`** on any engine that reads untrusted volumes of text.
- **Know the weak spots.** Jev's documented weak spots are literal reading, numbers, date comparison, double negatives, irrelevant state, and a slight lean toward the first option.
  See the [jaggedness page](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md).
