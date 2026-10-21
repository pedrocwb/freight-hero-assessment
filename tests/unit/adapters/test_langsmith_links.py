"""LangSmith trace links, from a fake SDK client: no network.

The trace itself is LangGraph's native tracing; this adapter only builds a run's link
from the project's address (read once, then kept) and the trace id the runtime chose.
"""

import asyncio
import logging
import threading
import warnings
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from freight_agent.adapters.observability import langsmith as links
from freight_agent.adapters.observability.langsmith import (
    DEFAULT_PROJECT,
    LangSmithLinks,
    LinkClient,
    langsmith_client_factory,
)
from freight_agent.adapters.observability.null import NullTracer
from freight_agent.interfaces.tracing import Tracer

pytestmark = pytest.mark.unit

BASE = "https://smith.langchain.com/o/T/projects/p/P"
TRACE = UUID("0b7e4c1a-5d3f-4e2a-9c8b-1a2b3c4d5e6f")


class FakeLinkClient:
    """Answers like the SDK: a project with an id, and a run URL under it. `fail` raises
    on the next lookups; `release` blocks them until set."""

    def __init__(self) -> None:
        self.project_reads: list[str] = []
        self.url_requests: list[tuple[Any, Any]] = []
        self.fail: list[Exception] = []
        self.release = threading.Event()
        self.release.set()

    def read_project(self, *, project_name: str) -> Any:
        self.project_reads.append(project_name)
        self.release.wait(timeout=5)
        if self.fail:
            raise self.fail.pop(0)
        return SimpleNamespace(id="P")

    def get_run_url(self, *, run: Any, project_id: Any) -> str:
        self.url_requests.append((run, project_id))
        warnings.warn("get_run_url is deprecated", DeprecationWarning, stacklevel=2)
        return f"https://smith.langchain.com/o/T/projects/p/{project_id}/r/{run.id}?poll=true"


class Factory:
    def __init__(self, client: FakeLinkClient) -> None:
        self.client = client
        self.calls = 0

    def __call__(self) -> LinkClient:
        self.calls += 1
        return self.client


def _links(client: FakeLinkClient, **options: Any) -> tuple[LangSmithLinks, Factory]:
    factory = Factory(client)
    return LangSmithLinks(factory, project_name="freight-test", **options), factory


def _frozen_time(monkeypatch: pytest.MonkeyPatch, start: float) -> Callable[[float], None]:
    """The module's `time.monotonic` on a hand-moved reading (the event loop keeps its own
    clock); returns a function that sets the reading."""
    now = [start]
    monkeypatch.setattr(links, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def move(to: float) -> None:
        now[0] = to

    return move


# --- trace_url ---------------------------------------------------------------------------


async def test_a_trace_url_is_the_project_page_plus_the_trace_id() -> None:
    client = FakeLinkClient()
    tracer: Tracer = _links(client)[0]

    assert await tracer.trace_url(TRACE) == f"{BASE}/r/{TRACE}?poll=true"
    assert client.project_reads == ["freight-test"]
    ((run, project_id),) = client.url_requests
    assert project_id == "P"
    assert run.id == UUID(int=0)  # only to learn the address: the trace id is added after


async def test_the_project_is_read_once_and_kept() -> None:
    client = FakeLinkClient()
    tracer, factory = _links(client)
    first, second = uuid4(), uuid4()

    urls = [await tracer.trace_url(first), await tracer.trace_url(second)]

    assert urls == [f"{BASE}/r/{first}?poll=true", f"{BASE}/r/{second}?poll=true"]
    assert client.project_reads == ["freight-test"]
    assert factory.calls == 1


async def test_a_failed_lookup_gives_no_link_with_a_warning_and_is_retried_a_minute_later(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The project only exists once the first trace reached it; in between, a LangSmith
    outage costs the runs one lookup a minute, not one each."""
    move = _frozen_time(monkeypatch, 1_000.0)
    client = FakeLinkClient()
    client.fail.append(LookupError("project not found"))
    tracer, _ = _links(client)

    with caplog.at_level(logging.WARNING):
        assert await tracer.trace_url(TRACE) is None
    assert "freight-test" in caplog.text
    assert "project not found" in caplog.text

    move(1_059.0)
    assert await tracer.trace_url(TRACE) is None
    assert client.project_reads == ["freight-test"]

    move(1_060.0)
    assert await tracer.trace_url(TRACE) == f"{BASE}/r/{TRACE}?poll=true"
    assert client.project_reads == ["freight-test", "freight-test"]


async def test_a_failing_client_factory_is_retried_after_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    move = _frozen_time(monkeypatch, 1_000.0)
    client = FakeLinkClient()
    attempts: list[int] = []

    def flaky() -> LinkClient:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no network yet")
        return client

    tracer = LangSmithLinks(flaky)

    assert await tracer.trace_url(TRACE) is None
    move(1_060.0)
    assert await tracer.trace_url(TRACE) == f"{BASE}/r/{TRACE}?poll=true"
    assert client.project_reads == [DEFAULT_PROJECT]


async def test_a_slow_lookup_times_out_to_no_link_without_blocking_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    move = _frozen_time(monkeypatch, 1_000.0)
    client = FakeLinkClient()
    client.release.clear()
    tracer, _ = _links(client, timeout_s=0.05)
    try:
        with caplog.at_level(logging.WARNING):
            assert await asyncio.wait_for(tracer.trace_url(TRACE), timeout=1) is None
        assert "TimeoutError" in caplog.text
    finally:
        client.release.set()

    # The lookup finishes in its thread and keeps the address for the next run.
    move(1_060.0)
    for _ in range(100):
        if await tracer.trace_url(TRACE) is not None:
            break
        await asyncio.sleep(0.01)
    assert await tracer.trace_url(TRACE) == f"{BASE}/r/{TRACE}?poll=true"
    assert client.project_reads == ["freight-test"]


@pytest.mark.filterwarnings("error::DeprecationWarning")
async def test_the_sdks_deprecation_of_get_run_url_is_silenced() -> None:
    """`get_run_url` is deprecated but builds the address locally, with no request."""
    assert await _links(FakeLinkClient())[0].trace_url(TRACE) == f"{BASE}/r/{TRACE}?poll=true"


# --- cached_project_url ------------------------------------------------------------------


async def test_the_cached_project_url_is_the_project_page() -> None:
    client = FakeLinkClient()
    tracer, _ = _links(client)

    assert await tracer.cached_project_url() == BASE
    assert await tracer.trace_url(TRACE) == f"{BASE}/r/{TRACE}?poll=true"
    assert await tracer.cached_project_url() == BASE
    assert client.project_reads == ["freight-test"]


async def test_after_a_failure_the_project_page_is_not_read_again_for_a_minute(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The platform's header asks on every state read: an outage must not slow it."""
    move = _frozen_time(monkeypatch, 1_000.0)
    client = FakeLinkClient()
    client.fail.append(ConnectionError("langsmith is down"))
    tracer, _ = _links(client)

    with caplog.at_level(logging.WARNING):
        assert await tracer.cached_project_url() is None
    assert "freight-test" in caplog.text
    assert "langsmith is down" in caplog.text

    move(1_059.0)
    assert await tracer.cached_project_url() is None
    assert len(client.project_reads) == 1  # not asked again within the minute

    move(1_060.0)
    assert await tracer.cached_project_url() == BASE
    assert len(client.project_reads) == 2


# --- construction ------------------------------------------------------------------------


def test_the_project_name_defaults_to_freight_agent() -> None:
    tracer = LangSmithLinks(Factory(FakeLinkClient()))

    assert tracer.project_name == DEFAULT_PROJECT == "freight-agent"
    assert _links(FakeLinkClient())[0].project_name == "freight-test"


def test_the_client_factory_builds_the_sdk_client_only_when_called(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, Any]] = []

    def fake_client(**options: Any) -> FakeLinkClient:
        built.append(options)
        return FakeLinkClient()

    monkeypatch.setattr(links, "Client", fake_client)

    factory = langsmith_client_factory("lsv2_pt_fake", api_url="https://eu.example")
    assert built == []
    factory()

    assert built == [{"api_key": "lsv2_pt_fake", "api_url": "https://eu.example"}]


async def test_the_null_tracer_links_nothing() -> None:
    tracer: Tracer = NullTracer()

    assert await tracer.trace_url(TRACE) is None
