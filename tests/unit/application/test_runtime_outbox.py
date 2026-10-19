"""Outbox delivery: by kind through the gateways, with retries and a final failure."""

import pytest

from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.actions import (
    CreateHeroTask,
    PostBrokerSlack,
    PostInternalSlack,
    Purpose,
    SendEmail,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Load, Stage
from freight_agent.domain.outbox import OutboxItem, OutboxKind, OutboxStatus
from freight_agent.domain.runs import RunStatus
from tests.unit.application.test_runtime_harness import (
    BROKER,
    DECIDE,
    DRIVER,
    GATE,
    LOAD_ID,
    SECTIONS,
    FakeSop,
    World,
    at,
    decision,
    plan,
    section,
)

pytestmark = pytest.mark.unit


async def outbox(world: World) -> list[OutboxItem]:
    return list((await world.snapshot()).outbox)


async def test_every_kind_is_delivered_with_its_payload_and_key(load: Load) -> None:
    departed = section("driver_departed", "Update the stage.", intents=(Intent.DRIVER_DEPARTED,))
    world = await World(load, sop=FakeSop((*SECTIONS, departed))).start()
    await world.message(at(7, 12), "Loaded and leaving now")
    world.model.push(GATE, decision(Intent.DRIVER_DEPARTED))
    world.model.push(
        DECIDE,
        plan(
            SendSms(contact_id=DRIVER, text="Thanks, drive safe.", purpose=Purpose.REPLY),
            SendEmail(to_contact_ids=(BROKER,), subject="Picked up", body="The load left."),
            PostBrokerSlack(text="Load 481207 left the pickup."),
            WriteTmsNote(text="Departed the pickup at 07:12."),
            CreateHeroTask(title="Check the BOL", description="Ask for a photo.", urgent=False),
            PostInternalSlack(text="481207 is rolling."),
            UpdateStage(stage=Stage.TO_DELIVERY),
        ),
    )

    runs = await world.tick(at(7, 13))

    assert [r.status for r in runs] == [RunStatus.ACTED]
    assert runs[0].sop_section_ids == ("communication_rules", "driver_departed", "escalation")
    log = world.gateways.log
    assert [(d.channel, d.idempotency_key) for d in log.deliveries] == [
        ("sms", "msg:0712:0"),
        ("email", "msg:0712:1"),
        ("broker_slack", "msg:0712:2"),
        ("tms_note", "msg:0712:3"),
        ("hero_task", "msg:0712:4"),
        ("internal_slack", "msg:0712:5"),
        ("stage_update", "msg:0712:6"),
    ]
    payloads = [d.payload for d in log.deliveries]
    assert payloads == [
        {"contact_id": DRIVER, "text": "Thanks, drive safe."},
        {"to_contact_ids": [BROKER], "subject": "Picked up", "body": "The load left."},
        {"broker_id": "northline", "text": "Load 481207 left the pickup."},
        {"text": "Departed the pickup at 07:12."},
        {"title": "Check the BOL", "description": "Ask for a photo.", "urgent": False},
        {"text": "481207 is rolling."},
        {"stage": "to_delivery"},
    ]
    assert all(d.load_id == LOAD_ID for d in log.deliveries)
    items = await outbox(world)
    assert {(i.status, i.attempts) for i in items} == {(OutboxStatus.SENT, 1)}
    assert (await world.snapshot()).load.stage is Stage.TO_DELIVERY


async def test_a_failed_delivery_stays_pending_and_is_retried(load: Load) -> None:
    world = await World(load).start()
    world.gateways.fail_next("sms", times=1)

    await world.high_risk_at_seven()

    [item] = await outbox(world)
    assert (item.status, item.attempts) == (OutboxStatus.PENDING, 1)
    assert item.last_error == "simulated sms failure"
    assert world.sent("sms") == []

    assert await world.runtime.dispatch_outbox() == 1

    [item] = await outbox(world)
    assert (item.status, item.attempts) == (OutboxStatus.SENT, 2)
    assert [d.idempotency_key for d in world.sent("sms")] == ["risk:p1:0"]


async def test_a_delivery_fails_for_good_after_the_max_attempts(load: Load) -> None:
    world = await World(load, config=RuntimeConfig(max_delivery_attempts=2)).start()
    world.gateways.fail_next("sms", times=5)

    await world.high_risk_at_seven()
    assert await world.runtime.dispatch_outbox() == 0

    [item] = await outbox(world)
    assert (item.status, item.attempts) == (OutboxStatus.FAILED, 2)
    assert await world.runtime.dispatch_outbox() == 0  # a failed item is not retried
    [item] = await outbox(world)
    assert item.attempts == 2
    assert world.sent("sms") == []


@pytest.mark.parametrize(
    "payload",
    [{"text": "no recipient"}, {"contact_id": 20931, "text": "a number, not a contact id"}],
)
async def test_a_malformed_payload_fails_at_once(load: Load, payload: dict[str, object]) -> None:
    world = await World(load).start()
    item = OutboxItem(
        outbox_id="out_bad",
        idempotency_key="bad:0",
        run_id="run_x",
        load_id=LOAD_ID,
        kind=OutboxKind.SMS,
        payload=payload,
        created_at=at(7, 0),
    )
    async with world.store.unit_of_work() as uow:
        await uow.outbox.add(item)
        await uow.commit()

    assert await world.runtime.dispatch_outbox() == 0

    [stored] = await outbox(world)
    assert stored.status is OutboxStatus.FAILED
    assert stored.attempts == 1
    assert stored.last_error is not None and stored.last_error.startswith("malformed payload")


async def test_the_batch_size_limits_one_dispatch(load: Load) -> None:
    world = await World(load, config=RuntimeConfig(outbox_batch_size=1)).start()
    for index in range(2):
        async with world.store.unit_of_work() as uow:
            await uow.outbox.add(
                OutboxItem(
                    outbox_id=f"out_{index}",
                    idempotency_key=f"k:{index}",
                    run_id="run_x",
                    load_id=LOAD_ID,
                    kind=OutboxKind.TMS_NOTE,
                    payload={"text": f"note {index}"},
                    created_at=at(7, index),
                )
            )
            await uow.commit()

    assert await world.runtime.dispatch_outbox() == 1
    assert await world.runtime.dispatch_outbox() == 1
    assert await world.runtime.dispatch_outbox() == 0
