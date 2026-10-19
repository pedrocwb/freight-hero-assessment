"""The run graph's nodes. Each factory takes the services it needs and returns the node.

Nodes are thin: the work lives in the services (`prefilter`, `Gate`, `domain.sop`,
`Decider`, `domain.validation`). A node adapts their results to the run state and
decides whether the run ends here, by writing a `Verdict`. Errors are not caught:
the runtime turns a model failure or the deadline into an escalation and anything
else into a failed attempt.
"""

from collections.abc import Awaitable, Callable, Sequence

from freight_agent.application import escalation
from freight_agent.application.context import RunContext
from freight_agent.application.decide import Decider
from freight_agent.application.gate import Gate
from freight_agent.application.graph.state import RunState, RunUpdate, Verdict, need
from freight_agent.application.prefilter import PrefilterResult, prefilter
from freight_agent.domain.actions import Plan
from freight_agent.domain.runs import RunStatus
from freight_agent.domain.sop import effective_sections, resolve
from freight_agent.domain.validation import (
    Rule,
    ValidationConfig,
    ValidationContext,
    ValidationOutcome,
    validate,
)
from freight_agent.ports.sop import SopRepository

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


def gate_node(gate: Gate) -> Node:
    """The event's intent. A run whose gate does not act ends as a no-op."""

    async def run_gate(state: RunState) -> RunUpdate:
        result = await gate.classify(state.ctx)
        update: RunUpdate = {"gate": result, "usage": _usage(result.usage)}
        if not result.should_act:
            update["verdict"] = Verdict(status=RunStatus.NOOP)
        return update

    return run_gate


def resolve_sop_node(sop: SopRepository) -> Node:
    """The SOP sections for this intent and stage, plus every section the load can see.

    General sections (no intents) apply to every event, so they do not count as
    a procedure: without a section that names the intent, the run escalates.
    """

    async def run_resolve_sop(state: RunState) -> RunUpdate:
        ctx = state.ctx
        load = ctx.load
        intent = need(state.gate_decision, "a gate decision").intent
        sections = await sop.sections_for_client(load.broker_id)
        resolved = resolve(
            sections,
            client_id=load.broker_id,
            load_types=load.load_types,
            intent=intent,
            stage=load.stage,
        )
        visible = effective_sections(sections, client_id=load.broker_id, load_types=load.load_types)
        update: RunUpdate = {
            "sop": resolved,
            "sop_version": sop.version(),
            "visible_sections": visible,
        }
        if not any(s.intents for s in resolved.sections):
            update["verdict"] = Verdict.escalated(escalation.sop_gap(ctx, intent))
        return update

    return run_resolve_sop


def decide_node(decider: Decider) -> Node:
    """One model call: the proposed plan."""

    async def run_decide(state: RunState) -> RunUpdate:
        result = await decider.decide(
            state.ctx,
            need(state.gate_decision, "a gate decision"),
            need(state.sop, "the resolved SOP"),
            state.visible_sections,
        )
        return {"plan": result.plan, "usage": result.usage}

    return run_decide


def validate_node(rules: Sequence[Rule], config: ValidationConfig) -> Node:
    """Code decides what the plan may do. Always ends the run."""

    async def run_validate(state: RunState) -> RunUpdate:
        ctx = state.ctx
        plan = need(state.plan, "a plan")
        checks = ValidationContext(
            load=ctx.load,
            now=ctx.now,
            open_obligations=ctx.open_obligations,
            recent_outbound=ctx.recent_outbound,
            config=config,
        )
        outcome = validate(plan, checks, rules)
        return {"validation": outcome, "verdict": _plan_verdict(ctx, plan, outcome)}

    return run_validate


def _plan_verdict(ctx: RunContext, plan: Plan, outcome: ValidationOutcome) -> Verdict:
    """A high-severity violation escalates, and the approved actions still run first."""
    if outcome.has_high_severity:
        return Verdict.escalated(
            escalation.blocked_actions(ctx, plan, outcome), approved=outcome.approved
        )
    if not outcome.approved:
        return Verdict(status=RunStatus.NOOP)
    return Verdict(status=RunStatus.ACTED, actions=outcome.approved)


def _usage[T](usage: T | None) -> tuple[T, ...]:
    return () if usage is None else (usage,)
