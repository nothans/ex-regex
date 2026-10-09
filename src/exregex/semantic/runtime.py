"""Explicit evaluation with physical-attempt accounting and cooperative cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from types import MappingProxyType

from ..backends import RETRYABLE
from ..errors import BackendError, BudgetExceeded, CacheMiss, ExRegexError
from .errors import (
    CandidateLimitExceeded,
    DeadlineExceeded,
    DefinitionError,
    EvaluationFailed,
    ExecutionFailed,
    InputError,
    PlanMismatch,
    PlanningLimitExceeded,
    RequestLimitExceeded,
    ResultLimitExceeded,
    UnsupportedCapability,
)
from .planner import Plan, engine_fingerprint, evaluate_expression, leaf_ids, pack, plan
from .results import (
    NO,
    UNKNOWN,
    YES,
    Attempt,
    Coverage,
    DefinitionIdentity,
    Evaluation,
    ExecutionIssue,
    LeafObservation,
    MatchReport,
    SemanticMatch,
    Trace,
    conjunction,
)
from .sequence import SequencePattern
from .transport import AttemptResponse, CancelToken


class Run:
    def __init__(self, engine, planned, cancel):
        self.engine, self.plan, self.cancel = engine, planned, cancel
        self.deadline = time.monotonic() + planned.limits.deadline_s
        self.lock = threading.Lock()
        self.attempts = []
        self.cached = []
        self.issues = []
        self.causes = []
        self.leaves = {}
        self.reservations = 0.0
        self.reported = 0.0
        self.unknown = 0
        self.sent = 0
        self.returned_model = None

    def check(self):
        self.cancel.check()
        if time.monotonic() >= self.deadline:
            raise DeadlineExceeded("semantic dispatch deadline exceeded")

    def trace(self):
        return Trace(
            self.plan.id,
            tuple(self.attempts),
            tuple(sorted(self.cached)),
            self.plan.pruned_nodes,
            self.reservations,
            self.reported,
            self.unknown,
            self.plan.cache_path,
        )

    def accept(self, request, observation, cached):
        with self.lock:
            if self.returned_model is not None and self.returned_model != observation.model:
                raise BackendError("inconsistent returned model IDs within the run")
            self.returned_model = observation.model
            for ident in request.leaf_ids:
                item = self.plan.leaves[ident]
                answer = observation.answers[ident]
                self.leaves[ident] = LeafObservation(
                    ident,
                    item.definition_digest,
                    item.input_digest,
                    answer,
                    self.plan.band.apply(answer.p),
                    self.plan.capabilities.model,
                    observation.model,
                    request.id,
                    cached,
                    tuple(sorted(item.records, key=lambda record: record.index)),
                    DefinitionIdentity(item.definition.name, item.definition.version),
                    self.plan.band,
                )

    def reserve(self, request):
        # Lock order is always Engine then Run. Unknown dispatched charges remain liabilities.
        with self.engine._lock, self.lock:
            if self.sent >= self.plan.limits.max_requests:
                raise RequestLimitExceeded("physical attempt limit exhausted")
            estimate = request.estimated_cost_usd
            if self.reservations + estimate > self.plan.limits.max_estimated_cost_usd + 1e-15:
                raise BudgetExceeded("evaluation attempt cost estimate exhausted")
            spent = self.engine.stats.cost_usd + self.engine._reserved + self.engine._semantic_liability
            if self.engine.max_cost_usd is not None and spent + estimate > self.engine.max_cost_usd + 1e-15:
                raise BudgetExceeded("shared Engine cost budget exhausted")
            self.engine._reserved += estimate
            self.reservations += estimate
            self.sent += 1

    def request(self, request):
        self.check()
        response: AttemptResponse | None
        stored = self.engine._semantic_store.get(request.id)
        if stored is not None:
            if (
                not isinstance(stored, dict)
                or stored.get("schema") != "semantic-observation-v1"
                or stored.get("request_body") != request.prepared.body.hex()
            ):
                raise BackendError("cached observation manifest does not match the request")
            try:
                response = AttemptResponse(200, bytes.fromhex(stored["response_body"]), 0, False)
            except (KeyError, ValueError, TypeError):
                raise BackendError("cached observation payload is malformed") from None
            observation = self.engine.backend.parse_strict(response, request.prepared)
            self.check()
            self.accept(request, observation, True)
            with self.lock:
                self.cached.append(request.id)
            with self.engine._lock:
                self.engine.stats.cached += 1
            return
        if self.engine.replay:
            raise CacheMiss("semantic replay has no exact observation for the request")
        for number in range(1, self.plan.limits.max_attempts + 1):
            self.check()
            while not self.engine._slots.acquire(timeout=min(0.05, max(0, self.deadline - time.monotonic()))):
                self.check()
            reserved = False
            dispatched = False
            observation = None
            response = None
            error: Exception | None = None
            started = time.monotonic()
            try:
                self.check()
                self.reserve(request)
                reserved = True
                self.check()
                dispatched = True  # conservatively charge if the transport raises after entry
                response = self.engine.backend.attempt(
                    request.prepared,
                    timeout_s=min(self.plan.limits.request_timeout_s, self.deadline - time.monotonic()),
                    cancel=self.cancel,
                )
                dispatched = response.dispatched
                if response.status is None or not 200 <= response.status < 300:
                    raise BackendError(
                        "semantic transport attempt failed",
                        status=response.status,
                        retryable=response.status is None or response.status in RETRYABLE,
                    )
                observation = self.engine.backend.parse_strict(response, request.prepared)
                self.check()
                self.accept(request, observation, False)
            except ExRegexError as exc:
                error = exc
            except Exception as exc:
                error = exc
                raise
            finally:
                if reserved:
                    cost = (
                        observation.cost_usd
                        if observation is not None
                        else 0.0
                        if request.prepared.capabilities.adapter == "scripted"
                        else None
                    )
                    with self.engine._lock, self.lock:
                        self.engine._reserved -= request.estimated_cost_usd
                        if dispatched:
                            self.engine.stats.requests += 1
                            self.engine.stats.retries += number > 1
                            if cost is None:
                                self.engine._semantic_liability += request.estimated_cost_usd
                                self.unknown += 1
                            else:
                                self.engine.stats.cost_usd += cost
                                self.reported += cost
                            if observation is not None and observation.input_tokens is not None:
                                self.engine.stats.input_tokens += observation.input_tokens
                            elapsed = (time.monotonic() - started) * 1000
                            self.engine.stats.latencies_ms.append(elapsed)
                            if error is not None:
                                self.engine.stats.failures += 1
                            self.attempts.append(
                                Attempt(
                                    request.id,
                                    number,
                                    True,
                                    elapsed,
                                    None if response is None else response.status,
                                    request.estimated_cost_usd,
                                    cost,
                                    "ok" if error is None else type(error).__name__,
                                )
                            )
                        else:
                            self.reservations -= request.estimated_cost_usd
                            self.sent -= 1
                self.engine._slots.release()
            self.cancel.check()
            if error is None:
                assert response is not None
                self.engine._semantic_store.put(
                    request.id,
                    {
                        "schema": "semantic-observation-v1",
                        "request_body": request.prepared.body.hex(),
                        "response_body": response.body.hex(),
                        "capabilities": asdict(request.prepared.capabilities),
                        "attempts": [asdict(a) for a in self.attempts if a.request_id == request.id],
                    },
                )
                return
            if not isinstance(error, BackendError) or not error.retryable or number >= self.plan.limits.max_attempts:
                raise error
            delay = response.retry_after_s if response is not None else None
            delay = min(2.0, 0.1 * 2 ** (number - 1)) if delay is None else delay
            if delay >= self.deadline - time.monotonic():
                raise DeadlineExceeded("remaining deadline cannot honor retry delay")
            self.cancel.event.wait(delay)
            self.check()

    def stage(self, requests, stage):
        def execute(request):
            try:
                self.request(request)
            except (DefinitionError, InputError, PlanMismatch, UnsupportedCapability, CandidateLimitExceeded, PlanningLimitExceeded):
                raise
            except ExRegexError as error:
                with self.lock:
                    self.causes.append(error)
                    self.issues.append(
                        ExecutionIssue(
                            type(error).__name__,
                            stage,
                            str(error),
                            request.id,
                            request.leaf_ids,
                            retryable=isinstance(error, BackendError) and error.retryable,
                        )
                    )
            except OSError as underlying:
                failure = ExecutionFailed("semantic request processing or persistence failed")
                failure.__cause__ = underlying
                with self.lock:
                    self.causes.append(failure)
                    self.issues.append(ExecutionIssue(type(failure).__name__, stage, str(failure), request.id, request.leaf_ids))

        if len(requests) <= 1 or self.engine.concurrency == 1:
            for request in requests:
                self.cancel.check()
                execute(request)
        else:
            with ThreadPoolExecutor(max_workers=min(len(requests), self.engine.concurrency)) as pool:
                list(pool.map(execute, requests))


def evaluate(engine, query_or_plan, *, band=None, limits=None, errors="raise", _cancel=None):
    if errors not in ("raise", "collect"):
        raise DefinitionError("errors must be 'raise' or 'collect'")
    if isinstance(query_or_plan, Plan):
        if band is not None or limits is not None:
            raise PlanMismatch("an existing plan cannot replace its band or limits")
        planned = query_or_plan
        if planned.engine_id != id(engine) or planned.fingerprint != engine_fingerprint(engine):
            raise PlanMismatch("engine/backend settings changed since planning")
    else:
        kwargs = {} if limits is None else {"limits": limits}
        planned = plan(engine, query_or_plan, band=band, **kwargs)
    run = Run(engine, planned, _cancel if _cancel is not None else CancelToken())
    run.stage(planned.requests, "unary")
    matches: list[SemanticMatch] = []
    uncertain: list[SemanticMatch] = []
    result: Evaluation
    definition = planned.query.definition
    if isinstance(definition, SequencePattern):
        outcomes = {key: value.outcome for key, value in run.leaves.items()}
        surviving = []
        resolved = {}
        needed: set[str] = set()
        for index, candidate in enumerate(planned.candidates):
            ids = {ident for expr in candidate.expressions for ident in leaf_ids(expr)}
            if not ids <= outcomes.keys():
                continue
            value = YES
            for expr in candidate.expressions:
                value = conjunction(value, evaluate_expression(expr, outcomes))
            if value is NO:
                resolved[index] = NO
            else:
                surviving.append((index, value))
                needed.update(candidate.relation_ids)
        run.stage(pack(engine, {ident: planned.leaves[ident] for ident in needed}), "relation")
        outcomes = {key: value.outcome for key, value in run.leaves.items()}
        for index, value in surviving:
            candidate = planned.candidates[index]
            if not set(candidate.relation_ids) <= outcomes.keys():
                continue
            for ident in candidate.relation_ids:
                value = conjunction(value, outcomes[ident])
            resolved[index] = value
        accepted = rejected = unresolved = evaluated = 0
        truncated = False
        for index, candidate in enumerate(planned.candidates):
            if index not in resolved:
                continue
            value = resolved[index]
            evaluated += 1
            if value is NO:
                rejected += 1
                continue
            if value is YES:
                accepted += 1
            else:
                unresolved += 1
            if len(matches) + len(uncertain) >= planned.limits.max_results:
                error = ResultLimitExceeded("an additional returnable tuple exceeds max_results")
                run.causes.append(error)
                run.issues.append(ExecutionIssue("ResultLimitExceeded", "enumeration", str(error), candidate_ids=(index,)))
                truncated = True
                break
            captures = MappingProxyType(
                {step.name: planned.query.records[i] for step, i in zip(definition.steps, candidate.indices, strict=True)}
            )
            match_ids = tuple(sorted({ident for expr in candidate.expressions for ident in leaf_ids(expr)} | set(candidate.relation_ids)))
            match = SemanticMatch(value, captures, match_ids)
            (matches if value is YES else uncertain).append(match)
        complete = not run.issues
        outcome = (YES if matches else UNKNOWN if uncertain else NO) if complete else None
        coverage = Coverage(
            len(planned.query.records),
            planned.input_groups,
            len(planned.candidates),
            evaluated,
            accepted,
            rejected,
            unresolved,
            len(planned.candidates) - evaluated,
            MappingProxyType(
                {
                    "exact": True,
                    "unary": not any(i.stage == "unary" for i in run.issues),
                    "relation": not any(i.stage in ("unary", "relation") for i in run.issues),
                    "enumeration": complete,
                }
            ),
            sum(leaf.near_edge for leaf in run.leaves.values()),
        )
        result = MatchReport(
            outcome,
            complete,
            ordered_leaves(run.leaves),
            tuple(run.issues),
            run.trace(),
            tuple(matches),
            tuple(uncertain),
            truncated,
            coverage,
        )
    else:
        complete = not run.issues
        assert planned.expression is not None
        outcome = evaluate_expression(planned.expression, {key: value.outcome for key, value in run.leaves.items()}) if complete else None
        result = Evaluation(outcome, complete, ordered_leaves(run.leaves), tuple(run.issues), run.trace())
    if run.causes and errors == "raise":
        raise EvaluationFailed(result, tuple(run.causes)) from run.causes[0]
    return result


def ordered_leaves(leaves):
    return tuple(
        sorted(
            leaves.values(),
            key=lambda leaf: (
                tuple(record.index for record in leaf.records),
                leaf.definition.name,
                str(leaf.definition.version),
                leaf.id,
            ),
        )
    )


async def aevaluate(engine, query_or_plan, **kwargs):
    cancel = CancelToken()
    task = asyncio.create_task(asyncio.to_thread(evaluate, engine, query_or_plan, _cancel=cancel, **kwargs))

    class CancellationWaiter(asyncio.Future):
        def cancel(self, msg=None):
            # Task.cancel cancels its waiter synchronously, before the coroutine next runs.
            cancel.cancel()
            return super().cancel(msg)

    waiter = CancellationWaiter()

    def finished(done):
        if waiter.done():
            return
        if done.cancelled():
            waiter.cancel()
        elif done.exception() is not None:
            waiter.set_exception(done.exception())
        else:
            waiter.set_result(done.result())

    task.add_done_callback(finished)
    try:
        return await waiter
    except asyncio.CancelledError:
        cancel.cancel()
        # Every new cancellation can interrupt a shielded wait, but cannot allow
        # this caller to return while its already-dispatched transport still runs.
        while not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(task)
        if not task.cancelled():
            task.exception()  # consume any failure that completed during cancellation
        raise
