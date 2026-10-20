"""The language model port. LangChain and OpenRouter live behind it, in adapters."""

from collections.abc import Awaitable, Callable
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
    """A tool the model may call.

    A read tool answers from the run's context or the knowledge base. An action tool
    may check an action against the rules and stage it, answering accepted or refused;
    it never executes it. Code executes the staged plan after the run, through the
    outbox, so a tool call has no side effect outside the run.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ImageInput:
    """An image sent with the user prompt (a scanned page), e.g. `image/png`."""

    data: bytes
    media_type: str


@dataclass(frozen=True)
class LlmRequest:
    role: ModelRole
    system: str
    user: str
    tools: tuple[ToolSpec, ...] = ()
    max_tool_rounds: int = 2
    timeout_s: float = 60.0
    images: tuple[ImageInput, ...] = ()
    # The adapter checks the running cost after each tool round and forces the answer
    # once it is spent. None: no cap.
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class LlmResponse[T: BaseModel]:
    parsed: T
    usage: Usage
    transcript: tuple[dict[str, Any], ...] = field(default=())


class LlmError(Exception):
    """The model failed, timed out, or returned output that does not match the schema."""


class LanguageModel(Protocol):
    async def generate(
        self, request: LlmRequest, schema: type[SchemaT]
    ) -> LlmResponse[SchemaT]: ...
