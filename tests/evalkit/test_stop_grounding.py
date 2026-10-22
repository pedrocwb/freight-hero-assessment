"""Negative controls for the actual stop-grounding eval specs.

The original Slack regression must fail even with the right load number, recipient
and escalation channel. Generic messages and a route listing both stops fail too.
"""

from pathlib import Path

import pytest

from freight_agent.application.contracts import LoadSnapshot
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.evalkit.graders import grade_step
from freight_agent.evalkit.results import StepObservation
from freight_agent.evalkit.runner import read_load, scenario_start
from freight_agent.evalkit.spec import Expectation, Scenario, Step
from freight_agent.evalkit.suite import discover

EVALS = Path(__file__).resolve().parents[2] / "evals"
SCENARIOS = discover(EVALS, "grounding")
CASES = [
    (scenario, index, step)
    for scenario in SCENARIOS
    for index, step in enumerate(scenario.steps)
    if step.expect.actions and step.expect.actions[0].regex is not None
]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("scenario", "index", "step"),
    CASES,
    ids=[f"{s.id}:{i}" for s, i, _ in CASES],
)
def test_eval_accepts_the_right_stop_and_rejects_wrong_missing_or_ambiguous_stop(
    scenario: Scenario, index: int, step: Step
) -> None:
    matcher = step.expect.actions[0]
    pickup = "Riverbend" in (matcher.regex or "")
    correct = "Riverbend plant, Stockton" if pickup else "Valley DC, Reno"
    wrong = "Valley DC, Reno" if pickup else "Riverbend plant, Stockton"
    # Test the actual YAML's text/recipient/purpose contract separately from its
    # ledger, timers and LLM judge, which are checked by the live scenario player.
    contract = step.model_copy(update={"expect": Expectation(actions=(matcher,))})
    load = read_load(EVALS, scenario)
    now = scenario_start(scenario)

    def failures(text: str) -> set[str]:
        item = OutboxItem(
            outbox_id="out_1",
            idempotency_key="event:0",
            run_id="run_1",
            load_id=load.load_id,
            kind=matcher.kind,
            payload={
                "contact_id": matcher.to,
                "purpose": matcher.purpose,
                "text": text,
            },
            created_at=now,
        )
        obs = StepObservation(
            step_index=index,
            at=now,
            outbox=(item,),
            runs=(),
            llm_calls=0,
            snapshot=LoadSnapshot(
                load=load, obligations=(), timers=(), events=(), outbox=(item,), runs=()
            ),
        )
        return {f.check for f in grade_step(contract, obs, timezone=scenario.timezone)}

    prefix = (
        "Load 481207: driver has not provided ETA for next stop"
        if (matcher.kind == OutboxKind.BROKER_SLACK)
        else "Load 481207 stop:"
    )
    assert not failures(f"{prefix} ({correct}).")
    # Reproduces the original bad Keystone alert when stage=to_pickup.
    bad = failures(f"{prefix} ({wrong}). Escalating.")
    assert "expect.actions[0]" in bad
    assert "forbid[0]" in bad
    assert "expect.actions[0]" in failures("Load 481207: ETA missing. Escalating.")
    assert "forbid[0]" in failures(f"{prefix} ({correct} or {wrong}).")
