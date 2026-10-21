"""The OpenRouter adapter against a fake LangChain chat model. No network.

`FakeChat` plays queued `AIMessage`s (tool calls, usage, reasoning) and records what the
adapter sent: the messages, the bound tools, the tool choice and the run config. Each
method is one model call: `generate` forces the answer tool, `chat` is one agent round.
"""

import asyncio
import base64
import json
import traceback
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
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
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openrouter import ChatOpenRouter
from openrouter import components, errors
from pydantic import Field, SecretStr

from freight_agent.adapters.llm.models_config import (
    FallbackConfig,
    ModelPricing,
    ModelsConfig,
    ProviderRouting,
    RoleConfig,
    load_models_config,
)
from freight_agent.adapters.llm.openrouter import (
    OpenRouterModel,
    answer_tool,
    models_for_role,
    openrouter_chat,
)
from freight_agent.domain.actions import FINISH, AgentOutcome
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.interfaces.llm import (
    AssistantTurn,
    ChatRequest,
    ImageInput,
    LlmError,
    LlmRequest,
    ModelRole,
    ToolCall,
    ToolResult,
    ToolSpec,
    TransientLlmError,
    UserTurn,
)

pytestmark = pytest.mark.unit

SHIPPED = Path(__file__).resolve().parents[3] / "config" / "models.yaml"
KEY = "sk-or-v1-never-show-me-0123456789"
ROLE = RoleConfig(model="acme/small", temperature=0, max_tokens=512)
PRICING = ModelPricing(input=0.10, output=0.40, cached_input=0.01)

GATE = LlmRequest(role=ModelRole.GATE, system="classify", user="driver: ok thanks")
ACK = {"intent": "acknowledgement", "act": False, "reason": "just ok"}
DONE = {"rationale": "delay_risk_high step 1: asked the driver for an ETA"}
READ_SOP = ToolSpec(
    name="read_sop_section",
    description="Read one SOP section.",
    parameters={
        "type": "object",
        "properties": {"section_id": {"type": "string"}},
        "required": ["section_id"],
    },
)


def round_request(*tools: ToolSpec, **options: Any) -> ChatRequest:
    """One agent round with `tools` (the SOP reader by default)."""
    return ChatRequest(
        role=ModelRole.AGENT,
        system="follow the SOP",
        user="delay risk is high",
        tools=tools or (READ_SOP,),
        **options,
    )


@dataclass
class SentCall:
    messages: list[BaseMessage]
    tools: list[dict[str, Any]]
    tool_choice: Any
    config: RunnableConfig

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

    async def ainvoke(
        self,
        input: LanguageModelInput,
        config: RunnableConfig | None = None,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        messages = self._convert_input(input).to_messages()
        self.sent.append(
            SentCall(messages, kwargs.get("tools", []), kwargs.get("tool_choice"), config or {})
        )
        return await super().ainvoke(input, config, stop=stop, **kwargs)

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


def calls(
    *items: tuple[str, dict[str, Any]],
    usage: UsageMetadata | None = None,
    **kwargs: Any,
) -> AIMessage:
    """A reply that calls tools; ids are c1, c2, ... in order."""
    tool_calls = [tool_call(name=n, args=a, id=f"c{i}") for i, (n, a) in enumerate(items, 1)]
    return AIMessage(
        content="", tool_calls=tool_calls, usage_metadata=usage or _usage(100, 10), **kwargs
    )


def answers(schema: str, args: dict[str, Any], usage: UsageMetadata | None = None) -> AIMessage:
    return calls((schema, args), usage=usage)


def _clock(*readings: float) -> Callable[[], float]:
    values: Iterator[float] = iter(readings)
    return lambda: next(values)


def make_model(
    *replies: AIMessage | BaseException,
    model_id: str = ROLE.model,
    delay_s: float = 0.0,
    timer: Callable[[], float] = lambda: 0.0,
) -> tuple[OpenRouterModel, FakeChat]:
    chat = FakeChat(script=list(replies), delay_s=delay_s)
    model = OpenRouterModel(
        ROLE.model_copy(update={"model": model_id}),
        PRICING,
        SecretStr(KEY),
        chat_factory=lambda _role, _key: chat,
        timer=timer,
    )
    return model, chat


# --- generate: one forced answer ---------------------------------------------------------


async def test_generate_forces_the_answer_tool_in_one_call() -> None:
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


def test_the_answer_tool_is_named_after_the_schema_and_says_it_answers() -> None:
    """The agent's answer is `finish`, by the title of `AgentOutcome`."""
    for schema, name in ((GateDecision, "GateDecision"), (AgentOutcome, FINISH)):
        function = answer_tool(schema)["function"]
        assert function["name"] == name
        assert function["description"].startswith("Give your final answer.")
        assert function["parameters"]["type"] == "object"


async def test_a_prose_reply_to_a_forced_call_is_an_llm_error() -> None:
    model, _ = make_model(AIMessage(content="It is an acknowledgement."))

    with pytest.raises(
        LlmError,
        match=r"acme/small \(gate\): did not return a GateDecision answer when forced to "
        r"\(replied in prose\)",
    ):
        await model.generate(GATE, GateDecision)


async def test_a_forced_call_answered_with_another_tool_names_that_tool() -> None:
    model, _ = make_model(calls(("read_sop_section", {"section_id": "a"})))

    with pytest.raises(LlmError, match=r"did not return a GateDecision .*called read_sop_section"):
        await model.generate(GATE, GateDecision)


async def test_answer_arguments_that_are_not_json_are_an_llm_error() -> None:
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            invalid_tool_call(name="GateDecision", args='{"intent": ', id="c1", error="bad JSON")
        ],
    )
    model, _ = make_model(broken)

    with pytest.raises(
        LlmError, match=r"acme/small \(gate\): the GateDecision arguments are not valid JSON"
    ):
        await model.generate(GATE, GateDecision)


async def test_an_answer_that_does_not_match_the_schema_is_an_llm_error_naming_the_model() -> None:
    model, _ = make_model(answers("GateDecision", {"intent": 42, "act": True, "reason": "x"}))

    with pytest.raises(
        LlmError, match=r"acme/small \(gate\): the answer does not match GateDecision: intent"
    ):
        await model.generate(GATE, GateDecision)


async def test_unknown_answer_fields_are_dropped_before_validation() -> None:
    """Small models add fields of their own; the schema forbids extras, so they go."""
    model, _ = make_model(answers("GateDecision", {**ACK, "confidence": 0.9, "notes": None}))

    response = await model.generate(GATE, GateDecision)

    assert response.parsed.reason == "just ok"


async def test_a_null_for_a_defaulted_field_takes_its_default() -> None:
    model, _ = make_model(answers(FINISH, {"rationale": None, "no_procedure": None}))

    response = await model.generate(replace(GATE, role=ModelRole.AGENT), AgentOutcome)

    assert response.parsed == AgentOutcome()


async def test_a_null_for_a_required_field_is_still_an_llm_error() -> None:
    model, _ = make_model(answers("GateDecision", {**ACK, "intent": None}))

    with pytest.raises(LlmError, match=r"GateDecision: intent"):
        await model.generate(GATE, GateDecision)


async def test_images_go_with_the_user_prompt_as_data_url_parts() -> None:
    model, chat = make_model(answers("GateDecision", ACK))
    png = b"\x89PNG\r\n\x1a\nfake"
    jpeg = b"\xff\xd8\xff fake"
    images = (ImageInput(data=png, media_type="image/png"), ImageInput(jpeg, "image/jpeg"))

    await model.generate(replace(GATE, images=images), GateDecision)

    user = chat.sent[0].messages[1]
    assert isinstance(user, HumanMessage)
    text, first, second = user.content
    assert text == {"type": "text", "text": GATE.user}
    assert first == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{base64.b64encode(png).decode()}"},
    }
    assert second == {
        "type": "image_url",
        "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"},
    }


async def test_a_prompt_without_images_is_plain_text() -> None:
    model, chat = make_model(answers("GateDecision", ACK))

    await model.generate(GATE, GateDecision)

    assert chat.sent[0].messages[1].content == GATE.user


# --- chat: one agent round ---------------------------------------------------------------


async def test_a_round_binds_the_tools_then_the_answer_tool_and_requires_a_call() -> None:
    model, chat = make_model(calls(("read_sop_section", {"section_id": "a"})))

    await model.chat(round_request(), AgentOutcome)

    (sent,) = chat.sent
    assert sent.tool_names == ["read_sop_section", FINISH]
    assert sent.tools[0] == {
        "type": "function",
        "function": {
            "name": "read_sop_section",
            "description": "Read one SOP section.",
            "parameters": READ_SOP.parameters,
        },
    }
    assert sent.tools[1] == answer_tool(AgentOutcome)
    assert sent.tool_choice == "required"


async def test_claude_rounds_use_auto_since_no_endpoint_takes_required() -> None:
    """OpenRouter has no Anthropic endpoint for `tool_choice: "required"` (a 404 for
    Claude Haiku 5.5); a forced call to the answer tool works there."""
    model, chat = make_model(
        calls(("read_sop_section", {"section_id": "a"})),
        answers(FINISH, DONE),
        model_id="anthropic/claude-haiku-5.5",
    )

    await model.chat(round_request(), AgentOutcome)
    await model.chat(round_request(force_answer=True), AgentOutcome)

    assert [s.tool_choice for s in chat.sent] == ["auto", FINISH]


async def test_a_forced_round_names_the_answer_tool_and_still_binds_the_tools() -> None:
    """The tools stay bound when the answer is forced: the history holds calls to them."""
    model, chat = make_model(answers(FINISH, DONE))

    reply = await model.chat(round_request(force_answer=True), AgentOutcome)

    (sent,) = chat.sent
    assert sent.tool_choice == FINISH
    assert sent.tool_names == ["read_sop_section", FINISH]
    assert reply.answer == AgentOutcome(rationale=DONE["rationale"])


async def test_a_round_without_tools_forces_the_answer() -> None:
    model, chat = make_model(answers(FINISH, DONE))
    request = replace(round_request(), tools=())

    await model.chat(request, AgentOutcome)

    (sent,) = chat.sent
    assert (sent.tool_names, sent.tool_choice) == ([FINISH], FINISH)


async def test_a_round_returns_every_call_the_finish_call_included() -> None:
    """A tool called next to the answer still runs: the graph reads every call."""
    model, _ = make_model(calls(("read_sop_section", {"section_id": "a"}), (FINISH, DONE)))

    reply = await model.chat(round_request(), AgentOutcome)

    assert reply.turn.tool_calls == (
        ToolCall(id="c1", name="read_sop_section", args={"section_id": "a"}),
        ToolCall(id="c2", name=FINISH, args=DONE),
    )
    assert reply.answer == AgentOutcome(rationale=DONE["rationale"])


async def test_a_round_that_did_not_call_finish_has_no_answer() -> None:
    model, _ = make_model(calls(("read_sop_section", {"section_id": "a"})))

    reply = await model.chat(round_request(), AgentOutcome)

    assert reply.answer is None
    assert [c.name for c in reply.turn.tool_calls] == ["read_sop_section"]


async def test_a_prose_round_has_no_answer_and_keeps_the_text() -> None:
    """Under `auto` a model may reply in prose; the graph then asks for the answer."""
    model, _ = make_model(AIMessage(content="I would text the driver."))

    reply = await model.chat(round_request(), AgentOutcome)

    assert reply.answer is None
    assert reply.turn == AssistantTurn(text="I would text the driver.")


async def test_a_forced_round_without_finish_is_an_llm_error() -> None:
    model, _ = make_model(calls(("read_sop_section", {"section_id": "a"})))

    with pytest.raises(
        LlmError,
        match=r"acme/small \(agent\): did not return a finish answer when forced to "
        r"\(called read_sop_section\)",
    ):
        await model.chat(round_request(force_answer=True), AgentOutcome)


async def test_a_finish_that_does_not_match_the_schema_is_an_llm_error() -> None:
    model, _ = make_model(answers(FINISH, {"rationale": "x", "no_procedure": "perhaps"}))

    with pytest.raises(LlmError, match=r"the answer does not match AgentOutcome: no_procedure"):
        await model.chat(round_request(), AgentOutcome)


async def test_unknown_finish_fields_and_nulls_of_defaulted_fields_are_dropped() -> None:
    """`actions` was part of the old answer; a model that still sends it is not wrong."""
    args = {"rationale": "x", "actions": [], "no_procedure": None, "intent": None}
    model, _ = make_model(answers(FINISH, args))

    reply = await model.chat(round_request(), AgentOutcome)

    assert reply.answer == AgentOutcome(rationale="x", no_procedure=False)


async def test_tool_calls_that_are_not_json_come_back_with_their_error() -> None:
    broken = AIMessage(
        content="",
        tool_calls=[tool_call(name="read_sop_section", args={"section_id": "a"}, id="c1")],
        invalid_tool_calls=[
            invalid_tool_call(name="read_sop_section", args="{oops", id="c9", error="bad JSON"),
            invalid_tool_call(name=FINISH, args='{"rationale": ', id="c10", error="cut off"),
        ],
    )
    model, _ = make_model(broken)

    reply = await model.chat(round_request(), AgentOutcome)

    assert reply.turn.tool_calls == (
        ToolCall(id="c1", name="read_sop_section", args={"section_id": "a"}),
        ToolCall(id="c9", name="read_sop_section", error="bad JSON"),
        ToolCall(id="c10", name=FINISH, error="cut off"),
    )
    assert reply.answer is None  # a broken finish is no answer: the graph answers its error


async def test_a_finish_that_is_not_json_in_a_forced_round_is_an_llm_error() -> None:
    broken = AIMessage(
        content="",
        invalid_tool_calls=[invalid_tool_call(name=FINISH, args="{", id="c1", error="bad JSON")],
    )
    model, _ = make_model(broken)

    with pytest.raises(LlmError, match=r"the finish arguments are not valid JSON \(bad JSON\)"):
        await model.chat(round_request(force_answer=True), AgentOutcome)


async def test_a_call_without_an_id_gets_one() -> None:
    """The tool's answer names the call it answers, so every call needs an id."""
    reply_message = AIMessage(
        content="",
        tool_calls=[
            tool_call(name="read_sop_section", args={"section_id": "a"}, id=None),
            tool_call(name="read_sop_section", args={"section_id": "b"}, id=None),
        ],
        invalid_tool_calls=[invalid_tool_call(name=None, args="{", id=None, error=None)],
    )
    model, _ = make_model(reply_message, reply_message)

    reply = await model.chat(round_request(), AgentOutcome)
    again = await model.chat(round_request(), AgentOutcome)

    calls = reply.turn.tool_calls
    assert [(c.name, c.args, c.error) for c in calls] == [
        ("read_sop_section", {"section_id": "a"}, None),
        ("read_sop_section", {"section_id": "b"}, None),
        ("", {}, ""),
    ]
    # Unique across the rounds of one loop, whose history holds them all.
    ids = [c.id for c in (*calls, *again.turn.tool_calls)]
    assert all(i.startswith("call_") for i in ids)
    assert len(set(ids)) == len(ids)


async def test_the_history_follows_the_system_and_user_messages() -> None:
    reasoning = {
        "reasoning_content": "The SOP says to ask the driver.",
        "reasoning_details": [{"type": "reasoning.text", "text": "ask the driver"}],
    }
    history = (
        AssistantTurn(
            text="Reading the SOP.",
            tool_calls=(ToolCall(id="c1", name="read_sop_section", args={"section_id": "a"}),),
            provider_data=reasoning,
        ),
        ToolResult(call_id="c1", name="read_sop_section", content="# a: body"),
        AssistantTurn(text="I would text the driver."),
        UserTurn(text="Answer now with finish."),
    )
    model, chat = make_model(answers(FINISH, DONE))

    await model.chat(round_request(history=history, force_answer=True), AgentOutcome)

    messages = chat.sent[0].messages
    assert [type(m) for m in messages] == [
        SystemMessage,
        HumanMessage,
        AIMessage,
        ToolMessage,
        AIMessage,
        HumanMessage,
    ]
    assert [messages[0].content, messages[1].content] == ["follow the SOP", "delay risk is high"]
    reading, result, prose, nudge = messages[2:]
    assert isinstance(reading, AIMessage)
    assert reading.content == "Reading the SOP."
    assert reading.tool_calls == [
        {"id": "c1", "name": "read_sop_section", "args": {"section_id": "a"}, "type": "tool_call"}
    ]
    assert reading.additional_kwargs == reasoning
    assert isinstance(result, ToolMessage)
    assert (result.tool_call_id, result.name, result.content) == (
        "c1",
        "read_sop_section",
        "# a: body",
    )
    assert isinstance(prose, AIMessage)
    assert (prose.content, prose.tool_calls, prose.additional_kwargs) == (
        "I would text the driver.",
        [],
        {},
    )
    assert nudge.content == "Answer now with finish."


async def test_the_reasoning_of_a_reply_goes_into_the_turn_for_the_next_round() -> None:
    """Only what the provider needs back: OpenRouter's reasoning text and details."""
    details = [{"type": "reasoning.encrypted", "data": "opaque"}]
    reply_message = calls(
        ("read_sop_section", {"section_id": "a"}),
        additional_kwargs={
            "reasoning_content": "check the SOP first",
            "reasoning_details": details,
            "refusal": None,
        },
    )
    model, _ = make_model(reply_message, calls(("read_sop_section", {"section_id": "b"})))

    reasoned = await model.chat(round_request(), AgentOutcome)
    plain = await model.chat(round_request(), AgentOutcome)

    assert reasoned.turn.provider_data == {
        "reasoning_content": "check the SOP first",
        "reasoning_details": details,
    }
    assert plain.turn.provider_data == {}


async def test_a_tool_named_like_the_answer_tool_is_refused_before_any_call() -> None:
    clash = ToolSpec(name=FINISH, description="d", parameters={"type": "object"})
    model, chat = make_model(answers(FINISH, DONE))

    with pytest.raises(ValueError, match=FINISH):
        await model.chat(round_request(READ_SOP, clash), AgentOutcome)

    assert chat.sent == []


# --- usage, latency and the run config ---------------------------------------------------


async def test_usage_is_priced_and_timed_for_the_one_call() -> None:
    model, _ = make_model(
        calls(("read_sop_section", {"section_id": "a"}), usage=_usage(1_000, 50, cached=400)),
        timer=_clock(10.0, 10.25),
    )

    reply = await model.chat(round_request(), AgentOutcome)

    usage = reply.usage
    assert (usage.role, usage.model) == ("agent", "acme/small")
    assert (usage.input_tokens, usage.output_tokens, usage.cached_tokens) == (1_000, 50, 400)
    assert usage.cost_usd == pytest.approx(
        PRICING.cost_usd(input_tokens=1_000, output_tokens=50, cached_tokens=400)
    )
    assert usage.latency_ms == 250


async def test_usage_names_the_provider_that_served_the_call() -> None:
    """OpenRouter may route to another provider of the model: the run records which."""
    served = answers("GateDecision", ACK)
    served.response_metadata["provider"] = "DeepInfra"
    model, _ = make_model(served, answers("GateDecision", ACK))

    first = await model.generate(GATE, GateDecision)
    second = await model.generate(GATE, GateDecision)

    assert (first.usage.provider, second.usage.provider) == ("DeepInfra", None)


async def test_each_call_reports_its_own_usage() -> None:
    model, _ = make_model(
        answers("GateDecision", ACK, usage=_usage(300, 20)),
        answers("GateDecision", ACK, usage=_usage(500, 40, cached=100)),
        timer=_clock(0.0, 0.1, 1.0, 1.5),
    )

    first = await model.generate(GATE, GateDecision)
    second = await model.generate(GATE, GateDecision)

    assert (first.usage.input_tokens, first.usage.latency_ms) == (300, 100)
    assert (second.usage.input_tokens, second.usage.cached_tokens) == (500, 100)
    assert second.usage.latency_ms == 500
    assert first.usage.role == "gate"


async def test_a_reply_without_usage_metadata_counts_as_zero_tokens() -> None:
    bare = AIMessage(content="", tool_calls=[tool_call(name="GateDecision", args=ACK, id="c1")])
    model, _ = make_model(bare)

    response = await model.generate(GATE, GateDecision)

    assert (response.usage.input_tokens, response.usage.cost_usd) == (0, 0.0)


async def test_each_call_is_a_langchain_run_named_after_its_role_and_model() -> None:
    """So a LangSmith trace shows the call under its graph node."""
    model, chat = make_model(answers("GateDecision", ACK), answers(FINISH, DONE))

    await model.generate(GATE, GateDecision)
    await model.chat(round_request(force_answer=True), AgentOutcome)

    gate, agent = (s.config for s in chat.sent)
    assert (gate.get("run_name"), gate.get("tags")) == ("gate · acme/small", ["role:gate"])
    assert gate.get("metadata") == {
        "role": "gate",
        "ls_provider": "openrouter",
        "ls_model_name": "acme/small",
    }
    assert (agent.get("run_name"), agent.get("tags")) == ("agent · acme/small", ["role:agent"])


async def test_a_call_rendered_from_a_template_carries_its_version_in_the_trace() -> None:
    model, chat = make_model(answers("GateDecision", ACK))

    await model.generate(replace(GATE, prompt_version="5b488b4298d0"), GateDecision)

    metadata = chat.sent[0].config.get("metadata") or {}
    assert metadata["prompt_version"] == "5b488b4298d0"


async def test_the_template_describes_the_answer_tool_when_it_sets_its_text() -> None:
    """The agent's `finish` is described by its template, not by `AgentOutcome`'s docstring."""
    finish = "Call it once, at the end, after you staged every action with its action tool."
    model, chat = make_model(answers(FINISH, DONE), answers("GateDecision", ACK))

    await model.chat(round_request(force_answer=True, answer_description=finish), AgentOutcome)
    await model.generate(replace(GATE, answer_description="Classify it."), GateDecision)

    agent, gate = (s.tools[-1]["function"]["description"] for s in chat.sent)
    assert agent == f"Give your final answer. {finish}"
    assert gate == "Give your final answer. Classify it."
    assert answer_tool(AgentOutcome, finish)["function"]["description"] == agent


# --- errors ------------------------------------------------------------------------------


async def test_a_provider_error_becomes_an_llm_error_naming_the_model() -> None:
    model, _ = make_model(RuntimeError("502 upstream unavailable"))

    with pytest.raises(
        LlmError, match=r"acme/small \(gate\): provider error: RuntimeError: 502 upstream"
    ) as caught:
        await model.generate(GATE, GateDecision)

    assert isinstance(caught.value.__cause__, RuntimeError)  # no key in it: the chain stays


async def test_a_round_that_fails_at_the_provider_is_an_llm_error_too() -> None:
    model, _ = make_model(ConnectionError("reset by peer"))

    with pytest.raises(LlmError, match=r"acme/small \(agent\): provider error: ConnectionError"):
        await model.chat(round_request(), AgentOutcome)


async def test_a_slow_model_times_out_as_an_llm_error() -> None:
    model, _ = make_model(answers("GateDecision", ACK), delay_s=5.0)

    with pytest.raises(LlmError, match=r"acme/small \(gate\): timed out after 0.01 s"):
        await model.generate(replace(GATE, timeout_s=0.01), GateDecision)


async def test_a_slow_round_times_out_as_an_llm_error() -> None:
    model, _ = make_model(answers(FINISH, DONE), delay_s=5.0)

    with pytest.raises(LlmError, match=r"acme/small \(agent\): timed out after 0.02 s"):
        await model.chat(round_request(timeout_s=0.02), AgentOutcome)


async def test_the_key_never_shows_in_repr_or_in_error_messages() -> None:
    model, _ = make_model(RuntimeError(f"401 Unauthorized: key {KEY} was rejected"))

    with pytest.raises(LlmError) as caught:
        await model.generate(GATE, GateDecision)

    assert KEY not in str(caught.value)
    assert "401 Unauthorized: key [redacted] was rejected" in str(caught.value)
    assert caught.value.__cause__ is None  # the original quotes the key: out of the chain
    assert caught.value.__suppress_context__
    assert KEY not in "".join(traceback.format_exception(caught.value))  # a logged traceback
    assert KEY not in repr(model)
    assert KEY not in str(model)


async def test_a_long_provider_error_is_cut_after_the_key_is_redacted() -> None:
    """Cutting first could leave half a key at the cut."""
    model, _ = make_model(RuntimeError("x" * 476 + KEY))

    with pytest.raises(LlmError) as caught:
        await model.generate(GATE, GateDecision)

    assert KEY[:8] not in str(caught.value)
    assert len(str(caught.value)) < 600


def _http_error(status: int, **headers: str) -> errors.OpenRouterError:
    """An HTTP error as the OpenRouter SDK raises it when no specific class matches."""
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    response = httpx.Response(status, headers=headers, text='{"error": {}}', request=request)
    return errors.OpenRouterDefaultError("API error occurred", response)


@pytest.mark.parametrize("status", [429, 502, 503, 529])
async def test_a_rate_limit_or_an_overload_is_a_transient_error(status: int) -> None:
    """The resilient wrapper retries these on the same model before falling back."""
    model, _ = make_model(_http_error(status, **{"Retry-After": "2"}))

    with pytest.raises(TransientLlmError) as caught:
        await model.chat(round_request(), AgentOutcome)

    assert caught.value.retry_after_s == 2.0
    assert str(caught.value).startswith(f"acme/small (agent): provider error (HTTP {status}):")


@pytest.mark.parametrize("status", [400, 401, 402, 404, 500])
async def test_other_http_errors_are_not_transient(status: int) -> None:
    model, _ = make_model(_http_error(status, **{"Retry-After": "2"}))

    with pytest.raises(LlmError, match=rf"provider error \(HTTP {status}\)") as caught:
        await model.generate(GATE, GateDecision)

    assert not isinstance(caught.value, TransientLlmError)


@pytest.mark.parametrize(
    ("headers", "wait"),
    [
        ({}, None),
        ({"Retry-After": "0.5"}, 0.5),
        ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),  # a date: no wait taken
        ({"Retry-After": "-3"}, None),
        ({"Retry-After": "inf"}, None),
    ],
)
async def test_retry_after_is_read_in_seconds(headers: dict[str, str], wait: float | None) -> None:
    model, _ = make_model(_http_error(429, **headers))

    with pytest.raises(TransientLlmError) as caught:
        await model.generate(GATE, GateDecision)

    assert caught.value.retry_after_s == wait


def test_the_routing_summary_goes_with_the_reply_into_the_trace() -> None:
    chat = openrouter_chat(ROLE, SecretStr(KEY))
    assert isinstance(chat, ChatOpenRouter)

    result = chat._create_chat_result(_completion(FINISH, DONE, "call_1"))  # pyright: ignore[reportPrivateUsage]

    metadata = result.generations[0].message.response_metadata
    assert metadata["provider"] == "Azure"
    assert metadata["routing"] == {"summary": "available=2, selected=Azure", "attempt": 2}


async def test_an_http_error_names_the_provider_that_answered_it() -> None:
    body = json.dumps(
        {"error": {"code": 429, "metadata": {"provider_name": "DeepInfra", "raw": "busy"}}}
    )
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    limited = errors.OpenRouterDefaultError(
        "Provider returned error", httpx.Response(429, text=body, request=request)
    )
    model, _ = make_model(limited)

    with pytest.raises(TransientLlmError, match=r"provider error \(HTTP 429 from DeepInfra\)"):
        await model.generate(GATE, GateDecision)


async def test_a_rate_limit_from_the_real_chat_openrouter_is_transient() -> None:
    """ChatOpenRouter lets the SDK's error through, so its status reaches the adapter."""
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    limited = errors.TooManyRequestsResponseError(
        errors.TooManyRequestsResponseErrorData(
            error=components.TooManyRequestsResponseErrorData(
                code=429, message="openai/gpt-oss-120b is temporarily rate-limited upstream"
            )
        ),
        httpx.Response(429, request=request),
    )

    class _Limited:
        async def send_async(self, **_request: Any) -> dict[str, Any]:
            raise limited

    def factory(config: RoleConfig, key: SecretStr) -> BaseChatModel:
        chat = openrouter_chat(config, key)
        assert isinstance(chat, ChatOpenRouter)
        chat.client = SimpleNamespace(chat=_Limited())
        return chat

    model = OpenRouterModel(ROLE, PRICING, SecretStr(KEY), chat_factory=factory)

    with pytest.raises(TransientLlmError, match=r"HTTP 429.*rate-limited upstream") as caught:
        await model.chat(round_request(), AgentOutcome)

    assert caught.value.retry_after_s is None


def test_an_empty_key_is_rejected_when_the_model_is_built() -> None:
    with pytest.raises(ValueError, match="key"):
        OpenRouterModel(ROLE, PRICING, SecretStr(" "), chat_factory=lambda _r, _k: FakeChat())


# --- wiring ------------------------------------------------------------------------------


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


def test_models_for_role_builds_one_model_per_entry_of_the_chain() -> None:
    config = ModelsConfig(
        roles={
            role: RoleConfig(
                model="acme/small", fallbacks=(FallbackConfig(model="acme/tiny"),), max_tokens=64
            )
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


def _completion(name: str, args: dict[str, Any], call_id: str, **message: Any) -> dict[str, Any]:
    """A chat completion as OpenRouter returns it, with one tool call and usage."""
    call = {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }
    return {
        "id": f"gen-{call_id}",
        "model": "openai/gpt-4.1-nano",
        "openrouter_metadata": {
            "attempt": 2,
            "summary": "available=2, selected=Azure",
            "endpoints": {
                "available": [
                    {"model": "openai/gpt-4.1-nano", "provider": "OpenAI", "selected": False},
                    {"model": "openai/gpt-4.1-nano", "provider": "Azure", "selected": True},
                ],
                "total": 3,
            },
        },
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [call],
                    **message,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 300,
            "completion_tokens": 40,
            "total_tokens": 340,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
    }


async def test_the_real_chat_openrouter_sends_the_expected_requests() -> None:
    """Drives ChatOpenRouter's own conversion code with only its SDK client stubbed, so the
    assumptions taken from its source (tool binding, tool choice, message, reasoning and
    usage mapping) break this test, not a live run, when the library changes."""
    role = RoleConfig(
        model="openai/gpt-4.1-nano",
        temperature=0,
        max_tokens=256,
        provider=ProviderRouting(order=("openai",), allow_fallbacks=False),
    )
    details = [{"type": "reasoning.text", "text": "read the SOP", "index": 0}]
    completions = _StubCompletions(
        [
            _completion(
                "read_sop_section",
                {"section_id": "escalation"},
                "call_1",
                reasoning="read the SOP",
                reasoning_details=details,
            ),
            _completion(FINISH, DONE, "call_2"),
        ]
    )

    def factory(config: RoleConfig, key: SecretStr) -> BaseChatModel:
        chat = openrouter_chat(config, key)
        assert isinstance(chat, ChatOpenRouter)
        chat.client = SimpleNamespace(chat=completions)
        return chat

    model = OpenRouterModel(role, PRICING, SecretStr(KEY), chat_factory=factory)

    # One tool round, then the round that forces the answer, with the first in its history.
    first_reply = await model.chat(round_request(), AgentOutcome)
    (read,) = first_reply.turn.tool_calls
    history = (
        first_reply.turn,
        ToolResult(call_id=read.id, name=read.name, content="# escalation: body"),
    )
    second_reply = await model.chat(round_request(history=history, force_answer=True), AgentOutcome)

    assert read == ToolCall(id="call_1", name="read_sop_section", args={"section_id": "escalation"})
    assert first_reply.turn.provider_data["reasoning_content"] == "read the SOP"
    assert second_reply.answer == AgentOutcome(rationale=DONE["rationale"])
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
    assert [t["function"]["name"] for t in first["tools"]] == ["read_sop_section", FINISH]
    assert second["tool_choice"] == {"type": "function", "function": {"name": FINISH}}
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool"]
    assistant, tool = second["messages"][2:]
    assert assistant["reasoning"] == "read the SOP"  # the reasoning goes back to the provider
    assert assistant["reasoning_details"][0]["text"] == "read the SOP"
    assert tool["tool_call_id"] == "call_1"
    usage = second_reply.usage
    assert (usage.input_tokens, usage.cached_tokens, usage.output_tokens) == (300, 200, 40)
    # The SDK's response model has no `provider`: the routing metadata names the endpoint.
    assert first["x_open_router_metadata"] == "enabled"
    assert usage.provider == "Azure"


async def test_the_agents_claude_fallback_sends_its_own_settings_in_rounds_and_answers() -> None:
    """The shipped agent chain: gpt-oss on its preferred provider first, then Claude Haiku
    with no temperature, no reasoning and its own cap, whose rounds use `auto` (no
    Anthropic endpoint takes `required`) and whose forced answers name the tool."""
    completions = _StubCompletions(
        [
            _completion("read_sop_section", {"section_id": "escalation"}, "call_1"),
            _completion(FINISH, DONE, "call_2"),
            _completion("GateDecision", ACK, "call_3"),
        ]
    )

    def factory(config: RoleConfig, key: SecretStr) -> BaseChatModel:
        chat = openrouter_chat(config, key)
        assert isinstance(chat, ChatOpenRouter)
        chat.client = SimpleNamespace(chat=completions)
        return chat

    config = load_models_config(SHIPPED)
    primary, claude, *_ = models_for_role(
        config, ModelRole.AGENT, SecretStr(KEY), chat_factory=factory
    )
    assert primary.model_id == "openai/gpt-oss-120b"
    assert claude.model_id == "anthropic/claude-haiku-5.5"

    await claude.chat(round_request(), AgentOutcome)
    await claude.chat(round_request(force_answer=True), AgentOutcome)
    await claude.generate(GATE, GateDecision)

    first, forced, answer = completions.requests
    assert first["model"] == "anthropic/claude-haiku-5.5"
    assert "temperature" not in first
    assert "reasoning" not in first
    assert first["max_tokens"] == 2048
    assert first["provider"] == {"require_parameters": True}
    assert first["tool_choice"] == "auto"
    assert forced["tool_choice"] == {"type": "function", "function": {"name": FINISH}}
    assert answer["tool_choice"] == {"type": "function", "function": {"name": "GateDecision"}}


def test_the_agents_primary_prefers_its_provider_and_may_be_served_by_another() -> None:
    config = load_models_config(SHIPPED)
    built: list[RoleConfig] = []

    def factory(role: RoleConfig, _key: SecretStr) -> BaseChatModel:
        built.append(role)
        return FakeChat()

    models_for_role(config, ModelRole.AGENT, SecretStr(KEY), chat_factory=factory)

    routing = built[0].provider.as_openrouter()
    assert routing == {
        "order": ["deepinfra"],
        "allow_fallbacks": True,
        "require_parameters": True,
        "quantizations": ["bf16"],
    }
    assert [r.model for r in built] == list(config.role(ModelRole.AGENT).models)
