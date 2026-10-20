# Classic regex cases on Jev 1.14

Measured with Jev 1.14 (`typesafe/jev-1.14`) through OpenRouter on 2026-10-20 with `python evals/classics.py`.
The run took 11 requests and $0.000676, with zero cache hits.
The fixtures are synthetic, and code generates the labels where the truth is computable.
The 1.13 run is in [classic-cases.md](classic-cases.md).

## Familiar examples

| Task | Cases | Regex accuracy | ex-regex accuracy |
|---|---|---|---|
| scunthorpe | 18 | 50% | 100% |
| sarcasm | 12 | 25% | 100% |
| ipv4 | 14 | 100% | 100% |
| date | 12 | 58% | 100% |
| hex_color | 10 | 100% | 100% |
| balanced_parens | 12 | 58% | 92% |
| palindrome | 10 | 50% | 100% |

## Random, unfamous strings

| Task | Cases | Regex accuracy | ex-regex accuracy |
|---|---|---|---|
| palindrome | 12 | 50% | 75% |
| balanced_parens | 12 | 58% | 83% |
| date | 12 | 50% | 100% |
| ipv4 | 12 | 100% | 92% |

Against 1.13, every number is the same except two on random strings:

- Balanced parentheses rose from 75% to 83%.
- Palindromes fell from 83% to 75%.

## Leap days asked one at a time

The eval packs each set into one request.
A follow-up probe on 2026-10-20 asked six dates (2024-02-29, 2025-02-29, 2025-02-30, 2025-04-31, 1900-02-29, 2000-02-29) both packed and one at a time, on both models.
It used three wordings: "a date that exists on the calendar, counting leap years", the eval's wording, and one that states the leap-year rule.

- Packed, every wording put 2000-02-29 at 0.98 or 0.99 on both models.
- Asked alone, 2000-02-29 scored 0.39 to 0.53 on 1.14 and 0.84 to 0.95 on 1.13.
- 2025-02-29, which does not exist, scored 0.21 to 0.59 across all twelve runs, every time in the review band.
  On 1.13, packed, two wordings put it above 0.5.
- 2025-02-30 and 2025-04-31 were never above 0.05.

The 100% date accuracy above holds for this set, and the probe shows it depends on packing and model.
Whether a date exists is a calculation: check it with `datetime`, and use ex-regex to find the date in the text.
