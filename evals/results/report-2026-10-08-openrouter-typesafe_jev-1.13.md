# ex-regex eval, 2026-10-08

Backend: openrouter, model `typesafe/jev-1.13`.
Spend: $0.0025 over 114 requests (0 cached), 33.3 s wall time.
Authored synthetic development fixtures and labels are in `evals/data/`; they are not an independent human-labeled benchmark.
Redaction recall counts a span only when every character of it is covered; a partial cover is a miss and a leak.

## Contact redaction

30 snippets, 18 spans that are a way to contact a specific person.

| Method | Precision | Recall (full cover) | Exact boundaries | Spans not fully covered | Snippets that leak |
|---|---|---|---|---|---|
| regex | 42% | 44% | 8/18 | 10 | 9 |
| ex-regex (plain) | 90% | 100% | 18/18 | 0 | 0 |
| ex-regex (specific) | 100% | 100% | 18/18 | 0 | 0 |
| finder ceiling | 47% | 100% | 18/18 | 0 | 0 |

### Additional contact cases

12 synthetic snippets, 6 spans. This is a regression set, not a blind holdout.

| Method | Precision | Recall (full cover) | Exact boundaries | Spans not fully covered | Snippets that leak |
|---|---|---|---|---|---|
| regex | 40% | 33% | 2/6 | 4 | 4 |
| ex-regex (plain) | 100% | 100% | 6/6 | 0 | 0 |
| ex-regex (specific) | 100% | 100% | 6/6 | 0 | 0 |
| finder ceiling | 50% | 100% | 6/6 | 0 | 0 |

## Refund requests (grep by meaning)

30 ticket lines, 14 that ask for a refund.

| Method | Accuracy | Precision | Recall |
|---|---|---|---|
| regex | 53% | 50% | 64% |
| ex-regex (plain) | 97% | 100% | 93% |
| ex-regex (specific) | 97% | 100% | 93% |

## Total due (extraction)

15 invoices and receipts; one has nothing due and should return none.

| Method | Exact value |
|---|---|
| regex | 47% |
| ex-regex (plain) | 93% |
| ex-regex (specific) | 100% |

## Misses

**contact, regex:**
- `{"id": "c02", "missed": [], "partial": [], "false": ["support@quasar-tools.example.invalid"]}`
- `{"id": "c04", "missed": [], "partial": [], "false": ["0123456784"]}`
- `{"id": "c05", "missed": ["nebula dot wombat dot 8p2 at example dot invalid"], "partial": [], "false": []}`
- `{"id": "c07", "missed": [], "partial": [], "false": ["(800) 555-0199"]}`
- `{"id": "c08", "missed": ["six one seven, five five five, oh one eight four"], "partial": [], "false": []}`
- `{"id": "c09", "missed": [], "partial": [], "false": ["billing@nebula-labs.example.invalid"]}`
- `{"id": "c11", "missed": ["@fixture_quasar_7k9"], "partial": [], "false": []}`
- `{"id": "c13", "missed": ["+44 7700 900124", "+44 7700 900125"], "partial": [], "false": []}`
- `{"id": "c14", "missed": [], "partial": [], "false": ["noreply@accounts.example.com"]}`
- `{"id": "c15", "missed": ["QuasarMarmot7k9 [at] example [dot] invalid"], "partial": [], "false": []}`
- `{"id": "c18", "missed": [], "partial": [], "false": ["press@comet-labs.example.invalid", "jobs@comet-labs.example.invalid"]}`
- `{"id": "c19", "missed": ["@fixture_puffin_4r6"], "partial": [], "false": []}`
- `{"id": "c21", "missed": ["ext. 4471"], "partial": [], "false": []}`
- `{"id": "c24", "missed": [], "partial": [], "false": ["800-555-2121"]}`
- `{"id": "c25", "missed": [], "partial": [], "false": ["sales@puffin-tools.example.invalid", "888-555-0110"]}`
- `{"id": "c28", "missed": ["07700 900123"], "partial": [], "false": []}`
- `{"id": "c29", "missed": [], "partial": [], "false": ["info@quasar-water.example.invalid"]}`
- `{"id": "c30", "missed": ["@fixture_wombat_8p2"], "partial": [], "false": []}`

**contact, ex-regex (plain):**
- `{"id": "c22", "missed": [], "partial": [], "false": ["@fixture_comet_tools_6t3"]}`
- `{"id": "c24", "missed": [], "partial": [], "false": ["800-555-2121"]}`

**contact, finder ceiling:**
- `{"id": "c02", "missed": [], "partial": [], "false": ["support@quasar-tools.example.invalid"]}`
- `{"id": "c04", "missed": [], "partial": [], "false": ["4417-2290-5531"]}`
- `{"id": "c06", "missed": [], "partial": [], "false": ["978-0-306-40615-7", "2019-03-14"]}`
- `{"id": "c07", "missed": [], "partial": [], "false": ["(800) 555-0199"]}`
- `{"id": "c09", "missed": [], "partial": [], "false": ["billing@nebula-labs.example.invalid"]}`
- `{"id": "c10", "missed": [], "partial": [], "false": ["10.20.30.40", "555-0101-77"]}`
- `{"id": "c14", "missed": [], "partial": [], "false": ["noreply@accounts.example.com"]}`
- `{"id": "c16", "missed": [], "partial": [], "false": ["2026-11-01", "555-12-3456"]}`
- `{"id": "c18", "missed": [], "partial": [], "false": ["press@comet-labs.example.invalid", "jobs@comet-labs.example.invalid"]}`
- `{"id": "c22", "missed": [], "partial": [], "false": ["@fixture_comet_tools_6t3"]}`
- `{"id": "c24", "missed": [], "partial": [], "false": ["800-555-2121"]}`
- `{"id": "c25", "missed": [], "partial": [], "false": ["sales@puffin-tools.example.invalid", "1-888-555-0110"]}`
- `{"id": "c27", "missed": [], "partial": [], "false": ["2.555.0101", "https://example.com/changes"]}`
- `{"id": "c29", "missed": [], "partial": [], "false": ["info@quasar-water.example.invalid"]}`

**contact_holdout, regex:**
- `{"id": "h01", "missed": ["fixture_quasar_7k9(at)example(dot)invalid"], "partial": [], "false": []}`
- `{"id": "h02", "missed": [], "partial": [], "false": ["617-555-0100"]}`
- `{"id": "h03", "missed": ["+44 7700 900126"], "partial": [], "false": []}`
- `{"id": "h05", "missed": [], "partial": [], "false": ["team-ops@comet-labs.example.invalid"]}`
- `{"id": "h08", "missed": ["+44 7700 900127"], "partial": [], "false": []}`
- `{"id": "h11", "missed": [], "partial": [], "false": ["orders@shop.example.invalid"]}`
- `{"id": "h12", "missed": ["zero seven seven zero zero, nine zero zero, one two eight"], "partial": [], "false": []}`

**contact_holdout, finder ceiling:**
- `{"id": "h02", "missed": [], "partial": [], "false": ["617-555-0100"]}`
- `{"id": "h05", "missed": [], "partial": [], "false": ["team-ops@comet-labs.example.invalid"]}`
- `{"id": "h07", "missed": [], "partial": [], "false": ["555-1234", "2026-11-12"]}`
- `{"id": "h09", "missed": [], "partial": [], "false": ["555-0177"]}`
- `{"id": "h11", "missed": [], "partial": [], "false": ["orders@shop.example.invalid"]}`

**refund, regex:**
- `{"id": "r02", "gold": false}`
- `{"id": "r04", "gold": false}`
- `{"id": "r06", "gold": false}`
- `{"id": "r07", "gold": true}`
- `{"id": "r10", "gold": false}`
- `{"id": "r11", "gold": true}`
- `{"id": "r13", "gold": false}`
- `{"id": "r15", "gold": false}`
- `{"id": "r17", "gold": true}`
- `{"id": "r21", "gold": false}`
- `{"id": "r22", "gold": true}`
- `{"id": "r23", "gold": false}`
- `{"id": "r24", "gold": true}`
- `{"id": "r30", "gold": false}`

**refund, ex-regex (plain):**
- `{"id": "r27", "gold": true}`

**refund, ex-regex (specific):**
- `{"id": "r27", "gold": true}`

**total, regex:**
- `{"id": "t01", "gold": "$1,315.50", "got": "$1,365.50"}`
- `{"id": "t02", "gold": "$94.28", "got": null}`
- `{"id": "t04", "gold": "$782.40", "got": null}`
- `{"id": "t08", "gold": "1,250 USD", "got": null}`
- `{"id": "t11", "gold": "$2,410.00", "got": "$3,410.00"}`
- `{"id": "t12", "gold": "$216.00", "got": null}`
- `{"id": "t14", "gold": "$0.00", "got": null}`
- `{"id": "t15", "gold": "$5,400.00", "got": null}`

**total, ex-regex (plain):**
- `{"id": "t11", "gold": "$2,410.00", "got": "$3,410.00"}`

## Reproduction

```json
{
  "package_version": "0.1.0a1",
  "fingerprint_schema": "lf-text-v1",
  "data_kind": "authored synthetic development fixtures; no independent human-label validation",
  "dataset_sha256": {
    "contact": "04c447214f90d325ef3563bb92cf41e3fb10f5c4f66064c252cf7bae5fcfcda4",
    "contact_holdout": "94d6e8ba53e984d6724764685f5b5aa9cc870523bb707d07e78fbf56bc9ecf44",
    "refund": "2a6d5ced2c7c6ef7546d5e46f6e5388b5f9cc09a678efd7d691d1d44726b1ea6",
    "total": "2f44576df4f7678fcf1e65129d17c525b3e6aa06774564e7a1f7ae4ea1669c2a"
  },
  "questions_sha256": "c9c076be2baacaf6c485e656fe990338774ddf39c221b3762a7ec0c4428fd059",
  "runner_sha256": "64a26cda370271839dca10065445603d9048488e24a1bf3ec005be69f3834dc3",
  "library_sha256": "759939cdbf0bb1bbb0df48541227579442b16576cb8f2663608b992982d702a0"
}
```
