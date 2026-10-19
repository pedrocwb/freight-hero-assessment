"""The per-run LangGraph graph: the thinking part of a run.

    prefilter -> gate -> resolve_sop -> decide -> validate
        |          |          |                      |
       END        END        END                    END

Each arrow to END is a conditional exit, taken when the node wrote a verdict:
`skipped` or `escalated` (unknown sender) after the prefilter, `noop` after the
gate, `escalated` (no SOP section) after SOP resolution. `validate` always ends
the run.

`load_context` and `commit_execute` from the pipeline in docs/decisions.md run
in the runtime, around this graph, because they are the run's two transactions:
no unit of work may stay open while a model thinks. The graph itself never
touches storage.
"""

from collections.abc import AsyncIterator, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

# langgraph is a namespace package, so pyright does not see its py.typed marker.
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]

from freight_agent.application.context import RunContext
from freight_agent.application.decide import Decider
from freight_agent.application.gate import Gate
from freight_agent.application.graph.nodes import (
    Node,
    decide_node,
    gate_node,
    prefilter_node,
    resolve_sop_node,
    validate_node,
)
from freight_agent.application.graph.state import RunState
from freight_agent.domain.rules import default_rules
from freight_agent.domain.validation import Rule, ValidationConfig
from freight_agent.ports.sop import SopRepository

PREFILTER = "prefilter"
GATE = "gate"
RESOLVE_SOP = "resolve_sop"
DECIDE = "decide"
VALIDATE = "validate"


class _Compiled(Protocol):
    """The part of LangGraph's compiled graph the runtime uses."""

    def astream(
        self, input: RunState, config: Mapping[str, object], *, stream_mode: str
    ) -> AsyncIterator[dict[str, Any]]: ...


class _Builder(Protocol):
    """The part of LangGraph's `StateGraph` builder used here.

    LangGraph's own signatures carry unbound type parameters (cache and checkpoint
    types) that strict mode reports as unknown, so the graph is built through this
    narrower, fully typed view of the same object.
    """

    def add_node(self, name: str, node: Node) -> object: ...

    def add_edge(self, start: str, end: str) -> object: ...

    def add_conditional_edges(
        self, source: str, path: Callable[[RunState], str], path_map: dict[Hashable, str]
    ) -> object: ...

    def compile(self) -> _Compiled: ...


@dataclass(frozen=True)
class GraphDeps:
    gate: Gate
    decider: Decider
    sop: SopRepository
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    rules: Sequence[Rule] = field(default_factory=default_rules)


class RunGraph:
    """The compiled graph, streamed so that a caller always has the latest state.

    When a node raises (a model failure) or the caller cancels the stream (the
    run deadline), the states already yielded still say what the run had decided
    and which model calls it paid for.
    """

    def __init__(self, compiled: _Compiled) -> None:
        self._compiled = compiled

    async def stream(self, ctx: RunContext, *, run_id: str) -> AsyncIterator[RunState]:
        """Yield the state after every step, the first one being the input."""
        config: dict[str, object] = {
            "run_name": "agent_run",
            "configurable": {"thread_id": run_id},
            "metadata": {
                "run_id": run_id,
                "event_id": ctx.event.event_id,
                "load_id": ctx.load.load_id,
            },
        }
        async for values in self._compiled.astream(RunState(ctx=ctx), config, stream_mode="values"):
            yield RunState(**values)


def build_graph(deps: GraphDeps) -> RunGraph:
    builder = cast(_Builder, StateGraph(RunState))
    builder.add_node(PREFILTER, prefilter_node())
    builder.add_node(GATE, gate_node(deps.gate))
    builder.add_node(RESOLVE_SOP, resolve_sop_node(deps.sop))
    builder.add_node(DECIDE, decide_node(deps.decider))
    builder.add_node(VALIDATE, validate_node(deps.rules, deps.validation))
    builder.add_edge(START, PREFILTER)
    for node, after in ((PREFILTER, GATE), (GATE, RESOLVE_SOP), (RESOLVE_SOP, DECIDE)):
        builder.add_conditional_edges(node, _unless_decided(after), {after: after, END: END})
    builder.add_edge(DECIDE, VALIDATE)
    builder.add_edge(VALIDATE, END)
    return RunGraph(builder.compile())


def _unless_decided(next_node: str) -> Callable[[RunState], str]:
    """Go on to `next_node` unless the node that just ran ended the run."""

    def route(state: RunState) -> str:
        return END if state.verdict is not None else next_node

    return route
