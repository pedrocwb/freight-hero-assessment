"""A fake language model for the tests: it answers, round by round, what a test gives it.

The product has no scripted model: the platform and the evals are always live. The tests
use this one so that they exercise the real graph, tools, rules and executor, with the
model's side written down:

- `push(role, *outputs)` queues the answers of a role, in call order. For a forced
  answer (the gate, a document read, the judge) an output is a pydantic model, validated
  against the schema the caller asks for.
- For the agent's rounds an output is a `Round`: the tool calls the model makes in that
  round and, or, its answer (`finish`). An `AgentOutcome` is a round that only answers.
  A `Plan` is the shorthand of the common case: one round that stages its actions with
  their action tools, then a round that finishes with its rationale.
- An exception in the queue is raised at that call (a model failure).
- Every request is recorded (`calls`), and a call with nothing queued raises
  `ScriptExhausted` (an `LlmError`, so a run escalates as on a real model failure).
"""

import itertools
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from freight_agent.domain.actions import FINISH, Action, AgentOutcome, Plan
from freight_agent.domain.runs import Usage
from freight_agent.interfaces.decisions import (
    Answer,
    ChoiceAnswer,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
    YesNoAnswer,
)
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
)

_IDS = itertools.count(1)


class ScriptExhausted(LlmError):
    """The model was called for a role that has nothing queued."""


def call(name: str, **args: Any) -> ToolCall:
    """One tool call, with a fresh id."""
    return ToolCall(id=f"call_{next(_IDS)}", name=name, args=args)


def act(action: Action) -> ToolCall:
    """The action tool call that stages `action`."""
    args = action.model_dump(mode="json", exclude={"type"}, exclude_none=True)
    return call(action.type, **args)


@dataclass(frozen=True)
class Round:
    """One agent round: its tool calls, then (or only) its answer."""

    calls: tuple[ToolCall, ...] = ()
    answer: AgentOutcome | None = None
    text: str = ""
    cost_usd: float = 0.0


def rounds(*actions: Action, rationale: str = "", **answer: Any) -> tuple[Round, ...]:
    """The rounds of an agent that stages `actions` in one round, then finishes."""
    finish = Round(answer=AgentOutcome(rationale=rationale, **answer))
    return (Round(calls=tuple(act(a) for a in actions)), finish) if actions else (finish,)


Output = BaseModel | Round | Exception


@dataclass(frozen=True)
class FakeCall:
    role: ModelRole
    schema: str
    request: LlmRequest


@dataclass
class FakeModel:
    name: str = "fake"
    calls: list[FakeCall] = field(default_factory=list[FakeCall])
    exhausted: list[FakeCall] = field(default_factory=list[FakeCall])
    _queues: dict[ModelRole, deque[Output]] = field(
        default_factory=lambda: defaultdict[ModelRole, deque[Output]](deque)
    )

    def push(self, role: ModelRole, *outputs: Output) -> None:
        for output in outputs:
            if role is ModelRole.AGENT and isinstance(output, Plan):
                self._queues[role].extend(rounds(*output.actions, rationale=output.rationale))
            else:
                self._queues[role].append(output)

    def remaining(self, role: ModelRole | None = None) -> int:
        if role is not None:
            return len(self._queues[role])
        return sum(len(q) for q in self._queues.values())

    def rounds_of(self, role: ModelRole) -> list[FakeCall]:
        return [c for c in self.calls if c.role is role]

    def _next(self, request: LlmRequest, schema: type[BaseModel]) -> BaseModel | Round:
        record = FakeCall(role=request.role, schema=schema.__name__, request=request)
        self.calls.append(record)
        queue = self._queues[request.role]
        if not queue:
            self.exhausted.append(record)
            raise ScriptExhausted(f"no {request.role.value} output queued")
        output = queue.popleft()
        if isinstance(output, Exception):
            raise output
        return output

    def _usage(self, request: LlmRequest, cost_usd: float = 0.0) -> Usage:
        return Usage(role=request.role.value, model=self.name, cost_usd=cost_usd)

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        output = self._next(request, schema)
        if isinstance(output, Round):
            raise TypeError(f"a Round was queued for the forced {request.role.value} answer")
        parsed = (
            output if isinstance(output, schema) else schema.model_validate(output.model_dump())
        )
        return LlmResponse(parsed=parsed, usage=self._usage(request))

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        output = self._next(request, schema)
        turn = (
            output
            if isinstance(output, Round)
            else Round(answer=AgentOutcome.model_validate(output.model_dump()))
        )
        if request.force_answer and turn.answer is None:
            raise LlmError(f"{self.name}: did not return a {FINISH} answer when forced to")
        answer = None if turn.answer is None else schema.model_validate(turn.answer.model_dump())
        calls = turn.calls + ((call(FINISH, **turn.answer.model_dump()),) if turn.answer else ())
        return ChatReply(
            turn=AssistantTurn(text=turn.text, tool_calls=calls),
            answer=answer,
            usage=self._usage(request, turn.cost_usd),
        )


# --- a fake decision model ------------------------------------------------------------

DecisionOutput = Mapping[str, Answer] | Exception


@dataclass
class FakeDecisions:
    """A fake decision model (`ports.decisions`): it answers, call by call, the answers a
    test queues (`push`), by question name; an exception in the queue is raised at that
    call. Every request is recorded, and a call with nothing queued raises a
    `DecisionError` (an `LlmError`, so a run escalates as on a real failure)."""

    name: str = "fake-jev"
    cost_usd: float = 0.0
    requests: list[DecisionRequest] = field(default_factory=list[DecisionRequest])
    _queue: deque[DecisionOutput] = field(default_factory=deque[DecisionOutput])

    def push(self, *outputs: DecisionOutput) -> None:
        self._queue.extend(outputs)

    def remaining(self) -> int:
        return len(self._queue)

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        self.requests.append(request)
        if not self._queue:
            raise DecisionError(f"no {request.role.value} decision queued")
        output = self._queue.popleft()
        if isinstance(output, Exception):
            raise output
        usage = Usage(role=request.role.value, model=self.name, cost_usd=self.cost_usd)
        return DecisionResponse(answers=dict(output), usage=usage)


def gate_answers(
    intent: str, act: float, probabilities: Mapping[str, float] | None = None
) -> dict[str, Answer]:
    """The jev gate's two answers: the intent (all its probability on it unless given)
    and P(act now)."""
    return {
        "intent": ChoiceAnswer(
            choice=intent, probabilities=dict(probabilities or {intent: 1.0}), confidence=0.9
        ),
        "act": YesNoAnswer(yes=act),
    }
