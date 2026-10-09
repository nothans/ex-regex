"""Independent runtime QA. Scripted only; no model API calls."""

import asyncio
import importlib.util
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from exregex import Engine, Scripted
from exregex import semantic as sx
from exregex.errors import BackendError

ROOT = Path(__file__).resolve().parents[1]
BAND = sx.Band(0.15, 0.85)


def pred(name="p", **kwargs):
    return sx.Predicate(name, question="Does item.text meet " + name + "?", fields={"text": ("text",)}, **kwargs)


def fixed(p=0.95, **kwargs):
    return Engine(Scripted(lambda state, qs: dict.fromkeys(qs, p)), **kwargs)


@pytest.fixture(scope="module")
def cookbook():
    # Exercise the public files applications can actually import, not copies of
    # their earlier design-document snippets. Importing application.py is safe;
    # this fixture never calls its credential-reading make_inspector factory.
    modules = {}
    previous = {}
    for name in ("support_patterns", "support_context", "support_service", "application", "archive_worker"):
        spec = importlib.util.spec_from_file_location(name, ROOT / "examples/support" / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        previous[name] = sys.modules.get(name)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        modules[name] = module
    yield modules
    for name, old in previous.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


def messages(cookbook):
    Message = cookbook["support_patterns"].Message
    stamp = datetime(2026, 10, 8, tzinfo=timezone.utc)
    return [
        Message("m1", "1", "t1", "customer", "My login does not work.", stamp),
        Message("m2", "2", "t1", "agent", "Clear your cookies then retry.", stamp + timedelta(minutes=1)),
        Message("m3", "3", "t1", "customer", "I cleared cookies and the login still fails.", stamp + timedelta(minutes=2)),
    ]


@pytest.mark.parametrize("p,status", [(0.95, "reported_failed_remedy"), (0.5, "uncertain"), (0.05, "no_match_within_declared_pattern")])
def test_complete_cookbook_service_and_worker(cookbook, p, status):
    e = fixed(p)
    row = cookbook["support_service"].inspect_ticket(messages(cookbook), engine=e, band=BAND, limits=sx.Limits())
    assert row["status"] == status
    if p == 0.95:
        assert row["matches"] == [{"problem": "m1", "remedy": "m2", "followup": "m3"}]
        assert len(e.backend.calls) == 5
        assert any(s.get("context") == {"problem_text": "My login does not work."} for s, q in e.backend.calls)
    again = list(cookbook["archive_worker"].inspect_archive([messages(cookbook)] * 2, engine=e, band=BAND, limits=sx.Limits()))
    assert again == [row, row]
    assert e.stats.cached >= 2


@pytest.mark.parametrize(
    "unary,relation,outcome", [(0.5, 0.99, sx.UNKNOWN), (0.99, 0.5, sx.UNKNOWN), (0.99, 0.01, sx.NO), (0.01, 0.99, sx.NO)]
)
def test_relations_cannot_erase_unknown(cookbook, unary, relation, outcome):
    prepared = cookbook["support_context"].attach_context(messages(cookbook))
    query = cookbook["support_patterns"].failed_remedy.bind(prepared, key=("id",), revision=("revision",))
    e = Engine(Scripted(lambda state, qs: dict.fromkeys(qs, unary if "item" in state else relation)))
    report = e.evaluate(query, band=BAND)
    assert report.complete and report.outcome is outcome
    assert len(report.uncertain_matches) == (outcome is sx.UNKNOWN)
    assert len(e.backend.calls) == (3 if unary < 0.15 else 5)


def test_empty_and_cross_thread_cookbook(cookbook):
    from dataclasses import replace

    service = cookbook["support_service"].inspect_ticket
    e = fixed()
    assert service([], engine=e, band=BAND, limits=sx.Limits())["status"].startswith("no_match")
    rows = messages(cookbook)
    rows[1] = replace(rows[1], ticket_id="other")
    assert service(rows, engine=e, band=BAND, limits=sx.Limits())["status"].startswith("no_match")
    assert not e.backend.calls


def test_snapshot_including_context_isolated_from_caller_mutation():
    original = {"text": ["old"], "role": "a", "ignored": object()}
    context = {"guide": {"rule": ["old-rule"]}, "ignored": object()}
    p = pred(context_fields={"rule": ("guide", "rule")})
    query = (sx.field("role").eq("a") & p).bind(original, key="one", revision="v1", context=context)
    original["text"].append("new")
    context["guide"]["rule"][0] = "new-rule"
    e = fixed()
    report = e.evaluate(query, band=BAND)
    state = e.backend.calls[0][0]
    assert state == {"item": {"text": ["old"]}, "context": {"rule": ["old-rule"]}}
    assert report.leaves[0].records[0].data["role"] == "a"


@pytest.mark.parametrize("suffix", ["sqlite", "jsonl"])
def test_full_relations_replay_readonly_and_threshold_change(cookbook, tmp_path, suffix):
    path = tmp_path / "spaced ü path" / ("decisions." + suffix)
    rows = cookbook["support_context"].attach_context(messages(cookbook))
    query = cookbook["support_patterns"].failed_remedy.bind(rows, key=("id",), revision=("revision",))
    live = fixed(cache=path)
    expected = live.evaluate(query, band=BAND)
    live.close()
    files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()}
    replay = Engine(Scripted(lambda *args: pytest.fail("network-equivalent replay dispatch")), cache=path, replay=True)
    actual = replay.evaluate(query, band=BAND)
    assert actual.matches == expected.matches and actual.complete
    assert len(actual.trace.cached_requests) == 5
    assert replay.stats.requests == 0
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()} == files
    replay.close()


def test_retry_charged_and_payload_identical():
    seen = []

    def handler(state, questions):
        seen.append((state, tuple(questions)))
        if len(seen) < 3:
            raise BackendError("temporary", retryable=True)
        return dict.fromkeys(questions, 0.9)

    e = Engine(Scripted(handler), cache=False)
    result = e.evaluate(pred().bind({"text": "x"}, key=1), band=BAND, limits=sx.Limits(max_requests=2), errors="collect")
    assert not result.complete and result.outcome is None
    assert len(seen) == 2 and seen[0] == seen[1]
    assert result.errors[0].code == "RequestLimitExceeded"
    assert len(result.trace.attempts) == 2


def test_concurrency_shared_by_independent_evaluators():
    lock, active, maximum = threading.Lock(), 0, 0

    def handler(state, qs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return dict.fromkeys(qs, 0.9)

    e = Engine(Scripted(handler), concurrency=2, cache=False)
    pattern = sx.sequence(pred().capture("hit"))

    def run(i):
        query = pattern.bind([{"id": k, "text": str((i, k))} for k in range(4)], key=("id",))
        return e.evaluate(query, band=BAND)

    with ThreadPoolExecutor(4) as pool:
        reports = list(pool.map(run, range(4)))
    assert all(r.complete for r in reports)
    assert maximum == 2 and e.stats.requests == 16


def test_cancellation_collect_does_not_send_queued_requests():
    start, release = threading.Event(), threading.Event()

    def handler(state, qs):
        start.set()
        assert release.wait(2)
        return dict.fromkeys(qs, 0.9)

    e = Engine(Scripted(handler), concurrency=1, cache=False)
    query = sx.sequence(pred().capture("hit")).bind([{"id": n, "text": str(n)} for n in range(3)], key=("id",))

    async def run():
        task = asyncio.create_task(e.aevaluate(query, band=BAND, errors="collect"))
        assert await asyncio.to_thread(start.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(e.backend.calls) == 1


def test_cache_write_failure_collects_execution_issue(tmp_path):
    blocker = tmp_path / "file-not-directory"
    blocker.write_text("x")
    e = fixed(cache=blocker / "cache.sqlite")
    result = e.evaluate(pred().bind({"text": "x"}, key=1), band=BAND, errors="collect")
    assert not result.complete and result.outcome is None
    assert result.errors


@pytest.mark.parametrize("kind", ["scalar", "relation"])
def test_aggregate_binding_byte_cap(kind):
    large = "x" * (2_200_000)
    if kind == "scalar":
        p = pred(context_fields={"rule": ("rule",)})

        def action():
            return p.bind({"text": large}, key=1, context={"rule": large})
    else:
        relation = sx.Relation("r", question="Related?", left_fields={"text": ("text",)}, right_fields={"text": ("text",)})

        def action():
            return relation.bind({"text": large}, {"text": large}, left_key=1, right_key=2)

    with pytest.raises(sx.InputError, match=r"byte|MiB"):
        action()


@pytest.fixture
def http_server():
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    state = {"calls": [], "mode": "retry"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            state["calls"].append((raw, self.headers.get("Authorization")))
            if state["mode"] == "retry" and len(state["calls"]) == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            body = {"model": payload["model"], "answers": {k: {"type": "noul", "noul": 0.95} for k in payload["questions"]}}
            if state["mode"] != "unknown_usage":
                body["usage"] = {"input_tokens": 10, "cost": 0.000001}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, "http://127.0.0.1:" + str(server.server_port)
    server.shutdown()
    server.server_close()
    thread.join()


def test_real_local_http_retry_exact_payload_and_credentials(http_server):
    from exregex import SystemOne

    state, url = http_server
    e = Engine(SystemOne(url, model="qa-test", api_key="local-fixture-secret", price_per_mtok=0.1), cache=False)
    plan = e.plan(pred().bind({"text": "x"}, key=1), band=BAND)
    assert not state["calls"] and "local-fixture-secret" not in repr(plan)
    report = e.evaluate(plan)
    assert report.complete and report.outcome is sx.YES
    assert len(state["calls"]) == len(report.trace.attempts) == 2
    assert state["calls"][0] == state["calls"][1]
    assert state["calls"][0][1] == "Bearer local-fixture-secret"
    assert report.trace.unknown_charge_attempts == 1
    assert report.trace.reported_cost_usd == 0.000001


def test_unknown_cost_liability_blocks_later_dispatch(http_server):
    from exregex import SystemOne

    state, url = http_server
    state["mode"] = "unknown_usage"
    e = Engine(SystemOne(url, model="qa-test", price_per_mtok=0.1), cache=False)
    query = pred().bind({"text": "x"}, key=1)
    plan = e.plan(query, band=BAND)
    estimate = plan.requests[0].estimated_cost_usd
    e.max_cost_usd = estimate * 1.5
    first = e.evaluate(query, band=BAND)
    assert first.complete and first.trace.unknown_charge_attempts == 1
    second = e.evaluate(query, band=BAND, errors="collect")
    assert not second.complete and second.outcome is None
    assert second.errors[0].code == "BudgetExceeded"
    assert len(state["calls"]) == 1


def test_unknown_price_preflight_sends_nothing(http_server):
    from exregex import SystemOne

    state, url = http_server
    e = Engine(SystemOne(url, model="qa-test"), cache=False)
    with pytest.raises(sx.UnsupportedCapability):
        e.plan(pred().bind({"text": "x"}, key=1), band=BAND)
    assert not state["calls"]


def test_invalid_cached_raw_probability_is_execution_failure(tmp_path):
    import json

    cache = tmp_path / "decisions.jsonl"
    query = pred().bind({"text": "x"}, key=1)
    live = fixed(cache=cache)
    live.evaluate(query, band=BAND)
    sidecar = cache.with_name("decisions.semantic-v1.jsonl")
    row = json.loads(sidecar.read_text())
    response = json.loads(bytes.fromhex(row["value"]["response_body"]))
    response["answers"][next(iter(response["answers"]))]["noul"] = 1.5
    row["value"]["response_body"] = json.dumps(response).encode().hex()
    sidecar.write_text(json.dumps(row) + "\n")
    replay = Engine(Scripted(lambda *args: pytest.fail("replay dispatch")), cache=cache, replay=True)
    result = replay.evaluate(query, band=BAND, errors="collect")
    assert not result.complete and result.outcome is None and not result.leaves
    assert result.errors[0].code == "BackendError"


def test_metadata_revisions_preserved_on_identical_observation_reuse():
    e = fixed()
    p = pred()
    before = e.evaluate(p.bind({"text": "same"}, key="a", revision=1), band=BAND)
    after = e.evaluate(p.bind({"text": "same"}, key="b", revision=2), band=BAND)
    assert len(e.backend.calls) == 1 and after.leaves[0].cached
    assert before.leaves[0].records[0].key == "a"
    assert after.leaves[0].records[0].key == "b" and after.leaves[0].records[0].revision == 2
