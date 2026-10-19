"""The in-memory pub/sub behind the event stream, and the SSE wire format."""

import asyncio
import json

import pytest
from pydantic import BaseModel

from freight_agent.api.bus import HEARTBEAT, EventBus, sse_message

pytestmark = pytest.mark.unit


class Note(BaseModel):
    text: str


def test_a_message_is_one_event_line_and_one_json_data_line() -> None:
    message = sse_message("run", Note(text="two\nlines"))

    assert message == 'event: run\ndata: {"text":"two\\nlines"}\n\n'


def test_plain_mappings_are_json_too() -> None:
    assert sse_message("load", {"load_id": "1"}) == 'event: load\ndata: {"load_id": "1"}\n\n'


async def test_every_subscriber_gets_every_message() -> None:
    bus = EventBus()
    with bus.subscribe() as first, bus.subscribe() as second:
        bus.publish("clock", {"now": "07:00"})

        got = [await first.next(0.1), await second.next(0.1)]

    assert got == [sse_message("clock", {"now": "07:00"})] * 2


async def test_a_slow_subscriber_drops_its_oldest_messages() -> None:
    bus = EventBus(max_queued=2)
    with bus.subscribe() as slow:
        for n in range(3):
            bus.publish("run", {"n": n})

        got = [await slow.next(0.1), await slow.next(0.1)]

    assert [json.loads(m.split("data: ")[1]) for m in got if m] == [{"n": 1}, {"n": 2}]
    assert slow.dropped == 1


async def test_a_subscription_ends_with_its_block() -> None:
    bus = EventBus()
    with bus.subscribe() as subscription:
        assert bus.subscribers == 1

    bus.publish("run", {"n": 1})
    assert bus.subscribers == 0
    assert await subscription.next(0.01) is None


async def test_the_stream_sends_the_initial_messages_then_heartbeats_when_idle() -> None:
    bus = EventBus()

    async def initial() -> list[str]:
        return [sse_message("clock", {"now": "06:55"})]

    stream = bus.stream(initial, heartbeat_s=0.01)
    first = [await anext(stream) for _ in range(3)]
    bus.publish("run", {"n": 1})
    after = await anext(stream)
    while after == HEARTBEAT:
        after = await anext(stream)
    await stream.aclose()

    assert first == [": connected\n\n", sse_message("clock", {"now": "06:55"}), HEARTBEAT]
    assert after == sse_message("run", {"n": 1})
    assert bus.subscribers == 0
    await asyncio.sleep(0)
