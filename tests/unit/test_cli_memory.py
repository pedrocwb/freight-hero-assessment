"""`agent memory list|extract`, on a runtime's stores with the fake model."""

import asyncio
from dataclasses import replace

import pytest
from typer.testing import CliRunner, Result

from freight_agent import cli, cli_memory
from freight_agent.adapters.memory.embeddings import HashEmbeddings, memory_store
from freight_agent.adapters.memory.store import StoreFacilityMemory
from freight_agent.application.memory import NullMemoryProcessor
from freight_agent.domain.load import Load
from freight_agent.domain.memory import MemoryExtraction, ProposedOperation
from freight_agent.interfaces.llm import ModelRole
from freight_agent.settings import Settings
from tests.unit.application.test_memory import seeded
from tests.unit.application.test_runtime_harness import World, at

pytestmark = pytest.mark.unit

runner = CliRunner()
PICKUP = "riverbend-plant~stockton-ca"


def agent(*args: str) -> Result:
    return runner.invoke(cli.app, ["memory", *args])


@pytest.fixture
def world(load: Load, monkeypatch: pytest.MonkeyPatch) -> World:
    facts = StoreFacilityMemory(memory_store(HashEmbeddings(), 64))
    world = World(load, memory=facts)
    asyncio.run(world.start())
    asyncio.run(facts.apply("northline", upserts=[seeded("f1", "Check in at gate 4")]))
    monkeypatch.setattr(cli_memory, "deps_for", lambda settings: world.deps)
    return world


def test_list_prints_a_clients_notes_by_facility(world: World) -> None:
    result = agent("list", "--client", "northline")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"Riverbend plant ({PICKUP})",
        "  - f1 [check_in] 'Check in at gate 4' (load 470001, msg:470001:3, 2026-10-02)",
    ]
    assert agent("list", "--client", "acme").output == "no notes for client 'acme'\n"


def test_list_prints_the_notes_of_each_stop_of_a_load(world: World) -> None:
    result = agent("list", "--load", "481207")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0] == f"stop 1, pickup: Riverbend plant ({PICKUP})"
    assert "Check in at gate 4" in result.output
    assert result.output.splitlines()[-1] == "  (no notes)"


def test_list_needs_one_of_client_or_load_and_a_known_load(world: World) -> None:
    assert agent("list").exit_code == 2
    assert agent("list", "--client", "a", "--load", "b").exit_code == 2
    unknown = agent("list", "--load", "nope")
    assert unknown.exit_code == 2 and "is not in the store" in unknown.output


def test_extract_runs_the_load_now_and_prints_what_changed(world: World) -> None:
    asyncio.run(world.message(at(7, 20), "check-in moved to gate 6, call 209-555-0147"))
    ops = (
        ProposedOperation(
            op="update",
            facility_key=PICKUP,
            fact_id="f1",
            text="Check in at gate 6",
            event_id="msg:0720",
        ),
        ProposedOperation(
            op="add", facility_key=PICKUP, text="Call 209-555-0147", event_id="msg:0720"
        ),
    )
    world.model.push(ModelRole.MEMORY, MemoryExtraction(operations=ops))

    result = agent("extract", "--load", "481207")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith(
        "load 481207 (northline): done, 0 added, 1 updated, 0 deleted, 1 dropped"
    )
    assert lines[1] == "  dropped: add riverbend-plant~stockton-ca: has a phone number"
    assert "Check in at gate 6" in agent("list", "--load", "481207").output


def test_extract_says_when_the_model_failed(world: World) -> None:
    asyncio.run(world.message(at(7, 20), "gate 4"))

    result = agent("extract", "--load", "481207")  # nothing queued for the memory role

    assert result.exit_code == 0
    assert "pending" in result.output and "error: the memory model failed" in result.output


def test_extract_needs_a_known_load_and_an_extraction(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = agent("extract", "--load", "nope")
    assert unknown.exit_code == 2 and "is not in the store" in unknown.output

    bare = replace(world.deps, memory=None, memory_extraction=NullMemoryProcessor())
    monkeypatch.setattr(cli_memory, "deps_for", lambda settings: bare)
    assert "no facility memory extraction" in agent("extract", "--load", "481207").output
    assert "no facility memory" in agent("list", "--client", "northline").output


def test_the_default_deps_come_from_the_composition_root(tmp_path: object) -> None:
    deps = cli_memory.deps_for(Settings(_env_file=None, prompts_source="local"))  # pyright: ignore[reportCallIssue]

    assert deps.memory is not None
