"""The OpenRouter adapter against a scripted LangChain chat model. No network.

`FakeChat` plays queued `AIMessage`s (tool calls, usage) and records what the adapter
sent: the messages, the bound tools and the tool choice.
"""

import asyncio
import base64
import json
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.ai import InputTokenDetails, UsageMetadata
from langchain_core.messages.tool import invalid_tool_call, tool_call
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_openrouter import ChatOpenRouter
from pydantic import Field, SecretStr

from freight_agent.adapters.llm.models_config import (
    ModelPricing,
    ModelsConfig,
    ProviderRouting,
    RoleConfig,
)
from freight_agent.adapters.llm.openrouter import (
    OpenRouterModel,
    answer_tool,
    models_for_role,
    openrouter_chat,
)
from freight_agent.domain.actions import Plan, Purpose, ScheduleTimer, SendEmail, SendSms
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.obligations import ObligationKind
from freight_agent.ports.llm import ImageInput, LlmError, LlmRequest, ModelRole, ToolSpec

KEY = "sk-or-v1-never-show-me-0123456789"
ROLE = RoleConfig(model="acme/small", temperature=0, max_tokens=512)
PRICING = ModelPricing(input=0.10, output=0.40, cached_input=0.01)

GATE = LlmRequest(
    role=ModelRole.GATE, system="classify", user="driver: ok thanks", max_tool_rounds=0
)
ACK = {"intent": "acknowledgement", "act": False, "reason": "just ok"}
PLAN = {
    "actions": [
        {
            "type": "send_sms",
            "contact_id": "drv-1",
            "text": "Hi, what is your ETA to pickup?",
            "purpose": "eta_request",
        }
    ],
    "rationale": "delay risk is high: the SOP says to ask the driver",
}


@dataclass
class SentCall:
    messages: list[BaseMessage]
    tools: list[dict[str, Any]]
    tool_choice: Any

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.tools]


class FakeChat(BaseChatModel):
    script: list[Any] = Field(default_factory=list)  # AIMessage or an exception to raise
    sent: list[Any] = Field(default_factory=list)  # SentCall per model call
    delay_s: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[LanguageModelInput, AIMessage]:
        return self.bind(tools=list(tools), tool_choice=tool_choice, **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("the adapter only calls the model asynchronously")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.sent.append(
            SentCall(list(messages), kwargs.get("tools", []), kwargs.get("tool_choice"))
        )
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return ChatResult(generations=[ChatGeneration(message=reply)])


@pytest.fixture(autouse=True)
def _no_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's shell may enable LangSmith; unit tests must not send traces."""
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(name, raising=False)


def _usage(input_tokens: int = 0, output_tokens: int = 0, cached: int = 0) -> UsageMetadata:
    return UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_details=InputTokenDetails(cache_read=cached),
    )


def calls(*items: tuple[str, dict[str, Any]], usage: UsageMetadata | None = None) -> AIMessage:
    """A reply that calls tools; ids are c1, c2, ... in order."""
    tool_calls = [tool_call(name=n, args=a, id=f"c{i}") for i, (n, a) in enumerate(items, 1)]
    return AIMessage(content="", tool_calls=tool_calls, usage_metadata=usage or _usage(100, 10))


def answers(schema: str, args: dict[str, Any], usage: UsageMetadata | None = None) -> AIMessage:
    return calls((schema, args), usage=usage)


def _clock(*readings: float) -> Callable[[], float]:
    values: Iterator[float] = iter(readings)
    return lambda: next(values)


def make_model(
    *replies: AIMessage | BaseException,
    delay_s: float = 0.0,
    timer: Callable[[], float] | None = None,
) -> tuple[OpenRouterModel, FakeChat]:
    chat = FakeChat(script=list(replies), delay_s=delay_s)
    model = OpenRouterModel(
        ROLE,
        PRICING,
        SecretStr(KEY),
        chat_factory=lambda _role, _key: chat,
        timer=timer or _clock(0.0, 0.0),
    )
    return model, chat


def sop_tool(log: list[dict[str, Any]], fail: Exception | None = None) -> ToolSpec:
    async def run(args: dict[str, Any]) -> str:
        log.append(args)
        if fail is not None:
            raise fail
        return f"# {args.get('section_id')}: body"

    return ToolSpec(
        name="read_sop_section",
        description="Read one SOP section.",
        parameters={
            "type": "object",
            "properties": {"section_id": {"type": "string"}},
            "required": ["section_id"],
        },
        run=run,
    )


def decide(*tools: ToolSpec, max_tool_rounds: int = 2, timeout_s: float = 60.0) -> LlmRequest:
    return LlmRequest(
        role=ModelRole.AGENT,
        system="follow the SOP",
        user="delay risk is high",
        tools=tools,
        max_tool_rounds=max_tool_rounds,
        timeout_s=timeout_s,
    )


# Structured output


@pytest.mark.unit
async def test_the_gate_gets_direct_structured_output_in_one_forced_call() -> None:
    model, chat = make_model(answers("GateDecision", ACK))

    response = await model.generate(GATE, GateDecision)

    assert response.parsed == GateDecision(
        intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok"
    )
    (sent,) = chat.sent
    assert [type(m) for m in sent.messages] == [SystemMessage, HumanMessage]
    assert [m.content for m in sent.messages] == ["classify", "driver: ok thanks"]
    assert sent.tool_names == ["GateDecision"]
    assert sent.tool_choice == "GateDecision"


@pytest.mark.unit
async def test_tools_are_not_offered_when_max_tool_rounds_is_zero() -> None:
    log: list[dict[str, Any]] = []
    model, chat = make_model(answers("Plan", PLAN))

    response = await model.generate(decide(sop_tool(log), max_tool_rounds=0), Plan)

    assert len(response.parsed.actions) == 1
    (sent,) = chat.sent
    assert sent.tool_names == ["Plan"]
    assert sent.tool_choice == "Plan"
    assert log == []


@pytest.mark.unit
async def test_a_request_without_tools_is_one_forced_call_whatever_the_round_limit() -> None:
    model, chat = make_model(answers("Plan", PLAN))

    await model.generate(decide(max_tool_rounds=2), Plan)

    assert [s.tool_choice for s in chat.sent] == ["Plan"]


@pytest.mark.unit
async def test_the_answer_tool_is_portable_across_providers() -> None:
    """No `oneOf`, no dangling `discriminator`, no `const`; the action `type` is required,
    so a small model cannot leave out the field that picks the action."""
    tool = answer_tool(Plan)

    text = json.dumps(tool)
    assert "oneOf" not in text
    assert "discriminator" not in text
    assert '"const"' not in text
    variants = tool["function"]["parameters"]["properties"]["actions"]["items"]["anyOf"]
    send_sms = next(v for v in variants if v["title"] == "SendSms")
    assert send_sms["properties"]["type"]["enum"] == ["send_sms"]
    assert "type" in send_sms["required"]
    assert all("type" in v["required"] for v in variants)


@pytest.mark.unit
@pytest.mark.parametrize("flat", [False, True])
async def test_the_plan_asks_for_the_rationale_before_the_actions(flat: bool) -> None:
    """Small models wrote "send the driver an SMS" in the rationale after an empty action
    list: the reasoning comes first, then the actions it names."""
    params = answer_tool(Plan, flatten_unions=flat)["function"]["parameters"]

    assert list(params["properties"]) == ["rationale", "actions"]
    assert "before" in params["properties"]["rationale"]["description"]
    assert "rationale" in params["properties"]["actions"]["description"]


@pytest.mark.unit
async def test_a_flat_answer_tool_has_no_union_at_all() -> None:
    """No `anyOf` either: Gemini through OpenRouter drops a union under `items`, and the
    model then sends `actions: []` whatever its rationale says."""
    tool = answer_tool(Plan, flatten_unions=True)

    text = json.dumps(tool)
    for keyword in ('"oneOf"', '"anyOf"', '"discriminator"', '"const"'):
        assert keyword not in text


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model_id", "flat"),
    [
        ("google/gemini-2.5-flash-lite", True),
        ("openai/gpt-4.1-nano", False),
        ("openai/gpt-oss-120b", False),
    ],
)
async def test_only_gemini_gets_the_flat_answer_tool(model_id: str, flat: bool) -> None:
    """GPT models given the flat object fill every field of it in one item, so two
    actions become one; they keep the union, which they read well."""
    chat = FakeChat(script=[answers("Plan", PLAN)])
    model = OpenRouterModel(
        RoleConfig(model=model_id, max_tokens=512),
        PRICING,
        SecretStr(KEY),
        chat_factory=lambda _role, _key: chat,
        timer=_clock(0.0, 0.0),
    )

    await model.generate(decide(), Plan)

    (sent,) = chat.sent
    items = sent.tools[0]["function"]["parameters"]["properties"]["actions"]["items"]
    assert ("anyOf" not in items) is flat


@pytest.mark.unit
async def test_the_action_union_goes_out_as_one_flat_object_picked_by_its_type() -> None:
    """`type` is the one required field and lists every action; the fields of every action
    sit side by side, each saying which actions use it."""
    tool = answer_tool(Plan, flatten_unions=True)

    item = tool["function"]["parameters"]["properties"]["actions"]["items"]
    assert item["type"] == "object"
    assert item["required"] == ["type"]
    properties = item["properties"]
    assert next(iter(properties)) == "type"
    assert properties["type"]["enum"] == [
        "send_sms",
        "send_email",
        "post_broker_slack",
        "write_tms_note",
        "create_hero_task",
        "post_internal_slack",
        "update_stage",
        "schedule_timer",
        "defer_obligation",
        "close_obligation",
        "open_obligation",
    ]
    assert "schedule_timer: kind, due_in_minutes" in properties["type"]["description"]
    assert properties["due_in_minutes"]["type"] == "integer"
    assert properties["due_in_minutes"]["description"].startswith("Only for schedule_timer.")
    assert properties["subject"] == {"type": "string", "description": "Only for send_email."}
    # Shared by several actions: only the constraints they all have are kept.
    assert properties["text"]["minLength"] == 1
    assert "maxLength" not in properties["text"]
    assert "default" not in properties["purpose"]


@pytest.mark.unit
async def test_a_flat_answer_is_narrowed_back_to_the_fields_of_its_action_type() -> None:
    """Small models fill the fields of the other actions too, often with null; those are
    dropped before validation, so `extra="forbid"` still guards the real fields."""
    plan = {
        "rationale": "first step",
        "actions": [
            {
                "type": "send_sms",
                "contact_id": "drv-1",
                "text": "What time will you arrive at the pickup?",
                "purpose": "eta_request",
                "kind": None,
                "due_in_minutes": None,
                "subject": None,
            },
            {"type": "schedule_timer", "kind": "eta_request", "due_in_minutes": 30, "text": ""},
            {"type": "send_email", "to_contact_ids": ["dsp-1"], "body": "ETA?", "subject": None},
        ],
    }
    model, _ = make_model(answers("Plan", plan))

    response = await model.generate(decide(), Plan)

    sms, timer, email = response.parsed.actions
    assert sms == SendSms(
        contact_id="drv-1",
        text="What time will you arrive at the pickup?",
        purpose=Purpose.ETA_REQUEST,
    )
    assert timer == ScheduleTimer(kind=ObligationKind.ETA_REQUEST, due_in_minutes=30)
    assert email == SendEmail(to_contact_ids=("dsp-1",), body="ETA?", subject=None)


@pytest.mark.unit
async def test_a_missing_field_of_the_chosen_action_is_still_an_llm_error() -> None:
    plan = {"actions": [{"type": "schedule_timer", "kind": "eta_request"}], "rationale": "x"}
    model, _ = make_model(answers("Plan", plan))

    with pytest.raises(LlmError, match=r"Plan.*due_in_minutes"):
        await model.generate(decide(), Plan)


@pytest.mark.unit
async def test_an_unknown_action_type_is_still_an_llm_error() -> None:
    plan = {"actions": [{"type": "call_driver", "contact_id": "drv-1"}], "rationale": "x"}
    model, _ = make_model(answers("Plan", plan))

    with pytest.raises(LlmError, match="Plan"):
        await model.generate(decide(), Plan)


# Tool loop


@pytest.mark.unit
async def test_the_tool_loop_runs_the_tool_and_returns_the_plan() -> None:
    log: list[dict[str, Any]] = []
    model, chat = make_model(
        calls(("read_sop_section", {"section_id": "escalation"})),
        answers("Plan", PLAN),
    )

    response = await model.generate(decide(sop_tool(log)), Plan)

    assert response.parsed.actions == (
        SendSms(
            contact_id="drv-1",
            text="Hi, what is your ETA to pickup?",
            purpose=Purpose.ETA_REQUEST,
        ),
    )
    assert log == [{"section_id": "escalation"}]
    first, second = chat.sent
    assert first.tool_names == ["read_sop_section", "Plan"]
    assert first.tool_choice == "required"
    result = second.messages[-1]
    assert isinstance(result, ToolMessage)
    assert (result.tool_call_id, result.content) == ("c1", "# escalation: body")


@pytest.mark.unit
async def test_a_tool_spec_is_bound_as_a_function_with_its_json_schema() -> None:
    spec = sop_tool([])
    model, chat = make_model(answers("Plan", PLAN))

    await model.generate(decide(spec), Plan)

    assert chat.sent[0].tools[0] == {
        "type": "function",
        "function": {
            "name": "read_sop_section",
            "description": "Read one SOP section.",
            "parameters": spec.parameters,
        },
    }


@pytest.mark.unit
async def test_a_model_that_keeps_calling_tools_is_forced_to_answer() -> None:
    log: list[dict[str, Any]] = []
    model, chat = make_model(
        calls(("read_sop_section", {"section_id": "a"})),
        calls(("read_sop_section", {"section_id": "b"})),
        answers("Plan", PLAN),
    )

    response = await model.generate(decide(sop_tool(log), max_tool_rounds=2), Plan)

    assert response.parsed.rationale == PLAN["rationale"]
    assert [s.tool_choice for s in chat.sent] == ["required", "required", "Plan"]
    assert log == [{"section_id": "a"}, {"section_id": "b"}]


@pytest.mark.unit
async def test_a_forced_call_that_still_calls_a_tool_is_an_error() -> None:
    model, _ = make_model(
        calls(("read_sop_section", {"section_id": "a"})),
        calls(("read_sop_section", {"section_id": "b"})),
    )

    with pytest.raises(LlmError, match=r"acme/small.*did not return a Plan"):
        await model.generate(decide(sop_tool([]), max_tool_rounds=1), Plan)


@pytest.mark.unit
async def test_a_prose_reply_is_followed_by_one_forced_answer() -> None:
    model, chat = make_model(AIMessage(content="I would text the driver."), answers("Plan", PLAN))

    response = await model.generate(decide(sop_tool([])), Plan)

    assert len(response.parsed.actions) == 1
    assert [s.tool_choice for s in chat.sent] == ["required", "Plan"]
    nudge = chat.sent[1].messages[-1]
    assert isinstance(nudge, HumanMessage)
    assert "Plan" in nudge.text


@pytest.mark.unit
async def test_claude_tool_rounds_use_auto_since_no_endpoint_takes_required() -> None:
    """OpenRouter has no Anthropic endpoint for `tool_choice: "required"` (a 404 for
    Claude Haiku 5.5), so the rounds let the model choose; prose still ends in one
    forced answer."""
    chat = FakeChat(
        script=[calls(("read_sop_section", {"section_id": "escalation"})), answers("Plan", PLAN)]
    )
    model = OpenRouterModel(
        RoleConfig(model="anthropic/claude-haiku-5.5", max_tokens=512),
        PRICING,
        SecretStr(KEY),
        chat_factory=lambda _role, _key: chat,
        timer=_clock(0.0, 0.0),
    )

    await model.generate(decide(sop_tool([])), Plan)

    assert [s.tool_choice for s in chat.sent] == ["auto", "auto"]


@pytest.mark.unit
async def test_an_answer_next_to_a_tool_call_runs_the_tool_then_ends_the_loop() -> None:
    """The agent recomputes its staged plan from the transcript, so a tool call in the
    transcript must have run: one made next to the answer runs too, before the answer."""
    log: list[dict[str, Any]] = []
    model, chat = make_model(calls(("read_sop_section", {"section_id": "a"}), ("Plan", PLAN)))

    response = await model.generate(decide(sop_tool(log)), Plan)

    assert len(response.parsed.actions) == 1
    assert len(chat.sent) == 1
    assert log == [{"section_id": "a"}]
    assert [t["role"] for t in response.transcript] == ["system", "user", "assistant", "tool"]
    assert response.transcript[3]["tool_call_id"] == "c1"


@pytest.mark.unit
async def test_a_tool_call_next_to_the_forced_answer_runs_too() -> None:
    log: list[dict[str, Any]] = []
    model, _ = make_model(
        calls(("read_sop_section", {"section_id": "a"})),
        calls(("read_sop_section", {"section_id": "b"}), ("Plan", PLAN)),
    )

    await model.generate(decide(sop_tool(log), max_tool_rounds=1), Plan)

    assert log == [{"section_id": "a"}, {"section_id": "b"}]


@pytest.mark.unit
async def test_the_cost_cap_forces_the_answer_after_the_round_that_reaches_it() -> None:
    """Each round of 1,000 input tokens costs $0.0001 here; a $0.0002 cap stops the loop
    after the second round, and one forced call answers."""
    log: list[dict[str, Any]] = []
    model, chat = make_model(
        calls(("read_sop_section", {"section_id": "a"}), usage=_usage(1_000, 0)),
        calls(("read_sop_section", {"section_id": "b"}), usage=_usage(1_000, 0)),
        answers("Plan", PLAN),
    )
    request = replace(decide(sop_tool(log), max_tool_rounds=8), max_cost_usd=0.0002)

    response = await model.generate(request, Plan)

    assert response.parsed.rationale == PLAN["rationale"]
    assert [s.tool_choice for s in chat.sent] == ["required", "required", "Plan"]
    assert log == [{"section_id": "a"}, {"section_id": "b"}]


@pytest.mark.unit
async def test_without_a_cost_cap_the_rounds_go_on() -> None:
    model, chat = make_model(
        calls(("read_sop_section", {"section_id": "a"}), usage=_usage(1_000_000, 0)),
        answers("Plan", PLAN),
    )

    await model.generate(decide(sop_tool([]), max_tool_rounds=8), Plan)

    assert [s.tool_choice for s in chat.sent] == ["required", "required"]


@pytest.mark.unit
async def test_each_assistant_entry_of_the_transcript_carries_its_rounds_usage() -> None:
    model, _ = make_model(
        calls(("read_sop_section", {"section_id": "a"}), usage=_usage(1_000, 50, cached=400)),
        answers("Plan", PLAN, usage=_usage(1_200, 150)),
    )

    response = await model.generate(decide(sop_tool([])), Plan)

    first, second = (t for t in response.transcript if t["role"] == "assistant")
    assert first["usage"] == {
        "input_tokens": 1_000,
        "output_tokens": 50,
        "cached_tokens": 400,
        "cost_usd": PRICING.cost_usd(input_tokens=1_000, output_tokens=50, cached_tokens=400),
    }
    assert second["usage"]["input_tokens"] == 1_200
    assert "usage" not in response.transcript[0]
    total = first["usage"]["cost_usd"] + second["usage"]["cost_usd"]
    assert response.usage.cost_usd == pytest.approx(total)


@pytest.mark.unit
async def test_images_go_with_the_user_prompt_as_data_url_parts() -> None:
    model, chat = make_model(answers("GateDecision", ACK))
    png = b"\x89PNG\r\n\x1a\nfake"
    request = replace(GATE, images=(ImageInput(data=png, media_type="image/png"),))

    response = await model.generate(request, GateDecision)

    user = chat.sent[0].messages[1]
    assert isinstance(user, HumanMessage)
    text, image = user.content
    assert text == {"type": "text", "text": GATE.user}
    encoded = base64.b64encode(png).decode()
    assert image == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
    assert response.transcript[1] == {"role": "user", "content": GATE.user, "images": 1}


@pytest.mark.unit
async def test_a_prompt_without_images_is_plain_text() -> None:
    model, chat = make_model(answers("GateDecision", ACK))

    response = await model.generate(GATE, GateDecision)

    assert chat.sent[0].messages[1].content == GATE.user
    assert response.transcript[1] == {"role": "user", "content": GATE.user}


@pytest.mark.unit
async def test_an_unknown_tool_gets_an_answer_listing_the_real_ones() -> None:
    model, chat = make_model(calls(("read_everything", {})), answers("Plan", PLAN))

    await model.generate(decide(sop_tool([])), Plan)

    result = chat.sent[1].messages[-1]
    assert isinstance(result, ToolMessage)
    assert "Unknown tool 'read_everything'" in result.text
    assert "read_sop_section" in result.text


@pytest.mark.unit
async def test_a_failing_tool_is_reported_to_the_model_and_the_loop_goes_on() -> None:
    log: list[dict[str, Any]] = []
    model, chat = make_model(
        calls(("read_sop_section", {"section_id": "a"})), answers("Plan", PLAN)
    )

    await model.generate(decide(sop_tool(log, fail=RuntimeError("index offline"))), Plan)

    result = chat.sent[1].messages[-1]
    assert isinstance(result, ToolMessage)
    assert "index offline" in result.text


@pytest.mark.unit
async def test_tool_arguments_that_are_not_json_get_an_error_reply() -> None:
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            invalid_tool_call(name="read_sop_section", args="{oops", id="c9", error="bad JSON")
        ],
    )
    model, chat = make_model(broken, answers("Plan", PLAN))

    await model.generate(decide(sop_tool([])), Plan)

    result = chat.sent[1].messages[-1]
    assert isinstance(result, ToolMessage)
    assert result.tool_call_id == "c9"
    assert "not valid JSON" in result.text


# Usage, cost and transcript


@pytest.mark.unit
async def test_usage_sums_every_round_and_is_priced() -> None:
    model, _ = make_model(
        calls(("read_sop_section", {"section_id": "a"}), usage=_usage(1_000, 50, cached=400)),
        answers("Plan", PLAN, usage=_usage(1_200, 150, cached=1_000)),
        timer=_clock(10.0, 10.25),
    )

    response = await model.generate(decide(sop_tool([])), Plan)

    usage = response.usage
    assert (usage.role, usage.model) == ("agent", "acme/small")
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (2_200, 200, 1_400)
    assert usage.cost_usd == pytest.approx(
        PRICING.cost_usd(input_tokens=2_200, output_tokens=200, cached_tokens=1_400)
    )
    assert usage.latency_ms == 250


@pytest.mark.unit
async def test_a_reply_without_usage_metadata_counts_as_zero_tokens() -> None:
    bare = AIMessage(content="", tool_calls=[tool_call(name="GateDecision", args=ACK, id="c1")])
    model, _ = make_model(bare)

    response = await model.generate(GATE, GateDecision)

    assert (response.usage.input_tokens, response.usage.cost_usd) == (0, 0.0)


@pytest.mark.unit
async def test_the_transcript_is_plain_json_ready_dicts() -> None:
    model, _ = make_model(calls(("read_sop_section", {"section_id": "a"})), answers("Plan", PLAN))

    response = await model.generate(decide(sop_tool([])), Plan)

    transcript = response.transcript
    assert [t["role"] for t in transcript] == ["system", "user", "assistant", "tool", "assistant"]
    assert transcript[2]["tool_calls"] == [
        {"id": "c1", "name": "read_sop_section", "args": {"section_id": "a"}}
    ]
    assert transcript[3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "read_sop_section",
        "content": "# a: body",
    }
    assert transcript[4]["tool_calls"][0]["name"] == "Plan"
    json.dumps(transcript)


# Errors


@pytest.mark.unit
async def test_a_provider_error_becomes_an_llm_error_naming_the_model() -> None:
    model, _ = make_model(RuntimeError("502 upstream unavailable"))

    with pytest.raises(LlmError, match=r"acme/small \(gate\).*502 upstream unavailable"):
        await model.generate(GATE, GateDecision)


@pytest.mark.unit
async def test_an_answer_that_does_not_match_the_schema_is_an_llm_error() -> None:
    model, _ = make_model(answers("GateDecision", {"intent": 42, "act": True, "reason": "x"}))

    with pytest.raises(LlmError, match=r"acme/small.*GateDecision.*intent"):
        await model.generate(GATE, GateDecision)


@pytest.mark.unit
async def test_a_prose_reply_to_a_forced_call_is_an_llm_error() -> None:
    model, _ = make_model(AIMessage(content="It is an acknowledgement."))

    with pytest.raises(LlmError, match="did not return a GateDecision"):
        await model.generate(GATE, GateDecision)


@pytest.mark.unit
async def test_answer_arguments_that_are_not_json_are_an_llm_error() -> None:
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            invalid_tool_call(name="GateDecision", args='{"intent": ', id="c1", error="bad JSON")
        ],
    )
    model, _ = make_model(broken)

    with pytest.raises(LlmError, match="not valid JSON"):
        await model.generate(GATE, GateDecision)


@pytest.mark.unit
async def test_a_slow_model_times_out_as_an_llm_error() -> None:
    model, _ = make_model(answers("GateDecision", ACK), delay_s=5.0)
    request = LlmRequest(
        role=ModelRole.GATE, system="s", user="u", max_tool_rounds=0, timeout_s=0.01
    )

    with pytest.raises(LlmError, match=r"acme/small \(gate\): timed out after 0.01 s"):
        await model.generate(request, GateDecision)


@pytest.mark.unit
async def test_the_timeout_covers_the_whole_tool_loop() -> None:
    model, _ = make_model(
        calls(("read_sop_section", {"section_id": "a"})),
        calls(("read_sop_section", {"section_id": "b"})),
        answers("Plan", PLAN),
        delay_s=0.02,
    )

    with pytest.raises(LlmError, match="timed out"):
        await model.generate(decide(sop_tool([]), timeout_s=0.05), Plan)


@pytest.mark.unit
async def test_the_key_never_shows_in_repr_or_in_error_messages() -> None:
    model, _ = make_model(RuntimeError(f"401 Unauthorized: key {KEY} was rejected"))

    with pytest.raises(LlmError) as caught:
        await model.generate(GATE, GateDecision)

    assert KEY not in str(caught.value)
    assert "401 Unauthorized" in str(caught.value)
    assert KEY not in "".join(traceback.format_exception(caught.value))  # a logged traceback
    assert KEY not in repr(model)
    assert KEY not in str(model)


@pytest.mark.unit
async def test_a_long_provider_error_is_cut_after_the_key_is_redacted() -> None:
    """Cutting first could leave half a key at the cut."""
    model, _ = make_model(RuntimeError("x" * 476 + KEY))

    with pytest.raises(LlmError) as caught:
        await model.generate(GATE, GateDecision)

    assert KEY[:8] not in str(caught.value)
    assert len(str(caught.value)) < 600


@pytest.mark.unit
def test_an_empty_key_is_rejected_when_the_model_is_built() -> None:
    with pytest.raises(ValueError, match="key"):
        OpenRouterModel(ROLE, PRICING, SecretStr(" "), chat_factory=lambda _r, _k: FakeChat())


@pytest.mark.unit
async def test_a_tool_named_like_the_answer_is_refused() -> None:
    clash = ToolSpec(name="Plan", description="d", parameters={}, run=sop_tool([]).run)
    model, _ = make_model(answers("Plan", PLAN))

    with pytest.raises(ValueError, match="Plan"):
        await model.generate(decide(clash), Plan)


# Wiring


@pytest.mark.unit
def test_the_default_factory_maps_the_role_settings_onto_chat_openrouter() -> None:
    role = RoleConfig(
        model="openai/gpt-4.1-nano",
        temperature=0,
        max_tokens=256,
        reasoning_effort="low",
        provider=ProviderRouting(order=("openai",), allow_fallbacks=False),
    )

    chat = openrouter_chat(role, SecretStr(KEY))

    assert isinstance(chat, ChatOpenRouter)
    assert (chat.model_name, chat.temperature, chat.max_tokens) == ("openai/gpt-4.1-nano", 0, 256)
    assert chat.openrouter_provider == {
        "order": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert chat.reasoning == {"effort": "low"}
    assert chat.max_retries == 0  # retries and fallbacks belong to the resilient wrapper
    assert KEY not in repr(chat)


@pytest.mark.unit
def test_models_for_role_builds_one_model_per_entry_of_the_chain() -> None:
    config = ModelsConfig(
        roles={
            role: RoleConfig(model="acme/small", fallbacks=("acme/tiny",), max_tokens=64)
            for role in ModelRole
        },
        pricing={
            "acme/small": PRICING,
            "acme/tiny": ModelPricing(input=0.05, output=0.2),
            "acme/mid": ModelPricing(input=0.2, output=0.8),
        },
    )
    built: list[str] = []

    def factory(role: RoleConfig, _key: SecretStr) -> BaseChatModel:
        built.append(role.model)
        return FakeChat()

    models = models_for_role(config, ModelRole.AGENT, SecretStr(KEY), chat_factory=factory)

    assert [m.model_id for m in models] == ["acme/small", "acme/tiny"]
    assert built == ["acme/small", "acme/tiny"]


class _StubCompletions:
    """Stands in for the OpenRouter SDK's `client.chat`: records requests, plays replies."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.requests: list[dict[str, Any]] = []

    async def send_async(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return self.replies.pop(0)


def _completion(name: str, args: dict[str, Any], call_id: str) -> dict[str, Any]:
    """A chat completion as OpenRouter returns it, with one tool call and usage."""
    call = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    return {
        "id": f"gen-{call_id}",
        "model": "openai/gpt-4.1-nano",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": None, "tool_calls": [call]},
            }
        ],
        "usage": {
            "prompt_tokens": 300,
            "completion_tokens": 40,
            "total_tokens": 340,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
    }


@pytest.mark.unit
async def test_the_real_chat_openrouter_sends_the_expected_request() -> None:
    """Drives ChatOpenRouter's own conversion code with only its SDK client stubbed, so the
    assumptions taken from its source (tool binding, tool choice, message and usage
    mapping) break this test, not a live run, when the library changes."""
    role = RoleConfig(
        model="openai/gpt-4.1-nano",
        temperature=0,
        max_tokens=256,
        provider=ProviderRouting(order=("openai",), allow_fallbacks=False),
    )
    completions = _StubCompletions(
        [
            _completion("read_sop_section", {"section_id": "escalation"}, "call_1"),
            _completion("Plan", PLAN, "call_2"),
        ]
    )

    def factory(config: RoleConfig, key: SecretStr) -> BaseChatModel:
        chat = openrouter_chat(config, key)
        assert isinstance(chat, ChatOpenRouter)
        chat.client = SimpleNamespace(chat=completions)
        return chat

    model = OpenRouterModel(role, PRICING, SecretStr(KEY), chat_factory=factory)

    # One tool round, then the call that forces the answer.
    response = await model.generate(decide(sop_tool([]), max_tool_rounds=1), Plan)

    assert response.parsed.rationale == PLAN["rationale"]
    first, second = completions.requests
    assert (first["model"], first["temperature"], first["max_tokens"]) == (
        "openai/gpt-4.1-nano",
        0,
        256,
    )
    assert first["provider"] == {
        "order": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert first["tool_choice"] == "required"
    assert [t["function"]["name"] for t in first["tools"]] == ["read_sop_section", "Plan"]
    assert second["tool_choice"] == {"type": "function", "function": {"name": "Plan"}}
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][3]["tool_call_id"] == "call_1"
    assert (response.usage.input_tokens, response.usage.cached_tokens) == (600, 400)
    assert response.usage.output_tokens == 80
