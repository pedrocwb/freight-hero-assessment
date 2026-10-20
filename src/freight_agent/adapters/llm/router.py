"""One `LanguageModel` for the runtime, a different model behind each role.

The runtime holds a single `LanguageModel`, and every request names its role (gate,
agent, judge, driver_sim, extract). `config/models.yaml` gives each role its
own model and fallback chain, so the composition root builds one model per role and
puts this router in front: the application never learns which model serves which call.
"""

from collections.abc import Mapping
from types import MappingProxyType

from freight_agent.ports.llm import (
    ChatReply,
    ChatRequest,
    LanguageModel,
    LlmError,
    LlmRequest,
    LlmResponse,
    ModelRole,
    SchemaT,
)


class NoModelForRole(LlmError):
    """The request's role has no model. A wiring gap, reported like a model failure so a
    run escalates to a Hero instead of crashing."""

    def __init__(self, role: ModelRole, configured: frozenset[ModelRole]) -> None:
        listed = ", ".join(sorted(r.value for r in configured))
        super().__init__(f"no model is configured for the {role.value} role (configured: {listed})")
        self.role = role


class RoleRouter:
    """`LanguageModel` that dispatches each request to the model of its role."""

    def __init__(self, models: Mapping[ModelRole, LanguageModel]) -> None:
        if not models:
            raise ValueError("the router needs at least one role with a model")
        # A private copy: changing the caller's mapping later does not reroute calls.
        self._models: Mapping[ModelRole, LanguageModel] = MappingProxyType(dict(models))

    @property
    def roles(self) -> frozenset[ModelRole]:
        return frozenset(self._models)

    def model_for(self, role: ModelRole) -> LanguageModel:
        try:
            return self._models[role]
        except KeyError:
            raise NoModelForRole(role, self.roles) from None

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        return await self.model_for(request.role).generate(request, schema)

    async def chat(self, request: ChatRequest, schema: type[SchemaT]) -> ChatReply[SchemaT]:
        return await self.model_for(request.role).chat(request, schema)
