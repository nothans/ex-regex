# Evaluate the library on your task

The text evaluation uses authored synthetic scenarios with explicit labels. They test behavior on controlled inputs; they do not establish real-world accuracy or independently reviewed human judgments.

```sh
python evals/run.py --backend openrouter --model typesafe/jev-1.13 --no-cache
python evals/classics.py
```

These commands make paid model requests. The text evaluation has an estimated $0.25 ceiling and writes a Markdown report plus raw scores under `evals/results/`. Reports include dataset, question, runner, and library fingerprints. The README comparison uses the specific question wording shown in the runner. The public report includes both wordings and every miss.

The `lf-text-v1` fingerprint schema normalizes CRLF line endings to LF before hashing text files, so Windows and Unix checkouts identify the same source and data. Question maps and the library's path-to-source-hash map use sorted JSON serialization before hashing.

| File | Contents |
|---|---|
| `data/contact.jsonl` | 30 contact-redaction scenarios with 18 labeled spans |
| `data/contact_holdout.jsonl` | 12 additional contact scenarios with 6 labeled spans; a development regression set, not a blind holdout |
| `data/refund.jsonl` | 30 support lines labeled for current refund requests |
| `data/total.jsonl` | 15 receipts/invoices labeled with the amount still due |
| `data/classics.jsonl`, `data/classics_hard.jsonl` | Familiar examples and generated cases for syntax, counting, and meaning |

The four text sets contain 87 scenarios. Contact recall requires every character of a gold span to be covered; exact-boundary matches and snippets with leaks are reported separately. The baseline regexes are fixed common patterns, not exhaustively tuned competitors.

## Fixture identities

Contact examples are deliberately fictional: names such as Quasar Marmot, handles beginning with `fixture_`, and emails on reserved example domains. No address is asserted to belong to a real person. These examples describe contact roles within the fictional scenario, not the deliverability of a mailbox.

- Email and personal-URL domains use [IANA's special-use domains](https://www.iana.org/assignments/special-use-domain-names/), primarily `example.invalid`. The designation includes their subdomains.
- North American phone examples use the [NANPA fictional 555-0100 through 555-0199 range](https://nanpa.com/numbering/555-line-numbers).
- UK examples use [Ofcom's drama number ranges](https://www.ofcom.org.uk/phones-and-broadband/phone-numbers/numbers-for-drama), including `07700 900000` through `07700 900999`.

Noncontact controls include part numbers, identifiers, version strings, private-network IP addresses, and dates. Social handles are unlinked synthetic tokens; no social platform reserves a universal test namespace.

For record-pattern evaluation, see [the support trial](SUPPORT-TRIAL.md). Its human labels and real-conversation measurements remain separate from these synthetic examples.
