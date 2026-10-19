"""Decide: the model reads the SOP and the load's memory and proposes a plan.

The model only proposes. It has read-only tools (`read_sop_section`,
`read_document`); the validator and the executor act, in code.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from freight_agent.application.context import RunContext
from freight_agent.application.gate import follow_up_intent
from freight_agent.application.prompts import DECIDE_SYSTEM_PROMPT, render_decide_prompt
from freight_agent.application.tools import decide_tools
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision
from freight_agent.domain.runs import Usage
from freight_agent.domain.sop import ResolvedSection, ResolvedSop
from freight_agent.ports.llm import LanguageModel, LlmRequest, ModelRole


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
        `read_sop_section` tool (every section this client and load can see).

        One model call. `LlmError` propagates: the runtime escalates a model failure.
        """
        in_progress = sections_in_progress(ctx, sop, visible_sections)
        request = LlmRequest(
            role=ModelRole.DECIDE,
            system=DECIDE_SYSTEM_PROMPT,
            user=render_decide_prompt(ctx, gate, sop, in_progress),
            tools=decide_tools(ctx, visible_sections),
            max_tool_rounds=self._max_tool_rounds,
            timeout_s=self._timeout_s,
        )
        response = await self._llm.generate(request, Plan)
        return DecideResult(
            plan=response.parsed, usage=(response.usage,), transcript=response.transcript
        )


def sections_in_progress(
    ctx: RunContext, sop: ResolvedSop, visible_sections: Sequence[ResolvedSection]
) -> tuple[ResolvedSection, ...]:
    """The procedures that own the follow-ups of the open obligations.

    At 07:13 the driver asks for the PO number while an ETA request is open. The
    sections for a question do not say how that request continues, so the model
    also sees the section its follow-up timer will run (`delay_risk_high`), as
    context. The ledger stays facts only: the link is the obligation kind, mapped
    to the intent its timer produces, and the section's own intents and stages.
    """
    intents = {
        intent for o in ctx.open_obligations if (intent := follow_up_intent(o.kind)) is not None
    }
    shown = set(sop.section_ids)
    stage = ctx.load.stage
    return tuple(
        s
        for s in visible_sections
        if s.section_id not in shown
        and intents.intersection(s.intents)
        and (not s.stages or stage in s.stages)
    )
