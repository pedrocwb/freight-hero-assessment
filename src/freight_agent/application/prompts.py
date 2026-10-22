"""The variables of the gate's (triage) and the agent's prompts, rendered through their
templates, and the text the agent's read tools return.

The prompts' fixed text is in their templates (`interfaces.prompts`: the LangSmith Hub, with
the repository copy in `prompts/gate.yaml` and `prompts/agent.yaml`); this module
computes the parts that depend on the broker or the run and renders each message with
them. A variable holds what code knows: a list from the broker's catalog (its intents,
obligation kinds, document types and requirements, limits), a block of the run (the
load, the ledger, the transcript, the SOP sections, the event), or a sentence that
depends on them (the standard ETA examples, the read tool a broker with documents
offers). Each prompt's rules were added for a failure seen in a live run
(`evals/RESULTS.md`): read the attempts from the ledger, never defer on a timer event,
an acknowledgement never acts, short SMS with no internal words, never share rates,
untrusted text. With the standard catalog the gate renders the prompt the bake-off
tuned, word for word.

A system message is rendered from the broker alone and a user message from the run
(`*_SYSTEM_VARIABLES`, `*_USER_VARIABLES`); `check_variables` refuses, when a service is
built, a template that names a variable its message does not get.

Rendering is a pure function of its inputs: stable ordering, fixed formats and
no clock but `ctx.now`, so the same context and template give byte-identical text.
Times are local clock times of the current stop, with how long ago or how soon they
are, so a small model never does timezone or date arithmetic.

The agent prompt puts the stable parts first (load, contacts, SOP) and the
event last, next to the task: the prefix stays cacheable across the runs of a
load, and the question sits where small models attend best.

Message text is always rendered as a JSON string. Quotes and newlines inside a
message are escaped, so untrusted text cannot open a heading of its own (the templates
insert every variable unescaped, `{{{name}}}`, so the JSON stays as written). The same
holds for what the read tools return from documents and the history.
"""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from freight_agent.application.context import RunContext
from freight_agent.domain.broker import Broker, IntentDef
from freight_agent.domain.documents import Document, DocumentStatus
from freight_agent.domain.events import Attachment, Event, EventType
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import AppointmentKind, Load, Stop
from freight_agent.domain.memory import FacilityFact, stop_by_key
from freight_agent.domain.obligations import Obligation, ObligationKind
from freight_agent.domain.outbox import OutboxKind
from freight_agent.domain.sop import ResolvedSection, ResolvedSop
from freight_agent.domain.timers import Timer
from freight_agent.interfaces.prompts import ChatPrompt, PromptError

GATE_RECENT_EVENTS = 5
CLOSED_OBLIGATIONS_SHOWN = 3
GATE_TEXT_LIMIT = 200
TRANSCRIPT_TEXT_LIMIT = 400
DOCUMENT_TEXT_LIMIT = 1500  # characters of a document's text read_document returns

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

# What each message of a template may name: the variables code computes for it.
GATE_SYSTEM_VARIABLES = frozenset(
    {"intents", "intent_order", "context_examples", "must_act", "quiet_intents", "never_acts"}
)
GATE_USER_VARIABLES = frozenset(
    {
        "load_id",
        "stage",
        "now",
        "timezone",
        "open_obligations",
        "recent_events",
        "sender",
        "channel",
        "time",
        "text",
        "attachments",
    }
)
AGENT_SYSTEM_VARIABLES = frozenset(
    {"intents", "obligations", "documents", "document_tools", "max_steps", "max_rejections"}
)
AGENT_USER_VARIABLES = frozenset(
    {
        "load_id",
        "timezone",
        "load",
        "contacts",
        "facility_memory",
        "sop",
        "ledger",
        "timers",
        "transcript",
        "now",
        "event",
        "gate_intent",
        "gate_reason",
    }
)


def check_variables(
    prompt: ChatPrompt, *, system: frozenset[str], user: frozenset[str]
) -> ChatPrompt:
    """`prompt`, when each message names only variables code computes for it; otherwise
    `PromptError`, naming them. A template may leave a variable out (its block is then
    not in the prompt), not name one that would render empty."""
    problems = [
        f"the {part} message names {', '.join(sorted(extra))}"
        for part, extra in (
            ("system", prompt.system.variables - system),
            ("user", prompt.user.variables - user),
        )
        if extra
    ]
    if problems:
        raise PromptError(
            f"the {prompt.role.value} prompt {prompt.describe()} does not fit the code: "
            + "; ".join(problems)
            + f". Its system message may name {', '.join(sorted(system)) or 'none'}; its user "
            f"message {', '.join(sorted(user)) or 'none'}."
        )
    return prompt


# --- the gate (triage) prompt -------------------------------------------------------------


def gate_system_variables(broker: Broker) -> dict[str, str]:
    """The broker's message intents, in their priority order, and what follows from them."""
    intents = broker.message_intents()
    must_act = [i.intent_id for i in intents if i.must_act]
    quiet = [i for i in intents if i.noop_eligible]
    return {
        "intents": "\n".join(f"- {i.intent_id}: {i.description}" for i in intents),
        "intent_order": ", ".join(i.intent_id for i in intents),
        "context_examples": _standard_context_examples(broker),
        "must_act": f" {_and_list(must_act)} always act." if must_act else "",
        "quiet_intents": "".join(f"{_a(i.title)}, " for i in quiet),
        "never_acts": "".join(f"\n{_never_acts(i, broker)}" for i in quiet),
    }


def render_gate_system_prompt(prompt: ChatPrompt, broker: Broker) -> str:
    return prompt.system.format(gate_system_variables(broker))


def _standard_context_examples(broker: Broker) -> str:
    """Examples for the standard ETA flow, when the catalog has the intents they name."""
    needed = (Intent.ETA_PROVIDED, Intent.ACKNOWLEDGEMENT)
    if any(broker.intent(i) is None for i in needed):
        return ""
    if broker.obligation(ObligationKind.ETA_REQUEST) is None:
        return ""
    return (
        ' "9:40" right after we asked for the ETA is eta_provided. "ok" right after we asked '
        "for the ETA is still an acknowledgement, not an ETA."
    )


def _never_acts(intent: IntentDef, broker: Broker) -> str:
    example = f'"{intent.examples[0]}"' if intent.examples else "it"
    waits = " or ".join(o.title for o in broker.obligations) or "request"
    return (
        f"- {_a(intent.title).capitalize()} is always act false, whatever is open: {example} "
        f"answers nothing, and an open {waits} goes on by itself."
    )


def gate_user_variables(ctx: RunContext) -> dict[str, str]:
    """The message, its sender and the little context needed to classify it, nothing more."""
    view = _View(ctx)
    event = ctx.event
    earlier = [e for e in ctx.recent_events if e.event_id != event.event_id]
    obligations = [f"- {o.kind}: attempts {o.attempts}" for o in ctx.open_obligations]
    recent = [_event_line(view, e, GATE_TEXT_LIMIT) for e in earlier[-GATE_RECENT_EVENTS:]]
    return {
        "load_id": ctx.load.load_id,
        "stage": ctx.load.stage.value,
        "now": view.now_line(),
        "timezone": view.zone_name,
        "open_obligations": "\n".join(_or_none(obligations)),
        "recent_events": "\n".join(_or_none(recent)),
        "sender": view.person(event.sender_contact_id),
        "channel": event.channel.value,
        "time": view.when(event.occurred_at),
        "text": _quote(event.body),
        "attachments": _attachments_inline(event.attachments),
    }


def render_gate_prompt(prompt: ChatPrompt, ctx: RunContext) -> str:
    return prompt.user.format(gate_user_variables(ctx))


# --- the agent system prompt --------------------------------------------------------------

# The read tool a broker that requires documents adds to the list of the template.
_REQUIREMENTS_TOOL = """
- list_document_requirements {type_id}: the documents the broker requires and where this load \
stands."""


def agent_system_variables(broker: Broker) -> dict[str, str]:
    """The broker's catalog and limits."""
    limits = broker.limits
    return {
        "intents": _intents_lines(broker),
        "obligations": _obligations_lines(broker),
        "documents": _documents_lines(broker),
        "document_tools": _REQUIREMENTS_TOOL if broker.documents else "",
        "max_steps": str(limits.max_steps),
        "max_rejections": str(limits.max_rejections),
    }


def render_agent_system_prompt(prompt: ChatPrompt, broker: Broker) -> str:
    return prompt.system.format(agent_system_variables(broker))


def _intents_lines(broker: Broker) -> str:
    lines = [
        *(f"- {i.intent_id}: {i.description}" for i in broker.message_intents()),
        *(f"- {i.intent_id} (set by code): {i.description}" for i in broker.system_intents()),
    ]
    return "\n".join(lines)


def _obligations_lines(broker: Broker) -> str:
    lines: list[str] = []
    for o in broker.obligations:
        exhausted = ", then ".join(_TARGETS[t] for t in o.on_exhausted)
        lines.append(
            f"- {o.kind}: {o.title}. {o.description} Follow-up after {o.follow_up_minutes} "
            f"minutes; at most {o.max_attempts} requests; when they are used up: {exhausted} "
            "(the SOP gives the exact steps)."
        )
    return "\n".join(lines or ["- none"])


_TARGETS = {
    "dispatcher_email": "an email to the dispatcher",
    "hero_task": "a Hero task",
    "broker_slack": "the broker's Slack channel",
}


def _documents_lines(broker: Broker) -> str:
    """The document types, then one line per requirement."""
    types = ", ".join(f"{d.type_id} ({d.title})" for d in broker.document_types) or "none"
    lines = [f"Document types: {types}."]
    for r in broker.documents:
        within = f", within {r.within_minutes} minutes" if r.within_minutes is not None else ""
        opens = (
            f"; code opens the {r.opens_obligation} obligation when the stage changes to it"
            if r.opens_obligation
            else ""
        )
        lines.append(f"- Required: {r.type_id} once the load is {r.at_stage.value}{within}{opens}.")
    return "\n".join(lines)


# --- the agent user prompt ----------------------------------------------------------------

_IN_PROGRESS_NOTE = (
    "An open obligation in the ledger follows this procedure. Its next step runs when its "
    "follow-up timer fires. Do not run its steps for this event unless the sections above "
    "tell you to."
)


def agent_user_variables(
    ctx: RunContext,
    gate: GateDecision,
    sop: ResolvedSop,
    in_progress: Sequence[ResolvedSection] = (),
    facility_facts: Sequence[FacilityFact] = (),
) -> dict[str, str]:
    """`in_progress` are the sections that govern the follow-ups of open obligations; they are
    shown for context, apart from the sections that apply to this event. `facility_facts`
    are the notes earlier loads left about this load's current and next stop."""
    view = _View(ctx)
    return {
        "load_id": ctx.load.load_id,
        "timezone": view.zone_name,
        "load": "\n".join(_load_lines(view)),
        "contacts": "\n".join(_contact_lines(ctx)),
        "facility_memory": "\n".join(_or_none(facility_lines(ctx.load, facility_facts))),
        "sop": _sop_block(sop, in_progress),
        "ledger": "\n".join(_ledger_lines(view)),
        "timers": "\n".join(_or_none([_timer_line(view, t) for t in ctx.pending_timers])),
        "transcript": "\n".join(_transcript_lines(view)),
        "now": view.now_line(),
        "event": "\n".join(_event_lines(view)),
        "gate_intent": gate.intent,
        "gate_reason": _quote(gate.reason),
    }


def render_agent_prompt(
    prompt: ChatPrompt,
    ctx: RunContext,
    gate: GateDecision,
    sop: ResolvedSop,
    in_progress: Sequence[ResolvedSection] = (),
    facility_facts: Sequence[FacilityFact] = (),
) -> str:
    return prompt.user.format(agent_user_variables(ctx, gate, sop, in_progress, facility_facts))


def facility_lines(load: Load, facts: Sequence[FacilityFact]) -> list[str]:
    """One line per fact: the stop it is about (when it is one of this load's), its kind,
    the date it was told and its text, quoted like message text: a fact is what someone
    said on an earlier load, untrusted."""
    stops = stop_by_key(load)
    current = load.current_stop().sequence
    lines: list[str] = []
    for fact in facts:
        stop = stops.get(fact.facility_key)
        if stop is not None:
            where = f"stop {stop.sequence}{' (current)' if stop.sequence == current else ''}, "
        else:
            where = ""
        lines.append(
            f"- {where}{_quote(fact.facility_name)}, {fact.kind.value}, told on load "
            f"{_quote(fact.source_load_id)} {learned_on(fact, stop)}: {_quote(fact.text)}"
        )
    return lines


def learned_on(fact: FacilityFact, stop: Stop | None) -> str:
    """The date the fact was told, in its stop's timezone when known."""
    moment = _aware(fact.source_at)
    if stop is not None:
        moment = moment.astimezone(ZoneInfo(stop.timezone))
    return f"{moment:%Y-%m-%d}"


# --- what the read tools return -----------------------------------------------------------


def render_history(ctx: RunContext, limit: int) -> str:
    """The last `limit` events of the run's longest history, oldest first, one line each."""
    view = _View(ctx)
    events = ctx.long_history[-limit:] if limit > 0 else ()
    current = ctx.event.event_id
    lines = [
        _event_line(view, e, TRANSCRIPT_TEXT_LIMIT)
        + ("  <- this event" if e.event_id == current else "")
        for e in events
    ]
    shown = f"{len(events)} of the {len(ctx.long_history)} events read for this run"
    return f"History ({shown}, oldest first):\n" + "\n".join(_or_none(lines))


def render_ledger(ctx: RunContext, kind: str | None = None) -> str:
    """Every obligation of the load, open and closed, and every pending timer; with `kind`,
    only that kind's."""
    view = _View(ctx)
    obligations = [o for o in ctx.obligations if kind is None or o.kind == kind]
    ids = {o.obligation_id for o in obligations}
    timers = [t for t in ctx.pending_timers if kind is None or t.obligation_id in ids]
    lines = [
        "Open:",
        *_or_none([_open_obligation(view, o) for o in obligations if o.is_open]),
        "Closed:",
        *_or_none([_closed_obligation(view, o) for o in obligations if not o.is_open]),
        "Pending timers (earliest first):",
        *_or_none([_timer_line(view, t) for t in timers]),
    ]
    return "\n".join(lines)


def render_load(ctx: RunContext, part: str = "all") -> str:
    """The load data and the contacts; `part` "stops" or "contacts" gives only that part."""
    view = _View(ctx)
    if part == "contacts":
        return _contacts_block(ctx)
    if part == "stops":
        current = ctx.load.current_stop().sequence
        stops = [_stop_line(stop, stop.sequence == current) for stop in ctx.load.stops]
        return "## Stops\n" + "\n".join(stops)
    return _load_block(view) + "\n\n" + _contacts_block(ctx)


def render_document(document: Document) -> str:
    """A document as the pipeline left it. Every value comes from a model's reading of an
    outside file, so each is JSON."""
    lines = [
        f"document {_quote(document.document_id)} (attachment {_quote(document.attachment_id)})",
        f"filename: {_quote(document.filename)}",
        f"status: {document.status}",
    ]
    # `==`, not `is`: a document saved with `model_copy(update=...)` keeps the plain string.
    if document.status == DocumentStatus.PENDING:
        lines.append(
            "The document pipeline has not read it yet. A document_processed event follows "
            "when it has; that run acts on the document, not this one."
        )
        return "\n".join(lines)
    lines.append(f"type: {_quote(document.type_id or 'unknown')}")
    if document.confidence is not None:
        lines.append(f"confidence: {document.confidence:.2f}")
    lines.append(f"pages: {document.pages}")
    lines.append(f"values: {json.dumps(document.extracted, ensure_ascii=False, sort_keys=True)}")
    lines.append("checks against the load:")
    lines.extend(
        _or_none(
            [
                f"- {c.field}: {'ok' if c.ok else 'MISMATCH'}; expected {_quote(c.expected or '')}"
                f", found {_quote(c.found or '')}"
                for c in document.checks
            ]
        )
    )
    if document.text.strip():
        lines.append(f"text: {_quote(document.text.strip(), DOCUMENT_TEXT_LIMIT)}")
    return "\n".join(lines)


def render_requirements(ctx: RunContext, type_id: str | None = None) -> str:
    """The broker's document requirements (of `type_id`, when given) and where this load
    stands on each."""
    requirements = [r for r in ctx.broker.documents if type_id is None or r.type_id == type_id]
    if not requirements:
        return "This broker requires no such document at any stage."
    view = _View(ctx)
    lines = [f"The load is {ctx.load.stage.value}. Required documents:"]
    for r in requirements:
        within = f" within {r.within_minutes} min" if r.within_minutes is not None else ""
        received = [d for d in ctx.documents if d.type_id == r.type_id]
        state = ", ".join(f"{d.status} ({_quote(d.document_id)})" for d in received)
        obligation = _obligation_state(view, ctx, r.opens_obligation) if r.opens_obligation else ""
        lines.append(
            f"- {r.type_id} at {r.at_stage.value}{within}: "
            f"{'received: ' + state if state else 'not received'}{obligation}"
        )
    return "\n".join(lines)


def _obligation_state(view: "_View", ctx: RunContext, kind: str) -> str:
    current = next((o for o in reversed(ctx.obligations) if o.kind == kind), None)
    if current is None:
        return f"; {kind} not open"
    if current.is_open:
        return f"; {kind} open since {view.clock(current.opened_at)}, attempts {current.attempts}"
    return f"; {kind} {current.status.value}"


class _View:
    """Formats times and people for one run, relative to `ctx.now`."""

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx
        self.zone_name = ctx.load.current_stop().timezone
        self._tz = ZoneInfo(self.zone_name)
        self._now = _aware(ctx.now)
        self._today: date = self._now.astimezone(self._tz).date()

    def now_line(self) -> str:
        local = self._now.astimezone(self._tz)
        utc = self._now.astimezone(UTC)
        weekday = _WEEKDAYS[local.weekday()]
        return f"{weekday} {local:%Y-%m-%d %H:%M %Z} ({utc:%Y-%m-%d %H:%M} UTC)"

    def clock(self, moment: datetime) -> str:
        """`07:31` today, `2026-10-14 09:30` on any other day."""
        local = _aware(moment).astimezone(self._tz)
        return f"{local:%H:%M}" if local.date() == self._today else f"{local:%Y-%m-%d %H:%M}"

    def relative(self, moment: datetime) -> str:
        seconds = int((_aware(moment) - self._now).total_seconds())
        minutes = abs(seconds) // 60
        if minutes == 0:
            return "now"
        return f"in {_span(minutes)}" if seconds > 0 else f"{_span(minutes)} ago"

    def when(self, moment: datetime) -> str:
        return f"{self.clock(moment)} ({self.relative(moment)})"

    def person(self, contact_id: str | None) -> str:
        """A contact's id comes from the load metadata. Any other id is whatever the outside
        world sent, so it is quoted like message text."""
        if contact_id is None:
            return "unknown sender"
        contact = self.ctx.load.contact(contact_id)
        if contact is None:
            return f"{_quote(contact_id)} (not a contact of this load)"
        return f"{contact_id} ({contact.role.value})"


# Agent prompt blocks.


def _load_block(view: _View) -> str:
    return "\n".join(["## Load", *_load_lines(view)])


def _load_lines(view: _View) -> list[str]:
    load = view.ctx.load
    current = load.current_stop().sequence
    lines = [
        f"load: {load.load_id}",
        f"broker: {load.broker_name} (id {load.broker_id})",
        f"shipper: {load.shipper}",
        f"carrier: {load.carrier_name} (MC {load.mc_number})",
        f"commodity: {load.commodity}",
        f"load types: {', '.join(load.load_types) or 'none'}",
        f"stage: {load.stage.value}",
        f"delay risk: {load.delay_risk.value}",
        "stops:",
        *(_stop_line(stop, stop.sequence == current) for stop in load.stops),
    ]
    if load.instructions.strip():
        lines.append("instructions (verbatim from the load metadata):")
        lines.extend(f"  {line}" for line in load.instructions.strip().splitlines())
    else:
        lines.append("instructions: none")
    return lines


def _stop_line(stop: Stop, current: bool) -> str:
    marker = " (current)" if current else ""
    return (
        f"- stop {stop.sequence}{marker}, {stop.type.value}: {stop.name}, {stop.address}. "
        f"Appointment: {_appointment(stop)}."
    )


def _appointment(stop: Stop) -> str:
    """In the stop's own timezone, which may differ from the current stop's."""
    tz = ZoneInfo(stop.timezone)
    appt = stop.appointment
    if appt.kind is AppointmentKind.FIXED and appt.at is not None:
        return f"fixed {_aware(appt.at).astimezone(tz):%Y-%m-%d %H:%M %Z}"
    if appt.kind is AppointmentKind.WINDOW and appt.start is not None and appt.end is not None:
        start = _aware(appt.start).astimezone(tz)
        end = _aware(appt.end).astimezone(tz)
        end_format = "%H:%M %Z" if end.date() == start.date() else "%Y-%m-%d %H:%M %Z"
        return f"window {start:%Y-%m-%d %H:%M} to {end:{end_format}}"
    return f"{appt.kind.value}, time not set"


def _contacts_block(ctx: RunContext) -> str:
    return "\n".join(["## Contacts (the only people you may address, by id)", *_contact_lines(ctx)])


def _contact_lines(ctx: RunContext) -> list[str]:
    return [
        f"- {c.id}: {c.role.value}; channels: {', '.join(ch.value for ch in c.channels)}"
        for c in ctx.load.contacts
    ]


def _sop_block(sop: ResolvedSop, in_progress: Sequence[ResolvedSection]) -> str:
    parts = ["## SOP sections for this event (follow them exactly)"]
    parts.extend(_section_text(s) for s in sop.sections)
    if not sop.sections:
        parts.append("(none)")
    if in_progress:
        parts.append(f"## Procedure in progress (context)\n{_IN_PROGRESS_NOTE}")
        parts.extend(_section_text(s) for s in in_progress)
    shown = {*sop.section_ids, *(s.section_id for s in in_progress)}
    others = [
        f"- {c.section_id}: {c.title}. {c.description}"
        for c in sop.catalog
        if c.section_id not in shown
    ]
    parts.append(
        "## Other SOP sections (read one with read_sop_section, or find one with search_sop, "
        "if this event needs it)\n" + "\n".join(others or ["(none)"])
    )
    return "\n\n".join(parts)


def _section_text(section: ResolvedSection) -> str:
    return f"### {section.section_id}: {section.title}\n{section.body.strip()}"


def _ledger_lines(view: _View) -> list[str]:
    ctx = view.ctx
    closed = [o for o in ctx.obligations if not o.is_open][-CLOSED_OBLIGATIONS_SHOWN:]
    return [
        "Open:",
        *_or_none([_open_obligation(view, o) for o in ctx.open_obligations]),
        "Recently closed:",
        *_or_none([_closed_obligation(view, o) for o in closed]),
    ]


def _closed_obligation(view: _View, obligation: Obligation) -> str:
    return (
        f"- {obligation.kind}: {obligation.status.value}; attempts {obligation.attempts}; "
        f"opened {view.clock(obligation.opened_at)}"
    )


def _open_obligation(view: _View, obligation: Obligation) -> str:
    last = obligation.last_action_at
    deferred = obligation.deferred_until
    return (
        f"- {obligation.kind}: attempts {obligation.attempts}; "
        f"opened {view.clock(obligation.opened_at)}; "
        f"last action {view.when(last) if last is not None else 'none'}; "
        f"deferred until {view.when(deferred) if deferred is not None else 'none'}, "
        f"deferrals used {obligation.deferrals_used}; "
        f"{_active_timer(view, obligation)}"
    )


def _active_timer(view: _View, obligation: Obligation) -> str:
    timer_id = obligation.active_timer_id
    if timer_id is None:
        return "no follow-up timer"
    firing = view.ctx.event_timer
    if firing is not None and firing.timer_id == timer_id:
        return f"follow-up timer due {view.when(firing.due_at)}, firing now (this event)"
    timer = next((t for t in view.ctx.pending_timers if t.timer_id == timer_id), None)
    if timer is not None:
        return f"follow-up timer due {view.when(timer.due_at)}"
    # Not pending: the timer has fired. Events of a load run in order, so if its timer_due
    # event is not in the history yet, it waits behind this event: a new timer now would
    # make that follow-up stale and push the SOP back by a whole wait.
    ran = next((e for e in view.ctx.recent_events if e.event_id == f"timer:{timer_id}"), None)
    if ran is None:
        return "follow-up due now: it runs right after this event; do not schedule another"
    return (
        f"follow-up timer fired at {view.clock(ran.occurred_at)} and that run is over; "
        "no follow-up timer"
    )


def _timer_line(view: _View, timer: Timer) -> str:
    obligation = view.ctx.obligation(timer.obligation_id)
    target = obligation.kind if obligation is not None else "an unknown obligation"
    reason = f" ({timer.reason})" if timer.reason else ""
    return f"- due {view.when(timer.due_at)}: follow-up for {target}{reason}"


def _transcript_lines(view: _View) -> list[str]:
    current = view.ctx.event.event_id
    lines = [
        _event_line(view, event, TRANSCRIPT_TEXT_LIMIT)
        + ("  <- this event" if event.event_id == current else "")
        for event in view.ctx.recent_events
    ]
    return _or_none(lines)


def _event_lines(view: _View) -> list[str]:
    """The trigger in full; the transcript line of the same event may be clipped."""
    event = view.ctx.event
    lines = [
        f"type: {event.type.value}",
        f"time: {view.when(event.occurred_at)}",
        f"channel: {event.channel.value}",
    ]
    match event.type:
        case EventType.RISK_CHANGED:
            lines.append(f"change: {_risk_change(event)}")
        case EventType.TIMER_DUE:
            lines.append(f"timer: {_timer_due(view, event)}")
        case EventType.AGENT_ACTION:
            lines.append(f"action: {_describe(view, event, None)}")
        case EventType.HERO_UPDATE | EventType.EXECUTION_STOP_REQUESTED:
            lines.append(f"text: {_quote(event.body)}")
        case EventType.DOCUMENT_PROCESSED:
            lines.append(f"document: {_document_processed(event)}")
            if event.body.strip():
                lines.append(f"summary: {_quote(event.body.strip())}")
            document = _announced_document(view.ctx, event)
            if document is not None:
                lines.append("what the pipeline found (untrusted values from the file):")
                lines.extend(f"  {line}" for line in render_document(document).splitlines())
        case EventType.INBOUND_MESSAGE | EventType.ATTACHMENT_RECEIVED:
            lines.append(f"from: {view.person(event.sender_contact_id)}")
            lines.append(f"text: {_quote(event.body)}")
            if event.attachments:
                lines.append(
                    "attachments (the document pipeline reads them, and their "
                    "document_processed run acts on them; read_document shows where each "
                    "stands):"
                )
                lines.extend(f"- {_attachment(a)}" for a in event.attachments)
            else:
                lines.append("attachments: none")
    return lines


def _timer_due(view: _View, event: Event) -> str:
    text = f"follow-up for {_kind_of(event)}"
    timer = view.ctx.event_timer
    if timer is not None:
        text += f", due {view.when(timer.due_at)}"
    return text


# Event lines, shared by the gate and the agent prompts and the history tool.


def _event_line(view: _View, event: Event, limit: int) -> str:
    return f"- {view.clock(event.occurred_at)} {_describe(view, event, limit)}"


def _describe(view: _View, event: Event, limit: int | None) -> str:
    match event.type:
        case EventType.AGENT_ACTION:
            return _describe_action(view, event, limit)
        case EventType.RISK_CHANGED:
            return f"system: {_risk_change(event)}"
        case EventType.TIMER_DUE:
            return f"system: follow-up timer due for {_kind_of(event)}"
        case EventType.HERO_UPDATE:
            return f"in hero update: {_quote(event.body, limit)}"
        case EventType.EXECUTION_STOP_REQUESTED:
            return f"system: execution stop requested: {_quote(event.body, limit)}"
        case EventType.DOCUMENT_PROCESSED:
            summary = f": {_quote(event.body.strip(), limit)}" if event.body.strip() else ""
            return f"system: {_document_processed(event)}{summary}"
        case EventType.INBOUND_MESSAGE | EventType.ATTACHMENT_RECEIVED:
            text = _quote(event.body, limit) if event.body else "(no text)"
            sender = view.person(event.sender_contact_id)
            line = f"in {event.channel.value} from {sender}: {text}"
            if event.attachments:
                line += f" [attachments: {_attachments_inline(event.attachments)}]"
            return line


def _describe_action(view: _View, event: Event, limit: int | None) -> str:
    """One line per outbox kind, from the `agent_action` event the executor logged."""
    payload = _mapping(event.data.get("payload"))
    text = _quote(event.body or _text(payload, "text") or "", limit)
    purpose = _text(payload, "purpose")
    purpose_part = f", purpose {purpose}" if purpose else ""
    match _text(event.data, "kind"):
        case OutboxKind.SMS:
            to = view.person(_text(payload, "contact_id"))
            return f"out sms to {to}{purpose_part}: {text}"
        case OutboxKind.EMAIL:
            recipients = [view.person(c) for c in _texts(payload, "to_contact_ids")]
            subject = _text(payload, "subject")
            subject_part = f", subject {_quote(subject)}" if subject else ""
            to = ", ".join(recipients) or "the main thread"
            return f"out email to {to}{purpose_part}{subject_part}: {text}"
        case OutboxKind.BROKER_SLACK:
            return f"out broker slack: {text}"
        case OutboxKind.INTERNAL_SLACK:
            return f"out internal slack: {text}"
        case OutboxKind.TMS_NOTE:
            return f"out tms note: {text}"
        case OutboxKind.HERO_TASK:
            urgent = " (urgent)" if payload.get("urgent") is True else ""
            return f"out hero task{urgent}: {text}"
        case OutboxKind.STAGE_UPDATE:
            return f"out stage update: {_text(payload, 'stage') or event.body}"
        case _:
            return f"out {event.channel.value}: {text}"


def _risk_change(event: Event) -> str:
    previous = _text(event.data, "previous") or "unknown"
    current = _text(event.data, "current") or "unknown"
    return f"delay risk changed {previous} -> {current}"


def _kind_of(event: Event) -> str:
    return _text(event.data, "kind") or "an obligation"


def _document_processed(event: Event) -> str:
    """What the document pipeline found (`data`: document_id, attachment_id, type_id,
    status). The values come from a model's reading of an outside file, so each is quoted
    like message text; so is the event's body, the pipeline's summary of its reading."""
    data = event.data
    document = _quote(_text(data, "document_id") or "unknown")
    attachment = _text(data, "attachment_id")
    of = f" (attachment {_quote(attachment)})" if attachment else ""
    kind = _quote(_text(data, "type_id") or "unknown")
    status = _quote(_text(data, "status") or "unknown")
    return f"document {document}{of} processed: type {kind}, status {status}"


def _announced_document(ctx: RunContext, event: Event) -> Document | None:
    """The document a `document_processed` event names, by its id or its attachment's."""
    for key in ("document_id", "attachment_id"):
        found = ctx.document(_text(event.data, key) or "")
        if found is not None:
            return found
    return None


# Small helpers.


def _attachments_inline(attachments: Sequence[Attachment]) -> str:
    if not attachments:
        return "none"
    return ", ".join(_attachment(a) for a in attachments)


def _attachment(attachment: Attachment) -> str:
    """As a JSON object: the filename and the type are chosen by the sender, like message
    text, and the id comes from outside too. The storage reference stays out of the
    prompt."""
    return json.dumps(
        {
            "id": attachment.attachment_id,
            "filename": attachment.filename,
            "content_type": attachment.content_type,
        },
        ensure_ascii=False,
    )


def _quote(text: str, limit: int | None = None) -> str:
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return json.dumps(text, ensure_ascii=False)


def _or_none(lines: list[str]) -> list[str]:
    return lines or ["- none"]


def _and_list(items: Sequence[str]) -> str:
    """`a`, `a and b`, `a, b and c`."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _a(title: str) -> str:
    """`an acknowledgement`, `a question`: the title in lower case with its article."""
    word = title.lower()
    return f"{'an' if word[:1] in 'aeiou' else 'a'} {word}"


def _span(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h" if rest == 0 else f"{hours} h {rest} min"


def _aware(moment: datetime) -> datetime:
    """A naive time is read as UTC, never as the machine's local time."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _mapping(value: Any) -> Mapping[str, Any]:
    return cast("Mapping[str, Any]", value) if isinstance(value, Mapping) else {}


def _text(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _texts(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list | tuple):
        return ()
    return tuple(v for v in cast("Sequence[Any]", value) if isinstance(v, str))
