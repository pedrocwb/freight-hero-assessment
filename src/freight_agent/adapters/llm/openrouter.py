"""A `LanguageModel` on OpenRouter, through LangChain's `ChatOpenRouter`.

The only module that calls a real model; LangChain stays in `adapters/llm` (the
import-linter keeps it there). One instance serves one model id. The role's fallback
list is walked by the resilient wrapper (`adapters/llm/resilient.py`) over one instance
per model (`models_for_role`), so retries, fallbacks and the circuit breaker live in one
place and this class stays a plain translation between the port and the provider.

Structured output is a forced tool call: the schema becomes an answer tool and the
final call sets `tool_choice` to it. That is the request `with_structured_output` sends
by default (`function_calling`), built here instead so the read-only tools and the answer
share one bound tool list, the raw reply (and its usage) stays in hand, and a parse
error names the model.

The conversation:

- No tools, or `max_tool_rounds == 0` (the gate): one forced call.
- Otherwise up to `max_tool_rounds` rounds with `tool_choice="required"`. The model
  calls read-only tools, which run here and go back as tool messages, or the answer
  tool, which ends the loop. When the rounds run out, or the model replies in prose,
  one last call forces the answer. A request makes at most `max_tool_rounds + 1` calls.

Any provider error, timeout (`request.timeout_s` covers the whole conversation) or
answer that does not fit the schema becomes `LlmError`, named after the model.
"""

import argparse
import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.tool import ToolCall
from langchain_core.utils.function_calling import convert_to_openai_tool
from langchain_openrouter import ChatOpenRouter
from pydantic import BaseModel, SecretStr, ValidationError

from freight_agent.adapters.llm.models_config import (
    ModelPricing,
    ModelsConfig,
    RoleConfig,
    Settings,
    load_models_config,
)
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import (
    LlmError,
    LlmRequest,
    LlmResponse,
    ModelRole,
    SchemaT,
    ToolSpec,
)

ChatFactory = Callable[[RoleConfig, SecretStr], BaseChatModel]
Timer = Callable[[], float]

_APP_TITLE = "freight-agent"
_MAX_CAUSE = 500  # characters of a provider's error text kept in an LlmError
_REQUIRED = "required"  # tool choice: call some tool, prose is not an option


def openrouter_chat(role: RoleConfig, api_key: SecretStr) -> BaseChatModel:
    """The production chat model for one single-model config. Builds a client, sends nothing."""
    return ChatOpenRouter(
        model=role.model,
        api_key=api_key,
        temperature=role.temperature,
        max_tokens=role.max_tokens,
        reasoning={"effort": role.reasoning_effort} if role.reasoning_effort else None,
        openrouter_provider=role.provider.as_openrouter(),
        max_retries=0,  # retries and fallbacks belong to the resilient wrapper
        app_title=_APP_TITLE,
        app_url=None,
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
        messages: list[BaseMessage] = [SystemMessage(request.system), HumanMessage(request.user)]
        started = self._timer()
        try:
            parsed = await asyncio.wait_for(
                self._converse(request, schema, messages), timeout=request.timeout_s
            )
        except TimeoutError as exc:
            # Provider and tool errors are caught inside, so a timeout here is our deadline.
            raise self._error(request, f"timed out after {request.timeout_s:g} s") from exc
        latency_ms = round((self._timer() - started) * 1000)
        return LlmResponse(
            parsed=parsed,
            usage=self._usage(request, messages, latency_ms),
            transcript=tuple(_transcript_entry(m) for m in messages),
        )

    async def _converse(
        self, request: LlmRequest, schema: type[SchemaT], messages: list[BaseMessage]
    ) -> SchemaT:
        """Appends every reply and tool result to `messages`, which become the transcript."""
        answer = answer_tool(schema)
        name = answer["function"]["name"]
        tools = {t.name: t for t in request.tools}
        if name in tools:
            raise ValueError(f"a tool is named {name}, the name of the answer tool")
        rounds = request.max_tool_rounds if tools else 0
        bound = [*(_function(t) for t in request.tools), answer] if rounds > 0 else [answer]

        for _ in range(rounds):
            reply = await self._ask(request, messages, bound, _REQUIRED)
            call = _answer_call(reply, name)
            if call is not None:
                return self._parse(request, schema, call)
            if not reply.tool_calls and not reply.invalid_tool_calls:
                # Prose instead of a tool call. A trailing assistant message reads as a
                # prefill to some providers, so ask for the answer in a user turn.
                messages.append(HumanMessage(f"Now call {name} with your final answer."))
                break
            messages.extend(await _tool_results(reply, tools))

        reply = await self._ask(request, messages, bound, name)
        call = _answer_call(reply, name)
        if call is None:
            raise self._error(request, _no_answer(reply, name))
        return self._parse(request, schema, call)

    async def _ask(
        self,
        request: LlmRequest,
        messages: list[BaseMessage],
        tools: Sequence[dict[str, Any]],
        tool_choice: str,
    ) -> AIMessage:
        model = self._chat.bind_tools(tools, tool_choice=tool_choice)
        try:
            reply = await model.ainvoke(list(messages))
        except Exception as exc:  # the SDK, httpx and LangChain raise many types
            text = f"{type(exc).__name__}: {exc}"
            redacted = self._redact(text)  # before the cut, which could split the key
            error = self._error(request, f"provider error: {redacted[:_MAX_CAUSE]}")
            # An error that quotes the key stays out of the chain, so no traceback logs it.
            raise error from (exc if redacted == text else None)
        messages.append(reply)
        return reply

    def _parse(self, request: LlmRequest, schema: type[SchemaT], call: ToolCall) -> SchemaT:
        try:
            return schema.model_validate(call["args"])
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            raise self._error(
                request, f"the answer does not match {schema.__name__}: {problems}"
            ) from exc

    def _usage(self, request: LlmRequest, messages: list[BaseMessage], latency_ms: int) -> Usage:
        input_tokens = output_tokens = cached_tokens = 0
        for message in messages:
            if isinstance(message, AIMessage) and message.usage_metadata:
                metadata = message.usage_metadata
                input_tokens += metadata["input_tokens"]
                output_tokens += metadata["output_tokens"]
                cached_tokens += metadata.get("input_token_details", {}).get("cache_read") or 0
        return Usage(
            role=request.role.value,
            model=self.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=self._pricing.cost_usd(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
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


def answer_tool(schema: type[BaseModel]) -> dict[str, Any]:
    """The schema as an OpenAI-style function the model calls to give its answer."""
    tool = convert_to_openai_tool(schema)
    function = tool["function"]
    function["description"] = f"Give your final answer. {function.get('description', '')}".strip()
    function["parameters"] = _portable(function["parameters"])
    return tool


def _portable(node: Any) -> Any:
    """A JSON schema every provider accepts and a small model fills in completely.

    Pydantic writes a discriminated union as `oneOf` plus a `discriminator` whose mapping
    points at `$defs` that LangChain has already inlined. Gemini rejects that shape and
    `const`, so: `oneOf` becomes `anyOf`, the discriminator goes, `const` becomes a
    one-value `enum`, and a constant property (the action `type`) becomes required,
    since pydantic leaves it optional only because it has a default.
    """
    if isinstance(node, list):
        return [_portable(item) for item in node]
    if not isinstance(node, dict):
        return node
    schema: dict[str, Any] = node
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "discriminator":
            continue
        if key == "const":
            out["enum"] = [value]
        elif key == "properties" and isinstance(value, dict):
            properties: dict[str, Any] = value
            out[key] = {name: _portable(prop) for name, prop in properties.items()}
        else:
            out["anyOf" if key == "oneOf" else key] = _portable(value)
    constants = [
        name
        for name, prop in schema.get("properties", {}).items()
        if isinstance(prop, dict) and "const" in prop
    ]
    if constants:
        required = list(out.get("required", []))
        out["required"] = required + [c for c in constants if c not in required]
    return out


def _function(spec: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    }


def _answer_call(reply: AIMessage, name: str) -> ToolCall | None:
    return next((c for c in reply.tool_calls if c["name"] == name), None)


def _no_answer(reply: AIMessage, name: str) -> str:
    broken = next((c for c in reply.invalid_tool_calls if c["name"] == name), None)
    if broken is not None:
        return f"the {name} arguments are not valid JSON ({broken['error']})"
    called = [c["name"] for c in reply.tool_calls]
    instead = f"called {', '.join(called)}" if called else "replied in prose"
    return f"did not return a {name} answer when forced to ({instead})"


async def _tool_results(reply: AIMessage, tools: Mapping[str, ToolSpec]) -> list[ToolMessage]:
    """One tool message per call, so the history stays valid for every provider."""
    results = [
        ToolMessage(
            await _run_tool(tools, call["name"], call["args"]),
            tool_call_id=call["id"] or "",
            name=call["name"],
        )
        for call in reply.tool_calls
    ]
    results.extend(
        ToolMessage(
            f"The arguments for {bad['name']} are not valid JSON ({bad['error']}). "
            "Call it again with a JSON object.",
            tool_call_id=bad["id"] or "",
            name=bad["name"] or "",
            status="error",
        )
        for bad in reply.invalid_tool_calls
    )
    return results


async def _run_tool(tools: Mapping[str, ToolSpec], name: str, args: dict[str, Any]) -> str:
    """A bad call is an answer the model can recover from, not an exception (see
    `application/tools.py`). Tools are read-only, so a failing one is reported too."""
    spec = tools.get(name)
    if spec is None:
        return f"Unknown tool {name!r}. Available tools: {', '.join(tools)}."
    try:
        return await spec.run(args)
    except Exception as exc:
        return f"The tool {name} failed ({type(exc).__name__}: {exc}). Continue without it."


def _transcript_entry(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, AIMessage):
        entry: dict[str, Any] = {"role": "assistant", "content": message.text}
        if message.tool_calls:
            entry["tool_calls"] = [
                {"id": c["id"], "name": c["name"], "args": c["args"]} for c in message.tool_calls
            ]
        if message.invalid_tool_calls:
            entry["invalid_tool_calls"] = [
                {"id": c["id"], "name": c["name"], "args": c["args"], "error": c["error"]}
                for c in message.invalid_tool_calls
            ]
        return entry
    if isinstance(message, ToolMessage):
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "name": message.name,
            "content": message.text,
        }
    role = "system" if isinstance(message, SystemMessage) else "user"
    return {"role": role, "content": message.text}


# Manual smoke test: `python -m freight_agent.adapters.llm.openrouter --smoke`.
# Makes two real calls (a fraction of a cent); not part of any test suite.


async def _smoke(config_path: Path) -> None:
    config = load_models_config(config_path)
    key = Settings().require_openrouter_key()

    async def read_sop_section(args: dict[str, Any]) -> str:
        return f"# {args.get('section_id')}: SMS the driver for the ETA, then follow up in 30 min."

    requests: list[tuple[LlmRequest, type[BaseModel]]] = [
        (
            LlmRequest(
                role=ModelRole.GATE,
                system="Classify the driver's message for a freight dispatcher.",
                user="Driver: running about 20 minutes late, ETA 9:40.",
                max_tool_rounds=0,
                timeout_s=30,
            ),
            GateDecision,
        ),
        (
            LlmRequest(
                role=ModelRole.DECIDE,
                system=(
                    "You follow the SOP. Read the section delay_risk_high with the tool, "
                    "then answer with a plan. The driver's contact id is drv-1."
                ),
                user="Delay risk just changed to high for load 481207.",
                tools=(
                    ToolSpec(
                        name="read_sop_section",
                        description="Read the full text of one SOP section.",
                        parameters={
                            "type": "object",
                            "properties": {"section_id": {"type": "string"}},
                            "required": ["section_id"],
                        },
                        run=read_sop_section,
                    ),
                ),
                timeout_s=60,
            ),
            Plan,
        ),
    ]
    for request, schema in requests:
        (model, *_) = models_for_role(config, request.role, key)
        response = await model.generate(request, schema)
        print(f"{request.role.value} {model.model_id}: {response.parsed.model_dump_json()}")
        print(f"  usage: {response.usage.model_dump_json()}")
        print(f"  transcript: {len(response.transcript)} messages")


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="OpenRouter adapter checks")
    parser.add_argument("--smoke", action="store_true", help="make two real model calls")
    parser.add_argument("--config", type=Path, default=Path("config/models.yaml"))
    args = parser.parse_args(argv)
    if not args.smoke:
        parser.print_help()
        return
    asyncio.run(_smoke(args.config))


if __name__ == "__main__":
    main()
