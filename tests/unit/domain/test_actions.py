"""The action vocabulary: `requests` on outbound messages, `open_obligation`, and the
agent's outcome.

Owner: card W3-AGT (C0 wrote the tests of the shape it shipped).
"""

import pytest
from pydantic import TypeAdapter, ValidationError

from freight_agent.domain.actions import (
    FINISH,
    Action,
    AgentOutcome,
    OpenObligation,
    Plan,
    Purpose,
    SendEmail,
    SendSms,
)

ACTION = TypeAdapter[Action](Action)


@pytest.mark.unit
def test_an_eta_request_sms_requests_the_eta_request_obligation() -> None:
    sms = SendSms(contact_id="drv", text="ETA?", purpose=Purpose.ETA_REQUEST)

    assert sms.requests == "eta_request"
    raw = {"contact_id": "drv", "text": "ETA?", "purpose": "eta_request"}
    assert SendSms.model_validate(raw).requests == "eta_request"


@pytest.mark.unit
def test_a_reply_requests_nothing_unless_it_says_so() -> None:
    assert SendSms(contact_id="drv", text="The PO is 55821.").requests is None
    pod = SendSms(contact_id="drv", text="Please send the POD.", requests="pod")
    assert (pod.purpose, pod.requests) == (Purpose.REPLY, "pod")


@pytest.mark.unit
def test_an_eta_request_that_names_another_kind_is_refused() -> None:
    with pytest.raises(ValidationError, match="eta_request"):
        SendSms(contact_id="drv", text="ETA?", purpose=Purpose.ETA_REQUEST, requests="pod")


@pytest.mark.unit
def test_an_email_takes_the_same_rule() -> None:
    email = SendEmail(to_contact_ids=("dsp",), body="ETA?", purpose=Purpose.ETA_REQUEST)

    assert email.requests == "eta_request"
    assert SendEmail(to_contact_ids=("dsp",), body="Thanks.").requests is None


@pytest.mark.unit
def test_a_plan_read_back_keeps_requests() -> None:
    plan = Plan(actions=(SendSms(contact_id="drv", text="ETA?", purpose=Purpose.ETA_REQUEST),))

    assert Plan.model_validate(plan.model_dump(mode="json")) == plan


@pytest.mark.unit
def test_open_obligation_is_an_action() -> None:
    action = ACTION.validate_python({"type": "open_obligation", "kind": "pod"})

    assert action == OpenObligation(kind="pod")


@pytest.mark.unit
def test_the_agent_outcome_is_the_agents_reading_without_actions() -> None:
    """The agent acts only through its action tools, so its answer carries no actions:
    an answer that lists some is refused, not silently dropped."""
    outcome = AgentOutcome(rationale="r")

    assert (outcome.rationale, outcome.intent, outcome.no_procedure) == ("r", None, False)
    assert not isinstance(outcome, Plan)
    labelled = AgentOutcome(intent="detention_request", no_procedure=True)
    assert (labelled.intent, labelled.no_procedure) == ("detention_request", True)
    with pytest.raises(ValidationError, match="actions"):
        AgentOutcome.model_validate({"rationale": "r", "actions": []})


@pytest.mark.unit
def test_the_agent_outcome_is_the_finish_tool() -> None:
    assert AgentOutcome.model_json_schema()["title"] == FINISH
