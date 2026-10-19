"""Code graders: did the agent do the right thing at each step of a scenario?

`grade_step` judges one step against its `Expectation` and `forbid` list;
`check_invariants` judges the whole run (idempotency, recipients, budget).
Graders never raise on a bad agent: every problem becomes a `Failure` whose
message states what was expected and lists what actually happened, so a red
scenario can be fixed from the report alone.

Payload keys follow the outbox table in `docs/decisions.md`.
"""

import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.domain.load import Load
from freight_agent.domain.obligations import Obligation, ObligationKind
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.timers import Timer, TimerStatus
from freight_agent.evalkit.results import Failure, StepObservation
from freight_agent.evalkit.spec import ActionMatcher, ObligationExpectation, Step, TimerExpectation

# Payload keys whose values are human-readable text, in the order they are joined.
_TEXT_FIELDS = ("text", "body", "subject", "title", "description")
_EXCERPT = 80

# A grader yields (check, message) pairs; the caller adds the step index.
type _Findings = Iterator[tuple[str, str]]


class Budget(BaseModel):
    """Spending limits for one scenario trial. A `None` limit is not checked.

    Tokens and latency are per run, from the run's model usage: run timestamps
    come from the virtual clock, so only the model calls measure real time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_cost_usd: float | None = Field(default=None, description="Total over every run")
    max_tokens_per_run: int | None = Field(default=None, description="Input plus output tokens")
    max_latency_ms_per_run: int | None = Field(default=None, description="Sum of model latency")


# --- step grading -----------------------------------------------------------------------


def grade_step(step: Step, obs: StepObservation, *, timezone: str) -> list[Failure]:
    """Every failed check of the step, in the order the spec declares them."""
    expect = step.expect
    findings = [
        *_check_actions(expect.actions, obs.outbox),
        *_check_no_actions(expect.no_actions, obs.outbox),
        *_check_forbid(step.forbid, obs.outbox),
        *_check_obligations(expect.obligations, obs.snapshot.obligations),
        *_check_timers(expect.timers, obs.snapshot.timers, timezone),
        *_check_run_status(expect.run_status, obs.runs),
        *_check_llm_calls(expect.llm_calls, obs),
        *_check_runs(expect.runs, obs),
    ]
    return [Failure(step_index=obs.step_index, check=c, message=m) for c, m in findings]


def _check_actions(matchers: Sequence[ActionMatcher], items: Sequence[OutboxItem]) -> _Findings:
    for index, matcher in enumerate(matchers):
        check = f"expect.actions[{index}]"
        found = _select(matcher, items)
        if matcher.count is None and not found:
            wanted = "at least 1"
        elif matcher.count is not None and len(found) != matcher.count:
            wanted = f"exactly {matcher.count}"
        else:
            continue
        yield (
            check,
            f"expected {wanted} {_describe_matcher(matcher)}, found {len(found)}; "
            f"step actions: {_list_items(items)}",
        )


def _check_no_actions(no_actions: bool, items: Sequence[OutboxItem]) -> _Findings:
    if no_actions and items:
        yield (
            "expect.no_actions",
            f"expected no actions, found {len(items)}: {_list_items(items)}",
        )


def _check_forbid(matchers: Sequence[ActionMatcher], items: Sequence[OutboxItem]) -> _Findings:
    for index, matcher in enumerate(matchers):
        check = f"forbid[{index}]"
        found = _select(matcher, items)
        if found:
            yield (
                check,
                f"expected no {_describe_matcher(matcher)}, found {len(found)}: "
                f"{_list_items(found)}",
            )


def _check_obligations(
    expected: Mapping[ObligationKind, ObligationExpectation | None],
    obligations: Sequence[Obligation],
) -> _Findings:
    for kind, want in expected.items():
        check = f"expect.obligations.{kind.value}"
        latest = _latest(obligations, kind)
        if want is None:
            if latest is not None:
                yield check, f"expected no {kind.value} obligation, found {_describe(latest)}"
        elif latest is None:
            yield check, f"expected an {kind.value} obligation, found none"
        else:
            yield from _compare_obligation(check, want, latest)


def _compare_obligation(check: str, want: ObligationExpectation, actual: Obligation) -> _Findings:
    facts = _describe(actual)
    if want.status is not None and actual.status != want.status:
        yield f"{check}.status", f"expected status {want.status}, got {actual.status} ({facts})"
    if want.attempts is not None and actual.attempts != want.attempts:
        yield (
            f"{check}.attempts",
            f"expected attempts {want.attempts}, got {actual.attempts} ({facts})",
        )
    if want.deferred is not None and (actual.deferrals_used > 0) != want.deferred:
        wanted = "a deferral" if want.deferred else "no deferral"
        yield (
            f"{check}.deferred",
            f"expected {wanted}, got deferrals_used={actual.deferrals_used} ({facts})",
        )


def _check_timers(
    want: TimerExpectation | None, timers: Sequence[Timer], timezone: str
) -> _Findings:
    if want is None:
        return
    tz = ZoneInfo(timezone)
    pending = [t for t in _by_due_time(timers) if t.status is TimerStatus.PENDING]
    listing = f"timers: {_list_timers(timers, tz)}"
    if want.pending is not None and len(pending) != want.pending:
        yield (
            "expect.timers.pending",
            f"expected {want.pending} pending timer(s), got {len(pending)}; {listing}",
        )
    if want.due_at is not None:
        actual = _hh_mm(pending[0].due_at, tz) if pending else "no pending timer"
        if actual != want.due_at:
            yield (
                "expect.timers.due_at",
                f"expected the earliest pending timer due at {want.due_at} ({timezone}), "
                f"got {actual}; {listing}",
            )


def _check_run_status(want: Sequence[RunStatus], runs: Sequence[RunRecord]) -> _Findings:
    actual = tuple(r.status for r in runs)
    if want and actual != tuple(want):
        yield (
            "expect.run_status",
            f"expected runs [{', '.join(want)}], got [{', '.join(actual)}]; "
            f"runs: {_list_runs(runs)}",
        )


def _check_llm_calls(want: int | None, obs: StepObservation) -> _Findings:
    if want is not None and obs.llm_calls != want:
        yield (
            "expect.llm_calls",
            f"expected {want} model call(s), got {obs.llm_calls}; runs: {_list_runs(obs.runs)}",
        )


def _check_runs(want: int | None, obs: StepObservation) -> _Findings:
    if want is not None and len(obs.runs) != want:
        yield (
            "expect.runs",
            f"expected {want} run(s), got {len(obs.runs)}: {_list_runs(obs.runs)}",
        )


# --- invariants -------------------------------------------------------------------------


def check_invariants(
    load: Load,
    observations: Sequence[StepObservation],
    *,
    budget: Budget | None = None,
) -> list[Failure]:
    """Scenario-wide checks; the message names the step, since `step_index` is None."""
    findings = [
        *_check_idempotency(observations),
        *_check_recipients(load, observations),
        *(_check_budget(budget, observations) if budget is not None else ()),
    ]
    return [Failure(step_index=None, check=c, message=m) for c, m in findings]


def _check_idempotency(observations: Sequence[StepObservation]) -> _Findings:
    uses: defaultdict[str, list[str]] = defaultdict(list)
    for obs in observations:
        for item in obs.outbox:
            uses[item.idempotency_key].append(f"{item.outbox_id} (step {obs.step_index})")
    for key, items in uses.items():
        if len(items) > 1:
            yield (
                "invariant.idempotency",
                f"idempotency key {key!r} is used by {len(items)} outbox items: {', '.join(items)}",
            )


def _check_recipients(load: Load, observations: Sequence[StepObservation]) -> _Findings:
    contacts = [c.id for c in load.contacts]
    for obs in observations:
        for item in obs.outbox:
            if item.kind not in (OutboxKind.SMS, OutboxKind.EMAIL):
                continue
            where = f"step {obs.step_index}: {item.kind.value} {item.outbox_id}"
            recipients = _recipients(item)
            if not recipients:
                yield "invariant.recipients", f"{where} has no recipient"
            for recipient in recipients:
                if recipient not in contacts:
                    yield (
                        "invariant.recipients",
                        f"{where} goes to {recipient!r}, who is not a contact of load "
                        f"{load.load_id} (contacts: {', '.join(contacts)})",
                    )


def _check_budget(budget: Budget, observations: Sequence[StepObservation]) -> _Findings:
    runs = [(obs.step_index, run) for obs in observations for run in obs.runs]
    if budget.max_cost_usd is not None:
        total = sum(run.cost_usd for _, run in runs)
        if total > budget.max_cost_usd:
            yield (
                "invariant.budget.cost_usd",
                f"total model cost ${total:.4f} over {len(runs)} run(s) exceeds the budget "
                f"of ${budget.max_cost_usd:.4f}",
            )
    for step_index, run in runs:
        where = f"step {step_index}, run {run.run_id}"
        tokens = sum(u.input_tokens + u.output_tokens for u in run.usage)
        if budget.max_tokens_per_run is not None and tokens > budget.max_tokens_per_run:
            yield (
                "invariant.budget.tokens",
                f"{where}: {tokens} input+output tokens exceed the budget of "
                f"{budget.max_tokens_per_run}",
            )
        latency = sum(u.latency_ms for u in run.usage)
        if budget.max_latency_ms_per_run is not None and latency > budget.max_latency_ms_per_run:
            yield (
                "invariant.budget.latency_ms",
                f"{where}: {latency} ms of model latency exceeds the budget of "
                f"{budget.max_latency_ms_per_run} ms",
            )


# --- matching ---------------------------------------------------------------------------


def _select(matcher: ActionMatcher, items: Sequence[OutboxItem]) -> list[OutboxItem]:
    """The items the matcher matches. The spec has already checked that the regex compiles."""
    pattern = re.compile(matcher.regex, re.IGNORECASE) if matcher.regex is not None else None
    return [item for item in items if _matches(matcher, pattern, item)]


def _matches(matcher: ActionMatcher, pattern: re.Pattern[str] | None, item: OutboxItem) -> bool:
    if item.kind != matcher.kind:
        return False
    if matcher.to is not None and matcher.to not in _recipients(item):
        return False
    text = _text(item)
    folded = text.casefold()
    if any(part.casefold() not in folded for part in matcher.contains):
        return False
    if pattern is not None and pattern.search(text) is None:
        return False
    if matcher.purpose is not None and item.payload.get("purpose") != matcher.purpose.value:
        return False
    return matcher.urgent is None or item.payload.get("urgent") == matcher.urgent


def _recipients(item: OutboxItem) -> tuple[str, ...]:
    """Contact ids an SMS or an email goes to; other kinds have no contact recipient."""
    if item.kind is OutboxKind.SMS:
        contact = item.payload.get("contact_id")
        return (contact,) if isinstance(contact, str) else ()
    if item.kind is OutboxKind.EMAIL:
        ids: object = item.payload.get("to_contact_ids")
        if isinstance(ids, list | tuple):
            return tuple(str(i) for i in cast("Sequence[object]", ids))
    return ()


def _text(item: OutboxItem) -> str:
    """The searchable text; newlines keep a substring from spanning two fields."""
    values = (item.payload.get(field) for field in _TEXT_FIELDS)
    return "\n".join(v for v in values if isinstance(v, str))


def _latest(obligations: Sequence[Obligation], kind: ObligationKind) -> Obligation | None:
    of_kind = [o for o in obligations if o.kind is kind]
    return max(of_kind, key=lambda o: (o.opened_at, o.obligation_id), default=None)


def _by_due_time(timers: Sequence[Timer]) -> list[Timer]:
    return sorted(timers, key=lambda t: (t.due_at, t.timer_id))


def _hh_mm(at: datetime, tz: ZoneInfo) -> str:
    return at.astimezone(tz).strftime("%H:%M")


# --- descriptions for failure messages -------------------------------------------------


def _describe_matcher(matcher: ActionMatcher) -> str:
    parts = [matcher.kind.value]
    if matcher.to is not None:
        parts.append(f"to {matcher.to}")
    if matcher.contains:
        parts.append("containing " + ", ".join(repr(s) for s in matcher.contains))
    if matcher.regex is not None:
        parts.append(f"matching /{matcher.regex}/i")
    if matcher.urgent is not None:
        parts.append("urgent" if matcher.urgent else "not urgent")
    if matcher.purpose is not None:
        parts.append(f"with purpose {matcher.purpose.value}")
    return " ".join(parts)


def _list_items(items: Sequence[OutboxItem]) -> str:
    return "; ".join(_describe_item(i) for i in items) or "(none)"


def _describe_item(item: OutboxItem) -> str:
    """Kind, recipient and the first characters of the text: enough to spot the mistake."""
    head = item.kind.value
    recipients = _recipients(item)
    if recipients:
        head += " to " + ", ".join(recipients)
    if item.payload.get("urgent") is True:
        head += " (urgent)"
    text = " ".join(_text(item).split())
    if not text:  # stage updates and the like carry data, not text
        text = ", ".join(f"{k}={v}" for k, v in item.payload.items())
    excerpt = text[:_EXCERPT] + ("..." if len(text) > _EXCERPT else "")
    return f"{head}: {excerpt!r}"


def _describe(obligation: Obligation) -> str:
    return (
        f"{obligation.obligation_id}: status={obligation.status}, "
        f"attempts={obligation.attempts}, deferrals_used={obligation.deferrals_used}"
    )


def _list_timers(timers: Sequence[Timer], tz: ZoneInfo) -> str:
    listed = (f"{t.timer_id} due {_hh_mm(t.due_at, tz)} {t.status}" for t in _by_due_time(timers))
    return "; ".join(listed) or "(none)"


def _list_runs(runs: Sequence[RunRecord]) -> str:
    return "; ".join(_describe_run(r) for r in runs) or "(none)"


def _describe_run(run: RunRecord) -> str:
    details = [
        f"skip_reason={run.skip_reason}" if run.skip_reason else "",
        f"gate={run.gate.intent}, act={run.gate.act}" if run.gate else "",
        "calls=" + ", ".join(u.role for u in run.usage) if run.usage else "",
        "violations=" + ", ".join(v.rule for v in run.violations) if run.violations else "",
        f"error={run.error}" if run.error else "",
    ]
    detail = "; ".join(d for d in details if d)
    return f"{run.run_id} on {run.event_id}: {run.status}" + (f" ({detail})" if detail else "")
