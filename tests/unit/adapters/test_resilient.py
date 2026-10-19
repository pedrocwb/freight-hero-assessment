import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.clock import VirtualClock
from freight_agent.adapters.llm.resilient import (
    AllModelsFailed,
    BreakerState,
    BreakerStatus,
    ResilientModel,
)
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import (
    LanguageModel,
    LlmError,
    LlmRequest,
    LlmResponse,
    ModelRole,
    SchemaT,
)

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=LA)
GATE = LlmRequest(role=ModelRole.GATE, system="s", user="u")
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")


def _models(*names: str) -> list[ScriptedModel]:
    return [ScriptedModel(model_name=name) for name in names]


def _resilient(
    models: Sequence[LanguageModel],
    clock: VirtualClock,
    names: Sequence[str] = ("primary", "backup", "third"),
    **options: Any,
) -> ResilientModel:
    return ResilientModel(list(zip(names, models, strict=False)), clock, **options)


def _state(model: ResilientModel, name: str) -> BreakerStatus:
    return next(s for s in model.status() if s.name == name)


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(T0)


# --- fallback ----------------------------------------------------------------


@pytest.mark.unit
async def test_the_first_healthy_model_answers(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, ACK)
    model = _resilient([primary, backup], clock)

    response = await model.generate(GATE, GateDecision)

    assert response.parsed == ACK
    assert response.usage.model == "primary"
    assert backup.calls == []


@pytest.mark.unit
async def test_falls_back_in_chain_order_on_model_failures(clock: VirtualClock) -> None:
    primary, backup, third = _models("primary", "backup", "third")
    primary.push(ModelRole.GATE, LlmError("rate limited"))
    backup.push(ModelRole.GATE, LlmError("timeout"))
    third.push(ModelRole.GATE, ACK)
    model = _resilient([primary, backup, third], clock)

    response = await model.generate(GATE, GateDecision)

    assert response.usage.model == "third"
    assert [len(m.calls) for m in (primary, backup, third)] == [1, 1, 1]


@pytest.mark.unit
async def test_every_model_failing_raises_one_llm_error_naming_each(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, LlmError("rate limited"))
    backup.push(ModelRole.GATE, LlmError("timeout"))
    model = _resilient([primary, backup], clock)

    with pytest.raises(AllModelsFailed) as caught:
        await model.generate(GATE, GateDecision)

    assert isinstance(caught.value, LlmError)  # the runtime escalates it like any model failure
    assert caught.value.failures == (
        ("primary", "LlmError: rate limited"),
        ("backup", "LlmError: timeout"),
    )
    message = str(caught.value)
    assert "primary: LlmError: rate limited" in message
    assert "backup: LlmError: timeout" in message


@pytest.mark.unit
async def test_unexpected_errors_propagate_without_fallback(clock: VirtualClock) -> None:
    """A bug or a missing cassette is not a provider failure: no fallback, no breaker change."""
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, RuntimeError("bug"))
    backup.push(ModelRole.GATE, ACK)
    model = _resilient([primary, backup], clock, failure_threshold=1)

    with pytest.raises(RuntimeError, match="bug"):
        await model.generate(GATE, GateDecision)

    assert backup.calls == []
    assert _state(model, "primary").state is BreakerState.CLOSED
    assert _state(model, "primary").consecutive_failures == 0


# --- breaker -----------------------------------------------------------------


@pytest.mark.unit
async def test_breaker_opens_after_consecutive_failures_and_skips_the_model(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, *[LlmError("down")] * 3)
    backup.push(ModelRole.GATE, *[ACK] * 4)
    model = _resilient([primary, backup], clock, failure_threshold=3)

    for _ in range(3):
        await model.generate(GATE, GateDecision)
    assert _state(model, "primary").state is BreakerState.OPEN

    response = await model.generate(GATE, GateDecision)

    assert response.usage.model == "backup"
    assert len(primary.calls) == 3  # the open breaker kept the fourth call away
    assert len(backup.calls) == 4


@pytest.mark.unit
async def test_a_success_resets_the_consecutive_failures(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, LlmError("a"), LlmError("b"), ACK, LlmError("c"), LlmError("d"))
    backup.push(ModelRole.GATE, *[ACK] * 4)
    model = _resilient([primary, backup], clock, failure_threshold=3)

    for _ in range(5):
        await model.generate(GATE, GateDecision)

    status = _state(model, "primary")
    assert status.state is BreakerState.CLOSED
    assert status.consecutive_failures == 2
    assert len(primary.calls) == 5


@pytest.mark.unit
async def test_open_breaker_half_opens_after_the_cooldown_and_closes_on_success(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, LlmError("down"))
    backup.push(ModelRole.GATE, ACK, ACK)
    model = _resilient([primary, backup], clock, failure_threshold=1, cooldown_s=60)
    await model.generate(GATE, GateDecision)

    clock.advance(timedelta(seconds=59))
    assert _state(model, "primary").state is BreakerState.OPEN
    await model.generate(GATE, GateDecision)
    assert len(primary.calls) == 1

    clock.advance(timedelta(seconds=1))
    assert _state(model, "primary").state is BreakerState.HALF_OPEN
    primary.push(ModelRole.GATE, ACK)
    response = await model.generate(GATE, GateDecision)

    assert response.usage.model == "primary"
    status = _state(model, "primary")
    assert status.state is BreakerState.CLOSED
    assert status.consecutive_failures == 0
    assert status.retry_at is None


@pytest.mark.unit
async def test_a_failed_trial_reopens_the_breaker_for_another_cooldown(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, *[LlmError("down")] * 3)
    backup.push(ModelRole.GATE, *[ACK] * 5)
    model = _resilient([primary, backup], clock, failure_threshold=3, cooldown_s=60)
    for _ in range(3):
        await model.generate(GATE, GateDecision)

    clock.advance(timedelta(seconds=60))
    primary.push(ModelRole.GATE, LlmError("still down"))
    await model.generate(GATE, GateDecision)  # the one trial fails: open again at once

    status = _state(model, "primary")
    assert status.state is BreakerState.OPEN
    assert status.retry_at == clock.now() + timedelta(seconds=60)
    assert status.last_error == "LlmError: still down"
    clock.advance(timedelta(seconds=30))
    await model.generate(GATE, GateDecision)
    assert len(primary.calls) == 4


class _FailsThenBlocks:
    """Fails `failures` times, then blocks each call until released."""

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        self.calls += 1
        if self._failures:
            self._failures -= 1
            raise LlmError("down")
        self.started.set()
        await self.release.wait()
        return LlmResponse(
            parsed=schema.model_validate(ACK.model_dump()),
            usage=Usage(role=request.role.value, model="blocking"),
        )


@pytest.mark.unit
async def test_half_open_lets_a_single_trial_call_through(clock: VirtualClock) -> None:
    primary = _FailsThenBlocks(failures=1)
    (backup,) = _models("backup")
    backup.push(ModelRole.GATE, ACK, ACK)
    model = _resilient([primary, backup], clock, failure_threshold=1, cooldown_s=60)
    await model.generate(GATE, GateDecision)
    clock.advance(timedelta(seconds=60))

    trial = asyncio.create_task(model.generate(GATE, GateDecision))
    await primary.started.wait()
    concurrent = await model.generate(GATE, GateDecision)  # the trial is still in flight
    primary.release.set()
    trial_response = await trial

    assert concurrent.usage.model == "backup"
    assert trial_response.usage.model == "blocking"
    assert primary.calls == 2
    assert _state(model, "primary").state is BreakerState.CLOSED


@pytest.mark.unit
async def test_a_trial_ended_by_an_unexpected_error_stays_half_open(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, LlmError("down"), RuntimeError("bug"), ACK)
    backup.push(ModelRole.GATE, ACK)
    model = _resilient([primary, backup], clock, failure_threshold=1, cooldown_s=60)
    await model.generate(GATE, GateDecision)
    clock.advance(timedelta(seconds=60))

    with pytest.raises(RuntimeError):
        await model.generate(GATE, GateDecision)
    assert _state(model, "primary").state is BreakerState.HALF_OPEN

    response = await model.generate(GATE, GateDecision)  # the trial slot was released
    assert response.usage.model == "primary"
    assert _state(model, "primary").state is BreakerState.CLOSED


@pytest.mark.unit
async def test_open_breakers_are_named_in_the_aggregated_error(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, LlmError("down"))
    backup.push(ModelRole.GATE, LlmError("down"))
    model = _resilient([primary, backup], clock, failure_threshold=1, cooldown_s=60)
    with pytest.raises(AllModelsFailed):
        await model.generate(GATE, GateDecision)

    with pytest.raises(AllModelsFailed) as caught:
        await model.generate(GATE, GateDecision)

    retry_at = (T0 + timedelta(seconds=60)).isoformat()
    assert caught.value.failures == (
        ("primary", f"circuit open until {retry_at}"),
        ("backup", f"circuit open until {retry_at}"),
    )
    assert len(primary.calls) == len(backup.calls) == 1


# --- health ------------------------------------------------------------------


@pytest.mark.unit
async def test_status_reports_every_breaker_in_chain_order(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    model = _resilient([primary, backup], clock, failure_threshold=2, cooldown_s=30)
    healthy = BreakerStatus(
        name="primary",
        state=BreakerState.CLOSED,
        consecutive_failures=0,
        last_error=None,
        retry_at=None,
    )
    assert model.status() == (healthy, replace(healthy, name="backup"))

    primary.push(ModelRole.GATE, LlmError("a"), LlmError("b"))
    backup.push(ModelRole.GATE, ACK, ACK)
    await model.generate(GATE, GateDecision)
    await model.generate(GATE, GateDecision)

    assert model.status()[0] == BreakerStatus(
        name="primary",
        state=BreakerState.OPEN,
        consecutive_failures=2,
        last_error="LlmError: b",
        retry_at=T0 + timedelta(seconds=30),
    )
    assert model.status()[1].state is BreakerState.CLOSED


# --- construction ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("names", "options", "message"),
    [
        ((), {}, "at least one model"),
        (("a", "a"), {}, "duplicate"),
        (("a",), {"failure_threshold": 0}, "failure_threshold"),
        (("a",), {"cooldown_s": -1}, "cooldown_s"),
    ],
)
def test_invalid_configuration_is_rejected(
    names: tuple[str, ...], options: dict[str, Any], message: str, clock: VirtualClock
) -> None:
    with pytest.raises(ValueError, match=message):
        _resilient(_models(*names), clock, names=names, **options)
