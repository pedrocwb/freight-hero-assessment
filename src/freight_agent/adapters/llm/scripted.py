"""A language model that returns queued outputs, per role, in order.

Used by unit tests and by the eval harness in `scripted` mode. Every call is
recorded so tests can assert how many model calls a run made. Queue an
exception to simulate a model failure.
"""

from collections import defaultdict, deque
from dataclasses import dataclass

from pydantic import BaseModel

from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import LlmError, LlmRequest, LlmResponse, ModelRole, SchemaT


class ScriptExhausted(LlmError):
    """The model was called for a role that has nothing queued."""


@dataclass(frozen=True)
class ScriptedCall:
    role: ModelRole
    schema: str
    request: LlmRequest


class ScriptedModel:
    def __init__(self, model_name: str = "scripted") -> None:
        self._model_name = model_name
        self._queues: dict[ModelRole, deque[BaseModel | Exception]] = defaultdict(deque)
        self.calls: list[ScriptedCall] = []
        self.exhausted: list[ScriptedCall] = []

    def push(self, role: ModelRole, *outputs: BaseModel | Exception) -> None:
        self._queues[role].extend(outputs)

    def remaining(self, role: ModelRole | None = None) -> int:
        if role is not None:
            return len(self._queues[role])
        return sum(len(q) for q in self._queues.values())

    def reset(self) -> None:
        """Drop queued outputs and the call history."""
        self._queues.clear()
        self.calls.clear()
        self.exhausted.clear()

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        call = ScriptedCall(role=request.role, schema=schema.__name__, request=request)
        self.calls.append(call)
        queue = self._queues[request.role]
        if not queue:
            self.exhausted.append(call)
            raise ScriptExhausted(f"no scripted {request.role.value} output left")
        output = queue.popleft()
        if isinstance(output, Exception):
            raise output
        parsed = (
            output if isinstance(output, schema) else schema.model_validate(output.model_dump())
        )
        usage = Usage(role=request.role.value, model=self._model_name)
        return LlmResponse(parsed=parsed, usage=usage)
