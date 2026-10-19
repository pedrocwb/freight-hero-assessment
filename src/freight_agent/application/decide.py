"""Decide: the model reads the SOP and the load's memory and proposes a plan.

The model only proposes. It has read-only tools (`read_sop_section`,
`read_document`); the validator and the executor act, in code.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from freight_agent.application.context import RunContext
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.runs import Usage
from freight_agent.domain.sop import ResolvedSection, ResolvedSop
from freight_agent.ports.llm import LanguageModel


@dataclass(frozen=True)
class DecideResult:
    plan: Plan
    usage: tuple[Usage, ...]
    transcript: tuple[dict[str, Any], ...] = ()


class Decider:
    def __init__(
        self, llm: LanguageModel, *, max_tool_rounds: int = 2, timeout_s: float = 60.0
    ) -> None:
        self._llm = llm
        self._max_tool_rounds = max_tool_rounds
        self._timeout_s = timeout_s

    async def decide(
        self,
        ctx: RunContext,
        gate: GateDecision,
        sop: ResolvedSop,
        visible_sections: Sequence[ResolvedSection],
    ) -> DecideResult:
        """`sop` holds the sections for this intent and stage; `visible_sections` back the
        `read_sop_section` tool (every section this client and load can see)."""
        raise NotImplementedError  # card APP-3
