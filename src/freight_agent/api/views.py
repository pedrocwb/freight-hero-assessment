"""Read models for the platform's panels, built from load snapshots. Pure functions.

What the agent sent comes from the outbox, not from the recording gateways: the outbox is
the record of the agent's decisions and carries the delivery status, and it is what a
database-backed deployment would read too.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from freight_agent.api.schemas import (
    ConversationChannel,
    ConversationEntry,
    HeroTask,
    LoadSummary,
    TaskStatus,
    TmsNote,
)
from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.timers import TimerStatus

# The outbox kind that writes on each conversation channel.
_OUTBOUND_KIND: Mapping[ConversationChannel, OutboxKind] = {
    "sms": OutboxKind.SMS,
    "email": OutboxKind.EMAIL,
    "slack": OutboxKind.BROKER_SLACK,
}
_CHANNELS: tuple[ConversationChannel, ...] = ("sms", "email", "slack")


@dataclass(frozen=True)
class Completion:
    """A Hero's "done" on a task: platform state only, no agent event (yet)."""

    done_at: datetime
    note: str | None


def load_summary(snapshot: LoadSnapshot) -> LoadSummary:
    load = snapshot.load
    return LoadSummary(
        load_id=load.load_id,
        broker_id=load.broker_id,
        broker_name=load.broker_name,
        stage=load.stage,
        delay_risk=load.delay_risk,
        open_obligations=sum(1 for o in snapshot.obligations if o.is_open),
        pending_timers=sum(1 for t in snapshot.timers if t.status is TimerStatus.PENDING),
        cost_usd=sum(run.cost_usd for run in snapshot.runs),
    )


# --- conversation -----------------------------------------------------------------------------


def conversation(
    snapshot: LoadSnapshot, channel: ConversationChannel | None = None
) -> list[ConversationEntry]:
    """Inbound messages and the agent's messages on `channel` (every channel when None),
    oldest first. At the same instant an inbound message comes before the agent's answer."""
    channels = _CHANNELS if channel is None else (channel,)
    entries = [
        (entry, 0)
        for event in snapshot.events
        if (entry := _inbound(snapshot.load, event, channels)) is not None
    ]
    entries += [
        (entry, 1) for item in snapshot.outbox if (entry := _outbound(item, channels)) is not None
    ]
    # `sorted` is stable, so each side keeps its own order within an instant.
    ordered = sorted(entries, key=lambda pair: (pair[0].at.astimezone(UTC), pair[1]))
    return [entry for entry, _ in ordered]


def _inbound(
    load: Load, event: Event, channels: Sequence[ConversationChannel]
) -> ConversationEntry | None:
    if event.type is not EventType.INBOUND_MESSAGE or event.channel.value not in channels:
        return None
    sender = event.sender_contact_id
    contact = load.contact(sender) if sender is not None else None
    return ConversationEntry(
        at=event.occurred_at,
        direction="in",
        channel=_channel(event.channel),
        who=sender or "unknown",
        role=contact.role.value if contact is not None else "unknown",
        text=event.body,
    )


def _outbound(
    item: OutboxItem, channels: Sequence[ConversationChannel]
) -> ConversationEntry | None:
    channel: ConversationChannel | None = next(
        (c for c in channels if _OUTBOUND_KIND[c] is item.kind), None
    )
    if channel is None:
        return None
    payload = item.payload
    recipients: tuple[str, ...] | None = None
    if item.kind is OutboxKind.SMS:
        recipients = (str(payload.get("contact_id", "")),)
    elif item.kind is OutboxKind.EMAIL:
        recipients = tuple(str(c) for c in payload.get("to_contact_ids", ()))
    return ConversationEntry(
        at=item.created_at,
        direction="out",
        channel=channel,
        who="agent",
        role="agent",
        text=_text(payload, "body" if item.kind is OutboxKind.EMAIL else "text"),
        subject=_optional_text(payload, "subject"),
        status=item.status,
        to=recipients,
    )


def _channel(channel: Channel) -> ConversationChannel:
    return next(c for c in _CHANNELS if c == channel.value)


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


# --- TMS and Hero tasks -----------------------------------------------------------------------


def tms_notes(snapshot: LoadSnapshot) -> list[TmsNote]:
    return [
        TmsNote(at=item.created_at, text=_text(item.payload, "text"))
        for item in snapshot.outbox
        if item.kind is OutboxKind.TMS_NOTE
    ]


def hero_tasks(
    snapshots: Iterable[LoadSnapshot],
    completions: Mapping[str, Completion],
    status: TaskStatus | None = None,
) -> list[HeroTask]:
    """The agent's `hero_task` outbox items across loads, oldest first, with any Hero
    completion; `status` None keeps both open and done."""
    tasks = [
        _task(item, completions.get(item.outbox_id))
        for snapshot in snapshots
        for item in snapshot.outbox
        if item.kind is OutboxKind.HERO_TASK
    ]
    tasks.sort(key=lambda task: (task.created_at.astimezone(UTC), task.task_id))
    return [task for task in tasks if status is None or task.status == status]


def _task(item: OutboxItem, completion: Completion | None) -> HeroTask:
    payload = item.payload
    return HeroTask(
        task_id=item.outbox_id,
        load_id=item.load_id,
        title=_text(payload, "title"),
        description=_text(payload, "description"),
        urgent=bool(payload.get("urgent", False)),
        created_at=item.created_at,
        status="open" if completion is None else "done",
        done_at=None if completion is None else completion.done_at,
        note=None if completion is None else completion.note,
    )
