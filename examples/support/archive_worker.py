from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress

from support_service import inspect_ticket


def inspect_archive(conversations, *, engine, band, limits, workers=4):
    """Bound in-flight tickets and preserve input order on one caller-owned Engine.

    workers bounds ticket tasks; Engine.concurrency separately bounds HTTP attempts.
    Closing this iterator cancels queued work and drains in-flight tasks.
    """
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    pending: deque[Future[dict]] = deque()
    source = iter(conversations)
    with ThreadPoolExecutor(max_workers=workers) as pool:

        def submit(conversation):
            return pool.submit(inspect_ticket, conversation, engine=engine, band=band, limits=limits)

        try:
            for _ in range(workers):
                try:
                    pending.append(submit(next(source)))
                except StopIteration:
                    break
            while pending:
                result = pending.popleft().result()
                yield result
                with suppress(StopIteration):
                    pending.append(submit(next(source)))
        finally:
            for future in pending:
                future.cancel()
