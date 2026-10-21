"""The assignment's scenario (`a_client_a_high_risk`) through the product runtime, on the
memory store and on Postgres, with the same scripted model (`tests.fakes.FakeModel`):
every step must leave the same ledger, timers, outbox and run statuses.

The contract suites check each repository on its own. This checks them as the runtime
uses them: its write order under deferred foreign keys, its claim, think and commit
units of work, a follow-up timer that fires, an escalation that closes the obligation.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.persistence.postgres.store import PostgresStore, open_store
from freight_agent.domain.actions import (
    CloseObligation,
    CloseStatus,
    CreateHeroTask,
    Plan,
    Purpose,
    ScheduleTimer,
    SendEmail,
    SendSms,
)
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.obligations import ObligationStatus
from freight_agent.domain.runs import RunStatus
from freight_agent.evalkit.graders import grade
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import Played, play_scenario
from freight_agent.evalkit.spec import load_scenario
from freight_agent.interfaces.llm import ModelRole
from freight_agent.interfaces.repositories import UnitOfWorkFactory
from tests.fakes import FakeModel
from tests.postgres import DATABASE_URL, POSTGRES, scratch_schema
from tests.prompts import PROMPTS

pytestmark = POSTGRES

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
SCENARIO = load_scenario(EVALS / "scenarios" / "core" / "a_client_a_high_risk.yaml")
LOAD_ID = "481207"
DRIVER, DISPATCHER = "drv_20931", "dsp_4410"


def _eta_request(text: str) -> Plan:
    return Plan(
        rationale="delay_risk_high: ask the driver for the ETA, follow up in 30 minutes",
        actions=(
            SendSms(contact_id=DRIVER, text=text, purpose=Purpose.ETA_REQUEST),
            ScheduleTimer(kind="eta_request", due_in_minutes=30),
        ),
    )


def scenario_a_agent() -> FakeModel:
    """A right agent for scenario A: ask for the ETA, answer the driver's question,
    follow up once, then escalate to the dispatcher and a Hero."""
    model = FakeModel()
    model.push(ModelRole.AGENT, _eta_request("Hi, what is your ETA to the Riverbend pickup?"))
    model.push(ModelRole.GATE, GateDecision(intent=Intent.QUESTION, act=True, reason="asks"))
    answer = SendSms(contact_id=DRIVER, text="The PO number is 55821.", purpose=Purpose.REPLY)
    model.push(ModelRole.AGENT, Plan(rationale="driver_questions", actions=(answer,)))
    model.push(ModelRole.AGENT, _eta_request("Checking again: what is your ETA to the pickup?"))
    escalate = (
        SendEmail(
            to_contact_ids=(DISPATCHER,),
            subject="Load 481207: driver ETA needed",
            body="The driver has not answered two ETA requests. Please send the ETA.",
            purpose=Purpose.ESCALATION,
        ),
        CreateHeroTask(title="Call the driver", description="No ETA after two SMS."),
        CloseObligation(kind="eta_request", status=CloseStatus.EXHAUSTED),
    )
    model.push(ModelRole.AGENT, Plan(rationale="escalation", actions=escalate))
    return model


async def _play(uow_factory: UnitOfWorkFactory | None = None) -> Played:
    return await play_scenario(
        SCENARIO,
        evals_dir=EVALS,
        knowledge=MemorySopIndex.from_files(ROOT / "sops"),
        prompts=PROMPTS,
        model=scenario_a_agent(),
        uow_factory=uow_factory,
    )


def _state(observation: StepObservation) -> dict[str, Any]:
    """What the step left: the ledger, the timers, the outbox and each run's outcome."""
    snapshot = observation.snapshot
    return {
        "obligations": snapshot.obligations,
        "timers": snapshot.timers,
        "outbox": snapshot.outbox,
        "runs": [(r.run_id, r.event_id, r.status, r.sop_section_ids) for r in snapshot.runs],
        "step_runs": [(r.run_id, r.status) for r in observation.runs],
        "step_outbox": [i.outbox_id for i in observation.outbox],
    }


@pytest.fixture
async def store() -> AsyncIterator[PostgresStore]:
    async with (
        scratch_schema(DATABASE_URL, prefix="scenario") as schema,
        open_store(DATABASE_URL, schema=schema) as opened,
    ):
        yield opened


async def test_scenario_a_leaves_the_same_state_on_postgres_as_in_memory(
    store: PostgresStore,
) -> None:
    memory = await _play()
    postgres = await _play(store.unit_of_work)

    for played in (memory, postgres):
        assert played.error is None
        assert grade(SCENARIO, played.observations) == []
    assert [_state(o) for o in postgres.observations] == [_state(o) for o in memory.observations]

    # It really ran on Postgres, and the final state is what the assignment expects.
    async with store.unit_of_work() as uow:
        (obligation,) = await uow.obligations.list_for_load(LOAD_ID)
        runs = await uow.runs.list_for_load(LOAD_ID)
        outbox = await uow.outbox.list_for_load(LOAD_ID)
        assert await uow.timers.next_due_at() is None
        assert await uow.queue.size() == 0
    assert obligation.status is ObligationStatus.EXHAUSTED
    assert all(run.status is not RunStatus.FAILED for run in runs)
    assert len(runs) == len(memory.observations[-1].snapshot.runs)
    assert [i.kind for i in outbox] == [i.kind for i in memory.observations[-1].snapshot.outbox]
