"""The language model port: one model round. LangChain and OpenRouter live behind it, in
adapters.

A round is one request and one reply. There are two kinds:

- `generate`: a forced structured answer. The model must answer with `schema` (the gate,
  the document extraction, the judge). One call, one parsed answer.
- `chat`: one round of the agent loop. The model sees the conversation so far
  (`ChatRequest.history`) and the tools, and either calls tools or gives its answer,
  `schema` as an answer tool (`finish`). Running the tools and asking again is the
  caller's job: the loop lives in the run graph (`application/graph`), so every round and
  every tool call is a step of its own in a trace.

The port never runs a tool, so a reply has no side effect: the graph runs the read tools
and stages the action tools through the rules, and code executes the staged plan after
the run, through the outbox.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from freight_agent.domain.runs import Usage

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class ModelRole(StrEnum):
    GATE = "gate"  # what a message means (triage)
    AGENT = "agent"  # the agent loop: reads, stages actions, answers with the outcome
    JUDGE = "judge"  # grades wording in the evals
    DRIVER_SIM = "driver_sim"  # plays a driver in multi-turn evals
    EXTRACT = "extract"  # a document's type and values, images included


@dataclass(frozen=True)
class ToolSpec:
    """A tool the model may call: what the model reads about it. Running it is the
    caller's job (the run graph's `tools` node)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One tool call of a reply. `error` is set when the arguments were not a JSON
    object; `args` is then empty and the caller answers the error."""

    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict[str, Any])
    error: str | None = None


@dataclass(frozen=True)
class AssistantTurn:
    """What the model said in one round: its text and its tool calls, the answer tool's
    call included. `provider_data` is what the provider needs back with the turn in the
    next round (its reasoning, for instance); only the adapter reads it."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    provider_data: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class ToolResult:
    """What a tool answered, sent back to the model in the next round."""

    call_id: str
    name: str
    content: str


@dataclass(frozen=True)
class UserTurn:
    """A line from code to the model inside the loop (ask again for the answer)."""

    text: str


# The conversation after the system and user prompts, oldest first.
Turn = AssistantTurn | ToolResult | UserTurn


@dataclass(frozen=True)
class ImageInput:
    """An image sent with the user prompt (a scanned page), e.g. `image/png`."""

    data: bytes
    media_type: str


@dataclass(frozen=True)
class LlmRequest:
    """`system` and `user` are rendered from the role's prompt template, whose version
    (`ports.prompts.ChatPrompt.version`) the call's trace carries as `prompt_version`.
    `answer_description` is what the model reads about the answer tool when the template
    sets it (the agent's `finish`); None leaves the schema's docstring."""

    role: ModelRole
    system: str
    user: str
    timeout_s: float = 60.0  # this one call; the run deadline bounds the whole run
    images: tuple[ImageInput, ...] = ()
    prompt_version: str | None = None
    answer_description: str | None = None


@dataclass(frozen=True)
class ChatRequest(LlmRequest):
    """One round of a tool loop. `force_answer` makes the model call the answer tool
    now (the loop's budget is spent, or the model replied in prose)."""

    history: tuple[Turn, ...] = ()
    tools: tuple[ToolSpec, ...] = ()
    force_answer: bool = False


@dataclass(frozen=True)
class LlmResponse[T: BaseModel]:
    parsed: T
    usage: Usage


@dataclass(frozen=True)
class ChatReply[T: BaseModel]:
    """One round's reply. `answer` is the answer tool's arguments, parsed, when the model
    called it (always, when the request forced it); `turn` holds every call of the reply,
    so tool calls made next to the answer still run."""

    turn: AssistantTurn
    answer: T | None
    usage: Usage


class LlmError(Exception):
    """The model failed, timed out, or returned output that does not match the schema."""


class TransientLlmError(LlmError):
    """The provider rate limited the call or is overloaded: the same call may succeed after
    a short wait. `retry_after_s` is the wait the provider asked for, when it said."""

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class LanguageModel(Protocol):
    async def generate(
        self, request: LlmRequest, schema: type[SchemaT]
    ) -> LlmResponse[SchemaT]: ...

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]: ...
