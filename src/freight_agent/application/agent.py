"""The agent: one `agent` model call with tools, inside the broker's budgets.

The call (`Agent.run`) is one `LanguageModel.generate` on the `agent` role: the adapter
runs the tool rounds (`max_tool_rounds = BrokerLimits.max_steps`) and stops spending
once `max_cost_usd` is reached, so a run makes one counted model call however many
rounds it takes. The model reads with the read tools, stages actions with the action
tools and answers with `finish` (`AgentOutcome`).

**The staged plan is a pure fold over the transcript** (`stage_from_transcript`). Scripted
and replayed models never run tools, so the actions a live model staged would vanish if
the plan lived in the tools. Instead each action tool call of `LlmResponse.transcript` is
replayed, in order, through a fresh `Staging`: the same rules against the same context
give the same answers, in every mode (live, replay, scripted). The live tools hold their
own `Staging` only to answer the model; the fold is the authority, and the trajectory of
the run (`TrajectoryStep`) is the transcript.

The plan the run records and validates is every action the agent proposed: the action
tool calls that built an action (accepted or refused, in call order), then the answer's
own actions that do not repeat one of them. `validate` over that plan approves exactly
what the fold accepted (`domain.validation.check_action` is `validate` one action at a
time), so the validate node is the merge point and a check that nothing drifted.

Budgets (`Broker.limits`): `max_steps` tool rounds (0: no action tools, read tools for
`READ_ONLY_ROUNDS` rounds, the plan in the answer: the pre-wave-3 behaviour);
`max_cost_usd` per call; `max_rejections` refused action calls (over it the run
escalates, `escalation.blocked_actions`, and the accepted actions still run).
"""

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError

from freight_agent.application.agent_tools import (
    ACTION_TOOLS,
    READ_SOP_SECTION,
    SEARCH_SOP,
    agent_tools,
    build_action,
    invalid_arguments,
)
from freight_agent.application.context import RunContext
from freight_agent.application.gate import follow_up_intent
from freight_agent.application.prompts import render_agent_prompt, render_agent_system_prompt
from freight_agent.domain.actions import FINISH, Action, AgentOutcome, Plan
from freight_agent.domain.broker import Broker
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.rules import default_rules
from freight_agent.domain.runs import TrajectoryStep, Usage
from freight_agent.domain.sop import ResolvedSection, ResolvedSop
from freight_agent.domain.validation import Rule, ValidationContext, Violation, check_action
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import LanguageModel, LlmRequest, ModelRole

logger = logging.getLogger(__name__)

READ_ONLY_ROUNDS = 2  # tool rounds with `max_steps` 0: read tools only, as before wave 3

Transcript = Sequence[Mapping[str, Any]]


def validation_context(ctx: RunContext) -> ValidationContext:
    """What the rules judge an action against: the run's snapshot and its broker's limits.

    The kinds an action may name are the catalog's, plus those of obligations already
    open (a kind dropped from the catalog can still be closed).
    """
    broker = ctx.broker
    kinds = [o.kind for o in broker.obligations] + [o.kind for o in ctx.open_obligations]
    return ValidationContext(
        load=ctx.load,
        now=ctx.now,
        open_obligations=ctx.open_obligations,
        recent_outbound=ctx.recent_outbound,
        config=broker.validation_config(),
        obligation_kinds=tuple(dict.fromkeys(kinds)),
    )


@dataclass(frozen=True)
class Proposal:
    """One action tool call that built an action, and whether the rules accepted it."""

    action: Action
    accepted: bool
    violations: tuple[Violation, ...] = ()


class Staging:
    """The plan of one agent call, staged one action tool call at a time.

    `answer` is what an action tool tells the model. An action is judged against the
    actions accepted before it, at its position among the proposed ones, so the staging
    of a sequence of calls is `validate` of the same sequence. Calls with bad arguments
    build no action: they are refused, counted as rejections, and leave the plan alone.
    """

    def __init__(self, checks: ValidationContext, rules: Sequence[Rule], max_rejections: int):
        self._checks = checks
        self._rules = tuple(rules)
        self._max_rejections = max_rejections
        self.proposals: list[Proposal] = []
        self.rejections = 0

    @property
    def accepted(self) -> tuple[Action, ...]:
        return tuple(p.action for p in self.proposals if p.accepted)

    def answer(self, tool: str, args: Mapping[str, Any]) -> tuple[str, bool]:
        """Stage one action tool call. Returns the answer and whether it was accepted."""
        try:
            action = build_action(tool, args)
        except ValidationError as exc:
            self.rejections += 1
            return self._over_budget(invalid_arguments(tool, exc)), False
        accepted = self.accepted
        violations = check_action(
            action, self._checks, accepted, self._rules, index=len(self.proposals)
        )
        self.proposals.append(Proposal(action, not violations, violations))
        if not violations:
            return f"accepted as action #{len(accepted) + 1}", True
        self.rejections += 1
        reasons = "; ".join(f"{v.rule}: {v.message}" for v in violations)
        return self._over_budget(f"rejected by {reasons}"), False

    def _over_budget(self, text: str) -> str:
        if self.rejections <= self._max_rejections:
            return text
        return (
            f"{text.rstrip('.')}. More than {self._max_rejections} actions were rejected: call "
            f"{FINISH} now; a person will take over the event."
        )


@dataclass(frozen=True)
class Staged:
    """What the agent's transcript staged (`stage_from_transcript`)."""

    proposals: tuple[Proposal, ...] = ()
    rejections: int = 0
    trajectory: tuple[TrajectoryStep, ...] = ()
    sections_searched: tuple[str, ...] = ()

    @property
    def proposed(self) -> tuple[Action, ...]:
        return tuple(p.action for p in self.proposals)

    @property
    def accepted(self) -> tuple[Action, ...]:
        return tuple(p.action for p in self.proposals if p.accepted)


def stage_from_transcript(
    transcript: Transcript,
    *,
    checks: ValidationContext,
    rules: Sequence[Rule],
    max_rejections: int,
    offered: Collection[str],
) -> Staged:
    """Replay the transcript's tool calls, in order, through a fresh `Staging`.

    Pure: the same transcript and context give the same plan. Only the action tools in
    `offered` stage (a call to a tool that was not offered got "unknown tool" live and
    staged nothing). Read tool calls and unknown tools are trajectory steps with
    `accepted` None and the result the transcript holds (none in scripted mode, where
    tools never run); the answer (`finish`) is not a step. An action tool's recorded
    answer is recomputed; one that differs from the recorded text (the rules changed
    since a cassette was recorded) is logged.
    """
    staging = Staging(checks, rules, max_rejections)
    results = _tool_results(transcript)
    steps: list[TrajectoryStep] = []
    sections: list[str] = []
    for entry in transcript:
        if entry.get("role") != "assistant":
            continue
        cost = _round_cost(entry)
        for call in _calls(entry):
            name, args, call_id = call
            if name == FINISH:
                continue
            recorded = results.get(call_id) if call_id is not None else None
            if name in ACTION_TOOLS and name in offered:
                text, accepted = staging.answer(name, args)
                if recorded is not None and recorded != text:
                    logger.warning(
                        "the %s call %s was answered %r, the rules now answer %r",
                        name,
                        call_id,
                        recorded,
                        text,
                    )
                steps.append(_step(name, args, text, accepted, cost))
                continue
            steps.append(_step(name, args, recorded or "", None, cost))
            sections.extend(_sections_of(name, args, recorded or ""))
    return Staged(
        proposals=tuple(staging.proposals),
        rejections=staging.rejections,
        trajectory=tuple(steps),
        sections_searched=tuple(dict.fromkeys(sections)),
    )


def _step(
    name: str, args: dict[str, Any], result: str, accepted: bool | None, cost: float
) -> TrajectoryStep:
    return TrajectoryStep(tool=name, args=args, result=result, accepted=accepted, cost_usd=cost)


def _calls(entry: Mapping[str, Any]) -> list[tuple[str, dict[str, Any], str | None]]:
    """(name, args, id) of each tool call of an assistant entry; malformed calls skipped."""
    raw = entry.get("tool_calls")
    if not isinstance(raw, list):
        return []
    calls: list[tuple[str, dict[str, Any], str | None]] = []
    for item in cast("list[Any]", raw):
        if not isinstance(item, Mapping):
            continue
        call = cast("Mapping[str, Any]", item)
        name = call.get("name")
        if not isinstance(name, str):
            continue
        args = call.get("args")
        call_id = call.get("id")
        calls.append(
            (
                name,
                dict(cast("Mapping[str, Any]", args)) if isinstance(args, Mapping) else {},
                call_id if isinstance(call_id, str) else None,
            )
        )
    return calls


def _tool_results(transcript: Transcript) -> dict[str, str]:
    """The content of each tool entry, by the id of the call it answers."""
    results: dict[str, str] = {}
    for entry in transcript:
        call_id, content = entry.get("tool_call_id"), entry.get("content")
        if entry.get("role") == "tool" and isinstance(call_id, str) and isinstance(content, str):
            results.setdefault(call_id, content)
    return results


def _round_cost(entry: Mapping[str, Any]) -> float:
    """The cost of the model round that wrote this assistant entry (`usage`, per round)."""
    usage = entry.get("usage")
    if not isinstance(usage, Mapping):
        return 0.0
    cost = cast("Mapping[str, Any]", usage).get("cost_usd")
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0


def _sections_of(tool: str, args: Mapping[str, Any], result: str) -> list[str]:
    """The sections a read tool call looked at: the one `read_sop_section` read, or those
    `search_sop` returned (its result lines start `- <section_id>:`)."""
    if tool == READ_SOP_SECTION:
        section_id = args.get("section_id")
        return [section_id.strip()] if isinstance(section_id, str) and section_id.strip() else []
    if tool == SEARCH_SOP:
        return [
            line[2:].split(":", 1)[0]
            for line in result.splitlines()
            if line.startswith("- ") and ":" in line
        ]
    return []


@dataclass(frozen=True)
class AgentResult:
    plan: Plan  # every proposed action: the staged calls, then the answer's own actions
    staged: Staged
    usage: tuple[Usage, ...]
    intent: str | None = None  # the agent's label for the event, when it is in the catalog
    no_procedure: bool = False


class Agent:
    def __init__(
        self,
        llm: LanguageModel,
        knowledge: SopIndex,
        *,
        rules: Sequence[Rule] | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self._llm = llm
        self._knowledge = knowledge
        self.rules: tuple[Rule, ...] = tuple(rules) if rules is not None else default_rules()
        self._timeout_s = timeout_s

    async def run(
        self,
        ctx: RunContext,
        gate: GateDecision,
        sop: ResolvedSop,
        visible_sections: Sequence[ResolvedSection],
    ) -> AgentResult:
        """`sop` holds the sections for this intent and stage; `visible_sections` back the
        `read_sop_section` tool (every section this broker and load can see).

        One model call. `LlmError` propagates: the runtime escalates a model failure.
        """
        broker = ctx.broker
        limits = broker.limits
        checks = validation_context(ctx)
        live = Staging(checks, self.rules, limits.max_rejections)
        stage = live.answer if limits.max_steps > 0 else None
        tools = agent_tools(ctx, visible_sections, self._knowledge, stage=stage)
        request = LlmRequest(
            role=ModelRole.AGENT,
            system=render_agent_system_prompt(broker),
            user=render_agent_prompt(
                ctx, gate, sop, sections_in_progress(ctx, sop, visible_sections)
            ),
            tools=tools,
            max_tool_rounds=limits.max_steps if limits.max_steps > 0 else READ_ONLY_ROUNDS,
            timeout_s=self._timeout_s,
            max_cost_usd=limits.max_cost_usd,
        )
        response = await self._llm.generate(request, AgentOutcome)
        staged = stage_from_transcript(
            response.transcript,
            checks=checks,
            rules=self.rules,
            max_rejections=limits.max_rejections,
            offered={t.name for t in tools},
        )
        outcome = response.parsed
        return AgentResult(
            plan=merge_plan(staged, outcome),
            staged=staged,
            usage=(response.usage,),
            intent=catalog_intent(broker, outcome.intent),
            no_procedure=outcome.no_procedure,
        )


def merge_plan(staged: Staged, outcome: AgentOutcome) -> Plan:
    """The staged calls, then the answer's actions that do not repeat one of them (a model
    that staged an SMS and lists it again in `finish` means one SMS). A plain `Plan`: the
    run record stores it, and a subclass would not compare equal to the same plan read
    back from the database."""
    proposed = staged.proposed
    extras = tuple(a for a in outcome.actions if a not in proposed)
    return Plan(rationale=outcome.rationale, actions=(*proposed, *extras))


def catalog_intent(broker: Broker, intent: str | None) -> str | None:
    """The agent's label when the catalog knows it (an intent, or a follow-up intent of an
    obligation kind); anything else is ignored, so a typo cannot relabel a run."""
    if intent is None:
        return None
    label = intent.strip()
    if broker.intent(label) is not None or broker.obligation_for_follow_up(label) is not None:
        return label
    return None


def sections_in_progress(
    ctx: RunContext, sop: ResolvedSop, visible_sections: Sequence[ResolvedSection]
) -> tuple[ResolvedSection, ...]:
    """The procedures that own the follow-ups of the open obligations.

    At 07:13 the driver asks for the PO number while an ETA request is open. The
    sections for a question do not say how that request continues, so the model
    also sees the section its follow-up timer will run (`delay_risk_high`), as
    context. The ledger stays facts only: the link is the obligation kind, mapped
    to the intent its timer produces, and the section's own intents and stages.
    """
    intents = {
        intent
        for o in ctx.open_obligations
        if (intent := follow_up_intent(o.kind, ctx.broker)) is not None
    }
    shown = set(sop.section_ids)
    stage = ctx.load.stage
    return tuple(
        s
        for s in visible_sections
        if s.section_id not in shown
        and intents.intersection(s.intents)
        and (not s.stages or stage in s.stages)
    )
