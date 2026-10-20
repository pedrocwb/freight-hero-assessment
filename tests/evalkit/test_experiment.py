"""Evals as LangSmith experiments, against a fake client and a fake `aevaluate` (no
network, no live model).

The dataset sync runs on a fake `ExperimentClient` that records its writes. The target
plays real scenarios on the trial runtime with a `FakeModel`; the evaluators and the
summary evaluators read only the target's outputs, so they are fed outputs (and fake
run and example objects) directly. `run_experiment` runs with `aevaluate` replaced by a
fake that calls the target and the evaluators the way LangSmith does.
"""

import inspect
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import langsmith.evaluation
import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.domain.actions import AgentOutcome, Purpose, ScheduleTimer, SendSms
from freight_agent.evalkit.experiment import (
    CostCapReached,
    CostMeter,
    ExperimentRun,
    LangSmithUnavailable,
    ScenarioOutcome,
    ScenarioTarget,
    dataset_name,
    evaluators,
    example_inputs,
    example_outputs,
    failures,
    link_to_agent,
    outcomes,
    regressions,
    run_experiment,
    scenario_hash,
    summary_evaluators,
    sync_dataset,
)
from freight_agent.evalkit.graders import grade
from freight_agent.evalkit.judge import JudgeSpec, JudgeVerdict
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.results import Failure, StepObservation
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.evalkit.suite import discover
from freight_agent.ports.llm import (
    ChatReply,
    ChatRequest,
    LlmRequest,
    LlmResponse,
    ModelRole,
    SchemaT,
)
from tests.fakes import FakeModel, Round, act

ROOT = Path(__file__).resolve().parents[2]
EVALS = ROOT / "evals"
SOPS = MemorySopIndex.from_files(ROOT / "sops")
DRIVER = "drv_20931"

pytestmark = pytest.mark.unit

ACK = """\
id: {id}
title: {id} title
suite: core
tags: {tags}
load: fixtures/load_481207.json
date: 2026-10-13
start: "06:55"
steps:
  - at: "07:05"
    message: {{channel: sms, sender: drv_20931, body: "{body}"}}
    expect: {{no_actions: true, run_status: [noop], llm_calls: 1}}
  - at: "07:20"
    note: time passes
"""

PING = """\
id: {id}
title: {id} title
suite: core
tags: [regression]
load: fixtures/load_481207.json
date: 2026-10-13
start: "06:55"
steps:
  - at: "07:00"
    ping: {{delay_risk: medium}}
"""


class FakeClient:
    """Records the calls of the `ExperimentClient` slice."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.datasets: dict[str, SimpleNamespace] = {}
        self.examples: dict[str, SimpleNamespace] = {}
        self.writes: list[tuple[str, int]] = []
        self.tags: list[dict[str, Any]] = [
            {"key": "Agent", "id": "k1", "values": [{"value": "freight-agent", "id": "tv1"}]}
        ]
        self.taggings: list[dict[str, Any]] = []

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise RuntimeError(f"{name} is down")

    def has_dataset(self, *, dataset_name: str) -> bool:
        return dataset_name in self.datasets

    def read_dataset(self, *, dataset_name: str) -> SimpleNamespace:
        return self.datasets[dataset_name]

    def create_dataset(self, dataset_name: str, **kwargs: Any) -> SimpleNamespace:
        self._maybe_fail("create_dataset")
        ds = SimpleNamespace(id=uuid.uuid4(), name=dataset_name, **kwargs)
        self.datasets[dataset_name] = ds
        return ds

    def list_examples(self, *, dataset_id: Any) -> list[SimpleNamespace]:
        return [e for e in self.examples.values() if e.dataset_id == dataset_id]

    def create_examples(self, *, dataset_id: Any, examples: Sequence[dict[str, Any]]) -> None:
        self.writes.append(("create", len(examples)))
        for body in examples:
            ex = SimpleNamespace(id=uuid.uuid4(), dataset_id=dataset_id, **body)
            self.examples[str(ex.id)] = ex

    def update_examples(self, *, dataset_id: Any, updates: Sequence[dict[str, Any]]) -> None:
        self.writes.append(("update", len(updates)))
        for body in updates:
            ex = self.examples[str(body["id"])]
            ex.inputs, ex.outputs, ex.metadata = body["inputs"], body["outputs"], body["metadata"]

    def delete_examples(self, example_ids: Sequence[Any]) -> None:
        self.writes.append(("delete", len(example_ids)))
        for example_id in example_ids:
            del self.examples[str(example_id)]

    def request_with_retries(
        self, method: Any, pathname: str, *, request_kwargs: Any = None
    ) -> Any:
        self._maybe_fail("tags")
        kwargs = request_kwargs or {}
        if method == "GET" and pathname == "/workspaces/current/tags":
            return self.tags
        if method == "GET" and pathname == "/workspaces/current/tags/resource":
            rid = kwargs["params"]["resource_id"]
            tagged = {t["tag_value_id"] for t in self.taggings if t["resource_id"] == rid}
            return [
                {"key": k["key"], "values": [v for v in k["values"] if v["id"] in tagged]}
                for k in self.tags
                if any(v["id"] in tagged for v in k["values"])
            ]
        if method == "POST" and pathname == "/workspaces/current/taggings":
            self.taggings.append(kwargs["json"])
            return {}
        raise AssertionError(f"unexpected {method} {pathname}")


@pytest.fixture
def evals(tmp_path: Path) -> Path:
    return tmp_path / "evals"


def write(evals: Path, scenario_id: str, *, tags: str = "[regression]", body: str = "ok") -> None:
    folder = evals / "scenarios" / "core"
    folder.mkdir(parents=True, exist_ok=True)
    text = ACK.format(id=scenario_id, tags=tags, body=body)
    (folder / f"{scenario_id}.yaml").write_text(text, encoding="utf-8")


def scenarios_of(evals: Path) -> list[Scenario]:
    return discover(evals, "core")


# --- the dataset ------------------------------------------------------------------------


def test_the_first_sync_creates_the_dataset_with_one_example_per_scenario(evals: Path) -> None:
    write(evals, "a_ack")
    write(evals, "b_ack")
    client = FakeClient()

    dataset, ids = sync_dataset(client, "core", scenarios_of(evals))

    assert dataset.name == dataset_name("core") == "freight-agent-core"
    assert dataset.metadata == {"suite": "core", "source": "evals/scenarios/core"}
    assert set(ids) == {"a_ack", "b_ack"}
    assert client.writes == [("create", 2)]
    example = client.examples[str(ids["a_ack"])]
    assert example.metadata == {
        "scenario_id": "a_ack",
        "suite": "core",
        "tags": ["regression"],
        "sha256": scenario_hash(scenarios_of(evals)[0]),
    }
    assert example.inputs["steps"][0]["message"]["body"] == "ok"
    assert example.outputs["steps"][0]["expect"]["no_actions"] is True


def test_a_second_sync_writes_only_what_changed(evals: Path) -> None:
    write(evals, "a_ack")
    write(evals, "b_ack")
    write(evals, "c_ack")
    client = FakeClient()
    _, first = sync_dataset(client, "core", scenarios_of(evals))

    _, same = sync_dataset(client, "core", scenarios_of(evals))
    assert same == first
    assert client.writes == [("create", 3)]

    write(evals, "a_ack", body="okay")  # edited
    (evals / "scenarios" / "core" / "c_ack.yaml").unlink()  # removed
    write(evals, "d_ack")  # added
    _, after = sync_dataset(client, "core", scenarios_of(evals))

    assert client.writes[1:] == [("create", 1), ("update", 1), ("delete", 1)]
    assert set(after) == {"a_ack", "b_ack", "d_ack"}
    assert after["a_ack"] == first["a_ack"]  # updated in place, same example
    assert client.examples[str(after["a_ack"])].inputs["steps"][0]["message"]["body"] == "okay"


def test_a_changed_tag_changes_the_hash(evals: Path) -> None:
    write(evals, "a_ack")
    before = scenario_hash(scenarios_of(evals)[0])
    write(evals, "a_ack", tags="[capability]")

    assert scenario_hash(scenarios_of(evals)[0]) != before


def test_inputs_carry_what_the_agent_is_given_and_outputs_what_is_graded(evals: Path) -> None:
    write(evals, "a_ack")
    scenario = scenarios_of(evals)[0]

    inputs, outputs = example_inputs(scenario), example_outputs(scenario)

    assert inputs["scenario_id"] == "a_ack"
    assert inputs["date"] == "2026-10-13" and inputs["start"] == "06:55"
    assert [s["at"] for s in inputs["steps"]] == ["07:05", "07:20"]
    assert inputs["steps"][1] == {"at": "07:20", "note": "time passes"}
    assert outputs["steps"][1] == {"at": "07:20", "expect": {}, "forbid": []}


def test_a_ping_step_is_part_of_the_dataset(evals: Path) -> None:
    folder = evals / "scenarios" / "core"
    folder.mkdir(parents=True)
    (folder / "p_ping.yaml").write_text(PING.format(id="p_ping"), encoding="utf-8")
    client = FakeClient()

    sync_dataset(client, "core", scenarios_of(evals))

    (example,) = client.examples.values()
    assert example.inputs["steps"][0]["ping"] == {
        "delay_risk": "medium",
        "lat": 37.9577,
        "lon": -121.2908,
    }


def test_examples_that_are_not_ours_or_duplicates_are_removed(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient()
    dataset, ids = sync_dataset(client, "core", scenarios_of(evals))
    original = client.examples[str(ids["a_ack"])]
    client.create_examples(
        dataset_id=dataset.id, examples=[{"inputs": {}, "outputs": {}, "metadata": {}}]
    )
    client.create_examples(
        dataset_id=dataset.id,
        examples=[{"inputs": {}, "outputs": {}, "metadata": dict(original.metadata)}],
    )

    _, again = sync_dataset(client, "core", scenarios_of(evals))

    assert client.writes[-1] == ("delete", 2)
    assert len(client.examples) == 1 and set(again) == {"a_ack"}


def test_a_sync_that_loses_examples_is_an_error(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient()
    client.create_examples = lambda **kwargs: None  # type: ignore[method-assign]  # accepts, stores nothing

    with pytest.raises(
        LangSmithUnavailable, match="examples missing after the dataset sync: a_ack"
    ):
        sync_dataset(client, "core", scenarios_of(evals))


# --- the agent page ---------------------------------------------------------------------


def test_the_dataset_is_tagged_with_its_agent_once(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient()
    dataset, _ = sync_dataset(client, "core", scenarios_of(evals))

    first = link_to_agent(client, dataset.id, "freight-agent")
    second = link_to_agent(client, dataset.id, "freight-agent")

    assert first and second
    assert client.taggings == [
        {"tag_value_id": "tv1", "resource_type": "dataset", "resource_id": str(dataset.id)}
    ]


@pytest.mark.parametrize(("agent", "fail"), [("another-agent", set()), ("freight-agent", {"tags"})])
def test_no_agent_tag_or_a_tags_error_leaves_the_dataset_unlinked(
    evals: Path, agent: str, fail: set[str]
) -> None:
    write(evals, "a_ack")
    client = FakeClient()
    dataset, _ = sync_dataset(client, "core", scenarios_of(evals))
    client.fail = fail

    assert link_to_agent(client, dataset.id, agent) is False
    assert client.taggings == []


# --- the target -------------------------------------------------------------------------

A = load_scenario(EVALS / "scenarios" / "core" / "a_client_a_high_risk.yaml")
# Scenario A's first step: the risk goes high, one SMS asks the driver for the ETA.
STEP_0 = A.model_copy(update={"steps": A.steps[:1]})
OUTPUT_KEYS = {
    "scenario_id",
    "error",
    "cost_usd",
    "judge_cost_usd",
    "model_calls",
    "rounds",
    "steps",
}


def right_agent(*, staging_cost: float = 0.01, answer_cost: float = 0.02) -> FakeModel:
    """Step 0's right agent: a round that stages the SMS and the timer, then the answer."""
    model = FakeModel()
    sms = SendSms(contact_id=DRIVER, text="Hi, what is your ETA?", purpose=Purpose.ETA_REQUEST)
    timer = ScheduleTimer(kind="eta_request", due_in_minutes=30)
    model.push(
        ModelRole.AGENT,
        Round(calls=(act(sms), act(timer)), cost_usd=staging_cost),
        Round(answer=AgentOutcome(rationale="delay_risk_high, step 1"), cost_usd=answer_cost),
    )
    return model


def idle_agent() -> FakeModel:
    """A wrong agent: it answers at once and does nothing."""
    model = FakeModel()
    model.push(ModelRole.AGENT, Round(answer=AgentOutcome(rationale="nothing to do")))
    return model


class PricedJudge:
    """The fake model with a price on the judge's verdicts (the fake's answers are free)."""

    def __init__(self, inner: FakeModel, cost_usd: float) -> None:
        self.inner = inner
        self.cost_usd = cost_usd

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        response = await self.inner.generate(request, schema)
        if request.role is not ModelRole.JUDGE:
            return response
        usage = response.usage.model_copy(update={"cost_usd": self.cost_usd})
        return LlmResponse(parsed=response.parsed, usage=usage)

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        return await self.inner.chat(request, schema)


def with_rubric(scenario: Scenario, rubric: str = "The SMS is polite.") -> Scenario:
    step = scenario.steps[0]
    expect = step.expect.model_copy(update={"judge": JudgeSpec(rubric=rubric)})
    return scenario.model_copy(update={"steps": (step.model_copy(update={"expect": expect}),)})


def target_of(
    scenarios: Sequence[Scenario],
    factory: Callable[[], Any] = right_agent,
    *,
    cap_usd: float = 1.0,
) -> ScenarioTarget:
    return ScenarioTarget(
        {s.id: s for s in scenarios},
        evals_dir=EVALS,
        knowledge=SOPS,
        model_factory=factory,
        meter=CostMeter(cap_usd=cap_usd),
    )


@dataclass
class Builds:
    """A model factory that keeps every model it built."""

    make: Callable[[], FakeModel] = right_agent
    built: list[FakeModel] = field(default_factory=list[FakeModel])

    def __call__(self) -> FakeModel:
        self.built.append(self.make())
        return self.built[-1]


async def outputs_of(scenario: Scenario, model: Any) -> dict[str, Any]:
    return await target_of([scenario], lambda: model)({"scenario_id": scenario.id})


async def test_the_target_plays_the_scenario_and_returns_compact_observations() -> None:
    target = target_of([STEP_0])

    outputs = await target({"scenario_id": STEP_0.id})

    assert set(outputs) == OUTPUT_KEYS
    assert (outputs["scenario_id"], outputs["error"]) == (STEP_0.id, None)
    assert (outputs["model_calls"], outputs["rounds"]) == (1, 2)  # one loop, two rounds
    assert outputs["cost_usd"] == pytest.approx(0.03)
    assert outputs["judge_cost_usd"] == 0.0
    (step,) = outputs["steps"]
    assert [i["kind"] for i in step["outbox"]] == ["sms"] and len(step["runs"]) == 1
    snapshot = step["snapshot"]
    assert (snapshot["events"], snapshot["outbox"], snapshot["runs"]) == ([], [], [])
    assert len(snapshot["timers"]) == 1 and len(snapshot["obligations"]) == 1
    assert target.meter.spent_usd == pytest.approx(0.03)


async def test_every_example_gets_a_model_of_its_own() -> None:
    builds = Builds()
    target = target_of([STEP_0], builds)

    await target({"scenario_id": STEP_0.id})
    await target({"scenario_id": STEP_0.id})

    assert len(builds.built) == 2 and all(m.remaining() == 0 for m in builds.built)
    assert target.meter.spent_usd == pytest.approx(0.06)


async def test_the_judge_runs_in_the_target_and_its_cost_is_counted() -> None:
    model = right_agent()
    model.push(ModelRole.JUDGE, JudgeVerdict(reasons=("polite",), score=0.9))
    judged = with_rubric(STEP_0)
    target = target_of([judged], lambda: PricedJudge(model, 0.04))

    outputs = await target({"scenario_id": judged.id})

    assert outputs["judge_cost_usd"] == pytest.approx(0.04)
    assert outputs["cost_usd"] == pytest.approx(0.07)
    assert outputs["model_calls"] == 1  # the judge is not one of the agent's calls
    assert outputs["steps"][0]["judge"]["verdict"]["score"] == 0.9
    assert target.meter.spent_usd == pytest.approx(0.07)


async def test_a_play_that_cannot_start_returns_its_error() -> None:
    broken = STEP_0.model_copy(update={"load": "fixtures/missing.json"})

    outputs = await target_of([broken])({"scenario_id": broken.id})

    assert outputs["error"] == "load fixture not found: fixtures/missing.json"
    assert (outputs["steps"], outputs["cost_usd"], outputs["model_calls"]) == ([], 0.0, 0)


async def test_over_the_cap_the_target_refuses_without_calling_the_model() -> None:
    builds = Builds()
    target = target_of([STEP_0], builds, cap_usd=0.02)

    await target({"scenario_id": STEP_0.id})  # spends 0.03, over the cap of 0.02
    with pytest.raises(CostCapReached, match=r"spent \$0\.0300, over its cap of \$0\.02"):
        await target({"scenario_id": STEP_0.id})

    assert len(builds.built) == 1
    assert target.meter.skipped == [STEP_0.id]
    assert target.meter.spent_usd == pytest.approx(0.03)


def test_the_meter_is_reached_only_past_its_cap() -> None:
    assert not CostMeter(cap_usd=0.5, spent_usd=0.5).reached
    assert CostMeter(cap_usd=0.5, spent_usd=0.51).reached
    assert CostMeter(cap_usd=0.0, spent_usd=0.01).reached


# --- evaluators -------------------------------------------------------------------------


def observations_of(outputs: dict[str, Any]) -> list[StepObservation]:
    return [StepObservation.model_validate(o) for o in outputs["steps"]]


async def test_failures_from_the_outputs_are_the_graders_verdict_on_the_observations() -> None:
    good = await outputs_of(STEP_0, right_agent())
    bad = await outputs_of(STEP_0, idle_agent())

    assert failures(STEP_0, good) == grade(STEP_0, observations_of(good)) == []
    found = failures(STEP_0, bad)
    assert found == grade(STEP_0, observations_of(bad))
    assert "expect.actions[0]" in {f.check for f in found}
    assert failures(STEP_0, bad, ("trajectory",)) == []  # step 0 declares no trajectory


@pytest.mark.parametrize("outputs", [None, {}])
def test_no_outputs_means_the_example_did_not_run(outputs: dict[str, Any] | None) -> None:
    assert failures(STEP_0, outputs) == [
        Failure(step_index=None, check="error", message="the example did not run")
    ]


def test_a_play_error_in_the_outputs_is_a_failure_of_its_own() -> None:
    outputs = {"scenario_id": STEP_0.id, "error": "step 0: RuntimeError: boom", "steps": []}

    assert failures(STEP_0, outputs) == [
        Failure(step_index=None, check="error", message="step 0: RuntimeError: boom")
    ]


def evaluate_all(scenario: Scenario, outputs: dict[str, Any] | None) -> dict[str, Any]:
    """Every evaluator's result by its name, called with the parameters it names."""
    found: dict[str, Any] = {}
    for evaluate in evaluators({scenario.id: scenario}):
        given = {"inputs": {"scenario_id": scenario.id}, "outputs": outputs}
        wanted = inspect.signature(evaluate).parameters
        found[evaluate.__name__] = evaluate(**{k: v for k, v in given.items() if k in wanted})
    return found


def test_there_is_one_evaluator_per_group_plus_passed_and_cost() -> None:
    assert [e.__name__ for e in evaluators({})] == [
        "facts",
        "trajectory",
        "judge",
        "invariants",
        "passed",
        "cost_usd",
    ]


async def test_a_passing_play_scores_1_on_every_evaluator() -> None:
    results = evaluate_all(STEP_0, await outputs_of(STEP_0, right_agent()))

    for key in ("facts", "trajectory", "invariants", "passed"):
        assert results[key] == {"key": key, "score": 1.0, "comment": None}
    assert results["judge"] == {"results": []}  # no rubric: no score rather than a free 1
    assert results["cost_usd"] == {"key": "cost_usd", "score": pytest.approx(0.03)}


async def test_a_failing_play_scores_0_with_the_failures_in_the_comment() -> None:
    results = evaluate_all(STEP_0, await outputs_of(STEP_0, idle_agent()))

    assert results["facts"]["score"] == results["passed"]["score"] == 0.0
    assert results["facts"]["comment"].startswith("step 0: expect.actions[0]: expected exactly 1")
    assert results["passed"]["comment"] == results["facts"]["comment"]
    assert results["trajectory"]["score"] == results["invariants"]["score"] == 1.0


async def test_the_judge_evaluator_scores_a_rubric_step() -> None:
    judged = with_rubric(STEP_0)
    nice, rude = right_agent(), right_agent()
    nice.push(ModelRole.JUDGE, JudgeVerdict(reasons=("polite",), score=0.9))
    rude.push(ModelRole.JUDGE, JudgeVerdict(reasons=("rude",), score=0.1))

    good = evaluate_all(judged, await outputs_of(judged, nice))
    bad = evaluate_all(judged, await outputs_of(judged, rude))

    assert good["judge"] == {"key": "judge", "score": 1.0, "comment": None}
    assert bad["judge"]["score"] == 0.0
    assert bad["judge"]["comment"] == (
        "step 0: expect.judge: the judge scored 0.10, below the minimum of 0.70: rude"
    )
    assert bad["facts"]["score"] == 1.0


def test_an_example_that_did_not_run_fails_every_group_but_costs_nothing() -> None:
    results = evaluate_all(with_rubric(STEP_0), None)

    for key in ("facts", "trajectory", "judge", "invariants", "passed"):
        assert (results[key]["score"], results[key]["comment"]) == (
            0.0,
            "error: the example did not run",
        )
    assert results["cost_usd"]["score"] == 0.0


# --- pass^k, pass@k and the outcomes ----------------------------------------------------

CAP_ERROR = "CostCapReached('not played: the experiment spent $1.0000, over its cap of $0.50')"


@dataclass
class Repetitions:
    """Fake run and example objects, as LangSmith hands them to summary evaluators."""

    runs: list[SimpleNamespace] = field(default_factory=list[SimpleNamespace])
    examples: list[SimpleNamespace] = field(default_factory=list[SimpleNamespace])

    def add(
        self, scenario: Scenario, outputs: dict[str, Any] | None, error: str | None = None
    ) -> "Repetitions":
        self.runs.append(SimpleNamespace(outputs=outputs, error=error))
        self.examples.append(SimpleNamespace(inputs={"scenario_id": scenario.id}))
        return self

    def rows(self) -> list[tuple[dict[str, Any], dict[str, Any] | None, str | None]]:
        return [
            (e.inputs, r.outputs, r.error) for r, e in zip(self.runs, self.examples, strict=True)
        ]


def tagged(scenario_id: str, *tags: str) -> Scenario:
    return STEP_0.model_copy(update={"id": scenario_id, "tags": tags})


REGRESSION = tagged("r_one", "regression", "sop:x")
CAPABILITY = tagged("c_one", "capability", "sop:x")
SCENARIOS = {s.id: s for s in (REGRESSION, CAPABILITY)}


@pytest.fixture
async def plays() -> tuple[dict[str, Any], dict[str, Any]]:
    """A passing and a failing play of step 0."""
    good = await outputs_of(STEP_0, right_agent())
    bad = await outputs_of(STEP_0, idle_agent())
    return good, bad


async def test_pass_hat_k_needs_every_repetition_and_pass_at_k_one(
    plays: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    good, bad = plays
    reps = Repetitions()
    reps.add(REGRESSION, good).add(REGRESSION, good).add(CAPABILITY, good).add(CAPABILITY, bad)
    pass_k, total_cost = summary_evaluators(SCENARIOS)

    scores = {r["key"]: r["score"] for r in pass_k(reps.runs, reps.examples)["results"]}

    assert scores == {
        "pass_hat_k": 0.5,
        "pass_at_k": 1.0,
        "pass_hat_k:capability": 0.0,
        "pass_hat_k:regression": 1.0,
        "pass_hat_k:sop:x": 0.5,
    }
    assert total_cost(reps.runs) == {"key": "total_cost_usd", "score": pytest.approx(0.09)}


async def test_a_scenario_failing_every_repetition_fails_pass_at_k_too(
    plays: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    _, bad = plays
    reps = Repetitions().add(REGRESSION, bad).add(REGRESSION, None, "RuntimeError('x')")
    pass_k, total_cost = summary_evaluators(SCENARIOS)

    scores = {r["key"]: r["score"] for r in pass_k(reps.runs, reps.examples)["results"]}

    assert (scores["pass_hat_k"], scores["pass_at_k"]) == (0.0, 0.0)
    assert total_cost(reps.runs)["score"] == 0.0  # the idle agent spent nothing


def test_no_runs_score_0() -> None:
    pass_k, _ = summary_evaluators(SCENARIOS)

    assert pass_k([], [])["results"] == [
        {"key": "pass_hat_k", "score": 0.0},
        {"key": "pass_at_k", "score": 0.0},
    ]


async def test_outcomes_group_the_repetitions_and_a_cost_cap_row_is_not_run(
    plays: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    good, bad = plays
    reps = Repetitions()
    reps.add(CAPABILITY, bad).add(REGRESSION, good).add(REGRESSION, None, CAP_ERROR)
    reps.add(CAPABILITY, good)

    found = outcomes(SCENARIOS, reps.rows())

    capability, regression = found  # first-seen order
    assert capability.scenario is CAPABILITY
    assert (capability.passed, capability.not_run) == ((False, True), 0)
    assert capability.failures[1] == ()
    assert (regression.passed, regression.not_run) == ((True,), 1)
    assert regression.cost_usd == pytest.approx(0.03)
    assert (regression.pass_hat_k, regression.pass_at_k) == (False, True)


def test_other_errors_are_failed_repetitions() -> None:
    (found,) = outcomes(SCENARIOS, [({"scenario_id": "r_one"}, None, "TimeoutError()")])

    assert (found.passed, found.not_run) == ((False,), 0)
    assert found.failures[0][0].message == "the example did not run"


def outcome(scenario: Scenario, *passed: bool, not_run: int = 0) -> ScenarioOutcome:
    return ScenarioOutcome(
        scenario=scenario,
        passed=passed,
        failures=tuple(() for _ in passed),
        not_run=not_run,
    )


def test_regressions_are_the_gating_scenarios_that_ran_and_failed_pass_hat_k() -> None:
    other = tagged("r_two", "sop:x")  # no tag at all gates too
    found = [
        outcome(REGRESSION, True, False),
        outcome(CAPABILITY, False, False),
        outcome(other, True, False),
        outcome(tagged("r_three", "regression"), True, True),
        outcome(tagged("r_four", "regression"), not_run=2),  # never ran: the cost cap's
        outcome(tagged("r_five", "regression"), False),
    ]

    assert [o.scenario.id for o in regressions(found)] == ["r_one", "r_two", "r_five"]


# --- run_experiment ---------------------------------------------------------------------


class FakeResults:
    """What `aevaluate` returns: rows to iterate, the experiment's name and links."""

    def __init__(self, rows: list[dict[str, Any]], *, comparison: str | None) -> None:
        self.rows = rows
        self.experiment_name = "core-k2-1234"
        self.url = "https://ls/projects/p/e1"
        self._comparison = comparison

    async def get_comparison_url(self) -> str | None:
        if self._comparison == "boom":
            raise RuntimeError("no link")
        return self._comparison

    async def _iterate(self) -> AsyncIterator[dict[str, Any]]:
        for row in self.rows:
            yield row

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        return self._iterate()


def call_by_name(function: Callable[..., Any], **available: Any) -> Any:
    """Call an evaluator with the arguments it names, as LangSmith does."""
    wanted = inspect.signature(function).parameters
    return function(**{k: v for k, v in available.items() if k in wanted})


class FakeEvaluate:
    """Stands in for `langsmith.evaluation.aevaluate`: plays every example `num_repetitions`
    times through the target, runs the evaluators on each row and the summary evaluators
    at the end. A target error becomes the run's error, as LangSmith records it."""

    def __init__(self, *, comparison: str | None = "https://ls/datasets/d1/compare") -> None:
        self.comparison = comparison
        self.target: Callable[..., Any] | None = None
        self.kwargs: dict[str, Any] = {}
        self.rows: list[dict[str, Any]] = []
        self.summary: list[Any] = []

    async def __call__(self, target: Callable[..., Any], **kwargs: Any) -> FakeResults:
        self.target, self.kwargs = target, kwargs
        rows: list[dict[str, Any]] = []
        for example in kwargs["data"]:
            for _ in range(kwargs["num_repetitions"]):
                try:
                    outputs, error = await target(example.inputs), None
                except Exception as exc:
                    outputs, error = None, repr(exc)
                run = SimpleNamespace(outputs=outputs, error=error)
                results = [
                    call_by_name(
                        e, run=run, example=example, inputs=example.inputs, outputs=outputs
                    )
                    for e in kwargs["evaluators"]
                ]
                rows.append({"run": run, "example": example, "evaluation_results": results})
        self.rows = rows
        runs, examples = [r["run"] for r in rows], [r["example"] for r in rows]
        self.summary = [
            call_by_name(s, runs=runs, examples=examples) for s in kwargs["summary_evaluators"]
        ]
        return FakeResults(rows, comparison=self.comparison)


@pytest.fixture
def fake_evaluate(monkeypatch: pytest.MonkeyPatch) -> FakeEvaluate:
    fake = FakeEvaluate()
    monkeypatch.setattr(langsmith.evaluation, "aevaluate", fake)
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):  # the target's span stays local
        monkeypatch.setenv(name, "false")
    return fake


def metadata() -> RunMetadata:
    return RunMetadata(
        created_at=datetime(2026, 10, 20, 1, 30, tzinfo=UTC),
        suite="core",
        k=2,
        cost_cap_usd=1.0,
        git_sha="abc1234",
        models={"gate": ["openai/gpt-4.1-nano"], "agent": ["openai/gpt-oss-120b", "x/y"]},
    )


async def experiment(
    client: FakeClient,
    target: ScenarioTarget,
    *,
    chosen: Sequence[Scenario] = (REGRESSION,),
    k: int = 2,
    agent: str | None = None,
) -> ExperimentRun:
    return await run_experiment(
        client,
        suite="core",
        suite_scenarios=[REGRESSION, CAPABILITY],
        chosen=chosen,
        target=target,
        metadata=metadata(),
        k=k,
        agent=agent,
        concurrency=3,
    )


async def test_run_experiment_syncs_the_suite_and_evaluates_the_chosen_examples(
    fake_evaluate: FakeEvaluate,
) -> None:
    client = FakeClient()
    target = target_of([REGRESSION, CAPABILITY])

    run = await experiment(client, target)

    assert len(client.examples) == 2  # the whole suite is synced
    # aevaluate takes a coroutine function, not the callable target itself
    assert inspect.iscoroutinefunction(fake_evaluate.target)
    kwargs = fake_evaluate.kwargs
    assert [e.metadata["scenario_id"] for e in kwargs["data"]] == ["r_one"]
    assert kwargs["num_repetitions"] == 2
    assert kwargs["metadata"] == metadata().for_langsmith()
    assert kwargs["metadata"]["model_agent"] == "openai/gpt-oss-120b, x/y"
    assert kwargs["experiment_prefix"] == "core-k2"
    assert kwargs["description"] == "agent eval run --suite core --k 2"
    assert (kwargs["max_concurrency"], kwargs["client"]) == (3, client)
    assert [e.__name__ for e in kwargs["evaluators"]][-2:] == ["passed", "cost_usd"]
    assert run.name == "core-k2-1234"
    assert run.url == "https://ls/datasets/d1/compare"
    (found,) = run.outcomes
    assert (found.scenario.id, found.passed, found.pass_hat_k) == ("r_one", (True, True), True)
    assert run.cost_usd == pytest.approx(0.06) == target.meter.spent_usd
    assert run.not_run == ()
    pass_k = {r["key"]: r["score"] for r in fake_evaluate.summary[0]["results"]}
    assert pass_k["pass_hat_k"] == 1.0


async def test_the_cost_cap_leaves_the_remaining_repetitions_not_run(
    fake_evaluate: FakeEvaluate,
) -> None:
    target = target_of([REGRESSION, CAPABILITY], cap_usd=0.01)

    run = await experiment(FakeClient(), target, chosen=(REGRESSION, CAPABILITY), k=2)

    assert run.not_run == ("r_one", "c_one")  # every example after the first play
    regression, capability = run.outcomes
    assert (regression.passed, regression.not_run) == ((True,), 1)
    assert (capability.passed, capability.not_run) == ((), 2)
    assert run.cost_usd == pytest.approx(0.03)
    errors = [r["run"].error for r in fake_evaluate.rows]
    assert errors[0] is None
    assert all(e is not None and e.startswith("CostCapReached(") for e in errors[1:])


async def test_a_failing_repetition_shows_in_the_outcomes(fake_evaluate: FakeEvaluate) -> None:
    models = iter([right_agent(), idle_agent()])
    target = target_of([REGRESSION, CAPABILITY], lambda: next(models))

    run = await experiment(FakeClient(), target)

    (found,) = run.outcomes
    assert found.passed == (True, False)
    assert regressions(run.outcomes) == [found]
    assert found.failures[1][0].check == "expect.actions[0]"


@pytest.mark.parametrize(
    ("comparison", "url"), [(None, "https://ls/projects/p/e1"), ("boom", None)]
)
async def test_without_a_comparison_link_the_experiment_link_or_none(
    fake_evaluate: FakeEvaluate, comparison: str | None, url: str | None
) -> None:
    fake_evaluate.comparison = comparison

    run = await experiment(FakeClient(), target_of([REGRESSION]))

    assert run.url == url


async def test_the_dataset_is_linked_to_the_agent_when_one_is_named(
    fake_evaluate: FakeEvaluate,
) -> None:
    client = FakeClient()

    await experiment(client, target_of([REGRESSION]), agent="freight-agent")

    assert [t["resource_type"] for t in client.taggings] == ["dataset"]


async def test_a_dataset_that_cannot_be_synced_stops_the_run_before_anything_plays(
    fake_evaluate: FakeEvaluate,
) -> None:
    builds = Builds()
    target = target_of([REGRESSION], builds)

    with pytest.raises(LangSmithUnavailable, match="RuntimeError: create_dataset is down"):
        await experiment(FakeClient(fail={"create_dataset"}), target)

    assert fake_evaluate.kwargs == {} and builds.built == []
