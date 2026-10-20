"""Live eval runs as LangSmith experiments: a dataset per suite, an experiment per run.

- **Dataset** `freight-agent-<suite>`: one example per scenario, synced from the YAML files
  before the run. The inputs are what the agent is given (the load, the clock, each step's
  message or ping); the reference outputs are what the graders check (each step's `expect`
  and `forbid`). An example is matched by `metadata.scenario_id` and rewritten only when the
  scenario's content hash changes; a scenario removed from the suite leaves the dataset.
  LangSmith versions the dataset on every change and each experiment pins its version.
  The dataset carries the workspace's `Agent` resource tag named after the tracing project
  (`LANGSMITH_PROJECT`, `freight-agent`), so it shows on that agent's Datasets & Experiments
  page; with no such tag in the workspace it is simply not linked.
- **Experiment**: one per `agent eval run --langsmith`, linked to the dataset and named
  `<suite>-<mode>-<model set>-<UTC time>`, with the run's reproducibility metadata (models,
  git sha, SOP version, prompt and config hashes, k).
- **One row per trial**: a root run linked to the scenario's example, a child run per step,
  and under each step the real model calls. LangChain traces those into the current run
  through LangSmith's context, with the prompts, answers and tokens.
- **Feedback** on each trial: `passed` (with the failed checks as the comment),
  `checks_failed`, `cost_usd`, `model_calls`; and `pass_hat_k` on every trial of a scenario
  once its k trials ran (1 when all passed). The experiment itself gets the run's totals:
  pass^k and pass@k overall and per group, cost and exit code.

Live only (the CLI refuses the other modes): scripted and replay runs measure the code and
are free to repeat locally; an experiment measures the model.

Failing open: the dataset and the experiment are created before the first trial and raise
`LangSmithUnavailable`, so a live run never spends money for an experiment that cannot be
written. After that, every LangSmith error is logged and the run and its verdict go on.
"""

import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Protocol

from langsmith import run_helpers
from langsmith.run_trees import RunTree

from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.observe import StepSpan, TrialSpan
from freight_agent.evalkit.results import ScenarioResult, StepObservation
from freight_agent.evalkit.spec import Scenario, Step
from freight_agent.evalkit.suite import SuiteResult

logger = logging.getLogger(__name__)

DATASET_PREFIX = "freight-agent-"


class LangSmithUnavailable(RuntimeError):
    """The dataset or the experiment could not be created: nothing has run or been spent."""


class ExperimentClient(Protocol):
    """The slice of `langsmith.Client` an experiment uses; a fake implements it in tests.

    Runs are sent by `langsmith.run_helpers.trace`, which calls the client's own
    `create_run` / `update_run`, so those are part of the slice too.
    """

    def has_dataset(self, *, dataset_name: str) -> bool: ...
    def read_dataset(self, *, dataset_name: str) -> Any: ...
    def create_dataset(
        self, dataset_name: str, *, description: str, metadata: dict[str, Any]
    ) -> Any: ...
    def list_examples(self, *, dataset_id: Any) -> Any: ...
    def create_examples(self, *, dataset_id: Any, examples: Sequence[dict[str, Any]]) -> Any: ...
    def update_examples(self, *, dataset_id: Any, updates: Sequence[dict[str, Any]]) -> Any: ...
    def delete_examples(self, example_ids: Sequence[Any]) -> None: ...
    def create_project(
        self,
        project_name: str,
        *,
        description: str,
        metadata: dict[str, Any],
        reference_dataset_id: Any,
    ) -> Any: ...
    def update_project(
        self, project_id: Any, *, metadata: dict[str, Any], end_time: datetime
    ) -> Any: ...
    def create_feedback(
        self,
        run_id: Any = None,
        key: str = "unnamed",
        *,
        score: float | None = None,
        comment: str | None = None,
        trace_id: Any = None,
        project_id: Any = None,
        session_id: Any = None,
        feedback_id: Any = None,
    ) -> Any: ...
    def create_run(
        self, name: str, inputs: dict[str, Any], run_type: Any, **kwargs: Any
    ) -> None: ...
    def update_run(self, run_id: Any, **kwargs: Any) -> None: ...
    def flush(self) -> None: ...
    def request_with_retries(
        self, method: Any, pathname: str, *, request_kwargs: Mapping[str, Any] | None = None
    ) -> Any: ...  # resource tags have no SDK helper


# --- the dataset ------------------------------------------------------------------------


def dataset_name(suite: str) -> str:
    return f"{DATASET_PREFIX}{suite}"


def example_inputs(scenario: Scenario) -> dict[str, Any]:
    """What the agent is given. The script is left out: live runs ignore it."""
    return {
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
            f"(evals/scenarios/{suite}/*.yaml), synced by `agent eval run --langsmith`.",
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


# --- the experiment ---------------------------------------------------------------------


def experiment_name(metadata: RunMetadata, suite: str) -> str:
    stamp = metadata.created_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{suite}-{metadata.mode}-{metadata.model_set}-{stamp}"


def _experiment_metadata(
    metadata: RunMetadata, k: int, cost_cap_usd: float | None
) -> dict[str, Any]:
    data = metadata.model_dump(mode="json", exclude={"settings"})
    data["models"] = {role: ", ".join(chain) for role, chain in metadata.models.items()}
    data.update({"k": k, "cost_cap_usd": cost_cap_usd, "harness": "freight_agent.evalkit"})
    return data


class LangSmithExperiment:
    """An `evalkit.observe.Observer` that records a live eval run as an experiment.

    `start()` syncs the dataset and creates the experiment (raising when it cannot);
    the suite runner then opens `trial` and `step` spans; `finish(result)` writes the
    run's totals, sends what is still queued and returns the experiment's URL.
    """

    def __init__(
        self,
        client: ExperimentClient,
        *,
        suite: str,
        dataset_scenarios: Sequence[Scenario],
        metadata: RunMetadata,
        k: int,
        cost_cap_usd: float | None = None,
        agent: str | None = None,
    ) -> None:
        self._client = client
        self._agent = agent
        self.linked_to_agent = False
        self._suite = suite
        self._dataset_scenarios = tuple(dataset_scenarios)
        self._metadata = metadata
        self._k = k
        self._cost_cap = cost_cap_usd
        self.name = experiment_name(metadata, suite)
        self._dataset: Any = None
        self._project: Any = None
        self._example_ids: dict[str, Any] = {}
        self._trial_runs: dict[str, list[tuple[Any, bool]]] = {}

    @property
    def url(self) -> str | None:
        """The experiment in the dataset's compare view, when the SDK can tell."""
        try:
            base = _get(self._dataset, "url")
            return f"{base}/compare?selectedSessions={_get(self._project, 'id')}" if base else None
        except Exception:
            return None

    def start(self) -> None:
        try:
            self._dataset, self._example_ids = sync_dataset(
                self._client, self._suite, self._dataset_scenarios
            )
            if self._agent:
                self.linked_to_agent = link_to_agent(
                    self._client, _get(self._dataset, "id"), self._agent
                )
            self._project = self._client.create_project(
                self.name,
                description=f"agent eval run --suite {self._suite} --mode {self._metadata.mode} "
                f"--k {self._k} --model-set {self._metadata.model_set}",
                metadata=_experiment_metadata(self._metadata, self._k, self._cost_cap),
                reference_dataset_id=_get(self._dataset, "id"),
            )
        except LangSmithUnavailable:
            raise
        except Exception as exc:
            raise LangSmithUnavailable(f"{type(exc).__name__}: {exc}") from exc

    # --- spans (evalkit.observe.Observer) ---

    @asynccontextmanager
    async def trial(self, scenario: Scenario, trial: int) -> AsyncIterator[TrialSpan]:
        span = _TrialRun(scenario, trial)
        try:
            # The SDK types `client` as its own Client; ours is the slice it uses.
            client: Any = self._client
            context = run_helpers.tracing_context(
                enabled=True, client=client, project_name=self.name
            )
            tracer = run_helpers.trace(
                scenario.id,
                "chain",
                inputs=example_inputs(scenario),
                project_name=self.name,
                client=client,
                reference_example_id=self._example_ids.get(scenario.id),
                metadata={"scenario_id": scenario.id, "trial": trial, "k": self._k},
                tags=[*scenario.tags, f"trial:{trial}"],
            )
        except Exception as exc:
            logger.warning("LangSmith trial span for %s failed: %r", scenario.id, exc)
            yield span
            return
        with context:
            async with tracer as run:
                span.run = run
                yield span
                _safe(lambda: run.add_outputs(span.outputs()), "trial outputs")
        self._feedback(span)

    @asynccontextmanager
    async def step(
        self, scenario: Scenario, index: int, step: Step, at: datetime
    ) -> AsyncIterator[StepSpan]:
        span = _StepRun()
        if run_helpers.get_current_run_tree() is None:  # not inside a recorded trial
            yield span
            return
        async with run_helpers.trace(
            f"step {index} · {at.strftime('%H:%M')} · {_what(step)}",
            "chain",
            inputs=_step_input(step),
            metadata={"step_index": index, "at": at.isoformat()},
        ) as run:
            span.run = run
            yield span

    # --- feedback ---

    def _feedback(self, span: "_TrialRun") -> None:
        result, run = span.result, span.run
        if result is None or run is None:
            return
        failures = [
            f"step {f.step_index}: {f.check}: {f.message}"
            if f.step_index is not None
            else f"{f.check}: {f.message}"
            for f in result.failures
        ]
        if result.error:
            failures.insert(0, f"error: {result.error}")
        scores: list[tuple[str, float, str | None]] = [
            ("passed", 1.0 if result.passed else 0.0, "\n".join(failures) or None),
            ("checks_failed", float(len(result.failures) + (result.error is not None)), None),
            ("cost_usd", result.cost_usd, None),
            ("model_calls", float(sum(o.llm_calls for o in result.observations)), None),
        ]
        for key, score, comment in scores:
            self._send_feedback(run.id, key, score, comment)
        runs = self._trial_runs.setdefault(span.scenario.id, [])
        runs.append((run.id, result.passed))
        if len(runs) == self._k:
            hat = 1.0 if all(passed for _, passed in runs) else 0.0
            comment = f"{sum(p for _, p in runs)}/{self._k} trials passed"
            for run_id, _ in runs:
                self._send_feedback(run_id, "pass_hat_k", hat, comment)

    def _send_feedback(self, run_id: Any, key: str, score: float, comment: str | None) -> None:
        _safe(
            lambda: self._client.create_feedback(
                run_id,
                key,
                score=score,
                comment=comment,
                trace_id=run_id,
                session_id=_get(self._project, "id"),  # the experiment the run belongs to
                feedback_id=feedback_id(run_id, key),
            ),
            f"feedback {key}",
        )

    # --- the end of the run ---

    def finish(self, result: SuiteResult) -> str | None:
        """The run's totals on the experiment, then flush. Returns the experiment URL."""
        totals: dict[str, float] = {
            "pass_hat_k": _share(s.passed for s in result.scenarios),
            "pass_at_k": _share(s.pass_at_k for s in result.scenarios),
            "cost_usd": result.cost_usd,
            "exit_code": float(int(result.exit_code)),
        }
        for group in result.groups:
            if group.tag in ("regression", "capability"):
                totals[f"pass_hat_k_{group.tag}"] = group.pass_hat_k
        project_id = _get(self._project, "id")
        for key, score in totals.items():
            _safe(
                lambda key=key, score=score: self._client.create_feedback(
                    None,
                    key,
                    score=score,
                    project_id=project_id,
                    feedback_id=feedback_id(project_id, key),
                ),
                f"experiment feedback {key}",
            )
        summary = {
            **_experiment_metadata(self._metadata, self._k, self._cost_cap),
            **{f"result_{k}": v for k, v in totals.items()},
            "scenarios": len(result.scenarios),
            "not_run": list(result.not_run),
            "stopped": result.stopped,
        }
        _safe(
            lambda: self._client.update_project(
                project_id, metadata=summary, end_time=datetime.now(UTC)
            ),
            "experiment summary",
        )
        flush(self._client)
        return self.url


def feedback_id(target: Any, key: str) -> uuid.UUID:
    """One id per (run or experiment, key): the SDK sends feedback in background batches,
    and a resent batch must update the same feedback, not add a second one."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"freight-agent:feedback:{target}:{key}")


def flush(client: ExperimentClient) -> None:
    """Send what the SDK and LangChain still hold before the process exits."""
    _safe(client.flush, "flush")
    try:
        from langchain_core.tracers.langchain import wait_for_all_tracers

        wait_for_all_tracers()
    except Exception as exc:
        logger.warning("waiting for LangChain tracers failed: %r", exc)


class _TrialRun:
    def __init__(self, scenario: Scenario, trial: int) -> None:
        self.scenario = scenario
        self.trial = trial
        self.run: RunTree | None = None
        self.result: ScenarioResult | None = None

    def graded(self, result: ScenarioResult) -> None:
        self.result = result

    def outputs(self) -> dict[str, Any]:
        r = self.result
        if r is None:
            return {}
        return {
            "passed": r.passed,
            "error": r.error,
            "failures": [
                {"step": f.step_index, "check": f.check, "message": f.message} for f in r.failures
            ],
            "steps": [_observation_summary(o) for o in r.observations],
            "cost_usd": r.cost_usd,
            "model_calls": sum(o.llm_calls for o in r.observations),
            "tokens": sum(u.input_tokens + u.output_tokens for u in r.usage),
        }


class _StepRun:
    def __init__(self) -> None:
        self.run: RunTree | None = None

    def observed(self, observation: StepObservation) -> None:
        run = self.run
        if run is not None:
            _safe(lambda: run.add_outputs(_observation_summary(observation)), "step outputs")


def _observation_summary(o: StepObservation) -> dict[str, Any]:
    """What a step did, in the terms the graders use: outbox items and runs."""
    return {
        "at": o.at.strftime("%H:%M:%S"),
        "llm_calls": o.llm_calls,
        "outbox": [{"kind": item.kind.value, **item.payload} for item in o.outbox],
        "runs": [
            {
                "event_id": run.event_id,
                "status": run.status.value,
                "intent": None if run.gate is None else run.gate.intent,
                "skip_reason": run.skip_reason,
                "sop_section_ids": list(run.sop_section_ids),
                "plan": None if run.plan is None else run.plan.model_dump(mode="json"),
                "violations": [v.model_dump(mode="json") for v in run.violations],
                "cost_usd": run.cost_usd,
            }
            for run in o.runs
        ],
    }


def _what(step: Step) -> str:
    if step.message is not None:
        return f"{step.message.channel.value} from {step.message.sender or 'unknown'}"
    if step.ping is not None:
        return f"ping {step.ping.delay_risk.value}"
    return "time passes"


def _share(values: Any) -> float:
    items = list(values)
    return sum(items) / len(items) if items else 0.0


def _safe(call: Any, what: str) -> None:
    try:
        call()
    except Exception as exc:
        logger.warning("LangSmith %s failed: %r", what, exc)
