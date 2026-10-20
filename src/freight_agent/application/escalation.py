"""The Hero tasks a run creates on its own, when it cannot or must not act.

Each escalation path of a run (docs/decisions.md, "Run pipeline") ends with one
Hero task written here in code, never by the model: an unknown sender, an event
that no SOP section covers, a model failure or a run over its deadline, and
actions the validator blocked. The texts tell a Hero what happened and what is
left to do by hand; the eval scenarios match on the titles.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from freight_agent.application.context import RunContext
from freight_agent.domain.actions import CreateHeroTask, Plan
from freight_agent.domain.events import EventType
from freight_agent.domain.validation import Severity, ValidationOutcome

# Titles are stable: scenarios and dashboards match on them.
UNKNOWN_SENDER_TITLE = "Message from an unknown sender"
SOP_GAP_TITLE = "No SOP for this situation"
MODEL_FAILURE_TITLE = "Agent failed on an event: handle it by hand"
DEADLINE_TITLE = "Agent run timed out: handle the event by hand"
BLOCKED_TITLE = "Agent action blocked by a safety check"

QUOTE_LIMIT = 300  # characters of a message quoted in a task


@dataclass(frozen=True)
class Escalation:
    """A Hero task and the reason the run records for it (`RunRecord.error`).

    The reason starts with a stable code (`unknown_sender`, `sop_gap`, `llm_error`,
    `deadline`, `blocked`) so the inspector and the graders can group escalations.
    """

    task: CreateHeroTask
    reason: str


def unknown_sender(ctx: RunContext, detail: str) -> Escalation:
    """The prefilter found a message from someone who is not a contact of the load."""
    return Escalation(
        task=CreateHeroTask(
            title=UNKNOWN_SENDER_TITLE,
            description=f"The agent did not answer {describe_event(ctx)}. {detail}.",
            urgent=False,
        ),
        reason=f"unknown_sender: {detail}",
    )


def sop_gap(ctx: RunContext, intent: str) -> Escalation:
    """No section with an explicit intent applies: the agent does not improvise."""
    load = ctx.load
    stage = load.stage.value
    return Escalation(
        task=CreateHeroTask(
            title=SOP_GAP_TITLE,
            description=(
                f"No SOP section of {load.broker_name} ({load.broker_id}) covers intent "
                f"{intent} at stage {stage}, so the agent did not act on "
                f"{describe_event(ctx)}. Handle it by hand, and tell the SOP editors if a "
                "procedure is missing."
            ),
            urgent=False,
        ),
        reason=f"sop_gap: {intent}/{stage}",
    )


def model_failure(ctx: RunContext, error: Exception) -> Escalation:
    """The model failed after its fallbacks. The event completes; it is never retried."""
    return Escalation(
        task=CreateHeroTask(
            title=MODEL_FAILURE_TITLE,
            description=(
                f"The agent's model failed on {describe_event(ctx)} "
                f"({type(error).__name__}: {error}). Nothing was sent for this event."
            ),
            urgent=True,
        ),
        reason=f"llm_error: {error}",
    )


def deadline_passed(ctx: RunContext, deadline_s: float) -> Escalation:
    """The run did not finish thinking in time. The event completes; it is never retried."""
    return Escalation(
        task=CreateHeroTask(
            title=DEADLINE_TITLE,
            description=(
                f"The agent did not finish {describe_event(ctx)} within {deadline_s:g} seconds. "
                "Nothing was sent for this event."
            ),
            urgent=True,
        ),
        reason=f"deadline: the run took more than {deadline_s:g} s",
    )


def blocked_actions(ctx: RunContext, plan: Plan, outcome: ValidationOutcome) -> Escalation:
    """The validator dropped an action for a high-severity reason (a security check)."""
    lines = [
        f"- action {v.action_index} ({plan.actions[v.action_index].type}): {v.rule}: {v.message}"
        for v in outcome.violations
    ]
    ran = len(outcome.approved)
    rules = sorted({v.rule for v in outcome.violations if v.severity is Severity.HIGH})
    return Escalation(
        task=CreateHeroTask(
            title=BLOCKED_TITLE,
            description=(
                f"The validator blocked actions the agent proposed for {describe_event(ctx)}:\n"
                + "\n".join(lines)
                + f"\n{ran} other action{'' if ran == 1 else 's'} of the plan ran. Check "
                "whether the message tried to redirect the agent."
            ),
            urgent=True,
        ),
        reason=f"blocked: {', '.join(rules)}",
    )


def describe_event(ctx: RunContext) -> str:
    """The event in a few words a Hero can act on, with its local time."""
    event = ctx.event
    data: Mapping[str, Any] = event.data
    when = _local(ctx, event.occurred_at)
    match event.type:
        case EventType.INBOUND_MESSAGE:
            sender = event.sender_contact_id or "an unknown sender"
            return (
                f"the {event.channel.value} message from {sender} at {when}: {_quote(event.body)}"
            )
        case EventType.RISK_CHANGED:
            previous, current = data.get("previous"), data.get("current")
            return f"the delay risk change from {previous} to {current} at {when}"
        case EventType.TIMER_DUE:
            return f"the {data.get('kind') or 'follow-up'} follow-up timer due at {when}"
        case _:
            return f"the {event.type.value} event {event.event_id} at {when}"


def _local(ctx: RunContext, moment: datetime) -> str:
    """Local to the load's current stop, where the Hero will call or write."""
    zone = ZoneInfo(ctx.load.current_stop().timezone)
    return moment.astimezone(zone).strftime("%Y-%m-%d %H:%M %Z")


def _quote(text: str) -> str:
    clipped = text if len(text) <= QUOTE_LIMIT else text[: QUOTE_LIMIT - 3] + "..."
    return f'"{clipped}"'
