"""Clock and id adapters."""

import itertools
import uuid
from datetime import UTC, datetime, timedelta


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class VirtualClock:
    """A clock the simulation and the eval harness move by hand. Never goes backwards."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("VirtualClock needs a timezone-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        if at < self._now:
            raise ValueError("the virtual clock cannot move backwards")
        self._now = at

    def advance(self, delta: timedelta) -> None:
        self.set(self._now + delta)


class UuidIdGenerator:
    def new(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SequentialIdGenerator:
    """Deterministic ids for tests and evals: run_1, run_2, timer_1, ..."""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count[int]] = {}

    def new(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter)}"
