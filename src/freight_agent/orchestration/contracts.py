"""Small JSON payloads in Temporal history. Message bodies stay in the application DB."""

from dataclasses import dataclass, field
from urllib.parse import quote


@dataclass(frozen=True)
class LoadKey:
    broker_id: str
    load_id: str

    @property
    def workflow_id(self) -> str:
        return f"load/{quote(self.broker_id, safe='')}/{quote(self.load_id, safe='')}"


@dataclass(frozen=True)
class WorkItem:
    load: LoadKey
    item_id: str


@dataclass(frozen=True)
class TimerRef:
    timer_id: str
    due_at: float  # UTC epoch seconds; independent of host timezone


@dataclass(frozen=True)
class WakeState:
    event_id: str | None = None
    outbox_id: str | None = None
    timers: list[TimerRef] = field(default_factory=list)
    finished: bool = False
    documents_pending: bool = False
    stop_requested: bool = False
