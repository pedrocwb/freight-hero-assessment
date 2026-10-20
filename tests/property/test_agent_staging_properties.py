"""Staging is `validate` one call at a time (wave 3 spec, F1).

The tools node stages each action call against the actions accepted before it
(`application.agent.stage`), so the model learns at the call whether an action was
accepted. For any sequence of tool calls, split into rounds in any way:

- staging the action calls in order is `validate` of the same sequence: the same
  accepted actions and the same violations, at the same plan positions, so the validate
  node over the run's plan is the check that nothing drifted;
- `Agent.call_tools` answers the same whatever the split into rounds, given the earlier
  rounds' proposals, and counts as refused exactly the action calls it did not accept.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.application.agent import Agent, Proposal, ToolRound, stage, validation_context
from freight_agent.application.agent_tools import ACTION_TOOLS
from freight_agent.application.context import RunContext
from freight_agent.domain.actions import FINISH, Plan
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.rules import default_rules
from freight_agent.domain.validation import OutboundRecord, validate
from freight_agent.ports.llm import ToolCall
from tests.fakes import FakeModel

pytestmark = pytest.mark.property

ROOT = Path(__file__).resolve().parents[2]
LOAD = Load.from_metadata(json.loads((ROOT / "evals/fixtures/load_481207.json").read_text()))
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 13, 7, 31, tzinfo=LA)
DRIVER, DISPATCHER, BROKER = "drv_20931", "dsp_4410", "brk_0087"

contacts = st.sampled_from([DRIVER, DISPATCHER, BROKER, "x@y.com"])
texts = st.sampled_from(["ETA?", "PO 55821", "Call 555-123-4567", "", "mail x@y.com"])
kinds = st.sampled_from(["eta_request", "eta_request", "pod"])


def _args(**fields: st.SearchStrategy[Any]) -> st.SearchStrategy[dict[str, Any]]:
    """Arguments with each field present or left out, so some calls miss a required one."""
    return st.fixed_dictionaries({}, optional=fields)


ACTION_CALLS: dict[str, st.SearchStrategy[dict[str, Any]]] = {
    "send_sms": _args(
        contact_id=contacts,
        text=texts,
        purpose=st.sampled_from(["eta_request", "reply", "info", "escalation"]),
        requests=st.none() | kinds,
    ),
    "send_email": _args(
        to_contact_ids=st.lists(contacts, max_size=2),
        body=texts,
        subject=st.none() | st.just("Load 481207"),
        purpose=st.sampled_from(["eta_request", "escalation"]),
    ),
    "post_broker_slack": _args(text=texts),
    "write_tms_note": _args(text=texts),
    "create_hero_task": _args(
        title=st.just("Call the driver"), description=texts, urgent=st.booleans()
    ),
    "post_internal_slack": _args(text=texts),
    "update_stage": _args(stage=st.sampled_from([s.value for s in Stage])),
    "schedule_timer": _args(kind=kinds, due_in_minutes=st.sampled_from([0, 30, 2000])),
    "defer_obligation": _args(kind=kinds, minutes=st.sampled_from([5, 20])),
    "close_obligation": _args(kind=kinds, status=st.sampled_from(["satisfied", "cancelled", "x"])),
    "open_obligation": _args(kind=kinds),
}
OTHER_CALLS: dict[str, st.SearchStrategy[dict[str, Any]]] = {
    "get_ledger": _args(kind=kinds),
    "read_sop_section": _args(section_id=st.sampled_from(["escalation", "nope"])),
    "send_fax": _args(to=contacts),  # not a tool: "unknown tool", nothing staged
    FINISH: _args(rationale=st.just("done")),  # the answer: left to the graph
}
assert set(ACTION_CALLS) == set(ACTION_TOOLS)

action_calls = st.one_of(*(st.tuples(st.just(n), a) for n, a in ACTION_CALLS.items()))
any_calls = st.one_of(action_calls, *(st.tuples(st.just(n), a) for n, a in OTHER_CALLS.items()))
not_json = st.builds(
    lambda name: ToolCall(id="", name=name, error="Expecting value"),
    st.sampled_from([*ACTION_TOOLS[:2], "get_load", FINISH]),
)


@st.composite
def contexts(draw: st.DrawFn) -> RunContext:
    trigger = Event(
        event_id="timer:t1",
        load_id=LOAD.load_id,
        type=EventType.TIMER_DUE,
        occurred_at=NOW,
        channel=Channel.SYSTEM,
        data={"timer_id": "t1", "kind": "eta_request"},
    )
    attempts = draw(st.none() | st.integers(0, 2))
    obligations = (
        ()
        if attempts is None
        else (
            Obligation(
                obligation_id="obl_1",
                load_id=LOAD.load_id,
                kind="eta_request",
                attempts=attempts,
                deferrals_used=draw(st.integers(0, 1)),
                opened_at=NOW - timedelta(minutes=30),
            ),
        )
    )
    sent = draw(st.integers(0, 3))
    outbound = tuple(
        OutboundRecord(contact_id=DRIVER, channel=Channel.SMS, at=NOW - timedelta(minutes=5 * i))
        for i in range(sent)
    )
    stage = draw(st.sampled_from([Stage.TO_PICKUP, Stage.AT_DELIVERY, Stage.DELIVERED]))
    return RunContext(
        event=trigger,
        load=LOAD.model_copy(update={"stage": stage}),
        now=NOW,
        obligations=obligations,
        pending_timers=(),
        recent_events=(trigger,),
        recent_outbound=outbound,
    )


@st.composite
def rounds(draw: st.DrawFn) -> list[list[ToolCall]]:
    """Tool calls grouped into model rounds, as a live conversation makes them."""
    drawn = draw(st.lists(any_calls | not_json, max_size=12))
    calls = [c if isinstance(c, ToolCall) else ToolCall(id="", name=c[0], args=c[1]) for c in drawn]
    calls = [ToolCall(f"c{i}", c.name, c.args, c.error) for i, c in enumerate(calls)]
    grouped: list[list[ToolCall]] = []
    while calls:
        size = draw(st.integers(1, 3))
        grouped.append(calls[:size])
        calls = calls[size:]
    return grouped


def call_tools(ctx: RunContext, grouped: list[list[ToolCall]]) -> list[ToolRound]:
    """What the graph's tools node does, round after round, each after the earlier ones."""
    agent = Agent(FakeModel(), MemorySopIndex(()))
    done: list[ToolRound] = []
    proposals: list[Proposal] = []

    async def play() -> None:
        for calls in grouped:
            answered = await agent.call_tools(ctx, (), calls, proposals=tuple(proposals))
            proposals.extend(answered.proposals)
            done.append(answered)

    asyncio.run(play())
    return done


@settings(max_examples=300, deadline=None)
@given(ctx=contexts(), calls=st.lists(action_calls, max_size=12))
def test_staging_a_sequence_of_calls_is_validate_of_the_same_sequence(
    ctx: RunContext, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    checks, rules = validation_context(ctx), default_rules()
    proposals: list[Proposal] = []
    answers: list[str] = []
    for tool, args in calls:
        text, proposal = stage(tool, args, checks=checks, rules=rules, proposals=proposals)
        answers.append(text)
        if proposal is not None:
            proposals.append(proposal)

    outcome = validate(Plan(actions=tuple(p.action for p in proposals)), checks, rules)

    accepted = tuple(p.action for p in proposals if p.accepted)
    event(f"accepted: {min(len(accepted), 3)}")
    assert outcome.approved == accepted
    assert outcome.violations == tuple(v for p in proposals for v in p.violations)
    for index, proposal in enumerate(proposals):
        assert proposal.accepted == (not proposal.violations)
        assert all(v.action_index == index for v in proposal.violations)
    numbered = [a for a in answers if a.startswith("accepted as action #")]
    assert numbered == [f"accepted as action #{n}" for n in range(1, len(accepted) + 1)]


@settings(max_examples=200, deadline=None)
@given(ctx=contexts(), grouped=rounds())
def test_the_answers_do_not_depend_on_how_the_calls_are_split_into_rounds(
    ctx: RunContext, grouped: list[list[ToolCall]]
) -> None:
    by_round = call_tools(ctx, grouped)
    (at_once,) = call_tools(ctx, [[c for calls in grouped for c in calls]])

    steps = tuple(s for r in by_round for s in r.steps)
    event(f"rounds: {min(len(grouped), 3)}")
    assert steps == at_once.steps
    assert tuple(r for d in by_round for r in d.results) == at_once.results
    assert tuple(p for d in by_round for p in d.proposals) == at_once.proposals
    assert sum(d.rejections for d in by_round) == at_once.rejections


@settings(max_examples=200, deadline=None)
@given(ctx=contexts(), grouped=rounds())
def test_every_call_but_the_answer_is_a_step_and_only_refused_action_calls_count(
    ctx: RunContext, grouped: list[list[ToolCall]]
) -> None:
    """The validate node over the proposals approves exactly what the steps accepted."""
    (done,) = call_tools(ctx, [[c for calls in grouped for c in calls]])
    calls = [c for r in grouped for c in r if c.name != FINISH or c.error is not None]

    assert [s.tool for s in done.steps] == [c.name for c in calls]
    assert [r.call_id for r in done.results] == [c.id for c in calls]
    for step, sent in zip(done.steps, calls, strict=True):
        assert (step.accepted is None) == (sent.name not in ACTION_TOOLS)
    assert done.rejections == sum(s.accepted is False for s in done.steps)
    accepted = [s for s in done.steps if s.accepted]
    assert len(accepted) == sum(p.accepted for p in done.proposals)
    plan = Plan(actions=tuple(p.action for p in done.proposals))
    outcome = validate(plan, validation_context(ctx), default_rules())
    assert outcome.approved == tuple(p.action for p in done.proposals if p.accepted)
