"""LangGraph Studio's scenario graph (`freight_agent.studio`), played in process.

The graph runs as the Agent Server runs it: a copy with an in-memory checkpointer, streamed
with its subgraphs. The fake model gives the agent's rounds, so the real runtime, run
graph, rules, executor and graders play the scenario. What these tests pin: the run graph
is drawn and streamed inside `run`, one run per visit; the state after each node is JSON;
the subgraph's checkpoints load back; the input is checked before any world is built; a
checkpoint behind its world cannot go on, while an interrupt can.
"""

import json
from collections.abc import Sequence
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from freight_agent.adapters.clock import VirtualClock
from freight_agent.interfaces.llm import LanguageModel, ModelRole
from freight_agent.settings import Settings
from freight_agent.studio import (
    ADVANCE,
    CHECK,
    DELIVER,
    RUN,
    SETUP,
    StudioError,
    Worlds,
    build_studio_graph,
    free_form,
)
from tests.fakes import FakeModel
from tests.studio_fixtures import (
    ASK_AGAIN,
    ASK_ETA,
    CALL_DRIVER,
    DISPATCHER,
    DRIVER,
    EMAIL_DISPATCHER,
    EXHAUSTED,
    FOLLOW_UP_IN_30,
    PO_REPLY,
    QUESTION,
    plan,
    studio_settings,
)

pytestmark = pytest.mark.unit

A = {"scenario_id": "a_client_a_high_risk"}
RUN_NODES = ["prefilter", "gate", "knowledge", "agent", "tools", "agent", "validate"]
ONE_STEP = [ADVANCE, RUN, DELIVER, ADVANCE, CHECK]


def scenario_a(model: FakeModel) -> FakeModel:
    """Scenario A's rounds: the ETA request, the PO answer after the gate, the follow-up,
    then the email to the dispatcher with the Hero task."""
    model.push(ModelRole.AGENT, plan(ASK_ETA, FOLLOW_UP_IN_30))
    model.push(ModelRole.GATE, QUESTION)
    model.push(
        ModelRole.AGENT,
        plan(PO_REPLY),
        plan(ASK_AGAIN, FOLLOW_UP_IN_30),
        plan(EMAIL_DISPATCHER, CALL_DRIVER, EXHAUSTED),
    )
    return model


def studio(model: FakeModel, settings: Settings | None = None, worlds: Worlds | None = None) -> Any:
    """The graph as the server serves it: a copy with its checkpointer."""

    def fake(clock: VirtualClock) -> LanguageModel:
        return model

    graph = build_studio_graph(settings or studio_settings(), model=fake, worlds=worlds)
    return graph.copy(update={"checkpointer": InMemorySaver()})


def thread(name: str = "t") -> dict[str, Any]:
    return {"configurable": {"thread_id": name}}


async def stream(graph: Any, value: Any, config: dict[str, Any], **kwargs: Any) -> list[Any]:
    """Every update, as (namespace, node, update), outer nodes with an empty namespace."""
    seen: list[Any] = []
    async for ns, chunk in graph.astream(
        value, config, stream_mode="updates", subgraphs=True, **kwargs
    ):
        for node, update in chunk.items():
            seen.append((ns, node, update))
    return seen


def outer(seen: Sequence[Any]) -> list[str]:
    return [node for ns, node, _ in seen if ns == ()]


def inner(seen: Sequence[Any]) -> dict[str, list[str]]:
    runs: dict[str, list[str]] = {}
    for ns, node, _ in seen:
        if ns:
            runs.setdefault(ns[0], []).append(node)
    return runs


# --- the drawing --------------------------------------------------------------------------


def test_the_run_graph_is_drawn_inside_run() -> None:
    drawn = studio(FakeModel()).get_graph(xray=True)

    nodes = set(drawn.nodes)
    assert {SETUP, ADVANCE, DELIVER, CHECK} <= nodes
    assert {f"{RUN}:{n}" for n in RUN_NODES} <= nodes


def test_the_server_serves_a_module_level_graph_named_scenario() -> None:
    from freight_agent import studio as module

    assert module.graph.name == "scenario"
    assert module.graph.config is not None
    assert module.graph.config.get("recursion_limit") == module.RECURSION_LIMIT


# --- scenario A ---------------------------------------------------------------------------


async def test_scenario_a_plays_each_run_as_a_subgraph_of_its_own_node() -> None:
    model = scenario_a(FakeModel())
    graph = studio(model)

    seen = await stream(graph, A, thread())

    assert outer(seen) == [SETUP, *ONE_STEP * 4]
    runs = inner(seen)
    assert len(runs) == 4 and all(ns.startswith(f"{RUN}:") for ns in runs)
    assert list(runs.values()) == [RUN_NODES] * 4
    assert model.remaining() == 0


async def test_scenario_a_ends_with_the_request_exhausted_an_email_and_a_hero_task() -> None:
    graph = studio(scenario_a(FakeModel()))

    final = await graph.ainvoke(A, thread())

    json.dumps(final)  # every value is JSON
    assert final["finished"] is True and final["step"] == 4
    (request,) = final["ledger"]
    assert (request["kind"], request["status"], request["attempts"]) == (
        "eta_request",
        "exhausted",
        2,
    )
    assert [t["status"] for t in final["timers"]] == ["fired", "fired"]
    emails = [d for d in final["sent"] if d["channel"] == "email"]
    assert [e["to_contact_ids"] for e in emails] == [[DISPATCHER]]
    assert [d["channel"] for d in final["sent"]].count("hero_task") == 1
    assert all(i["status"] == "sent" for i in final["outbox"])
    assert [r["status"] for r in final["runs"]] == ["acted"] * 4
    assert [r["event_type"] for r in final["runs"]] == [
        "risk_changed",
        "inbound_message",
        "timer_due",
        "timer_due",
    ]
    assert all(step["passed"] for step in final["checks"]), final["checks"]
    assert [c["name"] for c in final["checks"][-1]["checks"]][-2:] == [
        "invariant.idempotency",
        "invariant.recipients",
    ]


async def test_the_state_after_each_node_says_what_it_did() -> None:
    graph = studio(scenario_a(FakeModel()))

    seen = await stream(graph, {**A, "until_step": 0}, thread())

    updates = {node: update for ns, node, update in seen if ns == ()}
    assert updates[SETUP]["scenario"]["id"] == A["scenario_id"]
    assert updates[SETUP]["clock"] == "2026-10-13T06:55:00-07:00"
    advance = next(u for ns, n, u in seen if ns == () and n == ADVANCE)
    assert [e["type"] for e in advance["logged"]] == ["risk_changed"]
    assert advance["queued"] == 1 and advance["clock"] == "2026-10-13T07:01:00-07:00"
    run = updates[RUN]
    assert run["event"]["type"] == "risk_changed"
    assert run["run"]["status"] == "acted"
    assert run["run"]["nodes"] == RUN_NODES
    assert [a["type"] for a in run["run"]["plan"]["actions"]] == ["send_sms", "schedule_timer"]
    assert [t["status"] for t in run["timers"]] == ["pending"]
    assert [i["status"] for i in run["outbox"]] == ["pending"]
    assert [i["status"] for i in updates[DELIVER]["outbox"]] == ["sent"]
    assert [d["contact_id"] for d in updates[DELIVER]["sent"]] == [DRIVER]
    assert updates[CHECK]["checks"][0]["passed"] is True


async def test_the_run_graphs_checkpoints_load_back_with_its_state() -> None:
    graph = studio(scenario_a(FakeModel()))
    await graph.ainvoke({**A, "until_step": 0}, thread())

    tasks = [
        task
        async for snapshot in graph.aget_state_history(thread())
        for task in snapshot.tasks
        if task.name == RUN and task.state is not None
    ]

    (task,) = tasks
    run_state = (await graph.aget_state(task.state)).values
    assert run_state["ctx"].event.type.value == "risk_changed"
    assert run_state["verdict"].status.value == "acted"
    assert run_state["gate"].decision.intent == "delay_risk_high"


async def test_until_step_stops_after_that_step() -> None:
    model = scenario_a(FakeModel())
    graph = studio(model)

    final = await graph.ainvoke({**A, "until_step": 1}, thread())

    assert [c["step"] for c in final["checks"]] == [0, 1]
    assert [r["event_type"] for r in final["runs"]] == ["risk_changed", "inbound_message"]
    assert model.remaining(ModelRole.AGENT) == 4  # two plans of two rounds left


# --- the free-form input --------------------------------------------------------------------


async def test_a_free_form_ping_plays_one_step_on_the_load() -> None:
    model = FakeModel()
    model.push(ModelRole.AGENT, plan(ASK_ETA, FOLLOW_UP_IN_30))
    graph = studio(model)

    final = await graph.ainvoke({"load": "load_481207", "ping": {"delay_risk": "high"}}, thread())

    assert final["scenario"]["id"] == "free_form"
    assert [r["event_type"] for r in final["runs"]] == ["risk_changed"]
    assert [d["channel"] for d in final["sent"]] == ["sms"]
    assert final["checks"][0]["passed"] is True  # nothing declared but the invariants


async def test_a_free_form_message_is_ingested_from_its_sender() -> None:
    model = FakeModel()
    model.push(ModelRole.GATE, QUESTION)
    model.push(ModelRole.AGENT, plan(PO_REPLY))
    graph = studio(model)
    message = {"channel": "sms", "sender": DRIVER, "body": "What's the PO number?"}

    final = await graph.ainvoke({"load": "load_481207", "message": message}, thread())

    assert final["event"]["sender_contact_id"] == DRIVER
    assert [d["text"] for d in final["sent"]] == [PO_REPLY.text]


def test_a_free_form_input_needs_one_message_or_one_ping() -> None:
    with pytest.raises(StudioError, match="one message or one ping"):
        free_form("load_481207", message=None, ping=None)
    with pytest.raises(StudioError, match="one message or one ping"):
        free_form("load_481207", message={"body": "hi"}, ping={"delay_risk": "high"})
    with pytest.raises(StudioError, match="free-form ping is not valid"):
        free_form("load_481207", message=None, ping={"delay_risk": "very high"})


@pytest.mark.parametrize("load", ["../../pyproject", "fixtures/load_481207.json", ""])
def test_a_free_form_load_names_a_fixture_never_a_path(load: str) -> None:
    with pytest.raises(StudioError, match="names a fixture of evals/fixtures"):
        free_form(load, message=None, ping={"delay_risk": "high"})


def test_a_free_form_load_may_carry_its_json_suffix() -> None:
    scenario = free_form("load_481207.json", message=None, ping={"delay_risk": "high"})

    assert scenario.load == "fixtures/load_481207.json"


# --- refused inputs and threads --------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ({"scenario_id": "nope"}, "no scenario 'nope'; scenarios: .*a_client_a_high_risk"),
        ({**A, "load": "load_481207"}, "not both"),
        ({}, "give"),
        ({**A, "until_step": 4}, "until_step must be between 0 and 3"),
        ({"load": "load_404", "ping": {"delay_risk": "high"}}, "load fixture not found"),
    ],
)
async def test_an_input_that_cannot_play_stops_at_setup(value: Any, error: str) -> None:
    worlds = Worlds()
    graph = studio(FakeModel(), worlds=worlds)

    with pytest.raises(StudioError, match=error):
        await graph.ainvoke(value, thread())


async def test_without_the_key_the_live_model_stops_the_play_at_setup() -> None:
    graph: Any = build_studio_graph(studio_settings())
    graph = graph.copy(update={"checkpointer": InMemorySaver()})

    with pytest.raises(StudioError, match="OPENROUTER_API_KEY is not set"):
        await graph.ainvoke(A, thread())


async def test_an_interrupted_play_goes_on_where_it_stopped() -> None:
    graph = studio(scenario_a(FakeModel()))
    await graph.ainvoke(A, thread(), interrupt_after=[DELIVER])

    final = await graph.ainvoke(None, thread())

    assert final["finished"] is True
    assert all(step["passed"] for step in final["checks"])


async def test_a_checkpoint_behind_its_world_cannot_go_on() -> None:
    graph = studio(scenario_a(FakeModel()))
    await graph.ainvoke(A, thread(), interrupt_after=[DELIVER])
    history = [s async for s in graph.aget_state_history(thread())]
    after_setup = next(s for s in history if s.next == (ADVANCE,) and s.values.get("moves") == 0)

    with pytest.raises(StudioError, match="behind its world"):
        await graph.ainvoke(None, after_setup.config)


async def test_a_play_that_ended_leaves_no_world_behind() -> None:
    worlds = Worlds()
    graph = studio(scenario_a(FakeModel()), worlds=worlds)
    await graph.ainvoke({**A, "until_step": 0}, thread())
    history = [s async for s in graph.aget_state_history(thread())]
    before_check = next(s for s in history if s.next == (CHECK,))

    with pytest.raises(StudioError, match="world is gone"):
        await graph.ainvoke(None, before_check.config)


async def test_past_its_keep_the_oldest_world_goes() -> None:
    model = scenario_a(FakeModel())
    model.push(ModelRole.AGENT, plan(ASK_ETA, FOLLOW_UP_IN_30))
    graph = studio(model, worlds=Worlds(keep=1))
    await graph.ainvoke(A, thread("first"), interrupt_after=[DELIVER])
    await graph.ainvoke(A, thread("second"), interrupt_after=[DELIVER])

    with pytest.raises(StudioError, match="world is gone"):
        await graph.ainvoke(None, thread("first"))
