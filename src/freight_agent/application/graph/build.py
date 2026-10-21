"""The per-run LangGraph graph: the thinking part of a run.

    prefilter -> gate -> knowledge -> agent <-> tools
        |         |                    |  ^       |
       END       END                   |  '-------'
                                       v          v
                                    validate <----'  -> END

- `prefilter` and `gate` (the triage step) end the run when they write a verdict:
  `skipped` or `escalated` (unknown sender) after the prefilter, `noop` after the gate.
- `knowledge` never ends the run: an event no section covers is the agent's
  `no_procedure`.
- `agent` is one model round. It goes to `tools` when the round called tools (an
  answer given next to tool calls waits for them), to `validate` when it answered
  (`finish`), and back to itself when the model replied in prose (the next round must
  answer). It forces the answer itself once `max_steps` rounds or `max_cost_usd` are
  spent.
- `tools` answers the round's calls, then goes back to `agent`, or to `validate` when
  the round also answered or the refused calls went over `max_rejections`.
- `validate` always ends the run: `acted`, `noop`, or `escalated` (a high-severity
  violation, the rejection budget, no procedure).

The run deadline is the runtime's, around the whole stream. `load_context` and
`commit_execute` from the pipeline in docs/decisions.md run in the runtime, around this
graph, because they are the run's two transactions: no unit of work may stay open while
a model thinks. The graph itself never touches storage.

`compile_graph` builds the graph from its services alone (`GraphDeps`). LangGraph Studio
shows it inside the scenario graph of `freight_agent.studio`, whose `run` node calls the
runtime: a graph streamed from another graph's node inherits that node's config, so the
run graph becomes the node's subgraph, streamed and checkpointed under it. That is why
`RunGraph.stream` names no thread: a thread id would make it a graph of its own again,
and alone it has no checkpointer for a thread to address.

A run's root span carries, next to its ids, the gate's engine (`gate_engine`: `llm` or
`jev`) and the version of the gate's and the agent's prompt templates (`prompt_gate`, or
`prompt_gate_decision` on the jev engine, and `prompt_agent`); LangChain hands that
metadata down to every node and model call of the trace.
"""

from collections.abc import AsyncIterator, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol, cast
from uuid import UUID

# langgraph is a namespace package, so pyright does not see its py.typed marker.
from langgraph.graph import END, START, StateGraph  # pyright: ignore[reportMissingTypeStubs]

from freight_agent.application.agent import Agent
from freight_agent.application.context import RunContext
from freight_agent.application.gate import Triage
from freight_agent.application.graph.nodes import (
    Node,
    agent_node,
    knowledge_node,
    prefilter_node,
    tools_node,
    triage_node,
    validate_node,
)
from freight_agent.application.graph.state import RunState
from freight_agent.domain.rules import default_rules
from freight_agent.domain.validation import Rule
from freight_agent.ports.knowledge import SopIndex
from freight_agent.ports.prompts import PromptRole

PREFILTER = "prefilter"
# The triage step keeps the node id it had as the gate: LangSmith traces name each node
# run by it (agent_run -> gate -> the gate model call), and older traces stay comparable.
TRIAGE = "gate"
KNOWLEDGE = "knowledge"
AGENT = "agent"
TOOLS = "tools"
VALIDATE = "validate"
NODES = (PREFILTER, TRIAGE, KNOWLEDGE, AGENT, TOOLS, VALIDATE)


class _Drawable(Protocol):
    """The part of LangGraph's drawable graph the platform shows."""

    @property
    def nodes(self) -> Mapping[str, object]: ...

    def draw_mermaid(self) -> str: ...


class _Compiled(Protocol):
    """The part of LangGraph's compiled graph the runtime uses. With two stream modes,
    `astream` yields (mode, chunk) pairs."""

    def astream(
        self, input: RunState, config: Mapping[str, object], *, stream_mode: list[str]
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]: ...

    def get_graph(self) -> _Drawable: ...


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
    """`rules` are the validator's; the agent's staging uses `agent.rules` (the same by
    default), and the validate node logs any action the two judge differently."""

    gate: Triage
    agent: Agent
    knowledge: SopIndex
    rules: Sequence[Rule] = field(default_factory=default_rules)

    def prompt_versions(self) -> dict[str, str]:
        """Role -> the version of the prompt template the graph's model calls render."""
        return {
            self.gate.prompt.role.value: self.gate.prompt.version,
            PromptRole.AGENT.value: self.agent.prompt.version,
        }

    def trace_metadata(self) -> dict[str, str]:
        """What every run's root span carries besides its ids and the prompt versions."""
        return {"gate_engine": self.gate.engine.value}


class RunGraph:
    """The compiled graph, streamed so that a caller always has the latest state.

    When a node raises (a model failure) or the caller cancels the stream (the
    run deadline), the states already yielded still say what the run had decided,
    which model calls it paid for and which nodes it went through: `RunState.nodes`
    comes from LangGraph's task events, a visit counted when its node starts, so the
    node that failed is the last one.
    """

    def __init__(
        self,
        compiled: _Compiled,
        prompts: Mapping[str, str] | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """`prompts`: role -> prompt template version, and `metadata`, for each run's
        trace metadata."""
        self._compiled = compiled
        self._prompts = dict(prompts or {})
        self._metadata = dict(metadata or {})

    async def stream(
        self, ctx: RunContext, *, run_id: str, trace_id: UUID | None = None
    ) -> AsyncIterator[RunState]:
        """Yield the state after every step and as every node starts, the first one
        being the input.

        `trace_id` becomes the id of the run's root span when LangSmith traces it, so
        the run's trace link is known before the trace is sent.
        """
        load = ctx.load
        config: dict[str, object] = {
            "run_name": "agent_run",
            "metadata": {
                "run_id": run_id,
                "event_id": ctx.event.event_id,
                "event_type": ctx.event.type.value,
                "load_id": load.load_id,
                "broker_id": load.broker_id,
                **self._metadata,
                **{f"prompt_{role}": version for role, version in self._prompts.items()},
            },
            "tags": [f"load:{load.load_id}", f"broker:{load.broker_id}"],
        }
        if trace_id is not None:
            config["run_id"] = trace_id
        state = RunState(ctx=ctx)
        nodes: list[str] = []
        modes = ["values", "tasks"]
        async for mode, chunk in self._compiled.astream(state, config, stream_mode=modes):
            if mode == "values":
                state = replace(RunState(**chunk), nodes=tuple(nodes))
            elif "input" in chunk:  # a task starts; its end event carries `result`
                nodes.append(str(chunk["name"]))
                state = replace(state, nodes=tuple(nodes))
            else:
                continue
            yield state

    @property
    def compiled(self) -> object:
        """The compiled LangGraph graph itself, for an entry point that draws it inside its
        own graph (LangGraph Studio's scenario graph)."""
        return self._compiled

    def mermaid(self) -> str:
        """The graph as LangGraph draws it, in Mermaid."""
        return self._compiled.get_graph().draw_mermaid()

    def nodes(self) -> tuple[str, ...]:
        """The node ids, without LangGraph's start and end markers."""
        return tuple(n for n in self._compiled.get_graph().nodes if n not in (START, END))


def compile_graph(deps: GraphDeps) -> _Compiled:
    builder = cast(_Builder, StateGraph(RunState))
    builder.add_node(PREFILTER, prefilter_node())
    builder.add_node(TRIAGE, triage_node(deps.gate))
    builder.add_node(KNOWLEDGE, knowledge_node(deps.knowledge))
    builder.add_node(AGENT, agent_node(deps.agent))
    builder.add_node(TOOLS, tools_node(deps.agent))
    builder.add_node(VALIDATE, validate_node(deps.rules))
    builder.add_edge(START, PREFILTER)
    for node, after in ((PREFILTER, TRIAGE), (TRIAGE, KNOWLEDGE)):
        builder.add_conditional_edges(node, _unless_decided(node, after), {after: after, END: END})
    builder.add_edge(KNOWLEDGE, AGENT)
    builder.add_conditional_edges(
        AGENT, after_agent, {TOOLS: TOOLS, VALIDATE: VALIDATE, AGENT: AGENT}
    )
    builder.add_conditional_edges(TOOLS, after_tools, {AGENT: AGENT, VALIDATE: VALIDATE})
    builder.add_edge(VALIDATE, END)
    return builder.compile()


def build_graph(deps: GraphDeps) -> RunGraph:
    return RunGraph(compile_graph(deps), deps.prompt_versions(), deps.trace_metadata())


def _unless_decided(node: str, next_node: str) -> Callable[[RunState], str]:
    """Go on to `next_node` unless `node`, which just ran, ended the run."""

    def route(state: RunState) -> str:
        return END if state.verdict is not None else next_node

    route.__name__ = f"after_{node}"  # the edge's name in a trace
    return route


def after_agent(state: RunState) -> str:
    if state.pending_calls:
        return TOOLS
    if state.outcome is not None:
        return VALIDATE
    return AGENT  # prose: the next round must answer


def after_tools(state: RunState) -> str:
    over_budget = state.rejections > state.ctx.broker.limits.max_rejections
    return VALIDATE if state.outcome is not None or over_budget else AGENT
