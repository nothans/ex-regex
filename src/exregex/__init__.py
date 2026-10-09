"""ex-regex: leave regex behind. Match text by what it means.

    import exregex as ex

    ex.sub("a way to contact a specific person", "[redacted]", text, unit="contact")
    ex.findall("asks for a refund", ticket)
    ex.extract("the total amount due", invoice, unit="money")

Decisions come from a System One model (TypeSafe's Jev by default) as typed answers with
probabilities. ex-regex finds candidate spans, asks, and hands back verbatim spans with offsets.
See the README for backends, costs, and where a regex is still the right tool.
"""

from ._types import (
    Answer,
    Choice,
    ChoiceAnswer,
    Decision,
    Noul,
    NoulAnswer,
    Question,
    Refusal,
    Score,
    ScoreAnswer,
)
from ._version import __version__
from .backends import Backend, OpenAIDecisions, Scripted, SystemOne, from_env, local, openai, openrouter, typesafe
from .engine import Engine, Stats, get_engine, set_engine
from .errors import BackendError, BudgetExceeded, CacheMiss, ConfigError, ExRegexError, LimitError, RefusedError
from .patterns import (
    Diff,
    Match,
    Pattern,
    Pick,
    Rating,
    Verdict,
    aclassify,
    aextract,
    afilter,
    afindall,
    arate,
    asearch,
    asplit,
    asub,
    atest,
    classify,
    compile,
    diff,
    extract,
    extractall,
    filter,
    findall,
    finditer,
    fullmatch,
    rank,
    rate,
    scan,
    search,
    split,
    sub,
    subn,
    test,
)
from .units import Span, unit_names
from .units import find as find_units


def ask(state, questions):
    """Ask any typed questions about a state with the default engine: {name: Answer}."""
    return get_engine().ask(state, questions)


def stats() -> Stats:
    """Requests, cache hits, tokens, cost, and latency so far on the default engine."""
    return get_engine().stats


__all__ = [
    "__version__",
    # by meaning
    "compile",
    "Pattern",
    "test",
    "fullmatch",
    "search",
    "findall",
    "finditer",
    "scan",
    "sub",
    "subn",
    "split",
    "filter",
    "rank",
    "extract",
    "extractall",
    "classify",
    "rate",
    "atest",
    "asearch",
    "afindall",
    "asub",
    "asplit",
    "afilter",
    "aextract",
    "aclassify",
    "arate",
    "Match",
    "Diff",
    "diff",
    "Verdict",
    "Pick",
    "Rating",
    # typed questions directly
    "ask",
    "Noul",
    "Choice",
    "Score",
    "Question",
    "Answer",
    "NoulAnswer",
    "ChoiceAnswer",
    "ScoreAnswer",
    "Refusal",
    "Decision",
    # engine and backends
    "Engine",
    "Stats",
    "get_engine",
    "set_engine",
    "stats",
    "Backend",
    "SystemOne",
    "OpenAIDecisions",
    "Scripted",
    "typesafe",
    "openrouter",
    "local",
    "openai",
    "from_env",
    # units
    "Span",
    "find_units",
    "unit_names",
    # errors
    "ExRegexError",
    "ConfigError",
    "LimitError",
    "BudgetExceeded",
    "BackendError",
    "RefusedError",
    "CacheMiss",
]
