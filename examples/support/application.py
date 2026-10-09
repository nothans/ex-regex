from support_service import inspect_ticket

from exregex import Engine, typesafe
from exregex import semantic as sx


def make_inspector():
    engine = Engine(
        typesafe(model="jev-1.13.0"),
        cache="private-decisions.sqlite",
        concurrency=4,
    )
    band = sx.Band(no=0.15, yes=0.85)  # Illustrative; calibrate on development data.
    limits = sx.Limits(max_requests=32, max_results=20)

    def inspect(messages):
        return inspect_ticket(messages, engine=engine, band=band, limits=limits)

    return inspect
