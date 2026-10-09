"""Four-arm failed-remedy trial. Default mode is Scripted wiring, never model-quality evidence.

The direct arm owns enumeration, projections, deduplication, packing and composition.
Only the provider's single-attempt adapter is shared with the compiled arm.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "support"))

from support_context import attach_context  # noqa: E402
from support_patterns import Message, continues, failed_after, failed_remedy, problem, remedy, remedy_targets  # noqa: E402

import exregex  # noqa: E402
from exregex import Engine, Noul, Scripted, openrouter  # noqa: E402
from exregex import semantic as sx  # noqa: E402
from exregex.cli import load_dotenv  # noqa: E402
from exregex.errors import BackendError  # noqa: E402
from exregex.semantic.transport import AttemptResponse, CancelToken, NamedQuestion  # noqa: E402

DEFINITIONS = (problem, remedy, continues, remedy_targets, failed_after)
GAP = 3
WINDOW = timedelta(days=2)
NAMES = ("problem", "remedy", "followup")
BROAD = (
    "Does this conversation contain a customer reporting a problem, then an agent proposing a remedy "
    "for that problem, then a customer reporting that they attempted that remedy but the same problem "
    "continued? All three must occur in this order, in one ticket, within two days, with at most three "
    "intervening messages in each gap. Quoted or hypothetical reports and a different issue do not count. "
    "Use only the supplied records and context."
)


def data(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def hashed(value):
    return hashlib.sha256(data(value)).hexdigest()


def load_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def messages(item):
    return attach_context([Message(
        r["id"], r["revision"], r["ticket_id"], r["role"], r["text"], datetime.fromisoformat(r["sent_at"])
    ) for r in item["messages"]])


def structural(rows):
    """Independent bounded enumeration, deliberately no semantic planner calls."""
    grouped = defaultdict(list)
    for i, row in enumerate(rows):
        grouped[row.ticket_id].append(i)
    found = []
    for indices in grouped.values():
        for a, i in enumerate(indices):
            if rows[i].role != "customer":
                continue
            for b in range(a + 1, min(len(indices), a + GAP + 2)):
                j = indices[b]
                if rows[j].role != "agent":
                    continue
                for c in range(b + 1, min(len(indices), b + GAP + 2)):
                    k = indices[c]
                    if rows[k].role == "customer" and rows[k].sent_at - rows[i].sent_at <= WINDOW:
                        found.append((i, j, k))
    return sorted(found)


def capture(rows, triple):
    return [[rows[i].id, rows[i].revision] for i in triple]


def base_result():
    return {"complete": True, "outcome": "no", "matches": [], "uncertain": [], "observations": [], "errors": []}


class DirectCalls:
    """Bounded attempts and transparent payload recording for the direct/broad baselines.

    One attempt per payload in every trial arm. No retries are hidden by legacy APIs.
    A request failure remains an execution failure, not a negative answer.
    """

    def __init__(self, backend, directory, *, replay=False, budget=0.1, concurrency=4):
        self.backend, self.directory, self.replay = backend, directory, replay
        self.budget, self.concurrency = budget, concurrency
        self.lock = threading.Lock()
        self.reserved = 0.0
        self.attempts, self.observations = [], []
        self.tokens = self.cached = 0
        self.start_attempts = 0
        self.deadline = time.monotonic() + 180
        self.returned_model = None
        if not replay:
            self.directory.mkdir(parents=True, exist_ok=True)

    def request(self, state, definitions):
        if time.monotonic() >= self.deadline:
            raise ValueError("baseline dispatch deadline exhausted")
        questions = tuple(NamedQuestion(d.name, Noul(d.question, d.criteria)) for d in definitions)
        prepared = self.backend.prepare(data(state), questions)
        caps = prepared.capabilities
        ident = hashed([asdict(caps), prepared.body.hex()])
        path = self.directory / f"{ident}.json"
        estimated_tokens = len(prepared.body) + 32 * len(questions)
        if caps.price_per_mtok is None or caps.output_price_per_mtok != 0:
            raise ValueError("baseline requires explicit input price and free outputs")
        estimate = estimated_tokens * caps.price_per_mtok / 1e6
        if len(data(state).decode()) > caps.max_state_chars or (caps.max_input_tokens and estimated_tokens > caps.max_input_tokens):
            raise ValueError("baseline payload exceeds backend limits")
        if caps.max_state_question_tokens and max(len(data(state)) + len(data(q.question.to_wire())) + 128 for q in questions) > caps.max_state_question_tokens:
            raise ValueError("baseline state/question exceeds backend limits")
        with self.lock:
            if self.replay:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["request_body"] != prepared.body.hex():
                    raise ValueError("baseline replay payload mismatch")
                response = AttemptResponse(200, bytes.fromhex(saved["response_body"]), 0, False)
                self.cached += 1
            else:
                if self.reserved + estimate > self.budget or len(self.attempts) - self.start_attempts >= 2048:
                    raise ValueError("baseline cost/attempt budget exhausted")
                self.reserved += estimate
                evidence = {"request": ident, "estimated_cost_usd": estimate, "reported_cost_usd": None, "status": None}
                self.attempts.append(evidence)
        if not self.replay:
            started = time.perf_counter()
            response = self.backend.attempt(prepared, timeout_s=min(45, max(0.001, self.deadline - time.monotonic())), cancel=CancelToken())
            evidence.update(status=response.status, elapsed_ms=(time.perf_counter() - started) * 1000)
            if response.status is None or not 200 <= response.status < 300:
                raise ValueError("baseline transport failed")
        parsed = self.backend.parse_strict(response, prepared)
        with self.lock:
            if not self.replay:
                evidence["reported_cost_usd"] = parsed.cost_usd
                if parsed.cost_usd is not None:
                    self.reserved += parsed.cost_usd - estimate
            if self.returned_model is not None and parsed.model != self.returned_model:
                raise ValueError("returned model changed during baseline run")
            self.returned_model = parsed.model
            if time.monotonic() >= self.deadline:
                raise ValueError("baseline response completed after deadline")
            if not self.replay:
                self.tokens += parsed.input_tokens or 0
                path.write_bytes(data({"request_body": prepared.body.hex(), "response_body": response.body.hex()}))
            self.observations.append({"request": ident, "model": parsed.model})
        return parsed.answers

    def batch(self, tasks, *, naive=False):
        # tasks: logical key -> (projected state, one of the same versioned definitions).
        # Pack identical states, deduplicate questions; preserve mappings to every source identity.
        groups = defaultdict(list)
        for key, (state, definition) in tasks.items():
            groups[data(state)].append((key, state, definition))
        jobs = []
        maximum = 1 if naive else self.backend.capabilities().max_questions
        for entries in groups.values():
            definitions = {d.name: d for _, _, d in entries}
            names = sorted(definitions)
            for start in range(0, len(names), maximum):
                selected = names[start:start + maximum]
                jobs.append((entries, [definitions[name] for name in selected]))

        def run(job):
            entries, definitions = job
            answers = self.request(entries[0][1], definitions)
            return {key: answers[d.name].p for key, _, d in entries if d.name in answers}

        values = {}
        if naive:
            results = map(run, jobs)
            for result in results:
                values.update(result)
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                for result in pool.map(run, jobs):
                    values.update(result)
        return values


class TransportMeter:
    """Observe the actual adapter boundary equally for every model-backed arm."""

    def __init__(self, backend):
        self.attempts = []
        self.models = []
        self.active = self.peak = 0
        self.lock = threading.Lock()
        attempt, parse = backend.attempt, backend.parse_strict

        def measured_attempt(request, *, timeout_s, cancel):
            evidence = {"questions": len(request.questions), "payload_bytes": len(request.body), "status": None}
            with self.lock:
                self.attempts.append(evidence)
                self.active += 1
                self.peak = max(self.peak, self.active)
            started = time.perf_counter()
            try:
                response = attempt(request, timeout_s=timeout_s, cancel=cancel)
                evidence["status"] = response.status
                return response
            finally:
                evidence["elapsed_ms"] = (time.perf_counter() - started) * 1000
                with self.lock:
                    self.active -= 1

        def measured_parse(response, request):
            observation = parse(response, request)
            with self.lock:
                self.models.append(observation.model)
            return observation

        backend.attempt, backend.parse_strict = measured_attempt, measured_parse


def recording_root(output, *, replay_from=None):
    """Cold runs never read another run's data; replay points at one explicit record."""
    return (output if replay_from is None else replay_from) / "observations"


def direct(rows, calls, band, *, naive=False):
    triples = structural(rows)
    result = base_result()
    result["candidates"] = len(triples)
    tasks = {}
    for triple in triples:
        for definition, i in zip(DEFINITIONS[:3], triple, strict=True):
            row = rows[i]
            tasks[(definition.name, i)] = ({"item": {"text": row.text, "prior_turns": list(row.prior_turns)}, "context": {}}, definition)
    unary = calls.batch(tasks, naive=naive)
    live = [t for t in triples if all(band.apply(unary[(d.name, i)]) is not sx.NO for d, i in zip(DEFINITIONS[:3], t, strict=True))]
    pairs = {}
    for i, j, k in live:
        pairs[(remedy_targets.name, i, j)] = ({"left": {"text": rows[i].text}, "right": {"text": rows[j].text}, "context": {}}, remedy_targets)
        pairs[(failed_after.name, i, j, k)] = ({"left": {"text": rows[j].text}, "right": {"text": rows[k].text}, "context": {"problem_text": rows[i].text}}, failed_after)
    relations = calls.batch(pairs, naive=naive)
    for i, j, k in live:
        probabilities = [unary[(d.name, n)] for d, n in zip(DEFINITIONS[:3], (i, j, k), strict=True)]
        probabilities += [relations[(remedy_targets.name, i, j)], relations[(failed_after.name, i, j, k)]]
        outcomes = [band.apply(p) for p in probabilities]
        if sx.NO in outcomes:
            continue
        result["matches" if all(o is sx.YES for o in outcomes) else "uncertain"].append(capture(rows, (i, j, k)))
    for key, p in {**unary, **relations}.items():
        result["observations"].append({"name": key[0], "records": [[rows[i].id, rows[i].revision] for i in key[1:]], "p": p})
    result["outcome"] = "yes" if result["matches"] else "unknown" if result["uncertain"] else "no"
    return result


KEYWORDS = (
    re.compile(r"\b(error|fail\w*|crash\w*|broken|cannot|can't|problem)\b", re.I),
    re.compile(r"\b(try|run|restart|reinstall|remove|install|clear|check|type)\b", re.I),
    re.compile(r"\b(still|same|didn't work|doesn't work|no luck|nope)\b", re.I),
)


def keyword(rows):
    result = base_result()
    triples = structural(rows)
    result["candidates"] = len(triples)
    result["matches"] = [capture(rows, t) for t in triples if all(rx.search(rows[i].text) for rx, i in zip(KEYWORDS, t, strict=True))]
    result["outcome"] = "yes" if result["matches"] else "no"
    return result


def compiled(rows, engine, band, limits):
    planned = engine.plan(failed_remedy.bind(rows, key=("id",), revision=("revision",)), band=band, limits=limits)
    report = engine.evaluate(planned, errors="collect")
    result = base_result()
    result.update(complete=report.complete, outcome=None if report.outcome is None else report.outcome.value,
                  matches=[[[m[n].key, m[n].revision] for n in NAMES] for m in report.matches],
                  uncertain=[[[m[n].key, m[n].revision] for n in NAMES] for m in report.uncertain_matches],
                  errors=[asdict(e) for e in report.errors], candidates=report.coverage.structural_candidates,
                  trace=asdict(report.trace))
    source_atoms = defaultdict(set)
    for candidate in planned.candidates:
        i, j, k = candidate.indices
        for index, expression in zip(candidate.indices, candidate.expressions, strict=True):
            for operation, ident in expression:
                if operation == "leaf":
                    source_atoms[ident].add((index,))
        for ident in candidate.relation_ids:
            definition = planned.leaves[ident].definition
            source_atoms[ident].add((i, j, k) if definition.name == failed_after.name else (i, j))
    for leaf in report.leaves:
        for indices in sorted(source_atoms[leaf.id]):
            result["observations"].append({"name": planned.leaves[leaf.id].definition.name,
                                           "records": [[rows[i].id, rows[i].revision] for i in indices], "p": leaf.answer.p})
    return result


def profile(args, manifest):
    sources = [ROOT / "examples" / "support" / "support_patterns.py", ROOT / "examples" / "support" / "support_context.py", Path(__file__)]
    library = Path(exregex.__file__).resolve().parent
    return {
        "dataset": manifest["files"], "backend": "scripted" if args.mode == "scripted" else "openrouter", "model": args.model, "expected_model": args.expected_model, "band": [args.no, args.yes],
        "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        "library_fingerprint_schema": 2,
        "library_source_sha256": hashed({(Path("src/exregex") / p.relative_to(library)).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(library.rglob("*.py"))}),
        "reference_labels_sha256": None if args.reference_labels is None else hashlib.sha256(args.reference_labels.read_bytes()).hexdigest(),
        "gap": GAP, "window_s": WINDOW.total_seconds(), "attempts_per_request": 1,
        "concurrency": 4, "budget_per_arm_usd": args.budget, "arms": args.arms,
        "packing_difference": "Direct IDs use definition names and deduplicate identical projected states; compiled IDs include definition/input identities. Record payload projections and question text match; measure these payload differences.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--mode", choices=("scripted", "live", "replay"), default="scripted")
    parser.add_argument("--model", default="typesafe/jev-1.13")
    parser.add_argument("--expected-model", default="typesafe/jev-1.13-20260917", help="Reject responses if the tested alias resolves to another model.")
    parser.add_argument("--arms", nargs="+", choices=("keyword", "broad", "direct", "compiled", "naive"), default=["keyword", "broad", "direct", "compiled"])
    parser.add_argument("--budget", type=float, default=0.10)
    parser.add_argument("--no", type=float, default=0.15)
    parser.add_argument("--yes", type=float, default=0.85)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--freeze", type=Path, help="Before testing, supply an identical development profile; never tune after test exposure.")
    parser.add_argument("--reference-labels", type=Path, help="Human test labels to hash into the frozen profile; required for test runs.")
    parser.add_argument("--replay-from", type=Path, help="Original cold run directory; required in replay mode.")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; choose a new run directory")
    manifest = json.loads((args.dataset / "manifest.json").read_text(encoding="utf-8"))
    source = args.dataset / f"{args.split}.jsonl"
    if hashlib.sha256(source.read_bytes()).hexdigest() != manifest["files"][source.name]:
        parser.error("dataset hash mismatch")
    profile_args = argparse.Namespace(**vars(args))
    recorded_profile = None
    if args.mode == "replay":
        if args.replay_from is None:
            parser.error("replay requires --replay-from pointing to the original cold run")
        recorded_profile = json.loads((args.replay_from / "profile.json").read_bytes())
        recorded_environment = json.loads((args.replay_from / "environment.json").read_bytes())
        if recorded_environment["mode"] not in ("live", "scripted") or recorded_environment["split"] != args.split:
            parser.error("replay source must be an original cold run of the same split")
        if recorded_profile["backend"] == "scripted":
            profile_args.mode = "scripted"
    elif args.replay_from is not None:
        parser.error("--replay-from is only valid in replay mode")
    config = profile(profile_args, manifest)
    if recorded_profile is not None and config != recorded_profile:
        parser.error("replay configuration differs from the recorded profile")
    if args.split == "test" and (args.freeze is None or json.loads(args.freeze.read_text(encoding="utf-8")) != config):
        parser.error("test evaluation requires a matching frozen development profile")
    if args.split == "test":
        if args.reference_labels is None:
            parser.error("test evaluation requires frozen human reference labels")
        reference = load_lines(args.reference_labels)
        expected = {r["id"]: r["input_hash"] for r in load_lines(source)}
        if len(reference) != len(expected) or {r["id"]: r["input_hash"] for r in reference} != expected:
            parser.error("reference labels do not cover the frozen test set")
        if any(r.get("status") not in ("adjudicated", "single_annotator") or not r.get("reviewer") for r in reference):
            parser.error("reference labels still require human review")
    if args.limit < 1 or args.limit > 100 or (args.split == "test" and args.limit != 100):
        parser.error("limit is 1..100 for development; test must include all 100 conversations")
    band = sx.Band(no=args.no, yes=args.yes)
    limits = sx.Limits(max_requests=2048, max_attempts=1, max_estimated_cost_usd=args.budget, deadline_s=180, request_timeout_s=45)
    if args.mode == "live":
        load_dotenv(ROOT)
    args.output.mkdir(parents=True)
    (args.output / "profile.json").write_bytes(data(config))
    (args.output / "environment.json").write_bytes(data({
        "python": platform.python_version(), "platform": platform.platform(), "processor": platform.processor(),
        "logical_cpus": os.cpu_count(), "split": args.split, "mode": args.mode, "backend": config["backend"],
        "command": sys.argv, "replay_from": None if args.replay_from is None else str(args.replay_from.resolve()),
        "cache_policy": "isolated per cold run, arm and conversation; exact within-conversation reuse",
    }))
    items = load_lines(source)[:args.limit]
    cache_root = recording_root(args.output, replay_from=args.replay_from)
    for arm in args.arms:
        backend = Scripted(lambda state, qs: dict.fromkeys(qs, 0.95)) if config["backend"] == "scripted" else openrouter(
            api_key="replay-only-never-sent" if args.mode == "replay" else None, model=args.model)
        if config["backend"] != "scripted":
            original_parse = backend.parse_strict

            def pinned_parse(response, request, parse=original_parse):
                observation = parse(response, request)
                if observation.model != args.expected_model:
                    raise BackendError("returned model differs from the frozen trial profile")
                return observation

            backend.parse_strict = pinned_parse
        meter = TransportMeter(backend)
        spent = 0.0
        output = args.output / f"{arm}.jsonl"
        for item in items:
            case_root = cache_root / arm / hashed(item["id"])
            remaining = max(0.0, args.budget - spent)
            calls = DirectCalls(backend, case_root, replay=args.mode == "replay", budget=remaining, concurrency=config["concurrency"])
            meter.peak = 0
            with Engine(backend, cache=case_root / "compiled.sqlite", replay=args.mode == "replay", max_cost_usd=remaining,
                        concurrency=config["concurrency"]) as engine:
                rows = messages(item)
                started = time.perf_counter()
                before = engine.stats.as_dict()
                prior_attempts, prior_cached, prior_tokens = len(calls.attempts), calls.cached, calls.tokens
                prior_meter, prior_models = len(meter.attempts), len(meter.models)
                calls.start_attempts = prior_attempts
                calls.deadline = time.monotonic() + 180
                try:
                    if arm == "keyword":
                        result = keyword(rows)
                    elif arm in ("direct", "naive"):
                        result = direct(rows, calls, band, naive=arm == "naive")
                    elif arm == "compiled":
                        result = compiled(rows, engine, band, limits)
                    else:
                        broad = sx.Predicate("broad", question=BROAD, fields={"text": ("text",)})
                        allowed = [{"id": r.id, "role": r.role, "text": r.text, "prior_turns": list(r.prior_turns), "sent_at": r.sent_at.isoformat(), "ticket_id": r.ticket_id} for r in rows]
                        answers = calls.request({"records": allowed}, [broad])
                        result = base_result()
                        result.update(outcome=band.apply(answers["broad"].p).value, matches=None, uncertain=None)
                except Exception as exc:
                    result = base_result()
                    result.update(complete=False, outcome=None, errors=[{"code": type(exc).__name__, "message": str(exc)}])
                result.update(id=item["id"], input_hash=item["input_hash"], wall_ms=(time.perf_counter() - started) * 1000,
                              source_records=len(rows), source_utf8_bytes=sum(len(r.text.encode()) for r in rows),
                              returned_models=sorted(set(meter.models[prior_models:])), observed_peak_concurrency=meter.peak)
                if arm == "compiled":
                    after = engine.stats.as_dict()
                    result["stats"] = {k: after[k] - before[k] for k in ("requests", "cached", "retries", "input_tokens", "cost_usd")}
                    spent += engine.stats.cost_usd + engine._semantic_liability
                else:
                    attempts = calls.attempts[prior_attempts:]
                    result["stats"] = {"requests": len(attempts), "cached": calls.cached - prior_cached, "input_tokens": calls.tokens - prior_tokens,
                                       "cost_usd": sum(a["reported_cost_usd"] or 0 for a in attempts), "unknown_charge_attempts": sum(a["reported_cost_usd"] is None for a in attempts)}
                    result["attempts"] = attempts
                    spent += calls.reserved
                result["stats"]["dispatched_questions"] = sum(a["questions"] for a in meter.attempts[prior_meter:])
                result["stats"]["dispatched_payload_bytes"] = sum(a["payload_bytes"] for a in meter.attempts[prior_meter:])
                with output.open("ab") as stream:
                    stream.write(data(result) + b"\n")
        print(json.dumps({"arm": arm, "conversations": len(items), "output": str(output), "mode": args.mode}))


if __name__ == "__main__":
    main()
