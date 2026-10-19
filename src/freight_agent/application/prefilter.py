"""Code checks that end a run before any model call.

Cheap and deterministic: a stale timer, a duplicate event or an empty message
never reaches the LLM. An unknown sender goes to a Hero without a model call.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from freight_agent.application.context import RunContext
from freight_agent.domain.events import EventType
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.timers import Timer, TimerStatus, is_stale


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


Check = Callable[[RunContext], PrefilterResult | None]


def prefilter(ctx: RunContext) -> PrefilterResult | None:
    """None means the run continues to the gate.

    The checks run in the order of the table in docs/decisions.md; the first that
    matches decides. A duplicate is never escalated twice, and an empty message
    from a stranger is dropped rather than sent to a Hero.
    """
    return next((result for check in CHECKS if (result := check(ctx)) is not None), None)


def _already_processed(ctx: RunContext) -> PrefilterResult | None:
    if not ctx.already_processed:
        return None
    return PrefilterResult(
        skip=SkipReason.ALREADY_PROCESSED,
        detail=f"event {ctx.event.event_id} already has a run (a duplicate delivery or a retry)",
    )


def _stale_timer(ctx: RunContext) -> PrefilterResult | None:
    if ctx.event.type is not EventType.TIMER_DUE:
        return None
    timer = ctx.event_timer
    if timer is None:
        timer_id = ctx.event.data.get("timer_id")
        return PrefilterResult(skip=SkipReason.STALE_TIMER, detail=f"timer {timer_id!r} not found")
    obligation = ctx.obligation(timer.obligation_id)
    if not is_stale(timer, obligation):
        return None
    return PrefilterResult(skip=SkipReason.STALE_TIMER, detail=_why_stale(timer, obligation))


def _why_stale(timer: Timer, obligation: Obligation | None) -> str:
    """Explain `is_stale` for the inspector. Assumes the timer is stale."""
    if timer.status in (TimerStatus.SUPERSEDED, TimerStatus.CANCELLED):
        return f"timer {timer.timer_id} was {timer.status}"
    if obligation is None:
        return f"timer {timer.timer_id}: obligation {timer.obligation_id} not found"
    if not obligation.is_open:
        return (
            f"timer {timer.timer_id}: obligation {obligation.obligation_id} "
            f"is already {obligation.status}"
        )
    if obligation.active_timer_id is None:
        return f"timer {timer.timer_id}: obligation {obligation.obligation_id} has no active timer"
    return (
        f"timer {timer.timer_id} was replaced: obligation {obligation.obligation_id} "
        f"now waits on timer {obligation.active_timer_id}"
    )


def _empty_message(ctx: RunContext) -> PrefilterResult | None:
    event = ctx.event
    if event.type is not EventType.INBOUND_MESSAGE or event.body.strip() or event.attachments:
        return None
    return PrefilterResult(
        skip=SkipReason.EMPTY_MESSAGE,
        detail=f"{event.channel} message {event.event_id} has no text and no attachments",
    )


def _unknown_sender(ctx: RunContext) -> PrefilterResult | None:
    event = ctx.event
    if event.type is not EventType.INBOUND_MESSAGE:
        return None
    sender = event.sender_contact_id
    if sender is None:
        who = f"the sender of {event.channel} message {event.event_id} could not be matched"
    elif ctx.load.contact(sender) is None:
        who = f"{sender} is not a contact of load {ctx.load.load_id}"
    else:
        return None
    return PrefilterResult(
        escalate=EscalationReason.UNKNOWN_SENDER,
        detail=f"{who}; a Hero should check who wrote and whether to answer",
    )


# The decisions.md prefilter table, in order. A new check is one more entry here.
CHECKS: tuple[Check, ...] = (
    _already_processed,
    _stale_timer,
    _empty_message,
    _unknown_sender,
)
