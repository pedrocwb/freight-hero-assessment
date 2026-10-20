"""The staged plan is a pure fold over the transcript (wave 3 spec, F1).

A live model's action tools stage through a `Staging` and answer the model; scripted and
replayed models never run tools. For any sequence of tool calls, in any grouping into
rounds, folding the transcript the live run leaves (`stage_from_transcript`) must give
the plan, the answers and the rejection count the live tools gave, with or without the
tool results in the transcript, and `validate` over the proposed plan must approve
exactly the staged actions.
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

from freight_agent.application.agent import (
    Staging,
    merge_plan,
    stage_from_transcript,
    validation_context,
)
from freight_agent.application.agent_tools import agent_tools
from freight_agent.application.context import RunContext
from freight_agent.domain.actions import AgentOutcome
from freight_agent.domain.events import Event, EventType
from freight_agent.domain.load import Channel, Load, Stage
from freight_agent.domain.obligations import Obligation
from freight_agent.domain.rules import default_rules
from freight_agent.domain.validation import OutboundRecord, validate
from tests.unit.application.test_runtime_harness import FakeSop

pytestmark = pytest.mark.property

ROOT = Path(__file__).resolve().parents[2]
LOAD = Load.from_metadata(json.loads((ROOT / "evals/fixtures/load_481207.json").read_text()))
LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 13, 7, 31, tzinfo=LA)
DRIVER, DISPATCHER, BROKER = "drv_20931", "dsp_4410", "brk_0087"
MAX_REJECTIONS = 2

contacts = st.sampled_from([DRIVER, DISPATCHER, BROKER, "x@y.com"])
texts = st.sampled_from(["ETA?", "PO 55821", "Call 555-123-4567", "", "mail x@y.com"])
kinds = st.sampled_from(["eta_request", "eta_request", "pod"])


def _args(**fields: st.SearchStrategy[Any]) -> st.SearchStrategy[dict[str, Any]]:
    """Arguments with each field present or left out, so some calls miss a required one."""
    return st.fixed_dictionaries({}, optional=fields)


CALLS: dict[str, st.SearchStrategy[dict[str, Any]]] = {
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
    "get_ledger": _args(kind=kinds),
    "read_sop_section": _args(section_id=st.sampled_from(["escalation", "nope"])),
    "send_fax": _args(to=contacts),  # not a tool: "unknown tool" live, nothing staged
}

tool_calls = st.one_of(*(st.tuples(st.just(name), args) for name, args in CALLS.items()))


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
def rounds(draw: st.DrawFn) -> list[list[tuple[str, dict[str, Any]]]]:
    """Tool calls grouped into model rounds, as a live conversation makes them."""
    calls = draw(st.lists(tool_calls, max_size=12))
    grouped: list[list[tuple[str, dict[str, Any]]]] = []
    while calls:
        size = draw(st.integers(1, 3))
        grouped.append(calls[:size])
        calls = calls[size:]
    return grouped


def live_run(
    ctx: RunContext, grouped: list[list[tuple[str, dict[str, Any]]]]
) -> tuple[Staging, list[str], tuple[dict[str, Any], ...], frozenset[str]]:
    """What a live adapter does: run each call through the offered tools, in order, and
    keep the transcript. Returns the live staging, its answers to the action calls, the
    transcript and the offered tool names."""
    staging = Staging(validation_context(ctx), default_rules(), MAX_REJECTIONS)
    specs = {t.name: t for t in agent_tools(ctx, (), FakeSop(), stage=staging.answer)}
    transcript: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    answers: list[str] = []
    ids = iter(range(1, 1000))

    async def play() -> None:
        for calls in grouped:
            entry_calls = [{"id": f"c{next(ids)}", "name": n, "args": a} for n, a in calls]
            transcript.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": entry_calls,
                    "usage": {"cost_usd": 0.001},
                }
            )
            for call in entry_calls:
                spec = specs.get(call["name"])
                result = "Unknown tool." if spec is None else await spec.run(call["args"])
                if spec is not None and call["name"] not in ("get_ledger", "read_sop_section"):
                    answers.append(result)
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": call["name"],
                        "content": result,
                    }
                )

    asyncio.run(play())
    return staging, answers, tuple(transcript), frozenset(specs)


@settings(max_examples=300, deadline=None)
@given(ctx=contexts(), grouped=rounds())
def test_the_fold_of_the_live_transcript_is_the_live_staging(
    ctx: RunContext, grouped: list[list[tuple[str, dict[str, Any]]]]
) -> None:
    live, answers, transcript, offered = live_run(ctx, grouped)

    staged = stage_from_transcript(
        transcript,
        checks=validation_context(ctx),
        rules=default_rules(),
        max_rejections=MAX_REJECTIONS,
        offered=offered,
    )

    event(f"accepted: {min(len(staged.accepted), 3)}")
    event(f"over the budget: {staged.rejections > MAX_REJECTIONS}")
    assert staged.proposals == tuple(live.proposals)
    assert staged.accepted == live.accepted
    assert staged.rejections == live.rejections
    action_steps = [s for s in staged.trajectory if s.accepted is not None]
    assert [s.result for s in action_steps] == answers
    assert len(staged.trajectory) == sum(len(r) for r in grouped)


@settings(max_examples=200, deadline=None)
@given(ctx=contexts(), grouped=rounds())
def test_a_scripted_transcript_without_results_folds_to_the_same_plan(
    ctx: RunContext, grouped: list[list[tuple[str, dict[str, Any]]]]
) -> None:
    """Scripted mode: the tool calls only, no tool entries."""
    live, _, transcript, offered = live_run(ctx, grouped)
    bare = tuple(e for e in transcript if e["role"] != "tool")

    staged = stage_from_transcript(
        bare,
        checks=validation_context(ctx),
        rules=default_rules(),
        max_rejections=MAX_REJECTIONS,
        offered=offered,
    )

    assert staged.proposals == tuple(live.proposals)
    assert staged.rejections == live.rejections


@settings(max_examples=200, deadline=None)
@given(ctx=contexts(), grouped=rounds())
def test_validating_the_proposed_plan_approves_exactly_the_staged_actions(
    ctx: RunContext, grouped: list[list[tuple[str, dict[str, Any]]]]
) -> None:
    """The validate node is the merge point: over the staged calls it agrees with the fold,
    and the violations it reports are the refused calls, at their plan positions."""
    _, _, transcript, offered = live_run(ctx, grouped)
    staged = stage_from_transcript(
        transcript,
        checks=validation_context(ctx),
        rules=default_rules(),
        max_rejections=MAX_REJECTIONS,
        offered=offered,
    )

    outcome = validate(merge_plan(staged, AgentOutcome()), validation_context(ctx), default_rules())

    assert outcome.approved == staged.accepted
    refused = {i for i, p in enumerate(staged.proposals) if not p.accepted}
    assert {v.action_index for v in outcome.violations} == refused
