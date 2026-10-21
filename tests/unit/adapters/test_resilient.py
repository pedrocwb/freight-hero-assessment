"""The fallback chain, its circuit breakers and its retries, over fake models on a virtual
clock, with the backoff's waits on a fake timer.

Both methods of the port walk the chain the same way: a forced answer (`generate`) and
an agent round (`chat`) share each model's breaker.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
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
    RetryPolicy,
)
from freight_agent.domain.actions import AgentOutcome
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import (
    AssistantTurn,
    ChatReply,
    ChatRequest,
    LanguageModel,
    LlmError,
    LlmRequest,
    LlmResponse,
    ModelRole,
    SchemaT,
    ToolResult,
    TransientLlmError,
)
from tests.fakes import FakeModel, Round, call

pytestmark = pytest.mark.unit

LA = ZoneInfo("America/Los_Angeles")
T0 = datetime(2026, 10, 13, 7, 0, tzinfo=LA)
GATE = LlmRequest(role=ModelRole.GATE, system="s", user="u")
ROUND = ChatRequest(role=ModelRole.AGENT, system="s", user="u")
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")
DONE = AgentOutcome(rationale="nothing to do")


def _models(*names: str) -> list[FakeModel]:
    return [FakeModel(name=name) for name in names]


@dataclass
class FakeTime:
    """The backoff's wall clock: `sleep` records each wait and moves `now` by it."""

    now: float = 0.0
    waits: list[float] = field(default_factory=list[float])

    def timer(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def _resilient(
    models: Sequence[LanguageModel],
    clock: VirtualClock,
    names: Sequence[str] = ("primary", "backup", "third"),
    time: FakeTime | None = None,
    **options: Any,
) -> ResilientModel:
    """Jitter at 0.5, so a wait is three quarters of its backoff ceiling."""
    time = time or FakeTime()
    return ResilientModel(
        list(zip(names, models, strict=False)),
        clock,
        sleep=time.sleep,
        timer=time.timer,
        jitter=lambda: 0.5,
        **options,
    )


def _state(model: ResilientModel, name: str) -> BreakerStatus:
    return next(s for s in model.status() if s.name == name)


@pytest.fixture
def clock() -> VirtualClock:
    return VirtualClock(T0)


# --- fallback ----------------------------------------------------------------


async def test_the_first_healthy_model_answers(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, ACK)
    model = _resilient([primary, backup], clock)

    response = await model.generate(GATE, GateDecision)

    assert response.parsed == ACK
    assert response.usage.model == "primary"
    assert backup.calls == []


async def test_falls_back_in_chain_order_on_model_failures(clock: VirtualClock) -> None:
    primary, backup, third = _models("primary", "backup", "third")
    primary.push(ModelRole.GATE, LlmError("rate limited"))
    backup.push(ModelRole.GATE, LlmError("timeout"))
    third.push(ModelRole.GATE, ACK)
    model = _resilient([primary, backup, third], clock)

    response = await model.generate(GATE, GateDecision)

    assert response.usage.model == "third"
    assert [len(m.calls) for m in (primary, backup, third)] == [1, 1, 1]


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


async def test_unexpected_errors_propagate_without_fallback(clock: VirtualClock) -> None:
    """A bug is not a provider failure: no fallback, no breaker change."""
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, RuntimeError("bug"))
    backup.push(ModelRole.GATE, ACK)
    model = _resilient([primary, backup], clock, failure_threshold=1)

    with pytest.raises(RuntimeError, match="bug"):
        await model.generate(GATE, GateDecision)

    assert backup.calls == []
    assert _state(model, "primary").state is BreakerState.CLOSED
    assert _state(model, "primary").consecutive_failures == 0


# --- agent rounds ------------------------------------------------------------


async def test_an_agent_round_falls_back_in_chain_order(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.AGENT, LlmError("rate limited"))
    backup.push(ModelRole.AGENT, Round(calls=(call("search_sop", query="running late"),)))
    model = _resilient([primary, backup], clock)

    reply = await model.chat(ROUND, AgentOutcome)

    assert reply.usage.model == "backup"
    assert [c.name for c in reply.turn.tool_calls] == ["search_sop"]
    assert reply.answer is None
    assert [len(m.rounds_of(ModelRole.AGENT)) for m in (primary, backup)] == [1, 1]
    assert _state(model, "primary").consecutive_failures == 1


async def test_every_model_failing_a_round_raises_one_llm_error_naming_each(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.AGENT, LlmError("down"))
    backup.push(ModelRole.AGENT, LlmError("timeout"))
    model = _resilient([primary, backup], clock)

    with pytest.raises(AllModelsFailed) as caught:
        await model.chat(ROUND, AgentOutcome)

    assert caught.value.failures == (
        ("primary", "LlmError: down"),
        ("backup", "LlmError: timeout"),
    )


async def test_an_unexpected_error_in_a_round_propagates_without_fallback(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.AGENT, RuntimeError("bug"))
    backup.push(ModelRole.AGENT, DONE)
    model = _resilient([primary, backup], clock, failure_threshold=1)

    with pytest.raises(RuntimeError, match="bug"):
        await model.chat(ROUND, AgentOutcome)

    assert backup.calls == []
    assert _state(model, "primary").state is BreakerState.CLOSED


async def test_forced_answers_and_rounds_share_each_models_breaker(clock: VirtualClock) -> None:
    """A provider that keeps failing the gate is skipped for the agent's rounds too."""
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, LlmError("down"), LlmError("down"))
    backup.push(ModelRole.GATE, ACK, ACK)
    backup.push(ModelRole.AGENT, DONE)
    model = _resilient([primary, backup], clock, failure_threshold=2)
    await model.generate(GATE, GateDecision)
    await model.generate(GATE, GateDecision)

    reply = await model.chat(ROUND, AgentOutcome)

    assert (reply.answer, reply.usage.model) == (DONE, "backup")
    assert primary.rounds_of(ModelRole.AGENT) == []  # the open breaker kept the round away


async def test_the_rounds_of_one_loop_may_be_served_by_different_models(
    clock: VirtualClock,
) -> None:
    """Each round walks the chain on its own: the conversation travels in the request."""
    primary, backup = _models("primary", "backup")
    search = call("search_sop", query="running late")
    primary.push(ModelRole.AGENT, Round(calls=(search,)), LlmError("down"))
    backup.push(ModelRole.AGENT, DONE)
    model = _resilient([primary, backup], clock)

    first = await model.chat(ROUND, AgentOutcome)
    history = (first.turn, ToolResult(call_id=search.id, name=search.name, content="none"))
    second = await model.chat(replace(ROUND, history=history), AgentOutcome)

    assert (first.usage.model, second.usage.model) == ("primary", "backup")
    (served,) = backup.calls
    assert isinstance(served.request, ChatRequest)
    assert served.request.history == history


# --- retries -----------------------------------------------------------------


def _limited(retry_after_s: float | None = None) -> TransientLlmError:
    return TransientLlmError("429 rate limited upstream", retry_after_s=retry_after_s)


async def test_a_rate_limit_is_retried_on_the_same_model_before_falling_back(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited(), ACK)
    time = FakeTime()
    model = _resilient([primary, backup], clock, time=time)

    response = await model.generate(GATE, GateDecision)

    assert response.usage.model == "primary"
    assert len(primary.calls) == 2
    assert backup.calls == []
    assert time.waits == [0.75]  # 1 s backoff, jitter 0.5: between half and all of it
    assert _state(model, "primary").consecutive_failures == 0  # a retried success


async def test_the_waits_grow_and_the_retries_used_up_fall_back_as_one_failure(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited(), _limited(), _limited())
    backup.push(ModelRole.GATE, ACK)
    time = FakeTime()
    model = _resilient([primary, backup], clock, time=time, failure_threshold=1)

    response = await model.generate(GATE, GateDecision)

    assert response.usage.model == "backup"
    assert len(primary.calls) == 3  # the call and its two retries
    assert time.waits == [0.75, 1.5]
    status = _state(model, "primary")
    assert status.consecutive_failures == 1  # the breaker counts the call once
    assert status.state is BreakerState.OPEN
    assert status.last_error == "TransientLlmError: 429 rate limited upstream"


async def test_a_short_retry_after_is_the_least_wait(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited(retry_after_s=3), ACK)
    time = FakeTime()
    model = _resilient([primary, backup], clock, time=time)

    await model.generate(GATE, GateDecision)

    assert time.waits == [3]


async def test_a_retry_after_over_the_cap_falls_back_at_once(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited(retry_after_s=30))
    backup.push(ModelRole.GATE, ACK)
    time = FakeTime()
    model = _resilient([primary, backup], clock, time=time)

    response = await model.generate(GATE, GateDecision)

    assert response.usage.model == "backup"
    assert (len(primary.calls), time.waits) == (1, [])


async def test_each_retry_gets_what_is_left_of_the_calls_timeout(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited(), _limited(), ACK)
    model = _resilient([primary, backup], clock)

    await model.generate(replace(GATE, timeout_s=60), GateDecision)

    assert [c.request.timeout_s for c in primary.calls] == [60, 59.25, 57.75]


async def test_no_retry_when_the_timeout_would_leave_too_little_for_the_attempt(
    clock: VirtualClock,
) -> None:
    """The request's timeout bounds a model's attempts and waits together."""
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited())
    backup.push(ModelRole.GATE, ACK)
    time = FakeTime()
    policy = RetryPolicy(min_attempt_s=10)
    model = _resilient([primary, backup], clock, time=time, retry=policy)

    response = await model.generate(replace(GATE, timeout_s=10.5), GateDecision)

    assert response.usage.model == "backup"
    assert (len(primary.calls), time.waits) == (1, [])


async def test_other_model_errors_are_not_retried(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, LlmError("the answer does not match GateDecision"))
    backup.push(ModelRole.GATE, ACK)
    time = FakeTime()
    model = _resilient([primary, backup], clock, time=time)

    await model.generate(GATE, GateDecision)

    assert (len(primary.calls), time.waits) == (1, [])


async def test_an_agent_round_is_retried_with_its_conversation(clock: VirtualClock) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.AGENT, _limited(), DONE)
    model = _resilient([primary, backup], clock)
    history = (ToolResult(call_id="c1", name="search_sop", content="none"),)

    reply = await model.chat(replace(ROUND, history=history), AgentOutcome)

    assert (reply.answer, reply.usage.model) == (DONE, "primary")
    retried = primary.calls[1].request
    assert isinstance(retried, ChatRequest)
    assert retried.history == history


async def test_a_retry_cancelled_by_the_run_deadline_leaves_the_breaker_alone(
    clock: VirtualClock,
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited())

    async def cancelled(_seconds: float) -> None:
        raise asyncio.CancelledError

    model = ResilientModel([("primary", primary), ("backup", backup)], clock, sleep=cancelled)

    with pytest.raises(asyncio.CancelledError):
        await model.generate(GATE, GateDecision)

    assert _state(model, "primary").consecutive_failures == 0
    assert backup.calls == []


async def test_each_retry_is_logged_with_the_models_reason(
    clock: VirtualClock, caplog: pytest.LogCaptureFixture
) -> None:
    primary, backup = _models("primary", "backup")
    primary.push(ModelRole.GATE, _limited(), ACK)
    model = _resilient([primary, backup], clock)

    with caplog.at_level(logging.WARNING, logger="freight_agent.adapters.llm.resilient"):
        await model.generate(GATE, GateDecision)

    (record,) = caplog.records
    assert record.getMessage() == "primary: 429 rate limited upstream; retry 1 of 2 in 0.8 s"


@pytest.mark.parametrize(
    ("retry", "retry_after_s", "jitter", "wait"),
    [
        (1, None, 0.0, 0.5),  # half the 1 s ceiling
        (1, None, 1.0, 1.0),  # all of it
        (2, None, 1.0, 2.0),
        (2, 0.5, 0.0, 1.0),  # a Retry-After under the backoff changes nothing
        (2, 8.0, 0.0, 8.0),  # up to the cap
        (3, None, 0.0, None),  # past max_retries
        (1, 8.5, 0.0, None),  # over the cap
    ],
)
def test_the_policy_backs_off_exponentially_with_jitter(
    retry: int, retry_after_s: float | None, jitter: float, wait: float | None
) -> None:
    assert RetryPolicy().delay_s(retry, retry_after_s, jitter) == wait


def test_the_policy_caps_each_wait() -> None:
    policy = RetryPolicy(max_retries=10, base_delay_s=1, max_delay_s=5)

    assert policy.delay_s(10, None, 1.0) == 5


@pytest.mark.parametrize(
    "options",
    [{"max_retries": -1}, {"base_delay_s": -1}, {"base_delay_s": 9, "max_delay_s": 8}],
)
def test_an_invalid_policy_is_rejected(options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RetryPolicy(**options)


# --- breaker -----------------------------------------------------------------


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

    async def _wait(self) -> None:
        self.calls += 1
        if self._failures:
            self._failures -= 1
            raise LlmError("down")
        self.started.set()
        await self.release.wait()

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        await self._wait()
        return LlmResponse(
            parsed=schema.model_validate(ACK.model_dump()),
            usage=Usage(role=request.role.value, model="blocking"),
        )

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        await self._wait()
        return ChatReply(
            turn=AssistantTurn(),
            answer=schema.model_validate(DONE.model_dump()),
            usage=Usage(role=request.role.value, model="blocking"),
        )


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
