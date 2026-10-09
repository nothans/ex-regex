from support_context import attach_context
from support_patterns import Message, failed_remedy

from exregex import semantic as sx


def inspect_ticket(messages: list[Message], *, engine, band, limits):
    prepared = attach_context(messages)
    bound = failed_remedy.bind(prepared, key=("id",), revision=("revision",))
    try:
        report = engine.evaluate(bound, band=band, limits=limits)
    except sx.EvaluationFailed as failure:
        return {"status": "evaluation_failed", "errors": failure.cause_counts}

    # A completed report can still contain semantic uncertainty.
    if report.matches:
        return {
            "status": "reported_failed_remedy",
            "matches": [
                {
                    "problem": match["problem"].key,
                    "remedy": match["remedy"].key,
                    "followup": match["followup"].key,
                }
                for match in report.matches
            ],
            "unresolved_candidates": len(report.uncertain_matches),
            "near_edge": report.near_edge,
        }
    if report.uncertain_matches:
        return {"status": "uncertain", "near_edge": report.near_edge}
    return {"status": "no_match_within_declared_pattern", "near_edge": report.near_edge}
