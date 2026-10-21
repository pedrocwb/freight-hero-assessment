"""A `DecisionModel` for a process that has none: every call fails with the reason.

Like `adapters/llm/unavailable.py`: without `OPENROUTER_API_KEY` the platform still
starts, and a run whose gate needs the decision model fails like any model failure, so
the runtime escalates the event to a Hero with this reason.
"""

from freight_agent.interfaces.decisions import DecisionError, DecisionRequest, DecisionResponse


class UnavailableDecisions:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        raise DecisionError(self.reason)
