import asyncio
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.observability.langsmith import (
    LangSmithClient,
    LangSmithNotConfigured,
    LangSmithTracer,
    langsmith_client_factory,
    tracer_from_env,
)
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.domain.actions import Plan, Purpose, SendSms
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import RunRecord, RunStatus, TrajectoryStep, Usage
from freight_agent.domain.validation import Severity, Violation
from freight_agent.ports.tracing import Tracer

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 1, tzinfo=LA)
URL = "https://smith.langchain.com/o/org/projects/p/proj/r/run?poll=true"
PLAN = Plan(
    actions=(
        SendSms(contact_id="drv_20931", text="What's your ETA?", purpose=Purpose.ETA_REQUEST),
    ),
    rationale="SOP delay_risk_high step 1",
)
HIGH = Violation(
    rule="no_contact_info", action_index=1, message="phone number", severity=Severity.HIGH
)


class FakeClient:
    """Records what the SDK would send. `get_run_url` answers like the real client."""

    def __init__(self, *, fail_create: bool = False, fail_url: bool = False) -> None:
        self.runs: list[dict[str, Any]] = []
        self.url_requests: list[tuple[Any, str | None]] = []
        self.threads: set[int] = set()
        self.release = threading.Event()
        self.release.set()
        self._fail_create = fail_create
        self._fail_url = fail_url

    def create_run(self, name: str, inputs: dict[str, Any], run_type: Any, **kwargs: Any) -> None:
        self.threads.add(threading.get_ident())
        self.release.wait(timeout=5)
        if self._fail_create:
            raise ConnectionError("langsmith is down")
        self.runs.append({"name": name, "inputs": inputs, "run_type": run_type, **kwargs})

    def get_run_url(self, *, run: Any, project_name: str | None = None) -> str:
        if self._fail_url:
            raise RuntimeError("project not found")
        self.url_requests.append((run, project_name))
        return URL

    @property
    def root(self) -> dict[str, Any]:
        (root,) = [r for r in self.runs if "parent_run_id" not in r]
        return root

    def child(self, name: str) -> dict[str, Any]:
        (child,) = [r for r in self.runs if r["name"] == name]
        return child


class Factory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls = 0

    def __call__(self) -> LangSmithClient:
        self.calls += 1
        return self.client


def _run(**overrides: Any) -> RunRecord:
    fields: dict[str, Any] = {
        "run_id": "run_1",
        "event_id": "risk:p1",
        "load_id": "481207",
        "started_at": T0,
        "finished_at": T0 + timedelta(seconds=6),
        "status": RunStatus.ACTED,
        "gate": GateDecision(intent=Intent.DELAY_RISK_HIGH, act=True, reason="risk is high"),
        "sop_section_ids": ("communication_rules", "delay_risk_high"),
        "plan": PLAN,
        "violations": (),
        "usage": (
            Usage(
                role="gate",
                model="gate-model",
                input_tokens=100,
                output_tokens=20,
                cached_tokens=50,
                cost_usd=0.001,
                latency_ms=800,
            ),
            Usage(
                role="decide",
                model="decide-model",
                input_tokens=1000,
                output_tokens=200,
                cached_tokens=600,
                cost_usd=0.01,
                latency_ms=3000,
            ),
        ),
    }
    fields.update(overrides)
    return RunRecord(**fields)


def _tracer(client: FakeClient, **options: Any) -> LangSmithTracer:
    return LangSmithTracer(Factory(client), project_name="freight-test", **options)


async def _broker(load_id: str) -> str | None:
    return "northline"


# --- outcome -------------------------------------------------------------------


@pytest.mark.unit
async def test_a_run_exports_one_trace_and_returns_its_url() -> None:
    client = FakeClient()
    tracer: Tracer = _tracer(client)
    assert await tracer.record_run(_run()) == URL
    assert sorted(r["name"] for r in client.runs) == sorted(
        ["risk:p1", "gate", "agent", "validate"]
    )
    (requested, project) = client.url_requests[0]
    assert requested.id == client.root["id"]
    assert project == "freight-test"


@pytest.mark.unit
async def test_the_export_runs_off_the_event_loop_thread() -> None:
    client = FakeClient()
    await _tracer(client).record_run(_run())
    assert threading.get_ident() not in client.threads


@pytest.mark.unit
async def test_the_client_is_built_once_and_reused() -> None:
    factory = Factory(FakeClient())
    tracer = LangSmithTracer(factory)
    await tracer.record_run(_run())
    await tracer.record_run(_run(run_id="run_2"))
    assert factory.calls == 1
    assert len(factory.client.runs) == 8


# --- fail-open -----------------------------------------------------------------


@pytest.mark.unit
async def test_an_sdk_error_is_swallowed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        assert await _tracer(FakeClient(fail_create=True)).record_run(_run()) is None
    assert "run_1" in caplog.text
    assert "langsmith is down" in caplog.text


@pytest.mark.unit
async def test_a_failed_url_lookup_still_exports_the_trace(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient(fail_url=True)
    with caplog.at_level(logging.WARNING):
        assert await _tracer(client).record_run(_run()) is None
    assert len(client.runs) == 4
    assert "project not found" in caplog.text


@pytest.mark.unit
async def test_a_missing_api_key_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    tracer = LangSmithTracer(langsmith_client_factory(api_key=None))
    with caplog.at_level(logging.WARNING):
        assert await tracer.record_run(_run()) is None
    assert "LANGSMITH_API_KEY" in caplog.text


@pytest.mark.unit
def test_the_default_factory_refuses_a_blank_key() -> None:
    with pytest.raises(LangSmithNotConfigured):
        langsmith_client_factory(api_key="  ")()


@pytest.mark.unit
async def test_a_factory_failure_is_retried_on_the_next_run() -> None:
    client = FakeClient()
    attempts: list[int] = []

    def flaky() -> LangSmithClient:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no network yet")
        return client

    tracer = LangSmithTracer(flaky)
    assert await tracer.record_run(_run()) is None
    assert await tracer.record_run(_run()) == URL


@pytest.mark.unit
async def test_a_slow_export_times_out_without_blocking_the_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient()
    client.release.clear()
    tracer = _tracer(client, timeout_s=0.05)
    try:
        with caplog.at_level(logging.WARNING):
            assert await asyncio.wait_for(tracer.record_run(_run()), timeout=1) is None
        assert "timed out" in caplog.text
    finally:
        client.release.set()
        tracer.close()


@pytest.mark.unit
async def test_exports_over_the_pending_limit_are_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient()
    client.release.clear()
    tracer = _tracer(client, timeout_s=0.05, max_pending=1)
    try:
        await tracer.record_run(_run())  # times out, still holds the only slot
        with caplog.at_level(logging.WARNING):
            assert await tracer.record_run(_run(run_id="run_2")) is None
        assert "dropped" in caplog.text
    finally:
        client.release.set()
        tracer.close()


@pytest.mark.unit
async def test_a_slot_frees_up_once_the_slow_export_finishes() -> None:
    client = FakeClient()
    client.release.clear()
    tracer = _tracer(client, timeout_s=0.05, max_pending=1)
    await tracer.record_run(_run())  # times out, holds the only slot
    client.release.set()  # the export finishes in its thread and frees the slot
    url = None
    for _ in range(100):
        if (url := await tracer.record_run(_run(run_id="run_2"))) is not None:
            break
        await asyncio.sleep(0.01)
    assert url == URL
    tracer.close()


@pytest.mark.unit
async def test_failing_metadata_lookups_do_not_stop_the_export() -> None:
    async def broken_broker(load_id: str) -> str | None:
        raise LookupError("no load")

    def broken_version() -> str | None:
        raise OSError("sops/ unreadable")

    client = FakeClient()
    tracer = _tracer(client, broker_of=broken_broker, sop_version=broken_version)
    assert await tracer.record_run(_run()) == URL
    metadata = client.root["extra"]["metadata"]
    assert "broker_id" not in metadata
    assert "sop_version" not in metadata


@pytest.mark.unit
async def test_closing_twice_is_harmless() -> None:
    tracer = _tracer(FakeClient())
    tracer.close()
    tracer.close()
    assert await tracer.record_run(_run()) is None  # closed: nothing is exported


# --- payload -------------------------------------------------------------------


@pytest.mark.unit
async def test_root_run_carries_the_event_inputs_and_the_run_outcome() -> None:
    client = FakeClient()
    run = _run(violations=(HIGH,), status=RunStatus.ESCALATED, error="high-severity violation")
    await _tracer(client).record_run(run)
    root = client.root
    assert root["name"] == "risk:p1"
    assert root["run_type"] == "chain"
    assert root["inputs"] == {"event_id": "risk:p1", "load_id": "481207", "run_id": "run_1"}
    assert root["outputs"]["status"] == "escalated"
    assert root["outputs"]["plan"] == PLAN.model_dump(mode="json")
    assert root["outputs"]["violations"] == [HIGH.model_dump(mode="json")]
    assert root["error"] == "high-severity violation"
    assert root["session_name"] == "freight-test"
    assert root["start_time"] == T0.astimezone(UTC)
    assert root["end_time"] == (T0 + timedelta(seconds=6)).astimezone(UTC)


@pytest.mark.unit
async def test_root_metadata_has_tokens_cost_sop_and_identity() -> None:
    client = FakeClient()
    tracer = _tracer(client, broker_of=_broker, sop_version=lambda: "sha256:abc")
    await tracer.record_run(_run())
    metadata = client.root["extra"]["metadata"]
    assert metadata["load_id"] == "481207"
    assert metadata["broker_id"] == "northline"
    assert metadata["run_id"] == "run_1"
    assert metadata["event_id"] == "risk:p1"
    assert metadata["models"] == ["decide-model", "gate-model"]
    assert metadata["sop_version"] == "sha256:abc"
    assert metadata["sop_section_ids"] == ["communication_rules", "delay_risk_high"]
    assert metadata["input_tokens"] == 1100
    assert metadata["output_tokens"] == 220
    assert metadata["cached_tokens"] == 650
    assert metadata["cost_usd"] == pytest.approx(0.011)
    assert set(client.root["tags"]) == {
        "freight-agent",
        "status:acted",
        "intent:delay_risk_high",
        "load:481207",
        "broker:northline",
        "sop:communication_rules",
        "sop:delay_risk_high",
    }


@pytest.mark.unit
async def test_gate_child_is_an_llm_run_with_its_decision_and_usage() -> None:
    client = FakeClient()
    await _tracer(client).record_run(_run())
    gate = client.child("gate")
    assert gate["run_type"] == "llm"
    assert gate["parent_run_id"] == client.root["id"]
    assert gate["trace_id"] == client.root["id"]
    assert gate["inputs"] == {"event_id": "risk:p1"}
    assert gate["outputs"] == {
        "intent": "delay_risk_high",
        "act": True,
        "reason": "risk is high",
    }
    metadata = gate["extra"]["metadata"]
    assert metadata["ls_model_name"] == "gate-model"
    assert metadata["usage_metadata"] == {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "input_token_details": {"cache_read": 50},
        "total_cost": pytest.approx(0.001),
    }
    assert gate["start_time"] == T0.astimezone(UTC)
    assert gate["end_time"] == (T0 + timedelta(milliseconds=800)).astimezone(UTC)


@pytest.mark.unit
async def test_agent_and_validate_children_follow_the_gate() -> None:
    client = FakeClient()
    await _tracer(client).record_run(_run(violations=(HIGH,)))
    decide = client.child("agent")  # a record from before wave 3: its role says decide
    assert decide["run_type"] == "llm"
    assert decide["inputs"] == {"sop_section_ids": ["communication_rules", "delay_risk_high"]}
    assert decide["outputs"] == {
        "intent": "delay_risk_high",
        "plan": PLAN.model_dump(mode="json"),
        "sections_searched": [],
        "rejections": 0,
    }
    assert decide["extra"]["metadata"]["ls_model_name"] == "decide-model"
    assert decide["start_time"] == (T0 + timedelta(milliseconds=800)).astimezone(UTC)
    assert decide["end_time"] == (T0 + timedelta(milliseconds=3800)).astimezone(UTC)
    validate = client.child("validate")
    assert validate["run_type"] == "chain"
    assert validate["inputs"] == {"actions": 1}
    assert validate["outputs"] == {
        "violations": [HIGH.model_dump(mode="json")],
        "blocked_action_indexes": [1],
        "high_severity": True,
    }
    assert validate["start_time"] == decide["end_time"]


@pytest.mark.unit
async def test_child_spans_never_end_after_the_run() -> None:
    client = FakeClient()
    await _tracer(client).record_run(_run(finished_at=T0 + timedelta(seconds=1)))
    end = (T0 + timedelta(seconds=1)).astimezone(UTC)
    assert all(r["end_time"] <= end for r in client.runs)


@pytest.mark.unit
async def test_a_skipped_run_exports_only_the_root() -> None:
    client = FakeClient()
    run = _run(
        status=RunStatus.SKIPPED,
        gate=None,
        plan=None,
        usage=(),
        sop_section_ids=(),
        skip_reason="stale_timer",
        finished_at=None,
    )
    await _tracer(client).record_run(run)
    assert [r["name"] for r in client.runs] == ["risk:p1"]
    root = client.root
    assert root["outputs"]["skip_reason"] == "stale_timer"
    assert root["outputs"]["plan"] is None
    assert root["end_time"] == T0.astimezone(UTC)
    assert root["extra"]["metadata"]["models"] == []


@pytest.mark.unit
async def test_a_gate_without_a_model_call_is_a_chain_step() -> None:
    client = FakeClient()
    usage = (Usage(role="decide", model="decide-model", latency_ms=1000),)
    await _tracer(client).record_run(_run(usage=usage))
    gate = client.child("gate")
    assert gate["run_type"] == "chain"
    assert "usage_metadata" not in gate["extra"]["metadata"]
    assert gate["end_time"] == gate["start_time"]


@pytest.mark.unit
async def test_several_calls_of_one_role_are_summed_on_its_step() -> None:
    client = FakeClient()
    usage = (
        Usage(role="gate", model="m1", input_tokens=10, output_tokens=1, cost_usd=0.1),
        Usage(role="gate", model="m2", input_tokens=20, output_tokens=2, cost_usd=0.2),
    )
    await _tracer(client).record_run(_run(usage=usage, plan=None))
    gate = client.child("gate")
    assert gate["extra"]["metadata"]["ls_model_name"] == "m1, m2"
    assert gate["extra"]["metadata"]["usage_metadata"]["total_tokens"] == 33
    assert gate["extra"]["metadata"]["usage_metadata"]["total_cost"] == pytest.approx(0.3)
    assert sorted(r["name"] for r in client.runs) == ["gate", "risk:p1"]


@pytest.mark.unit
async def test_a_run_without_a_plan_has_no_decide_or_validate_step() -> None:
    client = FakeClient()
    usage = (Usage(role="gate", model="gate-model", latency_ms=500),)
    run = _run(usage=usage, plan=None, status=RunStatus.ESCALATED, error="LlmError: timeout")
    await _tracer(client).record_run(run)
    assert sorted(r["name"] for r in client.runs) == ["gate", "risk:p1"]
    assert client.root["error"] == "LlmError: timeout"


# --- the agent loop (wave 3) ---------------------------------------------------

TRAJECTORY = (
    TrajectoryStep(
        tool="search_sop", args={"query": "lumper"}, result="1. escalation", accepted=None
    ),
    TrajectoryStep(
        tool="send_email",
        args={"to_contact_ids": ["x@outside.example"]},
        result="rejected by recipient_is_contact",
        accepted=False,
        cost_usd=0.002,
    ),
    TrajectoryStep(
        tool="create_hero_task", args={"title": "t"}, result="accepted as action #1", accepted=True
    ),
)


def _loop_run(**overrides: Any) -> RunRecord:
    usage = (
        Usage(role="gate", model="gate-model", latency_ms=800),
        Usage(role="agent", model="agent-model", latency_ms=3000, cost_usd=0.01),
    )
    fields: dict[str, Any] = {
        "usage": usage,
        "intent": "other",
        "trajectory": TRAJECTORY,
        "rejections": 1,
        "sections_searched": ("escalation",),
        "broker_id": "acme",
    }
    return _run(**{**fields, **overrides})


@pytest.mark.unit
async def test_each_tool_call_is_a_tool_run_under_the_agent_step_in_order() -> None:
    client = FakeClient()
    await _tracer(client).record_run(_loop_run())
    agent = client.child("agent")
    assert agent["extra"]["metadata"]["ls_model_name"] == "agent-model"
    tools = [r for r in client.runs if r.get("parent_run_id") == agent["id"]]
    assert [(t["name"], t["run_type"]) for t in tools] == [
        ("search_sop", "tool"),
        ("send_email", "tool"),
        ("create_hero_task", "tool"),
    ]
    search, email, hero = tools
    assert search["inputs"] == {"query": "lumper"}
    assert search["outputs"] == {"result": "1. escalation", "accepted": None}
    assert email["error"] == "refused: rejected by recipient_is_contact"
    assert email["tags"] == ["refused"]
    email_metadata = email["extra"]["metadata"]
    assert (email_metadata["accepted"], email_metadata["cost_usd"]) == (False, 0.002)
    assert hero.get("error") is None and hero["outputs"]["accepted"] is True
    # The calls share the agent's span, end to end.
    assert search["start_time"] == agent["start_time"]
    assert search["end_time"] == email["start_time"]
    assert hero["end_time"] == agent["end_time"]


@pytest.mark.unit
async def test_the_root_carries_the_intent_broker_and_refusals() -> None:
    client = FakeClient()
    await _tracer(client, broker_of=_broker).record_run(_loop_run())
    root = client.root
    metadata = root["extra"]["metadata"]
    assert metadata["intent"] == "other"
    assert metadata["tools"] == ["search_sop", "send_email", "create_hero_task"]
    assert metadata["rejections"] == 1
    assert metadata["sections_searched"] == ["escalation"]
    assert metadata["broker_id"] == "acme"
    assert "broker_config_version" not in metadata
    assert (root["outputs"]["intent"], root["outputs"]["rejections"]) == ("other", 1)
    assert {"intent:other", "broker:acme", "refused"} <= set(root["tags"])
    assert "intent:delay_risk_high" not in root["tags"]


@pytest.mark.unit
async def test_an_agent_run_with_a_trajectory_and_no_plan_still_shows_its_calls() -> None:
    client = FakeClient()
    await _tracer(client).record_run(_loop_run(plan=None))
    names = sorted(r["name"] for r in client.runs)
    assert names == sorted(
        ["risk:p1", "gate", "agent", "search_sop", "send_email", "create_hero_task"]
    )


# --- choosing a tracer ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "env",
    [
        {},
        {"LANGSMITH_TRACING": "true"},
        {"LANGSMITH_TRACING": "true", "LANGSMITH_API_KEY": " "},
        {"LANGSMITH_TRACING": "false", "LANGSMITH_API_KEY": "lsv2_key"},
        {"LANGSMITH_API_KEY": "lsv2_key"},
    ],
)
def test_tracing_off_or_without_a_key_uses_the_null_tracer(env: dict[str, str]) -> None:
    assert isinstance(tracer_from_env(env), NullTracer)


@pytest.mark.unit
@pytest.mark.parametrize("flag", ["true", "TRUE", "1", "yes", "on"])
def test_tracing_on_with_a_key_uses_langsmith(flag: str) -> None:
    env = {"LANGSMITH_TRACING": flag, "LANGSMITH_API_KEY": "lsv2_key"}
    tracer = tracer_from_env(env)
    assert isinstance(tracer, LangSmithTracer)
    tracer.close()


@pytest.mark.unit
def test_the_env_names_the_project() -> None:
    env = {
        "LANGSMITH_TRACING": "true",
        "LANGSMITH_API_KEY": "lsv2_key",
        "LANGSMITH_PROJECT": "freight-prod",
    }
    tracer = tracer_from_env(env)
    assert isinstance(tracer, LangSmithTracer)
    assert tracer.project_name == "freight-prod"
    tracer.close()


@pytest.mark.unit
async def test_the_null_tracer_returns_no_url() -> None:
    assert await NullTracer().record_run(_run()) is None
