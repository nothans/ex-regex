"""Independent transport/schema controls and design-review regression checks."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from datetime import datetime, timedelta, tzinfo

import pytest

from exregex import Choice, Engine, Noul, Score, Scripted
from exregex import semantic as sx
from exregex.errors import BackendError
from exregex.semantic.transport import AttemptResponse, NamedQuestion, parse_strict, retry_after

BAND = sx.Band(0.2, 0.8)


def p(**kwargs):
    return sx.Predicate("p", question="Is item.text relevant?", fields={"text": ("text",)}, **kwargs)


def test_capability_failure_before_dispatch():
    class ChoiceOnly(Scripted):
        def capabilities(self):
            return replace(super().capabilities(), answer_types=("choice",))

    backend = ChoiceOnly(lambda *args: pytest.fail("unsupported request sent"))
    with pytest.raises(sx.UnsupportedCapability):
        Engine(backend).plan(p().bind({"text": "x"}, key=1), band=BAND)


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "choice", "choice": "unknown", "probabilities": {"a": 0.5, "b": 0.5}, "confidence": 0.5},
        {"type": "choice", "choice": "a", "probabilities": {"a": 0.5}, "confidence": 0.5},
        {"type": "choice", "choice": "a", "probabilities": {"a": 0.5, "b": 0.4}, "confidence": 0.5},
        {"type": "choice", "choice": "a", "probabilities": {"a": 0.5, "b": 0.5, "c": 0}, "confidence": 0.5},
        {"type": "choice", "choice": "a", "probabilities": {"a": 0.5, "b": 0.5}, "confidence": True},
    ],
)
def test_strict_distribution_rejects_invalid_answers(raw):
    backend = Scripted(lambda *_: {})
    request = backend.prepare(b"{}", (NamedQuestion("q", Choice("pick", {"a": "first", "b": "second"})),))
    response = AttemptResponse(200, json.dumps({"model": "m", "answers": {"q": raw}}).encode(), 0)
    with pytest.raises(BackendError):
        parse_strict(response, request)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"model":"m","answers":{"q":{"type":"noul","noul":0.9},"extra":{"type":"noul","noul":0.5}}}',
        b'{"model":"m","answers":{"q":{"type":"noul","noul":0.9,"noul":0.1}}}',
        b'{"model":"m","answers":{"q":{"type":"noul","noul":NaN}}}',
        b'{"answers":{"q":{"type":"noul","noul":0.9}}}',
    ],
)
def test_strict_wire_ids_duplicates_and_metadata(raw):
    request = Scripted(lambda *_: {}).prepare(b"{}", (NamedQuestion("q", Noul("yes?")),))
    with pytest.raises(BackendError):
        parse_strict(AttemptResponse(200, raw, 0), request)


def test_ordered_criteria_are_preserved():
    backend = Scripted(lambda *_: {})
    one = backend.prepare(b"{}", (NamedQuestion("q", Choice("pick", {"a": "first", "b": "second"})),))
    two = backend.prepare(b"{}", (NamedQuestion("q", Choice("pick", {"b": "second", "a": "first"})),))
    assert one.body != two.body
    request = backend.prepare(b"{}", (NamedQuestion("q", Score("rate", ["low", "high"])),))
    raw = {"model": "m", "answers": {"q": {"type": "score", "score": 1.5, "probabilities": {"0": 0, "1": 1}, "confidence": 1}}}
    with pytest.raises(BackendError):
        parse_strict(AttemptResponse(200, json.dumps(raw).encode(), 0), request)


def test_unknown_provider_model_change_fails_run():
    class Changing(Scripted):
        def parse_strict(self, response, request):
            value = super().parse_strict(response, request)
            return replace(value, model=json.loads(request.state_utf8)["item"]["text"])

    e = Engine(Changing(lambda state, qs: dict.fromkeys(qs, 0.9)), concurrency=1)
    query = sx.sequence(p().capture("x")).bind([{"id": 1, "text": "model1"}, {"id": 2, "text": "model2"}], key=("id",))
    result = e.evaluate(query, band=BAND, errors="collect")
    assert not result.complete and result.outcome is None and len(result.matches) == 1


def test_cache_failure_preserves_typed_partial_trace(monkeypatch):
    e = Engine(Scripted(lambda state, qs: dict.fromkeys(qs, 0.9)))

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(e._semantic_store, "put", fail)
    result = e.evaluate(p().bind({"text": "x"}, key=1), band=BAND, errors="collect")
    assert not result.complete and result.outcome is None
    assert len(result.leaves) == 1 and len(result.trace.attempts) == 1


def test_public_sequence_inputs_are_copied():
    parts = [sx.field("role").eq("customer").capture("original")]
    pattern = sx.SequencePattern(parts)
    e = Engine(Scripted(lambda *args: pytest.fail("exact-only request")))
    planned = e.plan(pattern.bind([{"id": 1, "role": "customer"}], key=("id",)), band=BAND)
    parts[0] = sx.field("role").eq("customer").capture("changed")
    assert list(e.evaluate(planned).matches[0].captures) == ["original"]


def test_aggregate_byte_limits_before_shared_reference_serialization():
    with pytest.raises(sx.InputError):
        p().bind({"text": ["x" * 100_000] * 5000}, key=1)
    with pytest.raises(sx.InputError):
        p(context_fields={"ctx": ("ctx",)}).bind({"text": "x" * 2_200_000}, key=1, context={"ctx": "x" * 2_200_000})
    rel = sx.Relation("rel", question="Related?", left_fields={"x": ("text",)}, right_fields={"x": ("text",)})
    with pytest.raises(sx.InputError):
        rel.bind({"text": "x" * 2_200_000}, {"text": "x" * 2_200_000}, left_key=1, right_key=2)


def test_dst_fold_and_signed_zero_groups():
    class FoldZone(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=-5 if dt.fold else -4)

        def dst(self, dt):
            return timedelta(0)

    zone = FoldZone()
    early = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    late = early.replace(fold=1)
    e = Engine(Scripted(lambda *args: pytest.fail("exact-only request")))
    assert e.evaluate(sx.field("t").lt(late).bind({"t": early}, key=1), band=BAND).outcome is sx.YES
    pattern = sx.sequence(sx.field("t").exists().capture("a"), sx.field("t").exists().capture("b"))
    rows = [{"id": 1, "t": early, "g": 0.0}, {"id": 2, "t": late, "g": -0.0}]
    assert e.evaluate(pattern.within(timedelta(0), timestamp="t").bind(rows, key=("id",)), band=BAND).outcome is sx.NO
    result = e.evaluate(pattern.group_by("g").bind(rows, key=("id",)), band=BAND)
    assert result.outcome is sx.YES and result.coverage.input_groups == 1
    assert math.copysign(1, result.matches[0]["b"].data["g"]) == -1


def test_retry_after_is_not_shortened():
    assert retry_after("120") == 120
    assert retry_after("NaN") is None
    assert retry_after("Thu, 01 Jan 1970 00:00:00 GMT") == 0


@pytest.mark.parametrize("input_price,output_price", [
    (float("nan"), 0), (float("inf"), 0), (-1, 0), (True, 0),
    (0.1, 0.2), (0.1, None), (0.1, float("nan")), (0.1, float("inf")),
])
def test_unsupported_prices_fail_before_dispatch(input_price, output_price):
    class Priced(Scripted):
        def capabilities(self):
            return replace(super().capabilities(), price_per_mtok=input_price, output_price_per_mtok=output_price)

    e = Engine(Priced(lambda *_: pytest.fail("invalid pricing dispatched")))
    with pytest.raises(sx.UnsupportedCapability):
        e.plan(p().bind({"text": "x"}, key=1), band=BAND)


def test_usage_estimate_is_not_reported_billing(server):
    from exregex import SystemOne

    server.handler = lambda body: {k: {"type": "noul", "noul": 0.9} for k in body["questions"]}
    e = Engine(SystemOne(server.url, model="fixed", price_per_mtok=0.1), max_cost_usd=0.1)
    result = e.evaluate(p().bind({"text": "x"}, key=1), band=BAND)
    assert result.complete and result.outcome is sx.YES
    assert result.trace.reported_cost_usd == 0 and result.trace.unknown_charge_attempts == 1
    assert result.trace.attempts[0].reported_cost_usd is None
    assert e._semantic_liability == result.trace.estimated_cost_usd > 0


def test_spoken_domains_and_singleton_extraction_limits():
    import exregex as ex

    for address in (
        "fixture_quasar_7k9 at mail dot example dot invalid",
        "fixture_quasar_7k9 at mail dot example dot com",
        "fixture_quasar_7k9 [at] mail [dot] example [dot] invalid",
        "fixture_quasar_7k9 at mail dot example.invalid",
        "fixture_quasar_7k9 at example.invalid",
        "fixture_wombat_8p2 at mail.example.invalid",
        "fixture_puffin_4r6 at inbox.mail.example.invalid",
    ):
        assert [s.text for s in ex.find_units(address + ".", "email")] == [address]
    backend = Scripted(lambda *args: pytest.fail("oversized extraction dispatched"), max_state_chars=1000)
    with pytest.raises(ex.errors.LimitError):
        ex.extract("the passage", "x" * 5000, unit="text", engine=Engine(backend))
