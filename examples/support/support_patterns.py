from dataclasses import dataclass
from datetime import datetime, timedelta

from exregex import semantic as sx


@dataclass(frozen=True)
class Message:
    id: str
    revision: str | None
    ticket_id: str
    role: str
    text: str
    sent_at: datetime
    prior_turns: tuple[str, ...] = ()


message_fields = {
    "text": ("text",),
    "prior_turns": ("prior_turns",),
}

problem = sx.Predicate[Message](
    "support.problem",
    question=(
        "Does `item.text` report a problem the author is experiencing? "
        "Use `item.prior_turns` only to resolve references in the current message."
    ),
    fields=message_fields,
    version=1,
)
remedy = sx.Predicate[Message](
    "support.remedy",
    question=(
        "Does `item.text` propose an action intended to resolve a problem? "
        "Use `item.prior_turns` only to resolve references in the current message."
    ),
    fields=message_fields,
    version=1,
)
continues = sx.Predicate[Message](
    "support.continues",
    question=(
        "Does `item.text` report that a problem continues after an attempted remedy? "
        "Use `item.prior_turns` only to resolve references in the current message."
    ),
    fields=message_fields,
    version=1,
)

remedy_targets = sx.Relation[Message, Message](
    "support.remedy_targets",
    question=("Is the action proposed in `right.text` intended to address the problem reported in `left.text`?"),
    left_fields={"text": ("text",)},
    right_fields={"text": ("text",)},
    version=1,
)
failed_after = sx.Relation[Message, Message](
    "support.failed_after",
    question=(
        "Does `right.text` report that the remedy in `left.text` was attempted "
        "but did not resolve the problem described in `context.problem_text`?"
    ),
    left_fields={"text": ("text",)},
    right_fields={"text": ("text",)},
    context_fields={"problem_text": ("problem", "text")},
    version=1,
)

customer = sx.field("role").eq("customer")
agent = sx.field("role").eq("agent")

failed_remedy = (
    sx.sequence(
        (customer & problem).capture("problem"),
        sx.gap(max_items=3),
        (agent & remedy).capture("remedy"),
        sx.gap(max_items=3),
        (customer & continues).capture("followup"),
    )
    .group_by("ticket_id")
    .within(timedelta(days=2), timestamp="sent_at")
    .require(
        remedy_targets.between("problem", "remedy"),
        failed_after.between("remedy", "followup", context={"problem": sx.ref("problem")}),
    )
)
