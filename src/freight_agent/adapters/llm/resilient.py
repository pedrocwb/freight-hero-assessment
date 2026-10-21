"""A model fallback chain with one circuit breaker per model, and retries on each.

A rate limit or an overloaded provider (`TransientLlmError`) often clears within seconds,
and another model of the chain may not pass the suites as the primary does. So a
transient failure is retried on the same model first (`RetryPolicy`): a few times, with
exponential backoff and jitter, honouring the provider's `Retry-After` when it is short,
and always inside the request's `timeout_s`, which bounds every attempt and wait of one
model together. Any other `LlmError`, or a transient one whose retries are used up, moves
the call to the next model.

A failing provider tends to keep failing for a while (an outage, a rate limit).
Without a breaker every run would wait on it before falling back, and the queue
would back up behind those waits. So each model has a breaker: after
`failure_threshold` consecutive failed calls (a call's retries count once) it opens and
the chain skips that model; after `cooldown_s` on the injected clock it lets one trial
call through, whose outcome closes it or opens it again. When every model fails or is
open, one `LlmError` names each model's reason, and the runtime escalates it to a Hero
like any model failure.

Each call walks the chain on its own, so the rounds of one agent loop may be served by
different models when the primary fails halfway: the conversation is in the request.

Only `LlmError` counts as a provider failure. Anything else (a bug) propagates at once:
falling back would hide it. The run deadline cancels a call, its waits included.

The backoff waits on real time (`sleep`, `timer`), not on the clock of the breakers: the
simulation drives the cooldowns on its virtual clock, but a provider's rate limit runs on
the wall clock.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from freight_agent.ports.clock import Clock
from freight_agent.ports.llm import (
    ChatReply,
    ChatRequest,
    LanguageModel,
    LlmError,
    LlmRequest,
    LlmResponse,
    SchemaT,
    TransientLlmError,
)

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Timer = Callable[[], float]  # seconds, monotonic
Jitter = Callable[[], float]  # uniform in [0, 1)


@dataclass(frozen=True)
class RetryPolicy:
    """How a transient failure is retried on the same model before the chain moves on.

    Retry n waits `base_delay_s * 2 ** (n - 1)`, at most `max_delay_s`, scaled by a jitter
    between half and all of it, so concurrent runs do not retry in step. A `Retry-After`
    the provider sent is the least wait; one over `max_delay_s` moves to the next model at
    once. A retry that would leave its attempt less than `min_attempt_s` of the request's
    `timeout_s` is not made.
    """

    max_retries: int = 2
    base_delay_s: float = 1.0
    max_delay_s: float = 8.0
    min_attempt_s: float = 10.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if not 0 <= self.base_delay_s <= self.max_delay_s:
            raise ValueError("the delays must satisfy 0 <= base_delay_s <= max_delay_s")

    def delay_s(self, retry: int, retry_after_s: float | None, jitter: float) -> float | None:
        """The wait before retry number `retry` (1 for the first), or None: no retry."""
        if retry > self.max_retries:
            return None
        if retry_after_s is not None and retry_after_s > self.max_delay_s:
            return None
        backoff = min(self.max_delay_s, self.base_delay_s * 2 ** (retry - 1))
        return max(backoff * (0.5 + jitter / 2), retry_after_s or 0.0)


DEFAULT_RETRY = RetryPolicy()


class BreakerState(StrEnum):
    CLOSED = "closed"  # calls go through
    OPEN = "open"  # calls are skipped until the cooldown ends
    HALF_OPEN = "half_open"  # the cooldown ended: one trial call decides


@dataclass(frozen=True)
class BreakerStatus:
    """One model's breaker, as `ResilientModel.status` reports it."""

    name: str
    state: BreakerState
    consecutive_failures: int
    last_error: str | None
    retry_at: datetime | None  # when an open breaker lets a trial call through


@dataclass(frozen=True)
class _Permit:
    trial: bool  # the one half-open call whose outcome closes or reopens the breaker


class CircuitBreaker:
    """Closed -> open after N consecutive failures; open -> half-open after the cooldown;
    half-open -> closed on a successful trial, open again on a failed one.

    The open-to-half-open move is computed from the clock when asked, so a virtual
    clock drives it in tests and evals without timers.
    """

    def __init__(self, clock: Clock, failure_threshold: int, cooldown: timedelta) -> None:
        self._clock = clock
        self._threshold = failure_threshold
        self._cooldown = cooldown
        self._failures = 0
        self._opened_at: datetime | None = None
        self._last_error: str | None = None
        self._trial_in_flight = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock.now() < self._opened_at + self._cooldown:
            return BreakerState.OPEN
        return BreakerState.HALF_OPEN

    def try_acquire(self) -> _Permit | None:
        """A permit to call the model now, or None. Half-open admits one call at a time,
        so concurrent runs do not all hit a provider that may still be down."""
        state = self.state
        if state is BreakerState.CLOSED:
            return _Permit(trial=False)
        if state is BreakerState.HALF_OPEN and not self._trial_in_flight:
            self._trial_in_flight = True
            return _Permit(trial=True)
        return None

    def record_success(self, permit: _Permit) -> None:
        self._release(permit)
        self._failures = 0
        self._opened_at = None

    def record_failure(self, permit: _Permit, error: str) -> None:
        self._release(permit)
        self._failures += 1
        self._last_error = error
        if permit.trial or self._failures >= self._threshold:
            self._opened_at = self._clock.now()

    def abandon(self, permit: _Permit) -> None:
        """The call ended with no verdict on the provider (a bug, a cancellation)."""
        self._release(permit)

    def unavailable_reason(self) -> str:
        if self.state is BreakerState.OPEN and self._opened_at is not None:
            return f"circuit open until {(self._opened_at + self._cooldown).isoformat()}"
        return "circuit half-open, a trial call is in flight"

    def status(self, name: str) -> BreakerStatus:
        return BreakerStatus(
            name=name,
            state=self.state,
            consecutive_failures=self._failures,
            last_error=self._last_error,
            retry_at=None if self._opened_at is None else self._opened_at + self._cooldown,
        )

    def _release(self, permit: _Permit) -> None:
        if permit.trial:
            self._trial_in_flight = False


class AllModelsFailed(LlmError):
    """Every model in the chain failed or was skipped by its open breaker."""

    def __init__(self, failures: Sequence[tuple[str, str]]) -> None:
        self.failures = tuple(failures)  # (model name, reason), in chain order
        listed = "; ".join(f"{name}: {reason}" for name, reason in self.failures)
        super().__init__(f"every model in the fallback chain failed: {listed}")


class ResilientModel:
    """A `LanguageModel` that tries a chain of named models in order, each behind a breaker,
    retrying a transient failure on the same model first."""

    def __init__(
        self,
        chain: Sequence[tuple[str, LanguageModel]],
        clock: Clock,
        failure_threshold: int = 3,
        cooldown_s: float = 60,
        *,
        retry: RetryPolicy = DEFAULT_RETRY,
        sleep: Sleep = asyncio.sleep,
        timer: Timer = time.monotonic,
        jitter: Jitter = random.random,
    ) -> None:
        names = [name for name, _ in chain]
        if not names:
            raise ValueError("the fallback chain needs at least one model")
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate model name in the fallback chain: {names}")
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown_s < 0:
            raise ValueError("cooldown_s must not be negative")
        cooldown = timedelta(seconds=cooldown_s)
        self._chain = tuple(chain)
        self._breakers = {
            name: CircuitBreaker(clock, failure_threshold, cooldown) for name in names
        }
        self._retry = retry
        self._sleep = sleep
        self._timer = timer
        self._jitter = jitter

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        return await self._first(request, lambda model, sent: model.generate(sent, schema))

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        return await self._first(request, lambda model, sent: model.chat(sent, schema))

    async def _first[Q: LlmRequest, R](
        self, request: Q, call: Callable[[LanguageModel, Q], Awaitable[R]]
    ) -> R:
        """The first model of the chain whose breaker lets it through and whose call
        succeeds, within its retries."""
        failures: list[tuple[str, str]] = []
        for name, model in self._chain:
            breaker = self._breakers[name]
            permit = breaker.try_acquire()
            if permit is None:
                failures.append((name, breaker.unavailable_reason()))
                continue
            try:
                response = await self._with_retries(name, model, request, call)
            except LlmError as exc:
                reason = f"{type(exc).__name__}: {exc}"
                breaker.record_failure(permit, reason)
                failures.append((name, reason))
                continue
            except BaseException:
                breaker.abandon(permit)
                raise
            breaker.record_success(permit)
            return response
        raise AllModelsFailed(failures)

    async def _with_retries[Q: LlmRequest, R](
        self,
        name: str,
        model: LanguageModel,
        request: Q,
        call: Callable[[LanguageModel, Q], Awaitable[R]],
    ) -> R:
        """One model's call, retried after a transient failure while the policy and the
        request's timeout allow; each retry gets what is left of that timeout."""
        deadline = self._timer() + request.timeout_s
        sent = request
        retry = 0
        while True:
            try:
                return await call(model, sent)
            except TransientLlmError as exc:
                retry += 1
                delay = self._retry.delay_s(retry, exc.retry_after_s, self._jitter())
                left = deadline - self._timer() - (delay or 0.0)
                if delay is None or left < self._retry.min_attempt_s:
                    raise
                logger.warning(
                    "%s: %s; retry %d of %d in %.1f s",
                    name,
                    exc,
                    retry,
                    self._retry.max_retries,
                    delay,
                )
                await self._sleep(delay)
                sent = replace(request, timeout_s=max(deadline - self._timer(), 0.0))

    def status(self) -> tuple[BreakerStatus, ...]:
        """Every breaker, in chain order."""
        return tuple(self._breakers[name].status(name) for name, _ in self._chain)
