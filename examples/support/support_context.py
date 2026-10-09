from collections import defaultdict, deque
from dataclasses import replace

from support_patterns import Message


def attach_context(messages: list[Message]) -> list[Message]:
    prior: defaultdict[str, deque[str]] = defaultdict(lambda: deque(maxlen=2))
    result = []
    for message in messages:
        history = prior[message.ticket_id]
        result.append(replace(message, prior_turns=tuple(history)))
        history.append(f"{message.role}: {message.text}")
    return result
