# The command line

The package installs two commands:

- `exgrep`: grep by meaning.
- `exregex`: the rest of the API.

```bash
exgrep "asks for a refund" tickets.txt -n            # grep by meaning; -v -c -l -H -p --json, -u sentence|line|paragraph
exgrep "a failed payment" app.log --prefilter "ERROR|WARN"   # only lines the regex finds get asked
exregex sub "a way to contact a specific person" "[redacted]" notes.md -u contact
exregex extract "the total amount due" invoice.txt -u money
exregex split "a section heading" README.md -u line
exregex diff "asks for a refund" '(?i)refund' tickets.txt   # audit a regex: only the disagreements; exit 1 if any
exregex test "asks for a refund" --text "the lamp is great"      # exit 0 on yes, 1 on no
exregex classify ticket.txt -o billing="Payments, refunds" -o bug="Errors, crashes"
exregex rate "How urgent is \`text\`?" ticket.txt -l "Can wait" -l "This week" -l "Today"
exregex units notes.md -u contact                     # what a unit finds; no requests, no cost
exregex backend                                       # which backend and model would be used
```

- Exit codes follow grep: 0 when something matched, 1 when nothing did, 2 on an error.
- The cost line goes to stderr, so stdout stays clean for pipes.

## Audit a regex you already have

`diff` runs your pattern and a meaning over the same text and prints only the lines where they disagree.
No labels are needed.

```bash
$ exregex diff "the writer asks, now, to get money back" '(?i)\b(refund\w*|money back|reimburs\w*|charge ?back)\b' tickets.txt
- 2:  p=0.02  Please don't refund me, I just want the right size sent out.
- 6:  p=0.02  The refund came through yesterday, thanks for the quick help!
+ 7:  p=0.93  Return this and give me back what I paid, it's useless.
+ 11:  p=0.96  I was billed $49 for a plan I never signed up for. Undo that charge.
+ 17:  p=0.96  Je voudrais être remboursé, le produit est défectueux.
...
exregex diff: agree 16, regex only 10, meaning only 4
```

- A `-` line is one your regex matched and the meaning did not: a likely false positive.
- A `+` line is one the meaning matched and your regex missed.
- The exit code is 0 when they agree everywhere, so a clean diff can gate a migration.
- The Python equivalent is `ex.diff(meaning, regex, text, unit="line")`.

## Caching, replay, and cost

- `--cache PATH` makes repeat runs free.
  A `.jsonl` path is a decision lockfile you can commit.
- `--replay` answers only from the cache and never touches the network.
  A decision that was not recorded is an error.
- `--max-cost USD` caps a run.

```bash
exregex diff "..." '...' tickets.txt --cache decisions.jsonl            # record once, with a key
exregex diff "..." '...' tickets.txt --cache decisions.jsonl --replay   # CI: offline, same answers, no key
```

## Keys and settings

- The CLI reads API keys (`OPENROUTER_API_KEY`, `TYPESAFE_API_KEY`, `OPENAI_API_KEY`) from a `.env` at or above the working directory, and nothing else from it.
- Settings that decide where requests go (`TYPESAFE_BASE_URL`, `EXREGEX_BACKEND`, and `EXREGEX_API_KEY`, the key for a URL backend) come only from the real environment or a flag.
- Running `exgrep` inside a cloned repository therefore cannot redirect your key or your text.
