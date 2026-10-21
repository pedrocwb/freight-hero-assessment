"""The run graph's nodes. Each factory takes the services it needs and returns the node.

Nodes are thin: the work lives in the services (`prefilter`, the gate, the knowledge
index, `Agent`, `domain.validation`). A node adapts their results to the run state and
decides whether the run ends here, by writing a `Verdict`. Errors are not caught:
the runtime turns a model failure or the deadline into an escalation and anything
else into a failed attempt.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence

from freight_agent.application import escalation
from freight_agent.application.agent import (
    ANSWER_NOW,
    Agent,
    Proposal,
    catalog_intent,
    validation_context,
)
from freight_agent.application.context import RunContext
from freight_agent.application.gate import Triage
from freight_agent.application.graph.state import RunState, RunUpdate, Verdict, need
from freight_agent.application.prefilter import PrefilterResult, prefilter
from freight_agent.domain.actions import Plan
from freight_agent.domain.runs import RunStatus
from freight_agent.domain.validation import Rule, ValidationOutcome, validate
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.llm import AssistantTurn, Turn, UserTurn

logger = logging.getLogger(__name__)

Node = Callable[[RunState], Awaitable[RunUpdate]]


def prefilter_node() -> Node:
    """Code checks before any model call: skip, or escalate an unknown sender."""

    async def run_prefilter(state: RunState) -> RunUpdate:
        result = prefilter(state.ctx)
        if result is None:
            return {"prefilter": None}
        return {"prefilter": result, "verdict": _prefilter_verdict(state.ctx, result)}

    return run_prefilter


def _prefilter_verdict(ctx: RunContext, result: PrefilterResult) -> Verdict:
    if result.skip is not None:
        return Verdict(status=RunStatus.SKIPPED, skip_reason=result.skip.value)
    return Verdict.escalated(escalation.unknown_sender(ctx, result.detail))


def triage_node(gate: Triage) -> Node:
    """The event's intent, on the broker's catalog. A run whose triage does not act ends
    as a no-op."""

    async def run_triage(state: RunState) -> RunUpdate:
        result = await gate.classify(state.ctx)
        update: RunUpdate = {
            "gate": result,
            "usage": _usage(result.usage),
            "intent": result.decision.intent,
        }
        if not result.should_act:
            update["verdict"] = Verdict(status=RunStatus.NOOP)
        return update

    return run_triage


def knowledge_node(knowledge: SopIndex) -> Node:
    """The SOP sections for this intent and stage (`by_applies`), and every section the
    load can see, for the catalog and the read tools.

    It never ends the run: an event no section covers is the agent's call
    (`finish(no_procedure=true)`), after it has searched the SOP.
    """

    async def run_knowledge(state: RunState) -> RunUpdate:
        load = state.ctx.load
        intent = need(state.gate_decision, "a triage decision").intent
        return {
            "sop": await knowledge.by_applies(load.broker_id, load.load_types, intent, load.stage),
            "sop_version": await knowledge.version(load.broker_id),
            "visible_sections": await knowledge.visible(load.broker_id, load.load_types),
        }

    return run_knowledge


def agent_node(agent: Agent) -> Node:
    """One model round of the agent loop: tool calls, or the answer.

    The round must answer (`force_answer`) once the broker's budget is spent: after
    `max_steps` rounds, or once the agent's rounds cost `max_cost_usd`. A model that
    replied in prose is asked once, in a user turn, for its answer (a trailing assistant
    message reads as a prefill to some providers).
    """

    async def run_agent(state: RunState) -> RunUpdate:
        ctx = state.ctx
        limits = ctx.broker.limits
        prose = _replied_in_prose(state.messages)
        nudge: tuple[Turn, ...] = (UserTurn(ANSWER_NOW),) if prose else ()
        spent = sum(u.cost_usd for u in state.agent_usage)
        reply = await agent.round(
            ctx,
            need(state.gate_decision, "a triage decision"),
            need(state.sop, "the resolved SOP"),
            state.visible_sections,
            (*state.messages, *nudge),
            force_answer=prose or state.rounds >= limits.max_steps or spent >= limits.max_cost_usd,
        )
        update: RunUpdate = {
            "messages": (*nudge, reply.turn),
            "rounds": state.rounds + 1,
            "usage": (reply.usage,),
        }
        if reply.answer is not None:
            update["outcome"] = reply.answer
            intent = catalog_intent(ctx.broker, reply.answer.intent)
            if intent is not None:
                update["intent"] = intent
        return update

    return run_agent


def _replied_in_prose(messages: Sequence[Turn]) -> bool:
    last = messages[-1] if messages else None
    return isinstance(last, AssistantTurn) and not last.tool_calls


def tools_node(agent: Agent) -> Node:
    """Answer the tool calls of the agent's last round: read tools from the run's context,
    action tools staged through the rules. Nothing executes until the run commits."""

    async def run_tools(state: RunState) -> RunUpdate:
        cost = state.agent_usage[-1].cost_usd if state.agent_usage else 0.0
        done = await agent.call_tools(
            state.ctx,
            state.visible_sections,
            state.pending_calls,
            proposals=state.proposals,
            round_cost=cost,
        )
        return {
            "messages": done.results,
            "trajectory": done.steps,
            "proposals": done.proposals,
            "rejections": state.rejections + done.rejections,
            "sections_searched": tuple(dict.fromkeys((*state.sections_searched, *done.sections))),
        }

    return run_tools


def validate_node(rules: Sequence[Rule]) -> Node:
    """Code decides what the plan may do, with the run's broker limits. Always ends the run.

    A check on the staging: an action the staging accepted and the rules now refuse is
    logged (it cannot happen unless the staging and the validator judged with different
    rules or context).
    """

    async def run_validate(state: RunState) -> RunUpdate:
        ctx = state.ctx
        plan = need(state.plan, "a plan")
        outcome = validate(plan, validation_context(ctx), rules)
        _warn_on_drift(ctx, state.proposals, outcome)
        return {"validation": outcome, "verdict": _plan_verdict(state, plan, outcome)}

    return run_validate


def _warn_on_drift(
    ctx: RunContext, proposals: Sequence[Proposal], outcome: ValidationOutcome
) -> None:
    refused = {v.action_index for v in outcome.violations}
    for index, proposal in enumerate(proposals):
        if proposal.accepted and index in refused:
            logger.warning(
                "run on event %s: action %d (%s) was accepted when staged and refused by the "
                "validator",
                ctx.event.event_id,
                index,
                proposal.action.type,
            )


def _plan_verdict(state: RunState, plan: Plan, outcome: ValidationOutcome) -> Verdict:
    """A high-severity violation, or more refused calls than the broker allows, escalates,
    and the approved actions still run first. An agent that found no procedure and
    approved nothing hands the event to a Hero (`sop_gap`)."""
    ctx = state.ctx
    budget = escalation.RejectionBudget(
        refused=state.rejections, limit=ctx.broker.limits.max_rejections
    )
    if outcome.has_high_severity or budget.exceeded:
        return Verdict.escalated(
            escalation.blocked_actions(ctx, plan, outcome, budget), approved=outcome.approved
        )
    if not outcome.approved:
        if state.no_procedure:
            intent = state.intent or need(state.gate_decision, "a triage decision").intent
            return Verdict.escalated(escalation.sop_gap(ctx, intent))
        return Verdict(status=RunStatus.NOOP)
    return Verdict(status=RunStatus.ACTED, actions=outcome.approved)


def _usage[T](usage: T | None) -> tuple[T, ...]:
    return () if usage is None else (usage,)
