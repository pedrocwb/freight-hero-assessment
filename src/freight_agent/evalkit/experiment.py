"""Evals as LangSmith experiments: a dataset per suite, an experiment per run.

- **Dataset** `freight-agent-<suite>`: one example per scenario, synced from the YAML files
  before the run. The inputs are what the agent is given (the load, the clock, each step's
  message or ping); the reference outputs are what the graders check (each step's `expect`
  and `forbid`). An example is matched by `metadata.scenario_id` and rewritten only when
  the scenario's content hash changes; a scenario removed from the suite leaves the
  dataset. LangSmith versions the dataset on every change and each experiment pins its
  version. The dataset carries the workspace's `Agent` resource tag named after the
  tracing project (`LANGSMITH_PROJECT`), so it shows on that agent's page; with no such
  tag in the workspace it is simply not linked.
- **Experiment**: `langsmith.evaluation.aevaluate` over the dataset, `num_repetitions=k`,
  with the run's reproducibility metadata (`evalkit.metadata`: git sha, the model of each
  role, the SOP tree hash, the version of each prompt template, its Hub commit, and the
  reference it was resolved from).
- **Target** (`ScenarioTarget`): plays one scenario, live, on a fresh in-memory runtime
  with the virtual clock, recording gateways and the run's prompt templates
  (`evalkit.runner.play_scenario`, the player the platform uses too) and returns each
  step's observations. The runtime's
  LangGraph runs nest under the example's trace, node by node, with every model round.
  The judge runs inside the target, right after its step, so its verdict is part of the
  outputs and the cost cap counts it.
- **Evaluators**, one score per row: `facts`, `trajectory`, `judge` and `invariants`
  (the graders' groups, `evalkit.graders`), `passed` (all of them), and `cost_usd`.
  **Summary evaluators**: `pass_hat_k` (a scenario passes when all its k repetitions
  pass) overall and per tag, `pass_at_k`, and the experiment's `cost_usd`. Both read
  only the outputs, so a score can be recomputed from the stored experiment.
- **Cost cap**: the target keeps the running cost of the experiment (agent and judge);
  once it passes `cost_cap_usd`, every example that has not started errors out at once,
  without a model call.

`LangSmithUnavailable` stops a run before it spends anything: the dataset is synced
before the first example plays.
"""

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from langchain_core.embeddings import Embeddings

from freight_agent.application.runtime import RuntimeConfig
from freight_agent.evalkit.graders import GROUPS, CheckGroup, grade
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.results import Failure, StepObservation
from freight_agent.evalkit.runner import play_scenario
from freight_agent.evalkit.spec import Scenario, Step
from freight_agent.evalkit.suite import gating
from freight_agent.interfaces.decisions import DecisionModel
from freight_agent.interfaces.knowledge import SopIndex
from freight_agent.interfaces.llm import LanguageModel
from freight_agent.interfaces.prompts import Prompts

logger = logging.getLogger(__name__)

DATASET_PREFIX = "freight-agent-"
DEFAULT_CONCURRENCY = 4  # examples played at once; each is one scenario on its own runtime


class LangSmithUnavailable(RuntimeError):
    """The dataset could not be synced: nothing has run or been spent."""


class CostCapReached(RuntimeError):
    """The experiment spent its cap: this example is not played."""


class ExperimentClient(Protocol):
    """The slice of `langsmith.Client` the dataset sync uses; a fake implements it in tests."""

    def has_dataset(self, *, dataset_name: str) -> bool: ...
    def read_dataset(self, *, dataset_name: str) -> Any: ...
    def create_dataset(
        self, dataset_name: str, *, description: str, metadata: dict[str, Any]
    ) -> Any: ...
    def list_examples(self, *, dataset_id: Any) -> Any: ...
    def create_examples(self, *, dataset_id: Any, examples: Sequence[dict[str, Any]]) -> Any: ...
    def update_examples(self, *, dataset_id: Any, updates: Sequence[dict[str, Any]]) -> Any: ...
    def delete_examples(self, example_ids: Sequence[Any]) -> None: ...
    def request_with_retries(
        self, method: Any, pathname: str, *, request_kwargs: Mapping[str, Any] | None = None
    ) -> Any: ...  # resource tags have no SDK helper


# --- the dataset ------------------------------------------------------------------------


def dataset_name(suite: str) -> str:
    return f"{DATASET_PREFIX}{suite}"


def example_inputs(scenario: Scenario) -> dict[str, Any]:
    """What the agent is given: the load, the clock, each step's message or ping, and the
    facility notes seeded before the first step (only when there are some)."""
    inputs: dict[str, Any] = {
        "scenario_id": scenario.id,
        "title": scenario.title,
        "description": scenario.description,
        "load": scenario.load,
        "load_overrides": scenario.load_overrides,
        "timezone": scenario.timezone,
        "date": scenario.date.isoformat(),
        "start": scenario.start,
        "steps": [_step_input(step) for step in scenario.steps],
    }
    if scenario.memories:
        inputs["memories"] = [
            m.model_dump(mode="json", exclude_defaults=True) for m in scenario.memories
        ]
    return inputs


def example_outputs(scenario: Scenario) -> dict[str, Any]:
    """What the graders check, step by step."""
    return {
        "steps": [
            {
                "at": step.at,
                "expect": step.expect.model_dump(mode="json", exclude_defaults=True),
                "forbid": [m.model_dump(mode="json", exclude_defaults=True) for m in step.forbid],
            }
            for step in scenario.steps
        ]
    }


def scenario_hash(scenario: Scenario) -> str:
    blob = json.dumps(
        {
            "inputs": example_inputs(scenario),
            "outputs": example_outputs(scenario),
            "tags": list(scenario.tags),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _step_input(step: Step) -> dict[str, Any]:
    data: dict[str, Any] = {"at": step.at}
    if step.note:
        data["note"] = step.note
    if step.message is not None:
        data["message"] = step.message.model_dump(mode="json", exclude_defaults=True)
    if step.ping is not None:
        data["ping"] = step.ping.model_dump(mode="json")
    return data


def _example(scenario: Scenario, suite: str) -> dict[str, Any]:
    return {
        "inputs": example_inputs(scenario),
        "outputs": example_outputs(scenario),
        "metadata": {
            "scenario_id": scenario.id,
            "suite": suite,
            "tags": list(scenario.tags),
            "sha256": scenario_hash(scenario),
        },
    }


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name)


def sync_dataset(
    client: ExperimentClient, suite: str, scenarios: Sequence[Scenario]
) -> tuple[Any, dict[str, Any]]:
    """Make the suite's dataset match its scenario files. Returns (dataset, {scenario id:
    example id})."""
    name = dataset_name(suite)
    if client.has_dataset(dataset_name=name):
        dataset = client.read_dataset(dataset_name=name)
    else:
        dataset = client.create_dataset(
            name,
            description=f"The {suite} eval scenarios of the freight load agent "
            f"(evals/scenarios/{suite}/*.yaml), synced by `agent eval run`.",
            metadata={"suite": suite, "source": f"evals/scenarios/{suite}"},
        )
    dataset_id = _get(dataset, "id")
    existing: dict[str, Any] = {}
    stale: list[Any] = []
    for example in client.list_examples(dataset_id=dataset_id):
        metadata = _get(example, "metadata") or {}
        scenario_id = metadata.get("scenario_id")
        if scenario_id is None or scenario_id in existing:
            stale.append(_get(example, "id"))  # not ours, or a duplicate
        else:
            existing[scenario_id] = example
    wanted = {s.id: s for s in scenarios}
    stale.extend(_get(e, "id") for sid, e in existing.items() if sid not in wanted)
    creates, updates = [], []
    for scenario in scenarios:
        body = _example(scenario, suite)
        current = existing.get(scenario.id)
        if current is None:
            creates.append(body)
        elif (_get(current, "metadata") or {}).get("sha256") != body["metadata"]["sha256"]:
            updates.append({"id": _get(current, "id"), **body})
    if creates:
        client.create_examples(dataset_id=dataset_id, examples=creates)
    if updates:
        client.update_examples(dataset_id=dataset_id, updates=updates)
    if stale:
        client.delete_examples(stale)
    ids: dict[str, Any] = {}
    if creates or updates or stale:
        for example in client.list_examples(dataset_id=dataset_id):
            scenario_id = (_get(example, "metadata") or {}).get("scenario_id")
            if scenario_id in wanted:
                ids[scenario_id] = _get(example, "id")
    else:
        ids = {sid: _get(e, "id") for sid, e in existing.items() if sid in wanted}
    missing = sorted(set(wanted) - set(ids))
    if missing:
        raise LangSmithUnavailable(f"examples missing after the dataset sync: {', '.join(missing)}")
    logger.info(
        "dataset %s: %d created, %d updated, %d removed",
        name,
        len(creates),
        len(updates),
        len(stale),
    )
    return dataset, ids


AGENT_TAG = "Agent"


def link_to_agent(client: ExperimentClient, dataset_id: Any, agent: str) -> bool:
    """Tag the dataset with the workspace's `Agent: <agent>` resource tag, which is what an
    agent's Datasets & Experiments page lists. Its experiments follow the dataset.

    Cosmetic, so it fails open: no such tag in the workspace, or any API error, leaves the
    dataset unlinked (a warning) and the run goes on. Returns whether it is linked.
    """
    try:
        value_id = _tag_value_id(
            _json(client.request_with_retries("GET", "/workspaces/current/tags")), agent
        )
        if value_id is None:
            logger.info(
                "no %s tag %r in the LangSmith workspace: the dataset is not linked",
                AGENT_TAG,
                agent,
            )
            return False
        params = {"resource_type": "dataset", "resource_id": str(dataset_id)}
        current = _json(
            client.request_with_retries(
                "GET", "/workspaces/current/tags/resource", request_kwargs={"params": params}
            )
        )
        if _tag_value_id(current, agent) == value_id:
            return True
        client.request_with_retries(
            "POST",
            "/workspaces/current/taggings",
            request_kwargs={"json": {"tag_value_id": value_id, **params}},
        )
        return True
    except Exception as exc:
        logger.warning("linking the dataset to the LangSmith agent %r failed: %r", agent, exc)
        return False


def _json(response: Any) -> Any:
    return response.json() if hasattr(response, "json") else response


def _tag_value_id(tags: Any, agent: str) -> str | None:
    """The id of the `Agent: <agent>` value in a `/workspaces/current/tags` style list."""
    for key in tags or []:
        if key.get("key") != AGENT_TAG:
            continue
        for value in key.get("values") or []:
            if value.get("value") == agent:
                return str(value.get("id"))
    return None


# --- the target -------------------------------------------------------------------------


@dataclass
class CostMeter:
    """The experiment's running cost, the judge included, against its cap."""

    cap_usd: float
    spent_usd: float = 0.0
    skipped: list[str] = field(default_factory=list[str])  # scenario ids not played

    @property
    def reached(self) -> bool:
        return self.spent_usd > self.cap_usd


class ScenarioTarget:
    """The experiment's target: one example in, one scenario played live, its
    observations out. A new model per example (`model_factory`), so circuit breakers
    never carry one example's outage into the next. `config` sets the gate's engine, and
    `decisions` is the jev engine's model (one for the experiment: it has no breaker), and
    `embeddings` index each trial's facility memory (one for the experiment)."""

    def __init__(
        self,
        scenarios: Mapping[str, Scenario],
        *,
        evals_dir: Any,
        knowledge: SopIndex,
        prompts: Prompts,
        model_factory: Callable[[], LanguageModel],
        meter: CostMeter,
        config: RuntimeConfig | None = None,
        decisions: DecisionModel | None = None,
        embeddings: Embeddings | None = None,
    ) -> None:
        self._scenarios = scenarios
        self._embeddings = embeddings
        self._evals_dir = evals_dir
        self._knowledge = knowledge
        self._prompts = prompts
        self._model_factory = model_factory
        self.meter = meter
        self._config = config
        self._decisions = decisions

    async def __call__(self, inputs: dict[str, Any]) -> dict[str, Any]:
        scenario = self._scenarios[inputs["scenario_id"]]
        meter = self.meter
        if meter.reached:
            meter.skipped.append(scenario.id)
            raise CostCapReached(
                f"not played: the experiment spent ${meter.spent_usd:.4f}, over its cap of "
                f"${meter.cap_usd:.2f}"
            )
        played = await play_scenario(
            scenario,
            evals_dir=self._evals_dir,
            knowledge=self._knowledge,
            prompts=self._prompts,
            model=self._model_factory(),
            config=self._config,
            decisions=self._decisions,
            embeddings=self._embeddings,
        )
        meter.spent_usd += played.cost_usd
        return {
            "scenario_id": scenario.id,
            "error": played.error,
            "cost_usd": played.cost_usd,
            "judge_cost_usd": sum((u.cost_usd for u in played.judge_usage), 0.0),
            "model_calls": sum(o.llm_calls for o in played.observations),
            "rounds": len(played.usage),
            "steps": [o.compact() for o in played.observations],
        }


# --- evaluators -------------------------------------------------------------------------


def failures(
    scenario: Scenario,
    outputs: Mapping[str, Any] | None,
    groups: Sequence[CheckGroup] = GROUPS,
) -> list[Failure]:
    """The graders' verdict on a target's outputs. No outputs: the example did not run."""
    if not outputs:
        return [Failure(step_index=None, check="error", message="the example did not run")]
    observations = [StepObservation.model_validate(o) for o in outputs.get("steps", [])]
    error = outputs.get("error")
    return grade(
        scenario, observations, error=error if isinstance(error, str) else None, groups=groups
    )


def _score(key: str, found: Sequence[Failure]) -> dict[str, Any]:
    comment = "\n".join(f.describe() for f in found) or None
    return {"key": key, "score": 0.0 if found else 1.0, "comment": comment}


def evaluators(scenarios: Mapping[str, Scenario]) -> list[Callable[..., dict[str, Any]]]:
    """One evaluator per graders' group, plus `passed` and `cost_usd`. LangSmith passes
    each the example's inputs and the target's outputs, by parameter name."""

    def group(name: CheckGroup) -> Callable[..., dict[str, Any]]:
        def evaluate(inputs: dict[str, Any], outputs: dict[str, Any] | None) -> dict[str, Any]:
            scenario = scenarios[inputs["scenario_id"]]
            if name == "judge" and not _judged(scenario.steps):
                return {"results": []}  # nothing to judge: no score, rather than a free 1
            return _score(name, failures(scenario, outputs, (name,)))

        evaluate.__name__ = name
        return evaluate

    def passed(inputs: dict[str, Any], outputs: dict[str, Any] | None) -> dict[str, Any]:
        return _score("passed", failures(scenarios[inputs["scenario_id"]], outputs))

    def cost_usd(outputs: dict[str, Any] | None) -> dict[str, Any]:
        return {"key": "cost_usd", "score": float((outputs or {}).get("cost_usd") or 0.0)}

    return [*(group(name) for name in GROUPS), passed, cost_usd]


def _judged(steps: Sequence[Step]) -> bool:
    return any(step.expect.judge is not None for step in steps)


@dataclass(frozen=True)
class ScenarioOutcome:
    """A scenario's repetitions in an experiment, graded."""

    scenario: Scenario
    passed: tuple[bool, ...]  # one per repetition that ran
    failures: tuple[tuple[Failure, ...], ...]
    cost_usd: float = 0.0
    not_run: int = 0  # repetitions the cost cap stopped

    @property
    def pass_hat_k(self) -> bool:
        return bool(self.passed) and all(self.passed) and not self.not_run

    @property
    def pass_at_k(self) -> bool:
        return any(self.passed)


def outcomes(
    scenarios: Mapping[str, Scenario],
    rows: Sequence[tuple[Mapping[str, Any], Mapping[str, Any] | None, str | None]],
) -> list[ScenarioOutcome]:
    """Every scenario's repetitions, from (inputs, outputs, error) rows, in first-seen
    order. A row that errored out on the cost cap is not run, not failed."""
    grouped: dict[str, list[tuple[Mapping[str, Any] | None, str | None]]] = defaultdict(list)
    for inputs, outputs, error in rows:
        grouped[str(inputs["scenario_id"])].append((outputs, error))
    found: list[ScenarioOutcome] = []
    for scenario_id, runs in grouped.items():
        scenario = scenarios[scenario_id]
        ran = [(o, e) for o, e in runs if not (e and CostCapReached.__name__ in e)]
        graded = [tuple(failures(scenario, o)) for o, _ in ran]
        found.append(
            ScenarioOutcome(
                scenario=scenario,
                passed=tuple(not f for f in graded),
                failures=tuple(graded),
                cost_usd=sum(float((o or {}).get("cost_usd") or 0.0) for o, _ in ran),
                not_run=len(runs) - len(ran),
            )
        )
    return found


def summary_evaluators(scenarios: Mapping[str, Scenario]) -> list[Callable[..., dict[str, Any]]]:
    """pass^k and pass@k over the scenarios, overall and per tag, and the total cost."""

    def pass_k(runs: Sequence[Any], examples: Sequence[Any]) -> dict[str, Any]:
        found = outcomes(scenarios, _rows(runs, examples))
        results = [
            {"key": "pass_hat_k", "score": _share(o.pass_hat_k for o in found)},
            {"key": "pass_at_k", "score": _share(o.pass_at_k for o in found)},
        ]
        for tag in sorted({t for o in found for t in o.scenario.tags}):
            members = [o for o in found if tag in o.scenario.tags]
            results.append(
                {"key": f"pass_hat_k:{tag}", "score": _share(o.pass_hat_k for o in members)}
            )
        return {"results": results}

    def total_cost(runs: Sequence[Any]) -> dict[str, Any]:
        cost = sum(float(((r.outputs or {}).get("cost_usd")) or 0.0) for r in runs)
        return {"key": "total_cost_usd", "score": cost}

    return [pass_k, total_cost]


def _rows(
    runs: Sequence[Any], examples: Sequence[Any]
) -> list[tuple[Mapping[str, Any], Mapping[str, Any] | None, str | None]]:
    return [(e.inputs, r.outputs, r.error) for r, e in zip(runs, examples, strict=True)]


def _share(values: Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def regressions(found: Sequence[ScenarioOutcome]) -> list[ScenarioOutcome]:
    """The gating scenarios with a repetition that ran and failed. One the cost cap left
    short, every repetition passing, is not run rather than failed."""
    return [o for o in found if gating(o.scenario) and not all(o.passed)]


# --- running ----------------------------------------------------------------------------


@dataclass(frozen=True)
class ExperimentRun:
    name: str
    url: str | None
    outcomes: tuple[ScenarioOutcome, ...]
    cost_usd: float
    not_run: tuple[str, ...]  # scenario ids the cost cap stopped


async def run_experiment(
    client: Any,
    *,
    suite: str,
    suite_scenarios: Sequence[Scenario],
    chosen: Sequence[Scenario],
    target: ScenarioTarget,
    metadata: RunMetadata,
    k: int,
    agent: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> ExperimentRun:
    """Sync the suite's dataset, then evaluate `chosen` with `aevaluate`."""
    from langsmith import traceable
    from langsmith.evaluation import aevaluate

    try:
        dataset, ids = sync_dataset(client, suite, suite_scenarios)
    except LangSmithUnavailable:
        raise
    except Exception as exc:
        raise LangSmithUnavailable(f"{type(exc).__name__}: {exc}") from exc
    if agent:
        link_to_agent(client, _get(dataset, "id"), agent)
    wanted = {ids[s.id] for s in chosen}
    found_examples = client.list_examples(dataset_id=_get(dataset, "id"))
    examples = [e for e in found_examples if _get(e, "id") in wanted]
    scenarios = {s.id: s for s in suite_scenarios}
    # aevaluate wants a coroutine function; traced under a name of ours, not "AsyncTarget".
    play = traceable(name="play_scenario", run_type="chain")(target.__call__)
    results = await aevaluate(
        play,
        data=examples,
        evaluators=evaluators(scenarios),
        summary_evaluators=cast("Any", summary_evaluators(scenarios)),
        num_repetitions=k,
        metadata=metadata.for_langsmith(),
        experiment_prefix=f"{suite}-k{k}",
        description=f"agent eval run --suite {suite} --k {k}",
        max_concurrency=concurrency,
        client=client,
    )
    rows: list[tuple[Mapping[str, Any], Mapping[str, Any] | None, str | None]] = []
    async for row in results:
        run, example = row["run"], row["example"]
        rows.append((example.inputs or {}, run.outputs, run.error))
    found = outcomes(scenarios, rows)
    try:
        url = await results.get_comparison_url() or results.url
    except Exception:  # only the link is lost
        url = None
    return ExperimentRun(
        name=results.experiment_name,
        url=url,
        outcomes=tuple(found),
        cost_usd=target.meter.spent_usd,
        not_run=tuple(dict.fromkeys(target.meter.skipped)),
    )
