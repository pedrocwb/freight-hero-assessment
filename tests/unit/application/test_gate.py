from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.application.context import RunContext
from freight_agent.application.gate import Gate, GateResult, follow_up_intent
from freight_agent.domain.events import Attachment, Direction, Event, EventType
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.load import Channel, Load
from freight_agent.domain.obligations import Obligation, ObligationKind
from freight_agent.domain.timers import Timer
from freight_agent.ports.llm import LlmError, ModelRole

LA = ZoneInfo("America/Los_Angeles")
DRIVER = "drv_20931"


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 13, hour, minute, tzinfo=LA)


def event(
    event_type: EventType,
    *,
    event_id: str = "evt_1",
    channel: Channel = Channel.SYSTEM,
    sender: str | None = None,
    body: str = "",
    data: dict[str, Any] | None = None,
    attachments: tuple[Attachment, ...] = (),
    direction: Direction = Direction.INBOUND,
) -> Event:
    return Event(
        event_id=event_id,
        load_id="481207",
        type=event_type,
        occurred_at=at(7, 12),
        channel=channel,
        direction=direction,
        sender_contact_id=sender,
        body=body,
        data=data or {},
        attachments=attachments,
    )


def sms(body: str) -> Event:
    return event(
        EventType.INBOUND_MESSAGE, event_id="msg_1", channel=Channel.SMS, sender=DRIVER, body=body
    )


ETA_REQUEST = Obligation(
    obligation_id="obl_1",
    load_id="481207",
    kind=ObligationKind.ETA_REQUEST,
    attempts=1,
    opened_at=at(7, 1),
    last_action_at=at(7, 1),
    active_timer_id="timer_1",
)
TIMER = Timer(
    timer_id="timer_1",
    load_id="481207",
    obligation_id="obl_1",
    due_at=at(7, 31),
    created_at=at(7, 1),
)


def context(
    load: Load,
    trigger: Event,
    *,
    obligations: tuple[Obligation, ...] = (),
    event_timer: Timer | None = None,
) -> RunContext:
    return RunContext(
        event=trigger,
        load=load,
        now=at(7, 13),
        obligations=obligations,
        pending_timers=(),
        recent_events=(trigger,),
        recent_outbound=(),
        event_timer=event_timer,
    )


async def classify(load: Load, trigger: Event, **kwargs: Any) -> tuple[GateResult, ScriptedModel]:
    model = ScriptedModel()
    result = await Gate(model).classify(context(load, trigger, **kwargs))
    return result, model


@pytest.mark.unit
@pytest.mark.parametrize(
    ("previous", "current", "intent", "act"),
    [
        ("low", "high", Intent.DELAY_RISK_HIGH, True),
        ("medium", "high", Intent.DELAY_RISK_HIGH, True),
        ("high", "low", Intent.DELAY_RISK_LOW, True),
        ("high", "medium", Intent.DELAY_RISK_LOW, True),
        ("low", "medium", Intent.OTHER, False),
        ("high", "bogus", Intent.OTHER, False),
    ],
)
async def test_risk_changes_get_their_intent_from_code(
    load: Load, previous: str, current: str, intent: Intent, act: bool
) -> None:
    trigger = event(EventType.RISK_CHANGED, data={"previous": previous, "current": current})

    result, model = await classify(load, trigger)

    assert (result.decision.intent, result.decision.act) == (intent, act)
    assert result.usage is None
    assert model.calls == []


@pytest.mark.unit
async def test_due_eta_timer_is_a_follow_up(load: Load) -> None:
    trigger = event(
        EventType.TIMER_DUE,
        data={"timer_id": "timer_1", "obligation_id": "obl_1", "kind": "eta_request"},
    )

    result, model = await classify(load, trigger)

    assert result.decision.intent == Intent.ETA_FOLLOW_UP_DUE
    assert result.decision.act is True
    assert result.usage is None
    assert model.calls == []


@pytest.mark.unit
async def test_timer_kind_falls_back_to_the_timer_obligation(load: Load) -> None:
    trigger = event(EventType.TIMER_DUE, data={"timer_id": "timer_1"})

    result, _ = await classify(load, trigger, obligations=(ETA_REQUEST,), event_timer=TIMER)

    assert result.decision.intent == Intent.ETA_FOLLOW_UP_DUE


@pytest.mark.unit
async def test_timer_kind_falls_back_to_the_obligation_named_in_the_event(load: Load) -> None:
    trigger = event(EventType.TIMER_DUE, data={"obligation_id": "obl_1"})

    result, _ = await classify(load, trigger, obligations=(ETA_REQUEST,))

    assert result.decision.intent == Intent.ETA_FOLLOW_UP_DUE


@pytest.mark.unit
async def test_timer_of_an_unknown_obligation_still_goes_to_decide(load: Load) -> None:
    trigger = event(EventType.TIMER_DUE, data={"kind": "pickup_appointment"})

    result, model = await classify(load, trigger)

    assert result.decision.intent == Intent.OTHER
    assert result.should_act
    assert model.calls == []


@pytest.mark.unit
async def test_attachment_is_a_document(load: Load) -> None:
    bol = Attachment(attachment_id="att_1", filename="bol.jpg", content_type="image/jpeg", ref="r")
    trigger = event(
        EventType.ATTACHMENT_RECEIVED, channel=Channel.SMS, sender=DRIVER, attachments=(bol,)
    )

    result, model = await classify(load, trigger)

    assert (result.decision.intent, result.decision.act) == (Intent.DOCUMENT, True)
    assert model.calls == []


@pytest.mark.unit
async def test_message_with_only_an_attachment_is_a_document_without_a_model_call(
    load: Load,
) -> None:
    pod = Attachment(
        attachment_id="att_2", filename="pod.pdf", content_type="application/pdf", ref="r"
    )
    trigger = event(
        EventType.INBOUND_MESSAGE, channel=Channel.SMS, sender=DRIVER, body="  ", attachments=(pod,)
    )

    result, model = await classify(load, trigger)

    assert (result.decision.intent, result.decision.act) == (Intent.DOCUMENT, True)
    assert model.calls == []


@pytest.mark.unit
async def test_message_with_text_and_an_attachment_still_asks_the_model(load: Load) -> None:
    pod = Attachment(
        attachment_id="att_2", filename="pod.pdf", content_type="application/pdf", ref="r"
    )
    trigger = event(
        EventType.INBOUND_MESSAGE,
        channel=Channel.SMS,
        sender=DRIVER,
        body="delivered, here is the POD",
        attachments=(pod,),
    )
    model = ScriptedModel()
    model.push(ModelRole.GATE, GateDecision(intent=Intent.DOCUMENT, act=True, reason="pod"))

    await Gate(model).classify(context(load, trigger))

    assert len(model.calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize("event_type", [EventType.HERO_UPDATE, EventType.AGENT_ACTION])
async def test_hero_updates_and_own_actions_do_not_act(load: Load, event_type: EventType) -> None:
    result, model = await classify(load, event(event_type, body="done"))

    assert (result.decision.intent, result.decision.act) == (Intent.OTHER, False)
    assert not result.should_act
    assert model.calls == []


@pytest.mark.unit
async def test_inbound_message_makes_exactly_one_gate_call(load: Load) -> None:
    model = ScriptedModel("cheap-model")
    question = GateDecision(intent=Intent.QUESTION, act=True, reason="asks for the PO")
    model.push(ModelRole.GATE, question)

    result = await Gate(model, timeout_s=12.5).classify(
        context(load, sms("What's the PO number?"), obligations=(ETA_REQUEST,))
    )

    assert result.decision == question
    assert result.usage is not None
    assert result.usage.model == "cheap-model"
    assert len(model.calls) == 1
    call = model.calls[0]
    assert (call.role, call.schema) == (ModelRole.GATE, "GateDecision")
    assert call.request.timeout_s == 12.5
    assert call.request.tools == ()
    assert "What's the PO number?" in call.request.user
    assert "drv_20931 (driver)" in call.request.user
    assert "channel: sms" in call.request.user
    assert "eta_request: attempts 1" in call.request.user
    assert "acknowledgement" in call.request.system


@pytest.mark.unit
async def test_model_failure_propagates(load: Load) -> None:
    model = ScriptedModel()
    model.push(ModelRole.GATE, LlmError("timeout"))

    with pytest.raises(LlmError, match="timeout"):
        await Gate(model).classify(context(load, sms("ok")))


@pytest.mark.unit
async def test_system_only_intent_from_the_model_becomes_other(load: Load) -> None:
    model = ScriptedModel()
    model.push(
        ModelRole.GATE, GateDecision(intent=Intent.DELAY_RISK_HIGH, act=False, reason="late")
    )

    result = await Gate(model).classify(context(load, sms("gonna be late")))

    assert result.decision.intent == Intent.OTHER
    assert result.decision.act is True
    assert "delay_risk_high" in result.decision.reason


@pytest.mark.unit
@pytest.mark.parametrize(
    ("intent", "act", "expected"),
    [
        (Intent.QUESTION, False, True),
        (Intent.ETA_PROVIDED, False, True),
        (Intent.DRIVER_DEPARTED, False, True),
        (Intent.ACKNOWLEDGEMENT, False, False),
        (Intent.ACKNOWLEDGEMENT, True, True),
        (Intent.STATUS_UPDATE, False, False),
        (Intent.OTHER, False, False),
    ],
)
def test_should_act_honors_must_act(intent: Intent, act: bool, expected: bool) -> None:
    result = GateResult(decision=GateDecision(intent=intent, act=act, reason="r"))

    assert result.should_act is expected


@pytest.mark.unit
def test_every_obligation_kind_has_a_follow_up_intent() -> None:
    for kind in ObligationKind:
        assert follow_up_intent(kind) is not None
    assert follow_up_intent(ObligationKind.ETA_REQUEST) == Intent.ETA_FOLLOW_UP_DUE
