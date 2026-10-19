"""Code checks that end a run before any model call.

Cheap and deterministic: a stale timer, a duplicate event or an empty message
never reaches the LLM. An unknown sender goes to a Hero without a model call.
"""

from dataclasses import dataclass
from enum import StrEnum

from freight_agent.application.context import RunContext


class SkipReason(StrEnum):
    ALREADY_PROCESSED = "already_processed"
    STALE_TIMER = "stale_timer"
    EMPTY_MESSAGE = "empty_message"


class EscalationReason(StrEnum):
    UNKNOWN_SENDER = "unknown_sender"


@dataclass(frozen=True)
class PrefilterResult:
    """Exactly one of `skip` and `escalate` is set."""

    skip: SkipReason | None = None
    escalate: EscalationReason | None = None
    detail: str = ""


def prefilter(ctx: RunContext) -> PrefilterResult | None:
    """None means the run continues to the gate."""
    raise NotImplementedError  # card APP-2
