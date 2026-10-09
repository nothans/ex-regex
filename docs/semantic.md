# Semantic programs in Python

`exregex.semantic` is an experimental API for reusable meaning-dependent conditions and finite patterns over application records. The existing text functions remain available. The runtime uses only the Python standard library.

## Import a definition, inject the runtime

```python
from exregex import Engine, Scripted
from exregex import semantic as sx

asks_for_refund = sx.Predicate[dict](
    "support.refund", version=1,
    question="Does item.body ask for money to be returned?",
    fields={"body": ("text",)},
)
eligible = sx.field("status").eq("open") & asks_for_refund

query = eligible.bind({"text": "Please refund the duplicate charge.", "status": "open"}, key="ticket-42", revision=3)
with Engine(Scripted(lambda state, questions: dict.fromkeys(questions, .95))) as engine:
    result = engine.evaluate(query, band=sx.Band(no=.15, yes=.85))
assert result.complete and result.outcome is sx.YES
```

This example runs offline. Scripted supplies the answer; it does not understand the text. An application can inject a configured TypeSafe, OpenRouter, OpenAI Decisions, or compatible System One backend instead. Semantic execution requires a declared price estimate and the explicit attempt protocol. Availability of an adapter does not establish its quality on a workload.

Definitions, binding, and planning perform no model I/O. The host owns the Engine and calls `evaluate` or awaits `aevaluate` explicitly. Imports do not select models, load keys, spend money, or create observation files.

For a first integration, start with a definition, a bound input, an injected Engine, and an explicit band. `evaluate(query, band=...)` plans for you. Use `engine.plan(...)` when you want to inspect work before execution; custom limits can come next.

## Define inputs precisely

A predicate has a stable name, version, literal question, and alias-to-path mapping. A path is a nonempty tuple of mapping keys or stored dataclass fields. `fields={"body": ("message", "text")}` gives the model `item.body`. `context_fields` supplies aliases under `context`. Unselected fields are neither retained nor traversed.

Binding snapshots the declared data immediately. Later mutations cannot change the query. Supported values are JSON-like scalars and containers plus aware datetimes. Missing required fields raise `InputError`; explicit `None` is a value. Cycles, arbitrary objects, properties, nonfinite floats, and naive timestamps are rejected. Dataclass instances and generated slots are supported. Datetimes retain wall time and a frozen UTC offset in captures, and use their UTC instant for comparisons and identity.

Every record needs an explicit string or integer key; booleans are invalid. A revision is optional and remains distinct from the computed content digest. Sequences reject duplicate keys, including across groups. Equal text with different keys remains separate captures even when one observation can serve both.

## Compose conditions

- `A & B`, `A | B`, and `~A` compose expressions.
- `sx.field("role").eq("customer")`, `.one_of(...)`, `.exists()`, and numeric/datetime `.lt/.le/.gt/.ge` are exact local guards.
- `Band(no, yes)` maps a Noul probability at or below `no` to NO, at or above `yes` to YES, and the middle to UNKNOWN. There is no default band or universal calibration claim.
- UNKNOWN remains UNKNOWN under negation. `A | ~A` is UNKNOWN when A is UNKNOWN.
- Exact decisive branches can prune model calls. Remaining semantic questions within a stage are evaluated eagerly.

Expressions, plans, outcomes, and results reject `bool(...)`. Check `result.complete`, compare `result.outcome is sx.YES`, and handle uncertainty explicitly. A composed result has no invented joint probability; inspect its leaf answers and trace.

## Match records and their relationships

```python
text = {"text": ("text",)}
problem = sx.Predicate("support.problem", question="Does item.text report an experienced problem?", fields=text)
remedy = sx.Predicate("support.remedy", question="Does item.text propose a remedy?", fields=text)
targets = sx.Relation("support.targets",
    question="Does the remedy in right.text address the problem in left.text?",
    left_fields=text, right_fields=text)

pattern = sx.sequence(
    (sx.field("role").eq("customer") & problem).capture("problem"),
    sx.gap(max_items=3),
    (sx.field("role").eq("agent") & remedy).capture("remedy"),
).group_by("ticket_id").require(targets.between("problem", "remedy"))
```

Bind a materialized sequence with `pattern.bind(messages, key=("id",), revision=("revision",))`. Optionally retain extra paths with `retain=(("source_url",),)`. Each capture consumes one record. Adjacent steps are adjacent within their group; a gap permits zero through its maximum intervening records. Input order is authoritative, and output tuples are ordered by original input indices. Overlapping matches are returned.

Add `.within(timedelta(days=2), timestamp="sent_at")` for an inclusive window. Timestamps must be aware and nondecreasing within each group. Time comparisons use UTC, including daylight-saving folds. The matcher does not infer absent events or missing future replies.

A relation compares selected captures directionally. Capture context can name another capture: `relation.between("remedy", "followup", context={"original": sx.ref("problem")})`, paired with `context_fields={"text": ("original", "text")}`. Relations run only for candidates whose unary steps are YES or UNKNOWN. A YES relation cannot erase an UNKNOWN step. Shared and capture context roots cannot collide.

The [complete support example](../examples/support/README.md) matches a problem, a remedy for that problem, and a customer report that the attempted remedy failed. Its service and batch worker import the same definition. Call its [attach_context helper](../examples/support/support_context.py) before binding: it supplies at most two preceding messages from the same thread. The runtime never adds neighboring messages secretly. An empty first-message history is legitimate; omitting all history may make terse follow-ups uncertain.

## Results and incomplete work

Scalar evaluation returns `Evaluation`. Sequence evaluation returns `MatchReport`:

- `matches`: accepted `SemanticMatch` values.
- `uncertain_matches`: unresolved candidates kept separately.
- `match["capture"]`: a `RecordSnapshot` with key, revision, original index, immutable `.data`, and digest.
- `leaves`: raw answers, thresholded outcomes, `definition.name` and `.version`, definition/input digests, model IDs, request/cache provenance, and associated source snapshots, ordered by original record position.
- `coverage`: structural/evaluated/rejected/accepted/uncertain/unevaluated counts and stage completion.
- `trace`: actual attempts, estimated reservations, reported costs, unknown charges, cache hits, and exact pruning.

For a complete sequence, presence is YES if any accepted tuple exists, UNKNOWN if only uncertain tuples exist, and NO if neither exists. Presence YES can coexist with uncertain candidates. Complete means declared work finished; it does not mean the model was correct or the real-world corpus exhaustive.

Default execution errors raise **`sx.EvaluationFailed`**, with `.trace`, `.partial_result`, and a tuple `.causes` holding the original errors. The exception is chained from a cause. `BackendError`, refusal, replay miss, runtime budget exhaustion, deadline, and result-limit failures all use this boundary. With `errors="collect"`, these failures return `complete=False`, `outcome=None`, and `.errors`, preserving completed leaves and partial matches. Definition, binding, planning, and unexpected programming errors always propagate; async cancellation remains `CancelledError`. Never interpret an incomplete empty match list as a negative.

For a compact failure summary, `.cause_counts` returns a fresh dictionary such as `{"BackendError": 3}`. Counts group the collected failures by type name, not by message or underlying root cause. They count final failures, not retry attempts. Use `.causes` for every original exception, `.partial_result.errors` for request/leaf associations, and `.trace.attempts` for attempts. The summary omits error messages and input text.

```python
def decision(query, *, engine, band):
    try:
        report = engine.evaluate(query, band=band)
    except sx.EvaluationFailed as failure:
        return {"status": "failed", "errors": failure.cause_counts}
    if report.outcome is sx.UNKNOWN:
        return {"status": "uncertain"}
    return {"status": "yes" if report.outcome is sx.YES else "no"}
```

## Explain observations and see threshold sensitivity

`report.explain()` returns detached JSON-compatible rows with definition name/version, probability, outcome, band, edge distance, request/model/cache metadata, and `associated_records` (key, revision, original index). It includes observed NO leaves even when no match was returned. It omits source text. A deduplicated observation may serve several records; `associated_records` is not a list of directional relation pairs.

```python
for row in result.explain():
    print(row["definition"], row["associated_records"], row["p"], row["outcome"])
print("Near a threshold:", result.near_edge)
```

These rows explain the observed judgments, not the complete expression proof. Negation can make a NO leaf contribute to YES. Exact guards can reject without any model question. Unexecuted or pruned leaves have no row; inspect `report.errors`, `report.trace.pruned_nodes`, and sequence coverage alongside the rows.

`result.near_edge` (also `report.coverage.near_edge` for sequences) counts observed logical leaves within **0.05 inclusive of either band edge**. Each deduplicated leaf counts once; cached observations count, unexecuted questions and exact guards do not. Each row exposes its `edge_distance` and `near_edge` flag. This is a sensitivity diagnostic, not a calibrated error probability or a prediction of how often an outcome will flip.

Live model answers can vary for identical prompts, and small changes near a threshold can change acceptance. Exact replay fixes recorded observations for repeatable tests; it does not make new live runs deterministic. Calibrate bands on development data and monitor both uncertainty and near-edge counts.

## Inspect and bound execution

`engine.plan(query, band=..., limits=sx.Limits(...))` freezes input, configuration, exact candidates, initial requests, and the finite relation requirements. `plan.describe()` returns redacted JSON-compatible metadata. `plan.requests` exposes effective payloads deliberately for local inspection. Changing backend/model/packing settings invalidates the plan. A plan is an in-process value, not a portable executable file.

`plan.describe()["diagnostics"]` lists null, empty, or whitespace-only projected fields, including fields such as `item.prior_turns`. Entries contain definition identity, scope, alias, and record positions, never field values. They are informational: a first-turn empty history can be correct. Zero and False are not treated as empty. Diagnostics do not alter the request, band, or result.

Start with `plan.describe()["diagnostic_summary"]` for the distribution of empties across definitions:

```python
for row in plan.describe()["diagnostic_summary"]:
    print(f"{row['scope']}.{row['field']}: {row['empty_leaves']}/{row['projected_leaves']} planned checks have empty input")
```

The support demo reports `item.prior_turns: 1/3` after attaching context, versus `3/3` when the helper is omitted. The first turn still appears in the detailed diagnostics; no empty value is suppressed. Each summary row groups a scope/alias across definitions and counts deduplicated logical leaves, including possible relation checks. These are not counts of records, requests, or executed checks. Exact-pruned branches are absent. Only fields with at least one empty projection appear.

| Limit | Default |
|---|---:|
| Planning work units | 1,000,000 |
| Exact structural candidates | 10,000 |
| Distinct logical questions | 10,000 |
| Physical attempts, including retries | 256 |
| Accepted plus uncertain results | 1,000 |
| Attempts per exact request | 3 |
| Per-attempt timeout | 30 seconds |
| Dispatch deadline | 60 seconds |
| Estimated attempt cost | $0.10 |

Binding has separate limits: 1,000 records, 4 MiB across selected records/context, 16 container levels, and 10,000 members per record. Planning limits count partial-prefix exploration even when no complete candidate survives. Exceeding a planning limit produces no executable partial plan. Backend state/question/token limits also apply; a character/byte estimator is not a certified tokenizer.

The request upper bound includes possible relation requests without assuming cache hits. The estimate conservatively counts each possible relation separately because survival changes packing. A result limit fails on the first additional returnable tuple; exactly filling the limit remains complete if nothing else qualifies.

Each retry resends the same full payload. Only transient transport failures, retryable statuses, and missing answers are retried. Refusals, malformed distributions, unknown IDs, invalid probabilities, and conflicting returned model identities remain errors. No answer is clipped or repaired in this path.

All callers share Engine concurrency and cost controls. Unknown charges retain an estimated liability, including when a provider returns token usage without billing. Token-derived estimates are never labeled reported cost. The current semantic budget protocol requires a finite nonnegative input price and free output tokens; other pricing fails preflight. Cost caps bound estimates, not provider invoices. Async cancellation stops new dispatch/retries and drains already-dispatched blocking calls; it cannot unsend a request. Deadlines limit dispatch and transport timeouts, not absolute process termination time.

## Process several tickets concurrently

The [archive worker](../examples/support/archive_worker.py) runs a bounded thread pool over one caller-owned Engine. `inspect_archive(conversations, engine=engine, band=band, limits=limits, workers=4)` yields results in input order and retains at most four submitted tickets. Set `workers=1` for sequential execution. Consume or close the iterator before closing the Engine; closing drains already-running work.

The service handles `EvaluationFailed` as an explicit per-ticket failure, so an outage is not counted as a negative. Programming and preflight errors propagate. `workers` limits ticket tasks; `engine.concurrency` limits simultaneous requests across every caller, and the Engine cost cap remains shared. Limits and deadlines supplied to each evaluation remain per ticket.

Concurrency overlaps independent tickets; it does not combine their questions or promise fewer requests. Cross-ticket packing would change model context and recording identity and needs a separate design and quality evaluation.

## Record and replay

Use `Engine(backend, cache="private.sqlite")` or a JSONL path. Semantic observations have a separate namespace: a `semantic_observations_v1` SQLite table, or `decisions.semantic-v1.jsonl` alongside a selected `decisions.jsonl`. Legacy rows are not treated as strict observations. Memory storage is separately bounded to 20,000 observations; `cache=False` disables it.

`Engine(backend_identity, cache=..., replay=True)` reads exact observations without credentials or network sends. A missing observation raises `sx.EvaluationFailed` with `CacheMiss` in `.causes`, or produces an incomplete collected report. Replay performs no writes and validates stored answers again. Store creation waits until successful recording. Observation files contain input payloads; they are application data.

Identity includes exact input and transformed body, ordered questions/options, adapter/model/settings, and packing policy. Different projected contexts or sibling question sets invalidate the affected request. Original record IDs do not enter model state unless selected. Changing bands can reuse unchanged observations but may activate previously unrecorded relation requests.

**Context preparation is part of replay identity.** For example, the support helper prefixes each prior turn with its role (`"customer: ..."`). Switching from plain prior text to that helper changes the affected requests, even if the message IDs and pattern are unchanged. Keep the original preparation code when replaying an old recording, or record the new requests with the revised helper. Replay will report a miss rather than silently call the model.

Changing the history window or input order can also change the projected context. Version that preparation code alongside the pattern and its recordings.

Existing `Noul`, `Choice`, and `Score` remain available through `engine.ask`. General joins, streaming views, automatic calibration, and framework integrations are separate later capabilities. Current automated evidence establishes mechanics and integration, not real-conversation accuracy or threshold calibration. The [support trial](../evals/SUPPORT-TRIAL.md) supplies reproducible data preparation, independent direct and compiled comparators, human-label validation and scoring.
