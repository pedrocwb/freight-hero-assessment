"""What `agent play` prints: a played scenario as a transcript a person can read.

Built from what the player observed (the experiment target's outputs, `StepObservation`)
and graded by the evals' own graders (`experiment.failures`, `graders.step_checks`), so
the transcript and the verdict never disagree with `agent eval run`. Each step shows:

- its time and what came in (a message, a ping, a file, or the clock moving on);
- what the agent did, run by run: each message it sent, in full, and its other actions
  in one line each, then why (the intent, the SOP sections, the refused calls);
- what is left open at the end of the step, and the step's checks.

Pure functions over plain data: no model, no network. Colors only mark PASSED / FAILED
and the check marks; `click.echo` drops them when the output is not a terminal.
"""

import re
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import click

from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.load import Load
from freight_agent.domain.obligations import Obligation, ObligationStatus
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import RunRecord, RunStatus
from freight_agent.domain.timers import TimerStatus
from freight_agent.evalkit.experiment import ScenarioOutcome, failures
from freight_agent.evalkit.graders import StepCheck, run_intent, step_checks
from freight_agent.evalkit.results import Failure, StepObservation
from freight_agent.evalkit.spec import Scenario, Step

WIDTH = 88
_LABEL = 7  # the width of the "in", "did", "why", "open", "checks" column
_INDENT = " " * (7 + _LABEL)  # under the label column: "HH:MM  " + label


@dataclass(frozen=True)
class PlayResult:
    """One play of a scenario (one repetition), graded."""

    scenario: Scenario
    repetition: int  # 1-based
    observations: tuple[StepObservation, ...]
    failures: tuple[Failure, ...]
    cost_usd: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures

    @classmethod
    def from_outputs(
        cls, scenario: Scenario, outputs: Mapping[str, Any], repetition: int = 1
    ) -> "PlayResult":
        """From an experiment target's outputs (`ScenarioTarget.__call__`)."""
        error = outputs.get("error")
        return cls(
            scenario=scenario,
            repetition=repetition,
            observations=tuple(StepObservation.model_validate(o) for o in outputs.get("steps", [])),
            failures=tuple(failures(scenario, outputs)),
            cost_usd=float(outputs.get("cost_usd") or 0.0),
            error=error if isinstance(error, str) else None,
        )

    def checks(self, index: int) -> list[StepCheck]:
        step, obs = self.scenario.steps[index], self.observations[index]
        return step_checks(step, obs, timezone=self.scenario.timezone)

    @property
    def steps_passed(self) -> int:
        return sum(all(c.passed for c in self.checks(i)) for i in range(len(self.observations)))


# --- the transcript ---------------------------------------------------------------------


def render_play(
    result: PlayResult, *, k: int = 1, verbose: bool = False, width: int = WIDTH
) -> str:
    """The whole play: a header, every step, the scenario-wide failures, the verdict."""
    scenario = result.scenario
    tz = ZoneInfo(scenario.timezone)
    run_of = f"  (run {result.repetition} of {k})" if k > 1 else ""
    lines = [f"== {scenario.id}  ({scenario.suite}){run_of}"]
    lines += textwrap.wrap(scenario.title, width, initial_indent="   ", subsequent_indent="   ")
    previous: LoadSnapshot | None = None
    for obs in result.observations:
        step = scenario.steps[obs.step_index]
        lines.append("")
        lines += render_step(
            step, obs, previous, result.checks(obs.step_index), tz, verbose=verbose, width=width
        )
        previous = obs.snapshot
    for index in range(len(result.observations), len(scenario.steps)):
        lines += ["", f"{scenario.steps[index].at}  not played"]
    overall = [f for f in result.failures if f.step_index is None]
    if overall:
        lines.append("")
        for failure in overall:
            lines += _marked(False, f"{failure.check}: {failure.message}", width, "   ")
    lines += ["", verdict_line(result)]
    return "\n".join(lines)


def verdict_line(result: PlayResult) -> str:
    total = len(result.scenario.steps)
    verdict = _verdict(result.passed)
    return (
        f"{verdict}  {result.scenario.id}  {result.steps_passed}/{total} steps passed  "
        f"${result.cost_usd:.4f}"
    )


def render_step(
    step: Step,
    obs: StepObservation,
    previous: LoadSnapshot | None,
    checks: Sequence[StepCheck],
    tz: ZoneInfo,
    *,
    verbose: bool = False,
    width: int = WIDTH,
) -> list[str]:
    load = obs.snapshot.load
    lines = _labelled(step.at, "in", describe_input(step, load), width)
    if verbose and step.note:
        lines += _labelled("", "note", step.note, width)
    ledger = ledger_changes(previous, obs.snapshot, tz)
    if not obs.runs:
        lines += _labelled("", "did", "nothing (no run)", width)
        lines += [line for change in ledger for line in _labelled("", "", change, width)]
    attributed = {run.run_id for run in obs.runs}
    for number, run in enumerate(obs.runs):
        did = [describe_item(i, load) for i in obs.outbox if i.run_id == run.run_id]
        if number == len(obs.runs) - 1:
            did += [describe_item(i, load) for i in obs.outbox if i.run_id not in attributed]
            did += ledger
        if not did:
            did = [did_nothing(run)]
        for index, text in enumerate(did):
            lines += _labelled("", "did" if index == 0 else "", text, width)
        lines += _labelled("", "why", explain_run(run), width)
    lines += _labelled("", "open", left_open(obs.snapshot, tz), width)
    lines += _render_checks(checks, verbose=verbose, width=width)
    return lines


def describe_input(step: Step, load: Load) -> str:
    """What came in at the step: a message, a file, a ping, or the clock moving on."""
    if step.message is not None:
        message = step.message
        sender = _who(message.sender, load) if message.sender else "an unknown sender"
        head = f"{_channel(message.channel.value)} from {sender}"
        files = ", ".join(a.filename for a in message.attachments)
        if files:
            head += f", with {files}"
        return f'{head}: "{message.body}"' if message.body else head
    if step.ping is not None:
        return f"location ping, delay risk {step.ping.delay_risk.value}"
    return f"clock moves to {step.at}"


def describe_item(item: OutboxItem, load: Load) -> str:
    """One outbox item: a message in full, any other action in one line."""
    payload = item.payload
    text = str(payload.get("text") or "")
    match item.kind:
        case OutboxKind.SMS:
            return f'SMS to {_who(str(payload.get("contact_id")), load)}: "{text}"'
        case OutboxKind.EMAIL:
            to = ", ".join(_who(str(c), load) for c in payload.get("to_contact_ids") or [])
            subject = payload.get("subject") or ""
            return f'email to {to}, "{subject}": "{payload.get("body") or ""}"'
        case OutboxKind.BROKER_SLACK:
            return f'Slack to the broker: "{text}"'
        case OutboxKind.INTERNAL_SLACK:
            return f'internal Slack: "{text}"'
        case OutboxKind.HERO_TASK:
            urgency = "urgent" if payload.get("urgent") else "not urgent"
            return f'Hero task ({urgency}): "{payload.get("title") or ""}"'
        case OutboxKind.TMS_NOTE:
            return f'TMS note: "{text}"'
        case OutboxKind.STAGE_UPDATE:
            return f"stage changed to {payload.get('stage')}"


def did_nothing(run: RunRecord) -> str:
    match run.status:
        case RunStatus.SKIPPED:
            return f"nothing (skipped: {run.skip_reason or 'nothing to do'})"
        case RunStatus.FAILED:
            return f"nothing: the run failed ({run.error or 'no error given'})"
        case RunStatus.ESCALATED:
            return "nothing it sent itself: the run escalated"
        case _:
            return "nothing"


def explain_run(run: RunRecord) -> str:
    """Why: the intent (and the gate's act probability), the SOP sections the run
    followed, and every call a rule refused, with the rule's reason."""
    intent = run_intent(run)
    parts = [f"intent {intent}" if intent else "no intent"]
    if run.gate_scores is not None:
        parts[0] += f" (act {run.gate_scores.act:.0%})"
    if run.sop_section_ids:
        parts.append("followed " + ", ".join(run.sop_section_ids))
    refused = [
        f"{step.tool} refused: {refusal_reason(step.result)}"
        for step in run.trajectory
        if step.accepted is False
    ]
    if not run.trajectory:  # a record without a trajectory: the validator's verdicts
        refused = [
            f"action {v.action_index} refused: {v.rule}: {v.message}" for v in run.violations
        ]
    parts += refused
    if run.status is RunStatus.ESCALATED and run.error:
        parts.append(f"escalated: {run.error}")
    return " · ".join(parts)


_REJECTED = re.compile(r"rejected by ([a-z][a-z0-9_]*):?\s*(.*)", re.IGNORECASE | re.DOTALL)


def refusal_reason(result: str) -> str:
    """`rejected by attempt_cap: 2 of 2 used` -> `attempt_cap: 2 of 2 used`."""
    match = _REJECTED.search(result)
    if match is None:
        return " ".join(result.split())
    rule, why = match.group(1), " ".join(match.group(2).split())
    return f"{rule}: {why}" if why else rule


def ledger_changes(before: LoadSnapshot | None, after: LoadSnapshot, tz: ZoneInfo) -> list[str]:
    """The requests opened, asked again, deferred or closed, and the follow-up timers set,
    between two snapshots."""
    old = {o.obligation_id: o for o in (before.obligations if before else ())}
    changes: list[str] = []
    for now in after.obligations:
        name = obligation_name(now.kind)
        was = old.get(now.obligation_id)
        if was is None:
            changes.append(f"{name} opened{_attempts(now)}")
        elif now.attempts > was.attempts:
            changes.append(f"{name}: attempt {now.attempts}")
        if was is not None and now.deferrals_used > was.deferrals_used and now.deferred_until:
            changes.append(f"{name} deferred until {_hh_mm(now.deferred_until, tz)}")
        if now.status is not ObligationStatus.OPEN and (was is None or was.is_open):
            changes.append(f"{name} closed: {now.status.value}")
    seen = {t.timer_id for t in (before.timers if before else ())}
    for timer in sorted(after.timers, key=lambda t: t.due_at):
        if timer.timer_id not in seen and timer.status is TimerStatus.PENDING:
            changes.append(f"follow-up timer for {_hh_mm(timer.due_at, tz)}")
    return changes


def left_open(snapshot: LoadSnapshot, tz: ZoneInfo) -> str:
    """Each open request with its attempts, and the next follow-up."""
    parts = [
        f"{obligation_name(o.kind)} open, {o.attempts} attempt{'s' if o.attempts != 1 else ''}"
        for o in snapshot.obligations
        if o.status is ObligationStatus.OPEN
    ]
    pending = sorted(t.due_at for t in snapshot.timers if t.status is TimerStatus.PENDING)
    if pending:
        more = f" ({len(pending)} pending)" if len(pending) > 1 else ""
        parts.append(f"next follow-up {_hh_mm(pending[0], tz)}{more}")
    return " · ".join(parts) or "nothing"


def obligation_name(kind: str) -> str:
    """`eta_request` -> `ETA request`."""
    words = [("ETA" if w == "eta" else "POD" if w == "pod" else w) for w in kind.split("_")]
    return " ".join(words)


def _render_checks(checks: Sequence[StepCheck], *, verbose: bool, width: int) -> list[str]:
    if not checks:
        return _labelled("", "checks", "none", width)
    failed = [c for c in checks if not c.passed]
    if not failed:
        summary = f"{_mark(True)} all {len(checks)} passed"
    else:
        summary = f"{_mark(False)} {len(failed)} of {len(checks)} failed"
    lines = _labelled("", "checks", summary, width)
    for check in checks if verbose else failed:
        detail = f": {check.detail}" if check.detail else ""
        lines += _marked(check.passed, f"{check.name}{detail}", width, _INDENT + "  ")
    return lines


# --- the summary ------------------------------------------------------------------------


@dataclass(frozen=True)
class Experiment:
    suite: str
    name: str
    url: str | None


def render_summary(
    found: Sequence[ScenarioOutcome],
    *,
    k: int,
    cost_usd: float,
    experiments: Sequence[Experiment] = (),
) -> str:
    passed = sum(o.pass_hat_k for o in found)
    not_run = [o.scenario.id for o in found if o.not_run]
    failed = sum(not all(o.passed) for o in found)  # a repetition that ran and failed
    lines = [
        f"{len(found)} scenario{'s' if len(found) != 1 else ''}: "
        f"{_color(f'{passed} passed', 'green') if passed else '0 passed'}, "
        f"{_color(f'{failed} failed', 'red') if failed else '0 failed'}"
    ]
    if k > 1:
        lines.append(f"pass^{k}: {passed}/{len(found)} (a scenario passes when all {k} runs pass)")
    if not_run:
        lines.append(f"not run (cost cap): {', '.join(not_run)}")
    lines.append(f"cost: ${cost_usd:.4f}")
    for experiment in experiments:
        link = experiment.url or "(no link)"
        lines.append(f"LangSmith experiment {experiment.name}: {link}")
    return "\n".join(lines)


# --- JSON -------------------------------------------------------------------------------


def play_json(result: PlayResult) -> dict[str, Any]:
    """One play as plain data: each step's input, actions, runs, open items and checks."""
    tz = ZoneInfo(result.scenario.timezone)
    steps: list[dict[str, Any]] = []
    previous: LoadSnapshot | None = None
    for obs in result.observations:
        step = result.scenario.steps[obs.step_index]
        load = obs.snapshot.load
        steps.append(
            {
                "index": obs.step_index,
                "at": step.at,
                "input": describe_input(step, load),
                "actions": [describe_item(i, load) for i in obs.outbox],
                "ledger": ledger_changes(previous, obs.snapshot, tz),
                "runs": [
                    {
                        "status": run.status.value,
                        "intent": run_intent(run),
                        "act_probability": run.gate_scores.act if run.gate_scores else None,
                        "sections": list(run.sop_section_ids),
                        "refused": [
                            {"tool": s.tool, "reason": refusal_reason(s.result)}
                            for s in run.trajectory
                            if s.accepted is False
                        ],
                    }
                    for run in obs.runs
                ],
                "open": left_open(obs.snapshot, tz),
                "checks": [c.model_dump() for c in result.checks(obs.step_index)],
            }
        )
        previous = obs.snapshot
    return {
        "repetition": result.repetition,
        "passed": result.passed,
        "steps_passed": result.steps_passed,
        "steps": steps,
        "failures": [f.describe() for f in result.failures],
        "error": result.error,
        "cost_usd": result.cost_usd,
    }


# --- helpers ----------------------------------------------------------------------------


def _labelled(at: str, label: str, text: str, width: int) -> list[str]:
    """`HH:MM  label  text`, the text wrapped under itself."""
    head = f"{at:<5}  {label:<{_LABEL - 1}} "
    return textwrap.wrap(
        text, width, initial_indent=head, subsequent_indent=_INDENT, break_on_hyphens=False
    ) or [head.rstrip()]


def _wrap(text: str, width: int, indent: str) -> list[str]:
    return textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=indent + "  ")


def _who(contact_id: str, load: Load) -> str:
    role = next((c.role.value for c in load.contacts if c.id == contact_id), None)
    return f"{contact_id} ({role})" if role else contact_id


def _channel(channel: str) -> str:
    return {"sms": "SMS", "email": "email", "slack": "Slack", "hero": "Hero"}.get(channel, channel)


def _attempts(obligation: Obligation) -> str:
    return f", attempt {obligation.attempts}" if obligation.attempts else ""


def _hh_mm(at: datetime, tz: ZoneInfo) -> str:
    return at.astimezone(tz).strftime("%H:%M")


def _marked(passed: bool, text: str, width: int, indent: str) -> list[str]:
    """`✓ text` wrapped, the mark colored after the wrap so it counts one column."""
    lines = _wrap(f"{'✓' if passed else '✗'} {text}", width, indent)
    lines[0] = indent + _mark(passed) + lines[0][len(indent) + 1 :]
    return lines


def _mark(passed: bool) -> str:
    return _color("✓", "green") if passed else _color("✗", "red")


def _verdict(passed: bool) -> str:
    return _color("PASSED", "green") if passed else _color("FAILED", "red")


def _color(text: str, color: str) -> str:
    return click.style(text, fg=color, bold=True)
