"""A `LanguageModel` on OpenRouter, through LangChain's `ChatOpenRouter`.

The only module that calls a real model; LangChain stays in `adapters/llm` (the
import-linter keeps it there). One instance serves one model id. The role's fallback
list is walked by the resilient wrapper (`adapters/llm/resilient.py`) over one instance
per model (`models_for_role`), so retries, fallbacks and the circuit breaker live in one
place and this class stays a plain translation between the port and the provider.

Every method is one model call:

- `generate` forces the answer: the schema becomes an answer tool and `tool_choice`
  names it. That is the request `with_structured_output` sends by default
  (`function_calling`), built here so the raw reply (and its usage) stays in hand and a
  parse error names the model.
- `chat` is one round of the agent loop: the history goes back as LangChain messages,
  the tools and the answer tool are bound together, and `tool_choice` is `"required"`
  (`"auto"` for Claude, see `round_tool_choice`), or the answer tool when the request
  forces the answer. The reply's tool calls come back as they are; this class runs none.
  The provider's reasoning travels in the turn's `provider_data`, so the next round
  sends it back as LangChain would.
- `request.images` go with the user prompt as image parts (data URLs).

Each call's `Usage` names the provider that served it: a role lists its preferred provider
first and may let OpenRouter serve the model elsewhere. The SDK's response model drops
OpenRouter's `provider` field, so the client opts in to OpenRouter's routing metadata
(`_RoutedChatOpenRouter`), which names the endpoint selected and how many it could choose
from; the trace keeps that summary in the reply's `response_metadata["routing"]`.

Any provider error, timeout (`request.timeout_s`, for the call) or answer that does not
fit the schema becomes `LlmError`, named after the model. A rate limit or an overloaded
provider (`_TRANSIENT_STATUSES`) becomes `TransientLlmError`, with the provider's
`Retry-After`: the resilient wrapper retries it on the same model before falling back,
while this class sends each call once (`max_retries=0` on the client). Each call is a
LangChain run, named after its role and model, so a LangSmith trace shows it under its
graph node, with the version of the prompt template it was rendered from
(`prompt_version`).
"""

import asyncio
import base64
import json
import math
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import ToolCall as LcToolCall
from langchain_core.outputs import ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openrouter import ChatOpenRouter
from openrouter.errors import OpenRouterError  # the SDK under ChatOpenRouter: HTTP errors
from pydantic import BaseModel, SecretStr, ValidationError

from freight_agent.adapters.llm.models_config import ModelPricing, ModelsConfig, RoleConfig
from freight_agent.domain.runs import Usage
from freight_agent.interfaces.llm import (
    AssistantTurn,
    ChatReply,
    ChatRequest,
    LlmError,
    LlmRequest,
    LlmResponse,
    ModelRole,
    SchemaT,
    ToolCall,
    ToolResult,
    ToolSpec,
    TransientLlmError,
    Turn,
    UserTurn,
)

ChatFactory = Callable[[RoleConfig, SecretStr], BaseChatModel]
Timer = Callable[[], float]

_APP_TITLE = "freight-agent"
_MAX_CAUSE = 500  # characters of a provider's error text kept in an LlmError
_REQUIRED = "required"  # tool choice: call some tool, prose is not an option
_AUTO = "auto"  # tool choice: a tool or prose, as the model likes
# What a reply carries for the next round: OpenRouter's reasoning text and details.
_PROVIDER_KEYS = ("reasoning_content", "reasoning_details")
# HTTP statuses after which the same call may succeed shortly: a rate limit (429), the
# model's providers down or overloaded behind OpenRouter (502, 503) or saying so (529).
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 529})

# Model id prefixes whose tool rounds use `auto`: OpenRouter has no Anthropic endpoint that
# takes `tool_choice: "required"` (a 404 for Claude Haiku 5.5), while a forced call to one
# named tool works. A prose reply in a round is asked once more for the answer (the graph).
_NO_REQUIRED_MODELS = ("anthropic/",)


def round_tool_choice(model_id: str) -> str:
    return _AUTO if model_id.startswith(_NO_REQUIRED_MODELS) else _REQUIRED


class _RoutedChatOpenRouter(ChatOpenRouter):
    """`ChatOpenRouter` that asks for OpenRouter's routing metadata (`openrouter_metadata`
    on the response) and copies from it the provider that served the call into the reply's
    `response_metadata`, where `OpenRouterModel` reads it."""

    def _create_chat_result(self, response: Any) -> ChatResult:
        result = super()._create_chat_result(response)
        data = response if isinstance(response, dict) else response.model_dump(by_alias=True)
        routing = cast("dict[str, Any]", data).get("openrouter_metadata")
        if isinstance(routing, dict):
            routing = cast("dict[str, Any]", routing)
            provider = _selected_provider(routing)
            for generation in result.generations:
                metadata = generation.message.response_metadata
                if provider is not None:
                    metadata.setdefault("provider", provider)
                metadata["routing"] = {k: routing.get(k) for k in ("summary", "attempt")}
        return result


def _selected_provider(routing: dict[str, Any]) -> str | None:
    endpoints = routing.get("endpoints")
    available = endpoints.get("available") if isinstance(endpoints, dict) else None
    for endpoint in available if isinstance(available, list) else []:
        if isinstance(endpoint, dict) and endpoint.get("selected"):
            provider = cast("dict[str, Any]", endpoint).get("provider")
            return provider if isinstance(provider, str) else None
    return None


def openrouter_chat(role: RoleConfig, api_key: SecretStr) -> BaseChatModel:
    """The production chat model for one single-model config. Builds a client, sends nothing."""
    return _RoutedChatOpenRouter(
        model=role.model,
        api_key=api_key,
        temperature=role.temperature,
        max_tokens=role.max_tokens,
        reasoning={"effort": role.reasoning_effort} if role.reasoning_effort else None,
        openrouter_provider=role.provider.as_openrouter(),
        max_retries=0,  # retries and fallbacks belong to the resilient wrapper
        app_title=_APP_TITLE,
        app_url=None,
        model_kwargs={"x_open_router_metadata": "enabled"},  # the routing metadata
    )


class OpenRouterModel:
    """`LanguageModel` for one model id. The key is held as a `SecretStr` and scrubbed
    from error messages; `repr` shows the model only."""

    def __init__(
        self,
        role_config: RoleConfig,
        pricing: ModelPricing,
        api_key: SecretStr,
        chat_factory: ChatFactory | None = None,
        *,
        timer: Timer = time.perf_counter,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("the OpenRouter API key is empty")
        self._role_config = role_config
        self._pricing = pricing
        self._api_key = api_key
        self._timer = timer
        self._chat = (chat_factory or openrouter_chat)(role_config, api_key)

    @property
    def model_id(self) -> str:
        return self._role_config.model

    def __repr__(self) -> str:
        return f"OpenRouterModel(model={self.model_id!r})"

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        answer = answer_tool(schema, request.answer_description)
        name = answer["function"]["name"]
        reply, usage = await self._call(request, _messages(request), [answer], name)
        call = _answer_call(reply, name)
        if call is None:
            raise self._error(request, _no_answer(reply, name))
        return LlmResponse(parsed=self._parse(request, schema, call), usage=usage)

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        answer = answer_tool(schema, request.answer_description)
        name = answer["function"]["name"]
        if any(t.name == name for t in request.tools):
            raise ValueError(f"a tool is named {name}, the name of the answer tool")
        forced = request.force_answer or not request.tools
        choice = name if forced else round_tool_choice(self.model_id)
        bound = [*(_function(t) for t in request.tools), answer]
        reply, usage = await self._call(request, _messages(request), bound, choice)
        call = _answer_call(reply, name)
        if call is None and forced:
            raise self._error(request, _no_answer(reply, name))
        parsed = self._parse(request, schema, call) if call is not None else None
        return ChatReply(turn=_turn(reply), answer=parsed, usage=usage)

    async def _call(
        self,
        request: LlmRequest,
        messages: list[BaseMessage],
        tools: Sequence[dict[str, Any]],
        tool_choice: str,
    ) -> tuple[AIMessage, Usage]:
        model = self._chat.bind_tools(tools, tool_choice=tool_choice)
        # Names the call in a LangSmith trace ("agent · openai/gpt-oss-120b"); without an
        # active trace the config changes nothing.
        role = request.role.value
        metadata: dict[str, Any] = {
            "role": role,
            "ls_provider": "openrouter",
            "ls_model_name": self.model_id,
        }
        if request.prompt_version is not None:
            metadata["prompt_version"] = request.prompt_version
        config: RunnableConfig = {
            "run_name": f"{role} · {self.model_id}",
            "tags": [f"role:{role}"],
            "metadata": metadata,
        }
        started = self._timer()
        try:
            reply = await asyncio.wait_for(
                model.ainvoke(messages, config=config), timeout=request.timeout_s
            )
        except TimeoutError as exc:
            raise self._error(request, f"timed out after {request.timeout_s:g} s") from exc
        except Exception as exc:  # the SDK, httpx and LangChain raise many types
            text = f"{type(exc).__name__}: {exc}"
            redacted = self._redact(text)  # before the cut, which could split the key
            status = _http_status(exc)
            error = self._error(request, f"provider error{status}: {redacted[:_MAX_CAUSE]}")
            if _transient(exc):
                error = TransientLlmError(str(error), retry_after_s=_retry_after_s(exc))
            # An error that quotes the key stays out of the chain, so no traceback logs it.
            raise error from (exc if redacted == text else None)
        if not isinstance(reply, AIMessage):
            raise self._error(request, f"the provider replied with a {type(reply).__name__}")
        latency_ms = round((self._timer() - started) * 1000)
        return reply, self._usage(request, reply, latency_ms)

    def _parse(self, request: LlmRequest, schema: type[SchemaT], call: LcToolCall) -> SchemaT:
        try:
            return schema.model_validate(_known_args(call["args"], schema))
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            raise self._error(
                request, f"the answer does not match {schema.__name__}: {problems}"
            ) from exc

    def _usage(self, request: LlmRequest, reply: AIMessage, latency_ms: int) -> Usage:
        metadata = reply.usage_metadata
        input_tokens = metadata["input_tokens"] if metadata else 0
        output_tokens = metadata["output_tokens"] if metadata else 0
        cached = (metadata.get("input_token_details", {}).get("cache_read") or 0) if metadata else 0
        provider = reply.response_metadata.get("provider")
        return Usage(
            role=request.role.value,
            model=self.model_id,
            provider=provider if isinstance(provider, str) and provider else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached,
            cost_usd=self._pricing.cost_usd(
                input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached
            ),
            latency_ms=latency_ms,
        )

    def _error(self, request: LlmRequest, cause: str) -> LlmError:
        return LlmError(self._redact(f"{self.model_id} ({request.role.value}): {cause}"))

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key.get_secret_value(), "[redacted]")


def models_for_role(
    config: ModelsConfig,
    role: ModelRole,
    api_key: SecretStr,
    chat_factory: ChatFactory | None = None,
) -> tuple[OpenRouterModel, ...]:
    """One model per entry of the role's chain, primary first, for the resilient wrapper."""
    return tuple(
        OpenRouterModel(entry, config.pricing_for(entry.model), api_key, chat_factory)
        for entry in config.role(role).chain()
    )


def _http_status(exc: Exception) -> str:
    """The status of an HTTP error, with the provider that answered it when OpenRouter's
    error body names one (" (HTTP 429 from DeepInfra)"); empty for any other error."""
    if not isinstance(exc, OpenRouterError):
        return ""
    try:
        body = json.loads(exc.body)
        provider = body["error"]["metadata"]["provider_name"]
    except (ValueError, TypeError, KeyError):
        provider = None
    source = f" from {provider}" if isinstance(provider, str) and provider else ""
    return f" (HTTP {exc.status_code}{source})"


def _transient(exc: Exception) -> bool:
    return isinstance(exc, OpenRouterError) and exc.status_code in _TRANSIENT_STATUSES


def _retry_after_s(exc: Exception) -> float | None:
    """The provider's `Retry-After`, in seconds; None without one or for an HTTP date."""
    value = exc.headers.get("retry-after") if isinstance(exc, OpenRouterError) else None
    try:
        seconds = float(value) if value is not None else math.nan
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def answer_tool(schema: type[BaseModel], description: str | None = None) -> dict[str, Any]:
    """The schema as an OpenAI-style function the model calls to give its answer.
    `description` replaces the schema's docstring: the template's text for the tool."""
    tool = convert_to_openai_tool(schema)
    function = tool["function"]
    about = function.get("description", "") if description is None else description
    function["description"] = f"Give your final answer. {about}".strip()
    return tool


def _known_args(args: dict[str, Any], schema: type[BaseModel]) -> dict[str, Any]:
    """The answer's fields the schema has. A small model adds fields of its own and writes
    null for "not this"; such a field goes, and a null field with a default takes its
    default. Anything else is for pydantic to judge."""
    fields = schema.model_fields
    return {
        name: value
        for name, value in args.items()
        if name in fields and not (value is None and not fields[name].is_required())
    }


def _messages(request: LlmRequest) -> list[BaseMessage]:
    messages: list[BaseMessage] = [SystemMessage(request.system), _user_message(request)]
    if isinstance(request, ChatRequest):
        messages.extend(_message(turn) for turn in request.history)
    return messages


def _user_message(request: LlmRequest) -> HumanMessage:
    """The user prompt, with each image as an OpenAI-style `image_url` part (a base64 data
    URL), which OpenRouter passes to every vision model."""
    if not request.images:
        return HumanMessage(request.user)
    parts: list[str | dict[str, Any]] = [{"type": "text", "text": request.user}]
    for image in request.images:
        data = base64.b64encode(image.data).decode("ascii")
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{data}"}}
        )
    return HumanMessage(content=parts)


def _message(turn: Turn) -> BaseMessage:
    match turn:
        case AssistantTurn():
            return AIMessage(
                content=turn.text,
                tool_calls=[
                    LcToolCall(id=c.id, name=c.name, args=c.args, type="tool_call")
                    for c in turn.tool_calls
                ],
                additional_kwargs=dict(turn.provider_data),
            )
        case ToolResult():
            return ToolMessage(turn.content, tool_call_id=turn.call_id, name=turn.name)
        case UserTurn():
            return HumanMessage(turn.text)


def _turn(reply: AIMessage) -> AssistantTurn:
    """The reply as the port's turn: every call, the broken ones with their error. A call
    with no id gets a fresh one, unique in the loop's history, so its answer can name it."""
    calls = [
        ToolCall(id=c["id"] or _new_call_id(), name=c["name"], args=dict(c["args"]))
        for c in reply.tool_calls
    ]
    calls.extend(
        ToolCall(id=c["id"] or _new_call_id(), name=c["name"] or "", error=c["error"] or "")
        for c in reply.invalid_tool_calls
    )
    kwargs = cast("dict[str, Any]", reply.additional_kwargs)
    return AssistantTurn(
        text=reply.text,
        tool_calls=tuple(calls),
        provider_data={k: kwargs[k] for k in _PROVIDER_KEYS if k in kwargs},
    )


def _new_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:16]}"


def _function(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _answer_call(reply: AIMessage, name: str) -> LcToolCall | None:
    return next((c for c in reply.tool_calls if c["name"] == name), None)


def _no_answer(reply: AIMessage, name: str) -> str:
    broken = next((c for c in reply.invalid_tool_calls if c["name"] == name), None)
    if broken is not None:
        return f"the {name} arguments are not valid JSON ({broken['error']})"
    called = [c["name"] for c in reply.tool_calls]
    instead = f"called {', '.join(called)}" if called else "replied in prose"
    return f"did not return a {name} answer when forced to ({instead})"
