"""A `LanguageModel` for a process that has no model: every call fails with the reason.

The platform starts without `OPENROUTER_API_KEY` (to show loads, SOPs and past runs);
a run that needs the model then fails like any model failure, and the runtime escalates
the event to a Hero with this reason, so no event is lost and the cause is plain.
"""

from freight_agent.interfaces.llm import (
    ChatReply,
    ChatRequest,
    LlmError,
    LlmRequest,
    LlmResponse,
    SchemaT,
)


class UnavailableModel:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        raise LlmError(self.reason)

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        raise LlmError(self.reason)
