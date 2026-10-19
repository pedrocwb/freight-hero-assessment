"""A language model wrapper that counts calls and keeps their usage.

The runner wraps the model in it in every mode, so model calls per step and
the cost of a trial are measured the same way for scripted, replay and live
models, without asking the runtime.
"""

from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import LanguageModel, LlmRequest, LlmResponse, SchemaT


class CountingModel:
    def __init__(self, inner: LanguageModel) -> None:
        self._inner = inner
        self.calls = 0  # every call, failed ones too: a failed call still counts toward budgets
        self.usage: list[Usage] = []  # one entry per successful call, in call order

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        self.calls += 1
        response = await self._inner.generate(request, schema)
        self.usage.append(response.usage)
        return response
