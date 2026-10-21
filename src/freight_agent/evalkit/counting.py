"""A language model wrapper that counts calls and keeps their usage.

The player wraps the runtime's model in it, so the model calls of each step and the
cost of a scenario are measured without asking the runtime.

`calls` counts what a scenario author counts: a forced answer (the gate, a document
read) is one call, and an agent loop is one call however many rounds it takes (a round
whose request has no history starts a loop). So `expect.llm_calls` stays a fact about
the run's path (did the gate run, did the agent run), not about a live model's rounds.
`rounds` counts every request; `usage` has one entry per successful request.

It counts a decision model too (`decide`, the jev gate's one call per message), when it
is given one: the gate is one call whichever engine reads the message.
"""

from freight_agent.domain.runs import Usage
from freight_agent.ports.decisions import (
    DecisionError,
    DecisionModel,
    DecisionRequest,
    DecisionResponse,
)
from freight_agent.ports.llm import (
    ChatReply,
    ChatRequest,
    LanguageModel,
    LlmRequest,
    LlmResponse,
    SchemaT,
)


class CountingModel:
    def __init__(self, inner: LanguageModel, decisions: DecisionModel | None = None) -> None:
        self._inner = inner
        self._decisions = decisions
        self.calls = 0  # failed ones too: a failed call still counts toward budgets
        self.rounds = 0
        self.usage: list[Usage] = []  # one entry per successful request, in order

    @property
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.usage)

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        self.calls += 1
        self.rounds += 1
        response = await self._inner.generate(request, schema)
        self.usage.append(response.usage)
        return response

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        self.calls += not request.history
        self.rounds += 1
        reply = await self._inner.chat(request, schema)
        self.usage.append(reply.usage)
        return reply

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        self.calls += 1
        self.rounds += 1
        if self._decisions is None:
            raise DecisionError("this runtime was given no decision model to count")
        response = await self._decisions.decide(request)
        self.usage.append(response.usage)
        return response
