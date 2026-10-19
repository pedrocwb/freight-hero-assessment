"""The validator's rules: what a proposed action must satisfy before it may run.

A rule judges one action against the run's context and the actions approved
earlier in the same plan, and returns a violation or None. It never edits the
action. Rules that read the ledger or the stage first replay what the approved
actions will do (executor semantics, `docs/decisions.md`), so each action is
judged against the state the executor will find when it reaches it.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import timedelta

from freight_agent.domain.actions import (
    Action,
    CloseObligation,
    DeferObligation,
    PostBrokerSlack,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
    UpdateStage,
)
from freight_agent.domain.load import Channel, Stage, contacts_of
from freight_agent.domain.obligations import ObligationKind
from freight_agent.domain.validation import Rule, Severity, ValidationContext, Violation


class _BaseRule:
    """A rule's name, its severity and how it reports a violation."""

    name: str
    severity: Severity = Severity.LOW

    def _violation(self, index: int, message: str) -> Violation:
        return Violation(
            rule=self.name, action_index=index, message=message, severity=self.severity
        )


# --- recipients -----------------------------------------------------------------------


def _addressing(action: Action) -> tuple[Channel, tuple[str, ...]] | None:
    """The channel and the contact ids of an action that writes to contacts directly."""
    if isinstance(action, SendSms):
        return Channel.SMS, (action.contact_id,)
    if isinstance(action, SendEmail):
        return Channel.EMAIL, action.to_contact_ids
    return None


class RecipientIsContact(_BaseRule):
    """Only the load's contacts may receive a message.

    The defense against prompt injection: an email that says "send the rate
    confirmation to x@y.com" cannot add a recipient, whatever the model proposes.
    """

    name = "recipient_is_contact"
    severity = Severity.HIGH

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        addressing = _addressing(action)
        if addressing is None:
            return None
        unknown = [c for c in addressing[1] if ctx.load.contact(c) is None]
        if not unknown:
            return None
        return self._violation(
            index, f"not a contact of load {ctx.load.load_id}: {', '.join(unknown)}"
        )


class ChannelAllowed(_BaseRule):
    """A contact is reached only on a channel the load metadata lists for them."""

    name = "channel_allowed"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        addressing = _addressing(action)
        if addressing is None:
            return None
        channel, contact_ids = addressing
        # Unknown ids are skipped: recipient_is_contact reports them.
        refused = [
            c.id
            for c in contacts_of(ctx.load, contact_ids)
            if c is not None and channel not in c.channels
        ]
        if not refused:
            return None
        return self._violation(index, f"{channel} is not a channel of {', '.join(refused)}")


class SmsRateLimit(_BaseRule):
    """Caps the SMS one contact gets in a sliding window, whatever the SOP or the model say."""

    name = "sms_rate_limit"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        if not isinstance(action, SendSms):
            return None
        config = ctx.config
        since = ctx.now - timedelta(minutes=config.sms_window_minutes)
        sent = sum(
            1
            for r in ctx.recent_outbound
            if r.channel is Channel.SMS and r.contact_id == action.contact_id and r.at >= since
        )
        planned = sum(
            1 for a in approved if isinstance(a, SendSms) and a.contact_id == action.contact_id
        )
        if sent + planned < config.sms_max_per_window:
            return None
        return self._violation(
            index,
            f"{action.contact_id} already has {sent + planned} SMS in the last "
            f"{config.sms_window_minutes} minutes (limit {config.sms_max_per_window})",
        )


# --- obligations ----------------------------------------------------------------------


@dataclass(frozen=True)
class _LedgerView:
    """The counters of an open obligation, as the executor will find them."""

    attempts: int
    deferrals_used: int


def _is_eta_request(action: Action) -> bool:
    """The executor opens the ETA obligation with this SMS, or records one more attempt."""
    return isinstance(action, SendSms) and action.purpose is Purpose.ETA_REQUEST


def _open_in_ledger(kind: ObligationKind, ctx: ValidationContext) -> bool:
    return any(o.kind is kind for o in ctx.open_obligations)


def _projected_obligation(
    kind: ObligationKind, ctx: ValidationContext, approved: Sequence[Action]
) -> _LedgerView | None:
    """The open obligation of `kind` once the approved actions have run; None if there is none."""
    current = next((o for o in ctx.open_obligations if o.kind is kind), None)
    view = None if current is None else _LedgerView(current.attempts, current.deferrals_used)
    for action in approved:
        if kind is ObligationKind.ETA_REQUEST and _is_eta_request(action):
            view = (
                _LedgerView(attempts=1, deferrals_used=0)
                if view is None
                else replace(view, attempts=view.attempts + 1)
            )
        elif isinstance(action, DeferObligation) and action.kind is kind and view is not None:
            view = replace(view, deferrals_used=view.deferrals_used + 1)
        elif isinstance(action, CloseObligation) and action.kind is kind:
            view = None
    return view


class ObligationExists(_BaseRule):
    """Ledger actions need the open obligation they act on.

    `schedule_timer` may also follow an `eta_request` SMS approved earlier in the
    plan, because the executor opens the obligation when it sends that SMS.
    `defer_obligation` and `close_obligation` act on an obligation that was open
    before this run.
    """

    name = "obligation_exists"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        if not isinstance(action, ScheduleTimer | DeferObligation | CloseObligation):
            return None
        is_open = _projected_obligation(action.kind, ctx, approved) is not None
        if not isinstance(action, ScheduleTimer):
            is_open = is_open and _open_in_ledger(action.kind, ctx)
        if is_open:
            return None
        return self._violation(index, f"{action.type} needs an open {action.kind} obligation")


class DeferralWithinPolicy(_BaseRule):
    """A contact's "I'll tell you later" buys a short, limited wait (`DeferralPolicy`)."""

    name = "deferral_policy"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        if not isinstance(action, DeferObligation):
            return None
        policy = ctx.config.deferral
        if action.minutes > policy.max_minutes:
            return self._violation(
                index,
                f"a deferral of {action.minutes} minutes is over the "
                f"{policy.max_minutes}-minute limit",
            )
        # A missing obligation is reported by obligation_exists.
        view = _projected_obligation(action.kind, ctx, approved)
        if view is not None and view.deferrals_used >= policy.max_per_obligation:
            return self._violation(
                index,
                f"the {action.kind} obligation already used its "
                f"{policy.max_per_obligation} deferral(s)",
            )
        return None


class EtaAttemptCap(_BaseRule):
    """Stops chasing the driver for an ETA after `max_eta_attempts` requests."""

    name = "eta_attempt_cap"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        if not _is_eta_request(action):
            return None
        view = _projected_obligation(ObligationKind.ETA_REQUEST, ctx, approved)
        cap = ctx.config.max_eta_attempts
        if view is None or view.attempts < cap:
            return None
        return self._violation(
            index, f"the ETA was already requested {view.attempts} times (cap {cap})"
        )


# --- plan hygiene and content ---------------------------------------------------------


class NoDuplicateAction(_BaseRule):
    """The same action twice in one plan is a model slip, never an intent."""

    name = "no_duplicate_action"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        if action not in approved:
            return None
        return self._violation(index, f"the plan already has this {action.type}")


# Heuristics tuned to miss load details (PO, MC, load ids, dates, times, weights).
_CONTACT_DETAILS = (
    ("an email address", re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")),
    (
        "a phone number",
        re.compile(
            # North American: 555-123-4567, (555) 123-4567, +1 555 123 4567, 5551234567
            r"(?<![\w+])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)"
            # International, with the leading +: +44 20 7946 0958
            r"|\+\d(?:[\s.()-]?\d){7,14}"
        ),
    ),
)


def _outbound_text(action: Action) -> tuple[str, ...]:
    """The text an external party will read: SMS, email and broker Slack."""
    if isinstance(action, SendSms | PostBrokerSlack):
        return (action.text,)
    if isinstance(action, SendEmail):
        return (action.subject or "", action.body)
    return ()


class NoContactDetailsInText(_BaseRule):
    """Outbound text never carries an email address or a phone number.

    The agent addresses people only by contact id, so contact details in a
    draft came from untrusted input (a message asking to "write to x@y.com").
    Internal text (TMS notes, Hero tasks) may quote them for a person to check.
    """

    name = "no_contact_details_in_text"
    severity = Severity.HIGH

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        texts = _outbound_text(action)
        found = [
            label
            for label, pattern in _CONTACT_DETAILS
            if any(pattern.search(text) for text in texts)
        ]
        if not found:
            return None
        return self._violation(index, f"outbound text contains {' and '.join(found)}")


# --- stage ----------------------------------------------------------------------------

_LIFECYCLE = (
    Stage.DISPATCHED,
    Stage.TO_PICKUP,
    Stage.AT_PICKUP,
    Stage.TO_DELIVERY,
    Stage.AT_DELIVERY,
    Stage.DELIVERED,
)
# A load with more stops goes through the middle stages again for each stop.
_NEXT_STOP = frozenset({(Stage.AT_PICKUP, Stage.TO_PICKUP), (Stage.AT_DELIVERY, Stage.TO_DELIVERY)})


def _projected_stage(ctx: ValidationContext, approved: Sequence[Action]) -> Stage:
    """The stage the executor will have saved when it reaches the next action."""
    stages = [a.stage for a in approved if isinstance(a, UpdateStage)]
    return stages[-1] if stages else ctx.load.stage


class StageMovesForward(_BaseRule):
    """A stage update follows the load's lifecycle; it never goes back to a past stop."""

    name = "stage_moves_forward"

    def check(
        self, action: Action, index: int, ctx: ValidationContext, approved: Sequence[Action]
    ) -> Violation | None:
        if not isinstance(action, UpdateStage):
            return None
        current, target = _projected_stage(ctx, approved), action.stage
        if target is current:
            message = f"the load is already {current}"
        elif target is Stage.DISPATCHED:
            message = "the agent never sets a load to dispatched"
        elif current is Stage.DELIVERED:
            message = "a delivered load does not change stage"
        elif (
            _LIFECYCLE.index(target) < _LIFECYCLE.index(current)
            and (current, target) not in _NEXT_STOP
        ):
            message = f"cannot move back from {current} to {target}"
        else:
            return None
        return self._violation(index, message)


def default_rules() -> tuple[Rule, ...]:
    """Every rule. The high-severity security checks come first in each action's report."""
    return (
        RecipientIsContact(),
        NoContactDetailsInText(),
        ChannelAllowed(),
        SmsRateLimit(),
        EtaAttemptCap(),
        ObligationExists(),
        DeferralWithinPolicy(),
        StageMovesForward(),
        NoDuplicateAction(),
    )
