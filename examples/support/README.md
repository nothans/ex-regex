# One pattern, two application contexts

Run from the project root after installing the package:

```powershell
python examples/support/demo.py
python examples/support/demo.py --answer unknown
python examples/support/demo.py --answer no
python examples/support/demo.py --outage
```

The default demonstration uses Scripted answers and makes no network calls. It proves application wiring and result handling; it does not measure whether a model understands the conversation.

| Command option | Service and worker status |
|---|---|
| Default (`--answer yes`) | `reported_failed_remedy` |
| `--answer unknown` | `uncertain` |
| `--answer no` | `no_match_within_declared_pattern` |
| `--outage` | `evaluation_failed`, with `errors: {"BackendError": 3}` |

An outage is a successful demonstration of failure handling, so the demo exits normally. The three errors are the three initial requests; no relation request runs. In your application, retain `failure.partial_result.errors` when you need individual request IDs, and use `failure.cause_counts` for a compact summary.

- `support_patterns.py` exports the Message type, three predicates, two capture relations, and the composed pattern. Importing it performs no I/O.
- `support_context.py` attaches at most two preceding messages from the same thread.
- `support_service.py` evaluates the pattern with an injected Engine, band, and limits; `EvaluationFailed` becomes an explicit `evaluation_failed` result.
- `archive_worker.py` reuses that service concurrently on the shared Engine, with bounded ticket submission and results in input order. Pass `workers=1` for sequential use.
- `application.py` shows an explicit factory for a pinned TypeSafe backend. Calling that factory reads credentials; calling its returned inspector can send requests.
- `demo.py` exercises the service and worker with the same Engine, showing cache reuse and explicit uncertainty.

Application code owns context, source revisions, model choice, budgets, and actions. The pattern returns observations and captures. The sample thresholds are illustrative and require calibration on development data before operational use.

Call `attach_context` before binding when using the pattern directly. `engine.plan(...).describe()["diagnostic_summary"]` reports how often each scoped field is empty across planned logical checks: the demo has `item.prior_turns` empty in 1 of 3 checks after attaching context, versus 3 of 3 without it. The detailed `diagnostics` still list every empty projection without exposing text. An empty first-turn history can be legitimate.

`report.explain()` names observed judgments and their source identities, and `report.near_edge` counts leaves within 0.05 of a band edge. Live near-edge outcomes may move between runs; replay preserves recorded answers. Context preparation is part of recording identity: changing the helper's role prefixes, history window, or ordering may require new recordings. Keep the original helper when replaying existing evidence.
