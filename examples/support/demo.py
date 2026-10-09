"""Run the complete service and worker offline. Scripted answers test wiring, not meaning."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from archive_worker import inspect_archive
from support_context import attach_context
from support_patterns import Message, failed_remedy
from support_service import inspect_ticket

from exregex import Engine, Scripted
from exregex import semantic as sx
from exregex.errors import BackendError


def fixture() -> list[Message]:
    start = datetime(2026, 10, 8, 9, tzinfo=timezone.utc)
    return [
        Message("m1", "1", "t1", "customer", "The printer jams every page.", start),
        Message("m2", "1", "t1", "agent", "Clear the rear paper path and retry printing.", start + timedelta(minutes=5)),
        Message("m3", "2", "t1", "customer", "I cleared the rear path. It still jams every page.", start + timedelta(minutes=10)),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    scenario = parser.add_mutually_exclusive_group()
    scenario.add_argument("--answer", choices=("yes", "no", "unknown"), default="yes")
    scenario.add_argument("--outage", action="store_true", help="simulate a backend failure without a network call")
    args = parser.parse_args()
    probability = {"yes": 0.99, "no": 0.01, "unknown": 0.5}[args.answer]

    def answer(state, questions):
        if args.outage:
            raise BackendError("simulated backend outage")
        return dict.fromkeys(questions, probability)

    with Engine(Scripted(answer)) as engine:
        band = sx.Band(0.15, 0.85)
        limits = sx.Limits(max_requests=32, max_results=20)
        messages = fixture()
        planned = engine.plan(failed_remedy.bind(attach_context(messages), key=("id",), revision=("revision",)), band=band, limits=limits)
        service = inspect_ticket(messages, engine=engine, band=band, limits=limits)
        worker = list(inspect_archive([messages], engine=engine, band=band, limits=limits))
        print(
            json.dumps(
                {
                    "evidence": "Scripted integration fixture; no model quality claim",
                    "plan": planned.describe(),
                    "service": service,
                    "worker": worker,
                    "stats": engine.stats.as_dict(),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
