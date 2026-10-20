"""The agent loop's two steps, which the run graph alternates: one model round
(`Agent.round`, the `agent` node) and the tool calls of that round (`Agent.call_tools`,
the `tools` node).

A round is one `LanguageModel.chat` on the `agent` role. The model sees the prompts, the
conversation so far and the tools, and it calls tools or answers with `finish`
(`AgentOutcome`). The tools node answers every call of the round, in order: a read tool
from the run's context or the knowledge index, an action tool by staging the action
(`stage`): built from the call's arguments and checked with
`domain.validation.check_action` against the actions accepted before it, it is answered
"accepted as action #n" or "rejected by <rule>: <why>". Nothing executes here: the
runtime executes the accepted actions after the run, in its commit.

The plan the run records and validates is every action the agent proposed, in call
order, refused ones included (`Proposal`). `validate` over that plan approves exactly
what the staging accepted (`check_action` is `validate` one action at a time), so the
validate node is the check that nothing drifted.

The budgets (`Broker.limits`) are the graph's, checked after every round
(`application/graph/build.py`): `max_steps` rounds of tool calls and `max_cost_usd`
for the agent's rounds (once either is spent, the next round must answer), and
`max_rejections` refused action calls (over it the run stops and escalates,
`escalation.blocked_actions`, and the accepted actions still run).
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from freight_agent.application.agent_tools import (
    ACTION_TOOLS,
    READ_SOP_SECTION,
    SEARCH_SOP,
    Toolbox,
    agent_tools,
    build_action,
    invalid_arguments,
)
from freight_agent.application.context import RunContext
from freight_agent.application.gate import follow_up_intent
from freight_agent.application.prompts import render_agent_prompt, render_agent_system_prompt
from freight_agent.domain.actions import FINISH, Action, AgentOutcome
from freight_agent.domain.broker import Broker
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.rules import default_rules
from freight_agent.domain.runs import TrajectoryStep
from freight_agent.domain.sop import ResolvedSection, ResolvedSop
from freight_agent.domain.validation import Rule, ValidationContext, Violation, check_action
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import (
    ChatReply,
    ChatRequest,
    LanguageModel,
    ModelRole,
    ToolCall,
    ToolResult,
    Turn,
)

logger = logging.getLogger(__name__)

# What code says to a model that replied in prose: the next round must answer.
ANSWER_NOW = f"Now call {FINISH} with your final answer."


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


def stage(
    tool: str,
    args: Mapping[str, Any],
    *,
    checks: ValidationContext,
    rules: Sequence[Rule],
    proposals: Sequence[Proposal],
) -> tuple[str, Proposal | None]:
    """Stage one action tool call after `proposals`: the answer to the model, and the
    proposal (None when the arguments build no action, a refused call all the same).

    The action is judged against the actions accepted before it, at its position among
    the proposed ones, so staging a sequence of calls is `validate` of the same sequence.
    """
    try:
        action = build_action(tool, args)
    except ValidationError as exc:
        return invalid_arguments(tool, exc), None
    accepted = tuple(p.action for p in proposals if p.accepted)
    violations = check_action(action, checks, accepted, rules, index=len(proposals))
    proposal = Proposal(action, not violations, violations)
    if not violations:
        return f"accepted as action #{len(accepted) + 1}", proposal
    reasons = "; ".join(f"{v.rule}: {v.message}" for v in violations)
    return f"rejected by {reasons}", proposal


@dataclass(frozen=True)
class ToolRound:
    """What answering one round's tool calls adds to the run."""

    results: tuple[ToolResult, ...] = ()  # one per call, in order, for the next round
    steps: tuple[TrajectoryStep, ...] = ()
    proposals: tuple[Proposal, ...] = ()  # the action calls that built an action
    rejections: int = 0  # refused action calls, bad arguments included
    sections: tuple[str, ...] = ()  # sections search_sop returned or read_sop_section read


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

    def tools(self, ctx: RunContext, visible_sections: Sequence[ResolvedSection]) -> Toolbox:
        return agent_tools(ctx, visible_sections, self._knowledge)

    @staticmethod
    def prompts(
        ctx: RunContext,
        gate: GateDecision,
        sop: ResolvedSop,
        visible_sections: Sequence[ResolvedSection],
    ) -> tuple[str, str]:
        """The agent's system and user prompts: the one place the loop renders them, the
        same for every round of a run."""
        in_progress = sections_in_progress(ctx, sop, visible_sections)
        return (
            render_agent_system_prompt(ctx.broker),
            render_agent_prompt(ctx, gate, sop, in_progress),
        )

    async def round(
        self,
        ctx: RunContext,
        gate: GateDecision,
        sop: ResolvedSop,
        visible_sections: Sequence[ResolvedSection],
        history: Sequence[Turn],
        *,
        force_answer: bool = False,
    ) -> ChatReply[AgentOutcome]:
        """One model round. `sop` holds the sections for this intent and stage;
        `visible_sections` back the `read_sop_section` tool (every section this broker and
        load can see). `LlmError` propagates: the runtime escalates a model failure."""
        system, user = self.prompts(ctx, gate, sop, visible_sections)
        request = ChatRequest(
            role=ModelRole.AGENT,
            system=system,
            user=user,
            history=tuple(history),
            tools=self.tools(ctx, visible_sections).specs,
            force_answer=force_answer,
            timeout_s=self._timeout_s,
        )
        return await self._llm.chat(request, AgentOutcome)

    async def call_tools(
        self,
        ctx: RunContext,
        visible_sections: Sequence[ResolvedSection],
        calls: Sequence[ToolCall],
        *,
        proposals: Sequence[Proposal] = (),
        round_cost: float = 0.0,
    ) -> ToolRound:
        """Answer every call of a round but the answer's, in order, after the actions
        staged in earlier rounds (`proposals`). A bad call is an answer the model can
        recover from in its next round, never an exception; a read tool that raises is
        reported the same way (a tool has no side effect outside the run)."""
        toolbox = self.tools(ctx, visible_sections)
        checks = validation_context(ctx)
        staged = list(proposals)
        results: list[ToolResult] = []
        steps: list[TrajectoryStep] = []
        sections: list[str] = []
        rejections = 0
        for call in calls:
            if call.name == FINISH and call.error is None:
                continue
            accepted: bool | None = None
            if call.error is not None:
                text = (
                    f"The arguments for {call.name} are not valid JSON ({call.error}). "
                    "Call it again with a JSON object."
                )
                if call.name in ACTION_TOOLS:
                    accepted = False
            elif call.name in ACTION_TOOLS:
                text, proposal = stage(
                    call.name, call.args, checks=checks, rules=self.rules, proposals=staged
                )
                accepted = proposal is not None and proposal.accepted
                if proposal is not None:
                    staged.append(proposal)
            elif (reader := toolbox.readers.get(call.name)) is not None:
                try:
                    text = await reader(call.args)
                except Exception as exc:
                    text = f"The tool {call.name} failed ({type(exc).__name__}: {exc}). "
                    text += "Continue without it."
                sections.extend(_sections_of(call.name, call.args, text))
            else:
                text = f"Unknown tool {call.name!r}. Available tools: {', '.join(toolbox.names)}."
            rejections += accepted is False
            results.append(ToolResult(call_id=call.id, name=call.name, content=text))
            steps.append(
                TrajectoryStep(
                    tool=call.name,
                    args=dict(call.args),
                    result=text,
                    accepted=accepted,
                    cost_usd=round_cost,
                )
            )
        return ToolRound(
            results=tuple(results),
            steps=tuple(steps),
            proposals=tuple(staged[len(proposals) :]),
            rejections=rejections,
            sections=tuple(sections),
        )


def _sections_of(tool: str, args: Mapping[str, Any], result: str) -> list[str]:
    """The sections a read tool call looked at: the one `read_sop_section` read (its result
    starts `# <section_id>:`; an unknown id read nothing), or those `search_sop` returned
    (its result lines start `- <section_id>:`)."""
    if tool == READ_SOP_SECTION:
        section_id = args.get("section_id")
        if not isinstance(section_id, str) or not result.startswith(f"# {section_id.strip()}:"):
            return []
        return [section_id.strip()]
    if tool == SEARCH_SOP:
        return [
            line[2:].split(":", 1)[0]
            for line in result.splitlines()
            if line.startswith("- ") and ":" in line
        ]
    return []


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
