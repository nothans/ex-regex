"""Permanent regressions for the original text API review; never calls a service."""

import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import exregex as ex
from exregex.testing import keyword_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.run import score_spans  # noqa: E402

NONE = "(none of these)"


def choice(q, selected):
    return ex.ChoiceAnswer(selected, {o: float(o == selected) for o in q.criteria}, 1.0)


def total_picker(state, questions):
    q = questions["pick"]
    if "text" in state:
        selected = next(o for o in q.criteria if o != NONE)
    else:
        neighborhoods = state["candidates_in_context"]
        selected = next((k for k, text in neighborhoods.items() if "Total due: ⟦" in text), None)
        selected = selected or (NONE if NONE in q.criteria else next(iter(q.criteria)))
    return {"pick": choice(q, selected)}


class ReviewRecheck(unittest.TestCase):
    def test_partial_redaction_and_coverage_union(self):
        address = "fixture_quasar_7k9@example.invalid"
        text = f"mail {address}"
        items = [{"id": "x", "text": text, "gold": [address]}]
        start = text.index(address)
        partial = score_spans(items, {"x": [(start, start + 1)]})
        self.assertEqual((partial["recall"], partial["leaked_items"], partial["exact"]), (0, 1, 0))
        split = score_spans(items, {"x": [(start, start + 5), (start + 5, start + len(address))]})
        self.assertEqual((split["recall"], split["leaked_items"], split["exact"]), (1, 0, 0))

    def test_original_email_boundaries(self):
        cases = [
            ("Reach me directly at quasar.marmot.7k9@example.invalid.", "quasar.marmot.7k9@example.invalid"),
            ("Drop me a line: nebula dot wombat dot 8p2 at example dot invalid.", "nebula dot wombat dot 8p2 at example dot invalid"),
            ("Easiest is comet.puffin.4r6 at example.invalid.", "comet.puffin.4r6 at example.invalid"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual([s.text for s in ex.find_units(text, "email")], [expected])

    def test_spoken_domains_keep_all_labels(self):
        for address in [
            "fixture_quasar_7k9 at mail dot example dot invalid",
            "fixture_quasar_7k9 at mail dot example dot com",
            "fixture_quasar_7k9 [at] mail [dot] example [dot] com",
            "fixture_quasar_7k9 at mail dot example.invalid",
        ]:
            with self.subTest(address=address):
                self.assertEqual([s.text for s in ex.find_units(address, "email")], [address])

    def test_dotted_spoken_domains_are_not_limited_to_a_short_tld_list(self):
        for address in (
            "fixture_quasar_7k9 at example.invalid",
            "fixture_wombat_8p2 at mail.example.invalid",
            "fixture_puffin_4r6 at inbox.mail.example.invalid",
        ):
            with self.subTest(address=address):
                self.assertEqual([s.text for s in ex.find_units(address, "email")], [address])

    def test_original_tournament_loop_is_bounded_with_and_without_cache(self):
        for cache in (True, False):
            with self.subTest(cache=cache):
                calls = []

                def handler(state, qs, calls=calls):
                    calls.append(state)
                    self.assertLess(len(calls), 10, "tournament failed to shrink")
                    q = qs["pick"]
                    return {"pick": choice(q, next(iter(q.criteria)))}

                text = "x" * 300 + " $10 " + "x" * 400 + " $20 " + "x" * 400
                engine = ex.Engine(ex.Scripted(handler, max_state_chars=1000), cache=cache)
                self.assertIsNotNone(ex.extract("the amount", text, unit="money", engine=engine))
                self.assertLessEqual(len(calls), 3)

    def test_uncomparable_candidates_fail_before_sending(self):
        backend = ex.Scripted(total_picker, max_state_chars=1000)
        text = "a" * 600 + " " * 100 + "b" * 600
        with self.assertRaises(ex.LimitError):
            ex.extract("the passage", text, unit=lambda t: [(0, 600), (700, 1300)], engine=ex.Engine(backend))
        self.assertEqual(backend.calls, [])

    def test_single_oversized_candidate_fails_before_sending(self):
        calls = []

        def handler(state, qs):
            calls.append(len(json.dumps(state, ensure_ascii=False)))
            q = qs["pick"]
            return {"pick": choice(q, next(iter(q.criteria)))}

        engine = ex.Engine(ex.Scripted(handler, max_state_chars=1000), cache=False)
        with self.assertRaises(ex.LimitError):
            ex.extract("the passage", "x" * 5000, unit="text", engine=engine)
        self.assertEqual(calls, [])

    def test_long_extraction_keeps_later_occurrence_context(self):
        text = "Line item: $10.00\n" + "filler " * 10000 + "\nTotal due: $10.00"
        match = ex.extract("the total due", text, unit="money", engine=ex.Engine(ex.Scripted(total_picker)))
        self.assertIsNotNone(match)
        self.assertEqual(match.start, text.rindex("$10.00"))

    def test_261st_occurrence_is_selectable_in_short_and_long_paths(self):
        for filler in ("", "x" * 250):
            with self.subTest(long=bool(filler)):
                text = ("Item: $10.00\n" + filler + "\n") * 260 + "Total due: $10.00"
                match = ex.extract("the total due", text, unit="money", engine=ex.Engine(ex.Scripted(total_picker)))
                self.assertIsNotNone(match)
                self.assertEqual(match.start, text.rindex("$10.00"))

    def test_search_options_and_original_offsets(self):
        text = "refund now\napple\nERROR refund please\nWARN refund again"
        engine = keyword_engine({"refund": ["refund"], "fruit": ["apple"]})
        self.assertIsNone(ex.search("refund", text, unit="line", prefilter=lambda t: False, engine=engine))
        self.assertEqual(engine.stats.requests, 0)
        for prefilter, expected in [(r"^ERROR", "ERROR refund please"), (r"ERROR|WARN", "ERROR refund please")]:
            match = ex.search("refund", text, unit="line", prefilter=prefilter, engine=engine)
            self.assertEqual((match.text, match.start), (expected, text.index(expected)))
        match = ex.search("refund", text, unit="line", question="Does {ref} name a fruit?", engine=engine)
        self.assertEqual(match.text, "apple")

    def test_keyless_replay_across_presets_and_cache_formats(self):
        for backend_name in ("openrouter", "typesafe", "openai", "local", "http://127.0.0.1:9999"):
            for suffix in ("jsonl", "sqlite"):
                with self.subTest(backend=backend_name, cache=suffix):
                    env = {"EXREGEX_BACKEND": backend_name, "EXREGEX_MODEL": "review-pinned"}
                    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp, patch.dict("os.environ", env, clear=True):
                        path = Path(temp) / ("decisions." + suffix)
                        from exregex.backends import identity_from_env

                        backend = identity_from_env()
                        with (
                            patch.object(backend, "decide", return_value=ex.Decision({"q": ex.NoulAnswer(0.7)}, backend.model)),
                            ex.Engine(backend, cache=path) as rec,
                        ):
                            rec.decide("x", {"q": ex.Noul("Is it?")})
                        with (
                            ex.Engine(cache=path, replay=True) as replay,
                            patch.object(replay.backend, "decide", side_effect=AssertionError("network forbidden")),
                        ):
                            self.assertEqual(replay.decide("x", {"q": ex.Noul("Is it?")}).answers["q"].p, 0.7)
                            with self.assertRaises(ex.CacheMiss):
                                replay.decide("unrecorded", {"q": ex.Noul("Is it?")})

    def test_concurrency_shared_by_batches_and_async_calls_after_failure(self):
        lock = threading.Lock()
        active = peak = 0

        def handler(state, qs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.01)
                if state == "fail":
                    raise ex.BackendError("intentional review failure")
                return {k: 0.9 for k in qs}
            finally:
                with lock:
                    active -= 1

        engine = ex.Engine(ex.Scripted(handler), concurrency=2, cache=False)
        requests = [(i, {"q": ex.Noul("Is it?")}) for i in range(6)]
        with self.assertRaises(ex.BackendError):
            engine.decide("fail", requests[0][1])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: engine.ask_many(requests), range(4)))
        self.assertTrue(all(len(r) == 6 for r in results))

        async def run():
            return await asyncio.gather(*(ex.atest("x", f"text {i}", engine=engine) for i in range(12)))

        self.assertTrue(all(asyncio.run(run())))
        self.assertEqual(peak, 2)
        self.assertEqual(active, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
