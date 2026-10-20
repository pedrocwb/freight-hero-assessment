"""LangSmith tracer: one trace per agent run, exported off the event loop, failing open.

Our `runs` and `events` tables are the source of truth for monitoring; a trace is a
convenience for reading one run in the LangSmith UI. So the export must never change
what the agent does: every error (no API key, network, SDK) is logged at warning level
and `record_run` returns None, and the export runs in a small worker pool bounded by a
pending limit and a timeout, so a slow or down LangSmith costs a run at most `timeout_s`.

The trace is built from the finished `RunRecord`, not by instrumenting the graph: a root
run named after the event, with child runs for the gate, agent and validate steps the
record has, and under the agent step one `tool` run per call of its trajectory (the
arguments, what the tool answered, whether a rule accepted or refused an action). The
record keeps no per-step timestamps, so the child spans are laid end to end from each
model call's latency, inside the run's own span, and the tool calls share the agent's
span in order. The root carries the run's intent, broker and refused calls.

Opik would implement the same `Tracer` port the same way: an `OpikTracer` that builds
one `opik.Opik().trace(...)` per run with a span per step (`trace.span(name=..., type=
"llm", usage=..., model=...)`), offloaded and bounded like this one, failing open, and
chosen by the composition root instead of this class.
"""

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from langsmith import Client
from langsmith.run_trees import RunTree
from langsmith.schemas import ExtractedUsageMetadata, InputTokenDetails

from freight_agent.adapters.observability.null import NullTracer
from freight_agent.domain.runs import RunRecord, TrajectoryStep, Usage
from freight_agent.domain.validation import Severity
from freight_agent.ports.tracing import Tracer

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "freight-agent"
_TRUE = frozenset({"1", "true", "yes", "on"})


class LangSmithClient(Protocol):
    """The slice of `langsmith.Client` the tracer uses; a fake implements it in tests.

    `RunTree.post` calls `create_run` once per run of the tree. `get_run_url` is
    deprecated in favour of `client.runs.get_url` (LangSmith backend 0.16+), but it is
    what works across backends today; when it fails, only the link is lost.
    """

    def create_run(
        self, name: str, inputs: dict[str, Any], run_type: Any, **kwargs: Any
    ) -> None: ...

    def get_run_url(self, *, run: Any, project_name: str | None = None) -> str: ...


ClientFactory = Callable[[], LangSmithClient]
BrokerLookup = Callable[[str], Awaitable[str | None]]


class LangSmithNotConfigured(RuntimeError):
    """No API key: the hosted LangSmith refuses every run without one."""


def langsmith_client_factory(api_key: str | None, api_url: str | None = None) -> ClientFactory:
    """A factory for the real SDK client. It checks the key itself, so a missing key is
    one clear warning instead of an SDK error at send time."""

    def build() -> LangSmithClient:
        if api_key is None or not api_key.strip():
            raise LangSmithNotConfigured("LANGSMITH_API_KEY is not set")
        return Client(api_key=api_key.strip(), api_url=api_url)

    return build


class LangSmithTracer:
    """Implements `ports.tracing.Tracer`.

    `sop_version` and `broker_of` add what the run record does not carry (the SOP
    content hash, the load's broker); each is optional and fails open on its own.
    """

    def __init__(
        self,
        client_factory: ClientFactory,
        *,
        project_name: str = DEFAULT_PROJECT,
        timeout_s: float = 2.0,
        max_pending: int = 32,
        workers: int = 2,
        sop_version: Callable[[], str | None] | None = None,
        broker_of: BrokerLookup | None = None,
    ) -> None:
        self._factory = client_factory
        self._project = project_name
        self._timeout_s = timeout_s
        self._sop_version = sop_version
        self._broker_of = broker_of
        self._client: LangSmithClient | None = None
        self._client_lock = threading.Lock()
        # Exports queued or running. Over the limit a trace is dropped, so a LangSmith
        # outage cannot pile up threads or memory behind the agent.
        self._slots = threading.BoundedSemaphore(max_pending)
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="langsmith")

    @property
    def project_name(self) -> str:
        return self._project

    async def record_run(self, run: RunRecord) -> str | None:
        try:
            return await asyncio.wait_for(self._record(run), timeout=self._timeout_s)
        except TimeoutError:
            # The export may still finish in its thread; only the link is lost.
            logger.warning("LangSmith export of run %s timed out", run.run_id)
        except Exception as exc:
            logger.warning("LangSmith export of run %s failed: %r", run.run_id, exc)
        return None

    def close(self) -> None:
        """Stop the workers. Queued exports are dropped; later runs export nothing."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _record(self, run: RunRecord) -> str | None:
        broker_id = await self._broker(run)
        if not self._slots.acquire(blocking=False):
            logger.warning("LangSmith export of run %s dropped: too many pending", run.run_id)
            return None
        try:
            future: Future[str | None] = self._executor.submit(self._export, run, broker_id)
        except BaseException:
            self._slots.release()
            raise
        # Released when the thread finishes, or when a queued export is cancelled.
        future.add_done_callback(lambda _: self._slots.release())
        return await asyncio.wrap_future(future)

    async def _broker(self, run: RunRecord) -> str | None:
        if self._broker_of is None:
            return None
        try:
            return await self._broker_of(run.load_id)
        except Exception as exc:
            logger.warning("broker lookup for run %s failed: %r", run.run_id, exc)
            return None

    def _version(self, run: RunRecord) -> str | None:
        if self._sop_version is None:
            return None
        try:
            return self._sop_version()
        except Exception as exc:
            logger.warning("SOP version for run %s failed: %r", run.run_id, exc)
            return None

    def _get_client(self) -> LangSmithClient:
        with self._client_lock:
            if self._client is None:
                self._client = self._factory()  # not cached when it raises: retried next run
            return self._client

    def _export(self, run: RunRecord, broker_id: str | None) -> str | None:
        """Runs in a worker thread: builds the tree, sends it, then asks for its link."""
        client = self._get_client()
        root = build_trace(
            run,
            client=client,
            project_name=self._project,
            broker_id=broker_id,
            sop_version=self._version(run),
        )
        root.post(exclude_child_runs=False)
        try:
            return client.get_run_url(run=root, project_name=self._project)
        except Exception as exc:
            logger.warning("LangSmith URL for run %s unavailable: %r", run.run_id, exc)
            return None


# --- the trace -------------------------------------------------------------------


def build_trace(
    run: RunRecord,
    *,
    client: LangSmithClient,
    project_name: str,
    broker_id: str | None = None,
    sop_version: str | None = None,
) -> RunTree:
    """The run as a LangSmith run tree. Pure: nothing is sent until `post`."""
    start = run.started_at.astimezone(UTC)
    end = (run.finished_at or run.started_at).astimezone(UTC)
    root = RunTree(
        name=run.event_id,
        run_type="chain",
        start_time=start,
        end_time=end,
        inputs={"event_id": run.event_id, "load_id": run.load_id, "run_id": run.run_id},
        outputs={
            "status": run.status.value,
            "skip_reason": run.skip_reason,
            "plan": None if run.plan is None else run.plan.model_dump(mode="json"),
            "violations": [v.model_dump(mode="json") for v in run.violations],
            "intent": _intent(run),
            "rejections": run.rejections,
        },
        error=run.error,
        extra={"metadata": _root_metadata(run, broker_id, sop_version)},
        tags=_tags(run, broker_id),
        project_name=project_name,
        ls_client=client,
        replicas=[],  # send to this client only, whatever LANGSMITH_RUNS_ENDPOINTS says
    )
    cursor = start
    if run.gate is not None:
        cursor = _step(
            root,
            "gate",
            _role_usage(run, "gate"),
            start=cursor,
            run_end=end,
            inputs={"event_id": run.event_id},
            outputs=run.gate.model_dump(mode="json"),
        )
    if run.plan is not None or run.trajectory:
        agent, cursor = _child(
            root,
            "agent",
            _role_usage(run, *_AGENT_ROLES),
            start=cursor,
            run_end=end,
            inputs={"sop_section_ids": list(run.sop_section_ids)},
            outputs={
                "intent": _intent(run),
                "plan": None if run.plan is None else run.plan.model_dump(mode="json"),
                "sections_searched": list(run.sections_searched),
                "rejections": run.rejections,
            },
        )
        _tool_calls(agent, run.trajectory)
    if run.plan is not None:
        _step(
            root,
            "validate",
            (),
            start=cursor,
            run_end=end,
            inputs={"actions": len(run.plan.actions)},
            outputs={
                "violations": [v.model_dump(mode="json") for v in run.violations],
                "blocked_action_indexes": sorted({v.action_index for v in run.violations}),
                "high_severity": any(v.severity is Severity.HIGH for v in run.violations),
            },
        )
    return root


# The agent step's model calls: the role is `agent` since wave 3; records stored before it
# say `decide`.
_AGENT_ROLES = ("agent", "decide")


def _role_usage(run: RunRecord, *roles: str) -> tuple[Usage, ...]:
    return tuple(u for u in run.usage if u.role in roles)


def _intent(run: RunRecord) -> str | None:
    """The intent the run settled on: the agent's label, else the gate's."""
    if run.intent is not None:
        return run.intent
    return None if run.gate is None else run.gate.intent


def _step(
    root: RunTree,
    name: str,
    usage: Sequence[Usage],
    *,
    start: datetime,
    run_end: datetime,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
) -> datetime:
    """Add one child run; returns where the next step starts."""
    return _child(root, name, usage, start=start, run_end=run_end, inputs=inputs, outputs=outputs)[
        1
    ]


def _child(
    root: RunTree,
    name: str,
    usage: Sequence[Usage],
    *,
    start: datetime,
    run_end: datetime,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
) -> tuple[RunTree, datetime]:
    """Add one child run; returns it and where the next step starts. A step with model
    calls is an `llm` run carrying LangSmith's usage metadata, so the UI shows tokens and
    cost."""
    end = min(start + timedelta(milliseconds=sum(u.latency_ms for u in usage)), run_end)
    metadata: dict[str, Any] = {}
    if usage:
        metadata["ls_model_name"] = ", ".join(dict.fromkeys(u.model for u in usage))
    child = root.create_child(
        name,
        "llm" if usage else "chain",
        inputs=inputs,
        outputs=outputs,
        start_time=start,
        end_time=end,
        extra={"metadata": metadata},
    )
    if usage:
        child.set(usage_metadata=_usage_metadata(usage))
    return child, end


def _tool_calls(agent: RunTree, trajectory: Sequence[TrajectoryStep]) -> None:
    """One `tool` run per call, in order, sharing the agent step's span. A call a rule
    refused is an error run, so the trace shows it in red."""
    if not trajectory:
        return
    start, end = agent.start_time, agent.end_time or agent.start_time
    slice_ = (end - start) / len(trajectory)
    for index, call in enumerate(trajectory):
        refused = call.accepted is False
        agent.create_child(
            call.tool,
            "tool",
            inputs=dict(call.args),
            outputs={"result": call.result, "accepted": call.accepted},
            start_time=start + slice_ * index,
            end_time=start + slice_ * (index + 1),
            error=f"refused: {call.result}" if refused else None,
            tags=["refused"] if refused else [],
            extra={"metadata": {"accepted": call.accepted, "cost_usd": call.cost_usd}},
        )


def _usage_metadata(usage: Sequence[Usage]) -> ExtractedUsageMetadata:
    input_tokens = sum(u.input_tokens for u in usage)
    output_tokens = sum(u.output_tokens for u in usage)
    return ExtractedUsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_details=InputTokenDetails(cache_read=sum(u.cached_tokens for u in usage)),
        total_cost=sum(u.cost_usd for u in usage),
    )


def _root_metadata(
    run: RunRecord, broker_id: str | None, sop_version: str | None
) -> dict[str, Any]:
    """Plain keys, not `usage_metadata`: LangSmith already sums the llm children into the
    trace's totals, and a root usage would count them twice."""
    metadata: dict[str, Any] = {
        "run_id": run.run_id,
        "event_id": run.event_id,
        "load_id": run.load_id,
        "status": run.status.value,
        "models": sorted({u.model for u in run.usage}),
        "sop_section_ids": list(run.sop_section_ids),
        "intent": _intent(run),
        "tools": [call.tool for call in run.trajectory],
        "rejections": run.rejections,
        "sections_searched": list(run.sections_searched),
        "input_tokens": sum(u.input_tokens for u in run.usage),
        "output_tokens": sum(u.output_tokens for u in run.usage),
        "cached_tokens": sum(u.cached_tokens for u in run.usage),
        "cost_usd": run.cost_usd,
    }
    broker_id = run.broker_id or broker_id
    if broker_id is not None:
        metadata["broker_id"] = broker_id
    if sop_version is not None:
        metadata["sop_version"] = sop_version
    return metadata


def _tags(run: RunRecord, broker_id: str | None) -> list[str]:
    tags = ["freight-agent", f"status:{run.status.value}", f"load:{run.load_id}"]
    intent = _intent(run)
    if intent is not None:
        tags.append(f"intent:{intent}")
    broker_id = run.broker_id or broker_id
    if broker_id is not None:
        tags.append(f"broker:{broker_id}")
    if run.rejections:
        tags.append("refused")
    tags.extend(f"sop:{section_id}" for section_id in run.sop_section_ids)
    return tags


# --- choosing a tracer -----------------------------------------------------------


def tracer_from_env(
    env: Mapping[str, str] | None = None,
    *,
    sop_version: Callable[[], str | None] | None = None,
    broker_of: BrokerLookup | None = None,
) -> Tracer:
    """The tracer the composition root should use.

    LangSmith only when `LANGSMITH_TRACING` is true (the SDK's own switch) and
    `LANGSMITH_API_KEY` is set; otherwise `NullTracer`, so a fresh checkout and the test
    suite never try to reach the network. `LANGSMITH_PROJECT` names the project and
    `LANGSMITH_ENDPOINT` points at a self-hosted instance.
    """
    env = os.environ if env is None else env
    api_key = env.get("LANGSMITH_API_KEY", "").strip()
    enabled = env.get("LANGSMITH_TRACING", "").strip().lower() in _TRUE
    if not enabled or not api_key:
        return NullTracer()
    return LangSmithTracer(
        langsmith_client_factory(api_key, env.get("LANGSMITH_ENDPOINT") or None),
        project_name=env.get("LANGSMITH_PROJECT") or DEFAULT_PROJECT,
        sop_version=sop_version,
        broker_of=broker_of,
    )
