# Failed-remedy validation trial

The semantic API has mechanical and synthetic live checks. **It has no measured real-data capture accuracy yet.** These scripts prepare and run the planned comparison. They require no extra runtime dependencies. Run commands from the project root after installing ex-regex.

This study is deferred research and does not block the experimental alpha. Benchmark fingerprint schema 2 hashes the actually imported package (including installed wheels) with portable path names. Older frozen benchmark profiles must be regenerated for a new study; they are not silently accepted under this identity change. Raw semantic decision recordings keep their existing replay identity.

## Data and provenance

`support_data.py` reads the reviewed Ubuntu Chat Logs archive directly without extracting archive paths. It pins the SHA-256, preserves all 200 conversations and 7,950 messages, excludes source human/model friction labels from inputs, and makes a reproducible 100/100 split. It refuses to overwrite an existing output directory.

```powershell
python evals/support_data.py .meta/validation-data/ubuntu-chat-logs.zip .meta/validation-data/prepared-v1
```

The prepared files are local research artifacts and are not included in the package or repository export. Obtain the licensed archive before running preparation. The source is the [ConvoKit Ubuntu Chat Logs corpus](https://convokit.cornell.edu/documentation/chatlogs.html), credited to Sarkar, Srikanth, Hudson, Rudinger, Bonial and Resnik (2025), with ConvoKit conversion by Axel Bax, under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Preserve the generated attribution and manifest with any redistribution.

The downloaded timestamp field contains naive date-time strings, despite the card describing sequence indices. Twenty-four elapsed-time values are nonfinite. Preparation uses source timestamp differences, which agree with every finite supplied elapsed value, and anchors each thread at a synthetic UTC date. These are relative times; the source timezone is unknown. Repeated deliveries remain distinct source IDs. No source message overlaps two conversations in this artifact. Twelve speakers occur in both splits. This is a single-source pilot, not a test on an unseen source or fully disjoint speakers. These facts are derived from the downloaded artifact and recorded in the manifest.

## Human annotation

Give each reviewer the relevant `dev.jsonl` or `test.jsonl` and their separate `.labels-a.jsonl` / `.labels-b.jsonl` template. Keep test reviewers blind to model predictions. Source friction/success labels do not answer this task. Do not replace pending labels with another model's guesses.

For each conversation, enumerate **every** qualifying ordered triple:

1. A customer reports a problem they are experiencing.
2. An agent proposes a remedy for that same problem.
3. A later customer reports attempting that remedy and that the original problem continued.

Each pair of adjacent captures permits zero through three intervening messages from the same thread; the full triple must fit within two days. Quoted/hypothetical reports, successful fixes, and reports about a different problem do not qualify. A terse reply can qualify when the supplied conversation establishes its references. Mark genuine uncertainty in `ambiguous_captures` and notes. A decisive earlier positive makes presence `yes` even if other candidates are ambiguous. Use `ambiguous` only when presence itself cannot be established. Incomplete source evidence must be tagged and explained, not assumed negative.

Each capture is exactly `[[problem_id, revision], [remedy_id, revision], [followup_id, revision]]`. Copy revisions from the source. Put the reviewer's name or stable identifier in `reviewer`. Set `status` to `reviewed` for independent drafts; after comparison/adjudication set the final file to `adjudicated`. A one-person pilot uses `single_annotator` and is reported as such. Keep both original independent files and the written disposition of disagreements.

Optional atomic annotations support calibration:

```json
{"name":"support.problem","records":[["message-id","revision-hash"]],"label":1}
```

Unary labels use one record; `support.remedy_targets` uses problem/remedy; `support.failed_after` uses problem/remedy/followup in that order. Use `label: null` for indeterminate atoms. `families` records contrasts such as `multiple_issues`, `terse`, `successful_fix`, `quoted`, `wrong_structure`, `incomplete`, or `adversarial`. Record all applicable families without forcing artificial balance.

```powershell
python evals/support_labels.py .meta/validation-data/prepared-v1/test.jsonl .meta/validation-data/prepared-v1/test.labels-a.jsonl --labels-b .meta/validation-data/prepared-v1/test.labels-b.jsonl
```

The checker verifies identities and structural bounds and lists disagreements. It never invents an adjudicated answer.

## Run and freeze

First check wiring on development inputs. Scripted answers deliberately say yes to everything. They validate code paths, not meaning:

```powershell
python evals/support_compare.py .meta/validation-data/prepared-v1 .meta/validation-data/wiring --limit 3 --arms keyword broad direct compiled naive
```

The four primary arms are keyword rules, one broad presence question, independently enumerated/batched direct Jev questions, and compiled matching. `naive` is an optional sequential performance comparator. Direct and compiled use the same definitions, projections, context, roles, gaps and time bounds. Question IDs and deduplication/packing differ; this is recorded in the profile and must be included in the interpretation. Both use a single physical attempt per payload. Each cold run, arm and conversation has a separate observation store; rerunning a live command cannot silently reuse a prior run. Within-conversation deduplication remains active. Execution failures remain failures. The broad arm has no capture metric.

For live development, add `--mode live`. Each arm has a default **$0.10 estimate-based engine budget across its conversations**, so four arms can consume up to four such budget allocations; dispatched failures may still be billed. Increase it only deliberately if a corpus run exhausts the cap. The budget is not a guaranteed invoice ceiling. Synthetic/live/replay evidence modes are recorded separately. A requested Jev alias is paired with the expected concrete model ID observed in the smoke test; an alias drift fails explicitly. Change both model flags only as a new calibrated profile.

Tune questions and bands on development data. Once human test labels are complete, repeat/finalize the development configuration with `--reference-labels PATH_TO_FINAL_TEST_LABELS`; this hashes the reference file into the profile without using its answers for tuning. Preserve the resulting `profile.json`. It includes source, library, dataset, band, model, limits, arm and label identities.

```powershell
python evals/support_compare.py .meta/validation-data/prepared-v1 .meta/validation-data/test-run --mode live --split test --freeze PATH_TO_FROZEN_PROFILE --reference-labels PATH_TO_FINAL_TEST_LABELS
python evals/support_score.py .meta/validation-data/test-run PATH_TO_FINAL_TEST_LABELS
```

Test runs require all 100 conversations and matching frozen profile/labels. Pending labels are rejected. After viewing test results, any tuning requires a fresh untouched test set. For exact keyless replay, repeat the same flags with `--mode replay --replay-from PATH_TO_ORIGINAL_COLD_RUN` and a new output directory. It reads that run's stored payload observations without creating or changing stores. Missing entries count as execution failures. Replay preserves the original backend/evidence type, so replaying Scripted data cannot become live-quality evidence. Warm timing is reported separately from cold timing.

## Report interpretation

The scorer reports confusion counts, execution failures, semantic abstentions, decision and fully resolved coverage, exact capture precision/recall/F1, ambiguity sensitivity, conversation-bootstrap F1 intervals, observed-atom calibration/Brier/reliability and risk/coverage curves, and per-conversation p50/p95 timing. It also reports dispatched question counts and payload bytes, source record/byte counts, returned model IDs, result counts, attempts and token usage. Runtime, processor, CPU count, command and cache policy are saved with the run. Uncertain or unexecuted gold tuples count as missed. Duplicate predicted tuples count as false positives. Missing conversations cause scoring to fail rather than shrinking the denominator. Cost totals contain reported billing; unknown-charge attempts are counted separately.

Provisional targets are 95% exact-capture precision, 70% conversation decision coverage, and capture F1 within two percentage points of the competent direct arm at matched coverage. Compare coverage explicitly and publish numerator/denominator counts; a threshold sweep on test data is diagnostic, never permission to select the best test threshold. Calibration is only for labeled, observed atoms and cannot certify joint match quality. Source completeness, candidate recall, and observed developer integration effort require their own accompanying review.

The current implementation and quality gate status live in [the project plan](../PLAN.md). Public release and real-world adoption claims remain separate decisions.
