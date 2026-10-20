"""Live eval runs as LangSmith experiments, against a fake client (no network).

The fake records what the SDK would send: dataset writes, the experiment, every run
(`langsmith.run_helpers.trace` and LangChain's tracer both post through the client's
`create_run` / `update_run`) and the feedback. The suite runs here in scripted mode or
on a stand-in model; the CLI allows `--langsmith` with live and record modes only.
"""

import shutil
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import Usage
from freight_agent.evalkit.langsmith_experiment import (
    LangSmithExperiment,
    LangSmithUnavailable,
    dataset_name,
    example_inputs,
    example_outputs,
    feedback_id,
    sync_dataset,
)
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.spec import Scenario
from freight_agent.evalkit.suite import SuiteResult, SuiteRunner, discover
from freight_agent.ports.clock import Clock
from freight_agent.ports.llm import LanguageModel, LlmRequest, LlmResponse, SchemaT
from freight_agent.settings import LlmMode

ROOT = Path(__file__).resolve().parents[2]
SOPS = MemorySopIndex.from_files(ROOT / "sops")

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
    script:
      gate: [{{intent: acknowledgement, act: false, reason: only ok}}]
  - at: "07:20"
    note: time passes
"""


class FakeClient:
    """Records the calls of the `ExperimentClient` slice."""

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.datasets: dict[str, SimpleNamespace] = {}
        self.examples: dict[str, SimpleNamespace] = {}
        self.writes: list[tuple[str, int]] = []
        self.projects: list[dict[str, Any]] = []
        self.project_updates: list[dict[str, Any]] = []
        self.runs: dict[str, dict[str, Any]] = {}
        self.feedback: list[dict[str, Any]] = []
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
        ds = SimpleNamespace(
            id=uuid.uuid4(), name=dataset_name, url="https://ls/datasets/d1", **kwargs
        )
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

    def create_project(self, project_name: str, **kwargs: Any) -> SimpleNamespace:
        self._maybe_fail("create_project")
        project = {"id": uuid.uuid4(), "name": project_name, **kwargs}
        self.projects.append(project)
        return SimpleNamespace(**project)

    def update_project(self, project_id: Any, **kwargs: Any) -> None:
        self.project_updates.append({"id": project_id, **kwargs})

    def create_feedback(self, run_id: Any = None, key: str = "unnamed", **kwargs: Any) -> None:
        self._maybe_fail("create_feedback")
        self.feedback.append({"run_id": run_id, "key": key, **kwargs})

    def create_run(self, name: str, inputs: dict[str, Any], run_type: Any, **kwargs: Any) -> None:
        self.runs[str(kwargs["id"])] = {
            "name": name,
            "inputs": inputs,
            "run_type": run_type,
            **kwargs,
        }

    def update_run(self, run_id: Any, **kwargs: Any) -> None:
        self.runs.setdefault(str(run_id), {}).update(
            {k: v for k, v in kwargs.items() if v is not None}
        )

    def flush(self) -> None:
        pass

    def get_run_url(self, **kwargs: Any) -> str:
        return "https://ls/r"

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

    # helpers for the assertions
    def runs_named(self, prefix: str) -> list[dict[str, Any]]:
        return [r for r in self.runs.values() if r.get("name", "").startswith(prefix)]

    def feedback_of(self, run_id: Any) -> dict[str, dict[str, Any]]:
        return {f["key"]: f for f in self.feedback if str(f["run_id"]) == str(run_id)}


@pytest.fixture
def evals(tmp_path: Path) -> Path:
    root = tmp_path / "evals"
    (root / "fixtures").mkdir(parents=True)
    shutil.copy(ROOT / "evals" / "fixtures" / "load_481207.json", root / "fixtures")
    return root


def write(evals: Path, scenario_id: str, *, tags: str = "[regression]", body: str = "ok") -> None:
    folder = evals / "scenarios" / "core"
    folder.mkdir(parents=True, exist_ok=True)
    text = ACK.format(id=scenario_id, tags=tags, body=body)
    (folder / f"{scenario_id}.yaml").write_text(text, encoding="utf-8")


def metadata(mode: LlmMode = "live") -> RunMetadata:
    return RunMetadata(
        created_at=datetime(2026, 10, 20, 1, 30, tzinfo=UTC),
        mode=mode,
        models={"gate": ["openai/gpt-4.1-nano"], "decide": ["openai/gpt-oss-120b"]},
        git_sha="abc1234",
    )


class ChattyModel:
    """A stand-in live model that also makes a LangChain call, like the OpenRouter adapter,
    so the test can see where LangChain's trace lands. `act` decides pass or fail."""

    def __init__(self, *, act: bool = False) -> None:
        self._act = act
        self._chat = GenericFakeChatModel(messages=iter([AIMessage(content="x")] * 100))

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        await self._chat.ainvoke("hi", config={"run_name": f"{request.role.value} · fake/model"})
        output = (
            GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=self._act, reason="ok")
            if schema is GateDecision
            else Plan()
        )
        usage = Usage(
            role=request.role.value,
            model="fake/model",
            input_tokens=10,
            output_tokens=2,
            cost_usd=0.001,
        )
        return LlmResponse(parsed=schema.model_validate(output.model_dump()), usage=usage)


def factory(*models: ChattyModel) -> Callable[[Clock], LanguageModel]:
    queue = list(models)

    def build(clock: Clock) -> LanguageModel:
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return build


async def run(
    evals: Path,
    client: FakeClient,
    *,
    k: int = 1,
    models: Callable[[Clock], LanguageModel] | None = None,
) -> tuple[LangSmithExperiment, SuiteResult]:
    scenarios = discover(evals, "core")
    mode: LlmMode = "live" if models is not None else "scripted"
    experiment = LangSmithExperiment(
        client,
        suite="core",
        dataset_scenarios=scenarios,
        metadata=metadata(mode),
        k=k,
    )
    experiment.start()
    runner = SuiteRunner(
        evals_dir=evals, knowledge=SOPS, mode=mode, model_factory=models, observer=experiment
    )
    result = await runner.run(scenarios, suite="core", k=k, metadata=metadata(mode))
    experiment.finish(result)
    return experiment, result


# --- the dataset ------------------------------------------------------------------------


def scenarios_of(evals: Path) -> list[Scenario]:
    return discover(evals, "core")


@pytest.mark.unit
def test_the_first_sync_creates_the_dataset_with_one_example_per_scenario(evals: Path) -> None:
    write(evals, "a_ack")
    write(evals, "b_ack")
    client = FakeClient()

    dataset, ids = sync_dataset(client, "core", scenarios_of(evals))

    assert dataset.name == dataset_name("core") == "freight-agent-core"
    assert set(ids) == {"a_ack", "b_ack"}
    assert client.writes == [("create", 2)]
    example = client.examples[str(ids["a_ack"])]
    assert example.metadata["scenario_id"] == "a_ack"
    assert example.inputs["steps"][0]["message"]["body"] == "ok"
    assert "script" not in example.inputs["steps"][0]  # live runs ignore the script
    assert example.outputs["steps"][0]["expect"]["no_actions"] is True


@pytest.mark.unit
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


@pytest.mark.unit
def test_inputs_carry_what_the_agent_is_given_and_outputs_what_is_graded(evals: Path) -> None:
    write(evals, "a_ack")
    scenario = scenarios_of(evals)[0]

    inputs, outputs = example_inputs(scenario), example_outputs(scenario)

    assert inputs["date"] == "2026-10-13" and inputs["start"] == "06:55"
    assert [s["at"] for s in inputs["steps"]] == ["07:05", "07:20"]
    assert inputs["steps"][1] == {"at": "07:20", "note": "time passes"}
    assert outputs["steps"][1] == {"at": "07:20", "expect": {}, "forbid": []}


# --- the experiment ---------------------------------------------------------------------


@pytest.mark.unit
async def test_the_experiment_is_linked_to_the_dataset_and_carries_the_run_metadata(
    evals: Path,
) -> None:
    write(evals, "a_ack")
    client = FakeClient()

    experiment, _ = await run(evals, client)

    (project,) = client.projects
    assert project["name"] == experiment.name == "core-scripted-default-20261020T013000Z"
    assert project["reference_dataset_id"] == client.datasets["freight-agent-core"].id
    assert project["metadata"]["git_sha"] == "abc1234"
    assert project["metadata"]["models"] == {
        "gate": "openai/gpt-4.1-nano",
        "decide": "openai/gpt-oss-120b",
    }
    assert project["metadata"]["k"] == 1
    assert experiment.url == f"https://ls/datasets/d1/compare?selectedSessions={project['id']}"


@pytest.mark.unit
async def test_each_trial_is_a_row_linked_to_its_example_with_a_child_run_per_step(
    evals: Path,
) -> None:
    write(evals, "a_ack")
    client = FakeClient()

    _, result = await run(evals, client, k=2)

    trials = client.runs_named("a_ack")
    assert len(trials) == 2
    example_id = next(iter(client.examples.values())).id
    for trial in trials:
        assert str(trial["reference_example_id"]) == str(example_id)
        assert trial["session_name"] == client.projects[0]["name"]
        assert trial["outputs"]["passed"] is True
    steps = client.runs_named("step ")
    assert len(steps) == 4  # two steps per trial
    assert {str(s["parent_run_id"]) for s in steps} == {str(t["id"]) for t in trials}
    assert {s["name"] for s in steps} == {
        "step 0 · 07:05 · sms from drv_20931",
        "step 1 · 07:20 · time passes",
    }
    first = next(s for s in steps if s["name"].startswith("step 0"))
    assert first["outputs"]["runs"][0]["status"] == "noop"
    assert result.scenarios[0].passed


@pytest.mark.unit
async def test_the_model_calls_of_a_step_nest_under_that_step(evals: Path) -> None:
    """LangGraph's graph is a LangChain runnable, so the run's nodes are traced too:
    step -> agent_run -> gate -> the model call."""
    write(evals, "a_ack")
    client = FakeClient()

    await run(evals, client, models=factory(ChattyModel()))

    (llm,) = [r for r in client.runs.values() if r.get("run_type") == "llm"]
    (step0,) = client.runs_named("step 0")
    assert llm["name"] == "gate · fake/model"
    ancestors = []
    parent = llm.get("parent_run_id")
    while parent is not None:
        node = client.runs[str(parent)]
        ancestors.append(node["name"])
        parent = node.get("parent_run_id")
    assert ancestors[:2] == ["gate", "agent_run"]
    assert step0["name"] in ancestors and ancestors[-1] == "a_ack"


@pytest.mark.unit
async def test_feedback_per_trial_and_pass_hat_k_once_the_scenario_is_done(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient()

    # trial 1 passes; in trial 2 the gate acts on an "ok", so decide is called too and the
    # step's `llm_calls: 1` fails
    await run(evals, client, k=2, models=factory(ChattyModel(), ChattyModel(act=True)))

    by_trial = {r["extra"]["metadata"]["trial"]: r for r in client.runs_named("a_ack")}
    good, bad = client.feedback_of(by_trial[1]["id"]), client.feedback_of(by_trial[2]["id"])
    assert good["passed"]["score"] == 1.0 and good["passed"]["comment"] is None
    assert bad["passed"]["score"] == 0.0
    assert "step 0: expect.llm_calls: expected 1 model call(s), got 2" in bad["passed"]["comment"]
    assert bad["checks_failed"]["score"] >= 1
    assert good["cost_usd"]["score"] == pytest.approx(0.001)
    assert good["model_calls"]["score"] == 1
    assert good["pass_hat_k"]["score"] == bad["pass_hat_k"]["score"] == 0.0
    assert good["pass_hat_k"]["comment"] == "1/2 trials passed"
    assert good["passed"]["session_id"] == client.projects[0]["id"]
    # one id per (run, key): a resent background batch cannot add a second `passed`
    ids = [f["feedback_id"] for f in client.feedback]
    assert len(ids) == len(set(ids))
    assert good["passed"]["feedback_id"] == feedback_id(by_trial[1]["id"], "passed")


@pytest.mark.unit
async def test_the_run_totals_land_on_the_experiment(evals: Path) -> None:
    write(evals, "a_ack", tags="[regression]")
    write(evals, "b_ack", tags="[capability]", body="okay")  # the gate script still fits: passes
    client = FakeClient()

    _, result = await run(evals, client)

    project_id = client.projects[0]["id"]
    totals = {f["key"]: f["score"] for f in client.feedback if f.get("project_id") == project_id}
    assert totals["pass_hat_k"] == 1.0
    assert totals["pass_hat_k_regression"] == totals["pass_hat_k_capability"] == 1.0
    assert totals["exit_code"] == float(int(result.exit_code)) == 0.0
    (update,) = client.project_updates
    assert update["metadata"]["result_pass_hat_k"] == 1.0
    assert update["end_time"] is not None


# --- failing open -----------------------------------------------------------------------


@pytest.mark.unit
def test_an_experiment_that_cannot_be_created_raises_before_anything_runs(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient(fail={"create_project"})
    experiment = LangSmithExperiment(
        client,
        suite="core",
        dataset_scenarios=scenarios_of(evals),
        metadata=metadata(),
        k=1,
    )

    with pytest.raises(LangSmithUnavailable, match="create_project is down"):
        experiment.start()


@pytest.mark.unit
async def test_feedback_errors_never_change_the_verdict(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient(fail={"create_feedback"})

    _, result = await run(evals, client)

    assert result.scenarios[0].passed
    assert int(result.exit_code) == 0
    assert client.runs_named("a_ack")  # the rows still went out


# --- edges ------------------------------------------------------------------------------

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


@pytest.mark.unit
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


@pytest.mark.unit
def test_a_sync_that_loses_examples_is_an_error(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient()
    client.create_examples = lambda **kwargs: None  # type: ignore[method-assign]  # accepts, stores nothing

    with pytest.raises(
        LangSmithUnavailable, match="examples missing after the dataset sync: a_ack"
    ):
        sync_dataset(client, "core", scenarios_of(evals))


@pytest.mark.unit
async def test_a_trial_that_cannot_run_reports_its_error_as_feedback(evals: Path) -> None:
    write(evals, "a_ack")
    path = evals / "scenarios" / "core" / "a_ack.yaml"
    path.write_text(path.read_text().replace("load_481207.json", "missing.json"), encoding="utf-8")
    client = FakeClient()

    await run(evals, client)

    (trial,) = client.runs_named("a_ack")
    passed = client.feedback_of(trial["id"])["passed"]
    assert passed["score"] == 0.0
    assert passed["comment"].startswith("error: load fixture not found: fixtures/missing.json")
    assert trial["outputs"]["error"] == "load fixture not found: fixtures/missing.json"


@pytest.mark.unit
async def test_a_ping_step_is_named_after_its_risk_and_is_part_of_the_dataset(evals: Path) -> None:
    folder = evals / "scenarios" / "core"
    folder.mkdir(parents=True)
    (folder / "p_ping.yaml").write_text(PING.format(id="p_ping"), encoding="utf-8")
    client = FakeClient()

    await run(evals, client)

    assert [s["name"] for s in client.runs_named("step ")] == ["step 0 · 07:00 · ping medium"]
    (example,) = client.examples.values()
    assert example.inputs["steps"][0]["ping"]["delay_risk"] == "medium"


@pytest.mark.unit
async def test_a_step_outside_a_recorded_trial_records_nothing(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient()
    scenario = scenarios_of(evals)[0]
    experiment = LangSmithExperiment(
        client, suite="core", dataset_scenarios=[scenario], metadata=metadata(), k=1
    )

    async with experiment.step(scenario, 0, scenario.steps[0], datetime.now(UTC)) as span:
        span.observed(None)  # type: ignore[arg-type]  # a null span ignores it

    assert client.runs == {}
    assert experiment.url is None  # not started: no dataset, no experiment


@pytest.mark.unit
async def test_sdk_failures_while_tracing_or_flushing_never_stop_the_run(
    monkeypatch: pytest.MonkeyPatch, evals: Path
) -> None:
    import langchain_core.tracers.langchain as lc_tracers

    from freight_agent.evalkit import langsmith_experiment

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("sdk bug")

    write(evals, "a_ack")
    monkeypatch.setattr(langsmith_experiment.run_helpers, "trace", broken)
    monkeypatch.setattr(lc_tracers, "wait_for_all_tracers", broken)
    client = FakeClient()

    _, result = await run(evals, client)

    assert result.scenarios[0].passed
    assert client.runs == {} and client.project_updates  # no rows, but the run finished


# --- the agent page ---------------------------------------------------------------------


def started(evals: Path, client: FakeClient, agent: str | None) -> LangSmithExperiment:
    experiment = LangSmithExperiment(
        client,
        suite="core",
        dataset_scenarios=scenarios_of(evals),
        metadata=metadata(),
        k=1,
        agent=agent,
    )
    experiment.start()
    return experiment


@pytest.mark.unit
def test_the_dataset_is_tagged_with_its_agent_once(evals: Path) -> None:
    write(evals, "a_ack")
    client = FakeClient()

    first = started(evals, client, "freight-agent")
    second = started(evals, client, "freight-agent")

    dataset = client.datasets["freight-agent-core"]
    assert first.linked_to_agent and second.linked_to_agent
    assert client.taggings == [
        {"tag_value_id": "tv1", "resource_type": "dataset", "resource_id": str(dataset.id)}
    ]


@pytest.mark.unit
@pytest.mark.parametrize(("agent", "fail"), [("another-agent", set()), ("freight-agent", {"tags"})])
def test_no_agent_tag_or_a_tags_error_leaves_the_dataset_unlinked(
    evals: Path, agent: str, fail: set[str]
) -> None:
    write(evals, "a_ack")
    client = FakeClient(fail=fail)

    experiment = started(evals, client, agent)

    assert not experiment.linked_to_agent
    assert client.taggings == [] and client.projects  # the experiment exists all the same
