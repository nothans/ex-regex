"""Command line: `exgrep` and `exregex`.

    exgrep "asks for a refund" tickets.txt
    exregex sub "a way to contact a specific person" "[redacted]" notes.md --unit contact
    exregex extract "the total amount due" invoice.txt --unit money

Exit codes follow grep: 0 when something matched, 1 when nothing did, 2 on an error.
Cost and timing go to stderr, so stdout stays clean for pipes. The CLI reads a .env file at or
above the working directory for API keys (variables already set win).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Optional

from . import backends as backends_mod
from ._version import __version__
from .engine import Engine, set_engine
from .errors import ExRegexError
from .patterns import Match, Pattern, classify, extract, rate
from .units import unit_names

DOTENV_KEYS = frozenset({"OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "OPENAI_API_KEY"})


def load_dotenv(start: Optional[Path] = None) -> Optional[Path]:
    """Load API keys from the nearest .env at or above `start`, without overriding the environment.

    Only the keys in DOTENV_KEYS are read. Anything that chooses where requests go (a base URL,
    EXREGEX_BACKEND, EXREGEX_API_KEY) must come from the real environment or a flag, so running
    exgrep inside a cloned repository cannot redirect your key or your text to another host.
    """
    d = (start or Path.cwd()).resolve()
    for folder in [d, *d.parents]:
        f = folder / ".env"
        if f.is_file():
            for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[7:]
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if k in DOTENV_KEYS:
                    os.environ.setdefault(k, v)
            return f
    return None


def _unit(value: str) -> Any:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    for p in parts:
        if p not in unit_names():
            raise argparse.ArgumentTypeError(f"unknown unit {p!r}; one of: {', '.join(unit_names())}")
    return parts[0] if len(parts) == 1 else tuple(parts)


_LINE_BREAK = re.compile("\r\n|[\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")  # what str.splitlines() breaks on


def _line_no(text: str, offset: int) -> int:
    """1-based line number of `offset`, counting the same breaks as the `line` unit."""
    return len(_LINE_BREAK.findall(text, 0, offset)) + 1


def _read(path: Optional[str], text: Optional[str]) -> str:
    if text is not None:
        return text
    if path is None or path == "-":
        return sys.stdin.buffer.read().decode("utf-8", "replace") if hasattr(sys.stdin, "buffer") else sys.stdin.read()
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _common(p: argparse.ArgumentParser, unit_default: Optional[str] = "sentence", threshold: bool = True) -> None:
    if unit_default is not None:
        p.add_argument(
            "-u",
            "--unit",
            type=_unit,
            default=unit_default,
            help=f"span unit, or several joined by commas (default {unit_default}): {', '.join(unit_names())}",
        )
    if unit_default is not None:
        p.add_argument(
            "--prefilter", metavar="REGEX", help="only spans this regex finds anywhere in get asked; the rest score 0 and cost nothing"
        )
    if threshold:
        p.add_argument("-t", "--threshold", type=float, default=0.5, help="match at p >= this (default 0.5)")
    p.add_argument("--context", help="one sentence about the text, sent with every request")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--text", help="use this text instead of a file or stdin")


def _engine_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("backend")
    g.add_argument("--backend", help="typesafe | openrouter | openai | local | a System One URL (default: from the environment)")
    g.add_argument("--model", help="model id for the backend")
    g.add_argument("--cache", metavar="PATH", help="answer cache: a .jsonl decision lockfile (commit it) or a SQLite path")
    g.add_argument("--replay", action="store_true", help="answer only from --cache; never call the network (for CI)")
    g.add_argument("--max-cost", type=float, metavar="USD", help="refuse to spend more than this")
    g.add_argument("--concurrency", type=int, default=8)
    g.add_argument("-q", "--quiet", action="store_true", help="no cost line on stderr")


def _make_engine(args: argparse.Namespace) -> Engine:
    if args.backend:
        os.environ["EXREGEX_BACKEND"] = args.backend
    if args.model:
        os.environ["EXREGEX_MODEL"] = args.model
    if args.replay and not args.cache:
        raise ExRegexError("--replay needs --cache PATH, the lockfile to replay from")
    backend = backends_mod.identity_from_env() if args.replay else backends_mod.from_env()
    engine = Engine(backend, cache=args.cache or True, replay=args.replay, concurrency=args.concurrency, max_cost_usd=args.max_cost)
    set_engine(engine)
    return engine


def _match_json(m: Match, text: Optional[str] = None) -> dict:
    d = {"text": m.text, "start": m.start, "end": m.end, "p": round(m.p, 4), "unit": m.unit}
    if m.confidence is not None:
        d["confidence"] = round(m.confidence, 4)
    if text is not None:
        d["line"] = _line_no(text, m.start)
    return d


def _out(obj: Any) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ grep


def _grep(args: argparse.Namespace, engine: Engine) -> int:
    files = args.files or ["-"]
    many = len(files) > 1 or args.with_filename
    pattern = Pattern(
        args.meaning,
        unit=args.unit,
        threshold=args.threshold,
        context=args.context,
        prefilter=getattr(args, "prefilter", None),
        engine=engine,
    )
    total = 0
    for f in files:
        text = _read(f, args.text if f == "-" else None)
        scanned = pattern.scan(text)
        hits = [m for m in scanned if (m.p < args.threshold) == args.invert]
        total += len(hits)
        name = "(stdin)" if f == "-" else f
        if args.files_with_matches:
            if hits:
                print(name)
            continue
        if args.count:
            print(f"{name}:{len(hits)}" if many else len(hits))
            continue
        for m in hits:
            if args.json:
                _out({"file": name, **_match_json(m, text)})
                continue
            prefix = f"{name}:" if many else ""
            if args.line_number:
                prefix += f"{_line_no(text, m.start)}:"
            p = f"{m.p:.2f}  " if args.show_p else ""
            body = m.text.replace("\n", " ") if args.unit != "line" else m.text
            print(f"{prefix}{p}{body}")
    return 0 if total else 1


def grep_main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="exgrep", description="grep by meaning: print the lines (or sentences, paragraphs) that fit a plain-English description."
    )
    p.add_argument("meaning", help='what to find, in plain English ("asks for a refund")')
    p.add_argument("files", nargs="*", help="files to search (default: stdin)")
    _common(p, unit_default="line")
    p.add_argument("-v", "--invert", action="store_true", help="print the spans that do not fit")
    p.add_argument("-c", "--count", action="store_true", help="print only a count per file")
    p.add_argument("-l", "--files-with-matches", action="store_true", help="print only the names of files with a match")
    p.add_argument("-n", "--line-number", action="store_true", help="prefix each span with its line number")
    p.add_argument("-H", "--with-filename", action="store_true", help="prefix each span with its file name")
    p.add_argument("-p", "--show-p", action="store_true", help="show each span's probability")
    p.add_argument("--version", action="version", version=f"exgrep {__version__}")
    _engine_args(p)
    return _run(p, argv, _grep)


# ------------------------------------------------------------------ exregex


def _cmd_test(args: argparse.Namespace, engine: Engine) -> int:
    v = Pattern(
        args.meaning, threshold=args.threshold, context=args.context, prefilter=getattr(args, "prefilter", None), engine=engine
    ).test(_read(args.file, args.text))
    if args.json:
        _out({"match": bool(v), "p": round(v.p, 4), "band": v.band})
    else:
        print(f"{'yes' if v else 'no'}  p={v.p:.2f}  ({v.band})")
    return 0 if v else 1


def _cmd_spans(args: argparse.Namespace, engine: Engine) -> int:
    text = _read(args.file, args.text)
    pat = Pattern(
        args.meaning,
        unit=args.unit,
        threshold=args.threshold,
        context=args.context,
        prefilter=getattr(args, "prefilter", None),
        engine=engine,
    )
    if args.cmd == "search":
        m = pat.search(text)
        hits = [m] if m else []
    elif args.cmd == "scan":
        hits = pat.scan(text)
    else:
        hits = pat.findall(text)
    for m in hits:
        if args.json:
            _out(_match_json(m, text))
        else:
            print(f"{m.p:.2f}  {_line_no(text, m.start)}:{m.start}-{m.end}  {m.text}")
    return 0 if any(m.p >= args.threshold for m in hits) else 1


def _cmd_diff(args: argparse.Namespace, engine: Engine) -> int:
    text = _read(args.file, args.text)
    pat = Pattern(args.meaning, unit=args.unit, threshold=args.threshold, context=args.context, engine=engine)
    d = pat.diff(args.regex, text)
    rows = [("-", m) for m in d.regex_only] + [("+", m) for m in d.meaning_only]
    rows.sort(key=lambda r: r[1].start)
    for sign, m in rows:
        if args.json:
            _out({"side": "regex_only" if sign == "-" else "meaning_only", **_match_json(m, text)})
        else:
            body = m.text.replace("\n", " ")
            print(f"{sign} {_line_no(text, m.start)}:  p={m.p:.2f}  {body}")
    sys.stdout.flush()
    if not args.quiet:
        sys.stderr.write(f"exregex diff: {d}  (- regex matched, meaning did not; + meaning matched, regex did not)\n")
    return 0 if d.clean else 1


def _cmd_sub(args: argparse.Namespace, engine: Engine) -> int:
    text = _read(args.file, args.text)
    pat = Pattern(
        args.meaning,
        unit=args.unit,
        threshold=args.threshold,
        context=args.context,
        prefilter=getattr(args, "prefilter", None),
        engine=engine,
    )
    out, n = pat.subn(args.repl, text)
    sys.stdout.write(out)
    sys.stdout.flush()
    if not args.quiet:
        sys.stderr.write(f"exregex: {n} replacement(s)\n")
    return 0 if n else 1


def _cmd_split(args: argparse.Namespace, engine: Engine) -> int:
    text = _read(args.file, args.text)
    pat = Pattern(
        args.meaning,
        unit=args.unit,
        threshold=args.threshold,
        context=args.context,
        prefilter=getattr(args, "prefilter", None),
        engine=engine,
    )
    matched = len(pat.findall(text))  # cached, so split below costs nothing more
    pieces = pat.split(text, keep=args.keep)
    if args.json:
        _out(pieces)
    else:
        sys.stdout.write("\n\n---\n\n".join(pieces) + "\n")
    return 0 if matched else 1


def _cmd_extract(args: argparse.Namespace, engine: Engine) -> int:
    text = _read(args.file, args.text)
    kw = {"unit": args.unit} if args.unit else {}
    m = extract(args.what, text, threshold=args.threshold, context=args.context, engine=engine, **kw)
    if args.json:
        _out(_match_json(m, text) if m else None)
    elif m:
        print(m.text)
    return 0 if m else 1


def _cmd_classify(args: argparse.Namespace, engine: Engine) -> int:
    options: dict[str, Optional[str]] = {}
    for o in args.option:
        label, _, desc = o.partition("=")
        options[label.strip()] = desc.strip() or None
    if len(options) < 2:
        raise ExRegexError("classify needs at least two --option LABEL[=description]")
    pick = classify(_read(args.file, args.text), options, context=args.context, engine=engine)
    if args.json:
        _out(
            {
                "label": pick.label,
                "p": round(pick.p, 4),
                "confidence": round(pick.confidence, 4),
                "probabilities": {k: round(v, 4) for k, v in pick.probabilities.items()},
            }
        )
    else:
        print(f"{pick.label}  p={pick.p:.2f}  confidence={pick.confidence:.2f}")
    return 0


def _cmd_rate(args: argparse.Namespace, engine: Engine) -> int:
    r = rate(
        _read(args.file, args.text),
        args.question,
        args.level,
        context=args.context,
        engine=engine,
    )
    if args.json:
        _out({"score": round(r.score, 4), "level": r.level, "label": r.label, "confidence": round(r.confidence, 4)})
    else:
        print(f"{r.label}  score={r.score:.2f}  confidence={r.confidence:.2f}")
    return 0


def _cmd_units(args: argparse.Namespace, engine: Optional[Engine]) -> int:
    from .units import find

    if args.file or args.text is not None:
        text = _read(args.file, args.text)
        for s in find(text, args.unit):
            print(f"{s.unit:9} {s.start}-{s.end}  {s.text}")
    else:
        print("\n".join(unit_names()))
    return 0


def _cmd_backend(args: argparse.Namespace, engine: Engine) -> int:
    b = engine.backend
    info = {
        "backend": b.name,
        "model": b.model,
        "url": getattr(b, "url", None),
        "price_per_mtok": getattr(b, "price_per_mtok", None),
        "max_questions": engine.max_questions,
    }
    _out(info) if args.json else print("\n".join(f"{k}: {v}" for k, v in info.items()))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="exregex", description="Leave regex behind: find, replace, split, and extract text by what it means.")
    p.add_argument("--version", action="version", version=f"exregex {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str, fn: Any, unit: Optional[str] = "sentence", engine: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn, needs_engine=engine)
        return sp

    add("grep", "grep by meaning (same as exgrep; see exgrep --help)", None)  # listed for --help; main() hands it to grep_main

    sp = add("test", "does the whole text fit the meaning? exit 0 if yes", _cmd_test)
    sp.add_argument("meaning")
    sp.add_argument("file", nargs="?")
    _common(sp, unit_default=None)
    _engine_args(sp)

    for name, help_ in (
        ("findall", "every span that fits"),
        ("search", "the single best-fitting span"),
        ("scan", "every span with its probability, matching or not"),
    ):
        sp = add(name, help_, _cmd_spans)
        sp.add_argument("meaning")
        sp.add_argument("file", nargs="?")
        _common(sp)
        _engine_args(sp)

    sp = add("sub", "replace every span that fits", _cmd_sub)
    sp.add_argument("meaning")
    sp.add_argument("repl")
    sp.add_argument("file", nargs="?")
    _common(sp)
    _engine_args(sp)

    sp = add("diff", "audit an existing regex: print only the spans where it and the meaning disagree (exit 1 if any)", _cmd_diff)
    sp.add_argument("meaning")
    sp.add_argument("regex", help="the regex you use today (Python syntax)")
    sp.add_argument("file", nargs="?")
    _common(sp, unit_default="line")
    _engine_args(sp)

    sp = add("split", "split the text at spans that fit", _cmd_split)
    sp.add_argument("meaning")
    sp.add_argument("file", nargs="?")
    sp.add_argument("--keep", action="store_true", help="keep the separators")
    _common(sp, unit_default="line")
    _engine_args(sp)

    sp = add("extract", "the one candidate span that is WHAT", _cmd_extract)
    sp.add_argument("what", help='a noun phrase: "the total amount due"')
    sp.add_argument("file", nargs="?")
    _common(sp, unit_default=None)
    sp.add_argument(
        "-u", "--unit", type=_unit, default=None, help="candidate unit (default: money, date, email, phone, url, percent, number)"
    )
    _engine_args(sp)

    sp = add("classify", "pick one label", _cmd_classify)
    sp.add_argument("file", nargs="?")
    sp.add_argument("-o", "--option", action="append", default=[], metavar="LABEL[=DESCRIPTION]", required=True)
    _common(sp, unit_default=None, threshold=False)
    _engine_args(sp)

    sp = add("rate", "place the text on an ordered scale", _cmd_rate)
    sp.add_argument("question", help="refer to the input as `text`")
    sp.add_argument("file", nargs="?")
    sp.add_argument("-l", "--level", action="append", default=[], required=True, help="a level, lowest first; repeat 2-10 times")
    _common(sp, unit_default=None, threshold=False)
    _engine_args(sp)

    sp = add("units", "list units, or show the spans a unit finds (no requests, no cost)", _cmd_units, engine=False)
    sp.add_argument("file", nargs="?")
    sp.add_argument("-u", "--unit", type=_unit, default="sentence")
    sp.add_argument("--text")

    sp = add("backend", "show which backend and model would be used (no requests)", _cmd_backend)
    sp.add_argument("--json", action="store_true")
    _engine_args(sp)

    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "grep":
        return grep_main(argv[1:])
    return _run(p, argv, None)


def _utf8_streams() -> None:
    """Pipes on Windows default to the ANSI code page, which mangles any non-English span."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _stdout_closed(e: OSError) -> bool:
    """A reader that went away (| head -1): EPIPE on POSIX, EINVAL on Windows pipes."""
    import errno

    if isinstance(e, BrokenPipeError):
        return True
    if e.errno not in (errno.EPIPE, errno.EINVAL):
        return False
    try:
        sys.stdout.flush()
    except OSError:
        return True
    return False


def _silence_stdout() -> None:
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def _run(parser: argparse.ArgumentParser, argv: Optional[Sequence[str]], fn: Any) -> int:
    args = parser.parse_args(argv)
    fn = fn or args.fn
    _utf8_streams()
    load_dotenv()
    t0 = time.perf_counter()
    engine: Optional[Engine] = None
    try:
        if getattr(args, "needs_engine", True):
            engine = _make_engine(args)
        code = fn(args, engine)
    except ExRegexError as e:
        sys.stderr.write(f"{parser.prog}: {e}\n")
        return 2
    except (BrokenPipeError, OSError) as e:
        if _stdout_closed(e):
            _silence_stdout()
            return 0
        sys.stderr.write(f"{parser.prog}: {e}\n")
        return 2
    except ValueError as e:
        sys.stderr.write(f"{parser.prog}: {e}\n")
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        with contextlib.suppress(OSError):
            sys.stdout.flush()  # results first, then the cost line
        if engine is not None and not getattr(args, "quiet", False):
            sys.stderr.write(f"[{engine.backend.describe()}] {engine.stats.line()}, {time.perf_counter() - t0:.1f} s\n")
        if engine is not None:
            engine.close()
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
