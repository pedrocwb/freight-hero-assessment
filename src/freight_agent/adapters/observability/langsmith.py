"""LangSmith trace links: a run's trace URL, from the id of its root span.

The trace itself is LangGraph's and LangChain's native tracing, switched on by the
environment (`bootstrap.enable_native_tracing`): the run's root span `agent_run` with
its metadata (run, event, load, broker), a span per graph node visit (prefilter, gate,
knowledge, each agent round and each tools step, validate) with the state in and out,
and under each model round the chat model call with its prompt, tools, answer and
tokens. The runtime chooses the root span's id, so this adapter only builds the link:
the project's address (workspace and project ids), read from the API once and kept,
plus that id. `poll=true` makes the page wait for a trace still being sent.

The lookup runs off the event loop with a timeout and fails open: a run whose link
cannot be built keeps no link, with a warning, and the lookup is tried again a minute
later (the project only exists once the first trace reached it).
"""

import asyncio
import logging
import threading
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from langsmith import Client

logger = logging.getLogger(__name__)

DEFAULT_PROJECT = "freight-agent"
_RETRY_S = 60.0


class LinkClient(Protocol):
    """The slice of `langsmith.Client` the links need; a fake implements it in tests."""

    def read_project(self, *, project_name: str) -> Any: ...

    def get_run_url(self, *, run: Any, project_id: Any) -> str: ...


@dataclass(frozen=True)
class _RunRef:
    id: UUID


def langsmith_client_factory(api_key: str, api_url: str | None = None) -> Callable[[], LinkClient]:
    """The real SDK client, built on first use (it reads nothing from the network)."""

    def build() -> LinkClient:
        return Client(api_key=api_key, api_url=api_url)

    return build


class LangSmithLinks:
    """Implements `interfaces.tracing.Tracer` for the traces of one LangSmith project."""

    def __init__(
        self,
        client_factory: Callable[[], LinkClient],
        *,
        project_name: str = DEFAULT_PROJECT,
        timeout_s: float = 5.0,
    ) -> None:
        self._factory = client_factory
        self._project = project_name
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._base: str | None = None  # .../o/<workspace>/projects/p/<project>
        self._failed_at: float | None = None  # when the platform's lookup last failed

    @property
    def project_name(self) -> str:
        return self._project

    async def trace_url(self, trace_id: UUID) -> str | None:
        base = await self.cached_project_url()
        return None if base is None else f"{base}/r/{trace_id}?poll=true"

    async def cached_project_url(self) -> str | None:
        """The project's page. Read at most once a minute until it is known, so a LangSmith
        outage never slows a run or the page that asks by more than one lookup a minute."""
        if self._base is not None:
            return self._base
        now = time.monotonic()
        if self._failed_at is not None and now - self._failed_at < _RETRY_S:
            return None
        try:
            return await asyncio.wait_for(asyncio.to_thread(self._project_url), self._timeout_s)
        except Exception as exc:  # a timeout too
            self._failed_at = now
            logger.warning("the LangSmith project %s cannot be read: %r", self._project, exc)
            return None

    def _project_url(self) -> str:
        """Read once, in a worker thread: the SDK's lookups block."""
        with self._lock:
            if self._base is None:
                client = self._factory()
                project_id = client.read_project(project_name=self._project).id
                with warnings.catch_warnings():  # deprecated, but it builds the URL locally
                    warnings.simplefilter("ignore", DeprecationWarning)
                    url = client.get_run_url(run=_RunRef(UUID(int=0)), project_id=project_id)
                self._base = url.split("/r/", 1)[0]
            return self._base
