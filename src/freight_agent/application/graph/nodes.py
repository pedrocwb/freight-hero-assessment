"""The run graph's nodes. Each factory takes the services it needs and returns the node.

Nodes are thin: the work lives in the services (`prefilter`, `Gate`, the knowledge
index, `Agent`, `domain.validation`). A node adapts their results to the run state and
decides whether the run ends here, by writing a `Verdict`. Errors are not caught:
the runtime turns a model failure or the deadline into an escalation and anything
else into a failed attempt.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence

from freight_agent.application import escalation
from freight_agent.application.agent import Agent, Staged, validation_context
from freight_agent.application.context import RunContext
from freight_agent.application.gate import Gate
from freight_agent.application.graph.state import RunState, RunUpdate, Verdict, need
from freight_agent.application.prefilter import PrefilterResult, prefilter
from freight_agent.domain.actions import Plan
from freight_agent.domain.runs import RunStatus
from freight_agent.domain.validation import Rule, ValidationOutcome, validate
from freight_agent.ports.knowledge import SopIndex

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


def triage_node(gate: Gate) -> Node:
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
    """One agent model call: what it staged, the plan it proposed, how it read the event."""

    async def run_agent(state: RunState) -> RunUpdate:
        result = await agent.run(
            state.ctx,
            need(state.gate_decision, "a triage decision"),
            need(state.sop, "the resolved SOP"),
            state.visible_sections,
        )
        staged = result.staged
        update: RunUpdate = {
            "plan": result.plan,
            "staged": staged,
            "no_procedure": result.no_procedure,
            "usage": result.usage,
            "trajectory": staged.trajectory,
            "sections_searched": staged.sections_searched,
            "rejections": staged.rejections,
        }
        if result.intent is not None:
            update["intent"] = result.intent
        return update

    return run_agent


def validate_node(rules: Sequence[Rule]) -> Node:
    """Code decides what the plan may do, with the run's broker limits. Always ends the run.

    The merge point of the staged calls and the answer's actions, and a check on the fold:
    an action the staging accepted and the rules now refuse is logged (it cannot happen
    unless the staging and the validator judged with different rules or context).
    """

    async def run_validate(state: RunState) -> RunUpdate:
        ctx = state.ctx
        plan = need(state.plan, "a plan")
        outcome = validate(plan, validation_context(ctx), rules)
        staged = state.staged or Staged()
        _warn_on_drift(ctx, staged, outcome)
        return {"validation": outcome, "verdict": _plan_verdict(state, plan, outcome, staged)}

    return run_validate


def _warn_on_drift(ctx: RunContext, staged: Staged, outcome: ValidationOutcome) -> None:
    refused = {v.action_index for v in outcome.violations}
    for index, proposal in enumerate(staged.proposals):
        if proposal.accepted and index in refused:
            logger.warning(
                "run on event %s: action %d (%s) was accepted when staged and refused by the "
                "validator",
                ctx.event.event_id,
                index,
                proposal.action.type,
            )


def _plan_verdict(
    state: RunState, plan: Plan, outcome: ValidationOutcome, staged: Staged
) -> Verdict:
    """A high-severity violation, or more refused calls than the broker allows, escalates,
    and the approved actions still run first. An agent that found no procedure and
    approved nothing hands the event to a Hero (`sop_gap`)."""
    ctx = state.ctx
    budget = escalation.RejectionBudget(
        refused=staged.rejections, limit=ctx.broker.limits.max_rejections
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
