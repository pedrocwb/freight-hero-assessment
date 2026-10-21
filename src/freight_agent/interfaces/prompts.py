"""The prompt interface: the template each model role renders its prompts from.

Every prompt a model reads is a chat template of two messages, system then user, whose
fixed text is versioned in the LangSmith Prompt Hub, with a reviewable copy in the
repository (`prompts/<role>.yaml`). Code computes the parts that depend on the broker or
on the run (the catalog's intents, the SOP sections, the ledger, a document's pages) and
passes them as variables; the template holds everything else, so a prompt is edited,
reviewed and versioned without a code change. SOP files are not prompts: they are the
clients' data, edited by their own people, in `sops/`. A decision model reads no chat,
but its texts are versioned the same way: the two messages are the parts of the state it
reads, and the template's `tools` hold its questions (`gate_decision`).

A template's version is its Hub commit, or `local:<sha256 prefix>` for the repository
copy. The composition root resolves every role once, at startup (`Prompts.resolve`), so
no run waits on the Hub and every run of a process reads the same versions. A run
records the version of each prompt it sent (`RunRecord.prompts`); the run's trace and
an eval experiment's metadata carry them too.

Each message has its own variables: the system message is rendered from the broker
alone (one cacheable prefix for every run of a broker), the user message from the run.
`MessageTemplate.format` raises `PromptError` for a variable it is not given, and each
service checks when it is built that its template names only variables it computes
(`application.prompts.check_variables`), so a bad edit in the Hub stops the process at
startup instead of rendering an empty block.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class PromptRole(StrEnum):
    """The model roles that render a prompt (the eval's simulated driver has none)."""

    GATE = "gate"  # triage of an inbound message
    AGENT = "agent"  # the agent loop's rounds
    EXTRACT = "extract"  # a document's type and values
    JUDGE = "judge"  # grades the wording of a step in the evals
    # Triage on a decision model (`GATE_ENGINE=jev`): the state's preamble and context,
    # and in `tools` each question's instructions and criteria, by key.
    GATE_DECISION = "gate_decision"
    MEMORY = "memory"  # the facility facts a delivered load taught us (add, update, delete)


class PromptError(Exception):
    """A template is missing, malformed, or does not fit the code that renders it.
    The message names the prompt and what to change."""


class MessageTemplate(Protocol):
    """One message of a chat template."""

    @property
    def variables(self) -> frozenset[str]:
        """The variables the message names."""
        ...

    def format(self, variables: Mapping[str, str]) -> str:
        """The message with every variable replaced by its value, unescaped. Raises
        `PromptError` when a variable the message names has no value."""
        ...


@dataclass(frozen=True)
class ChatPrompt:
    """One role's chat template, as resolved at startup."""

    role: PromptRole
    name: str  # the Hub prompt, `freight-agent-gate`
    ref: str  # what it was resolved from: `freight-agent-gate:prod`, or `prompts/gate.yaml`
    version: str  # the Hub commit, or `local:<sha256 prefix>` of the repository copy
    system: MessageTemplate
    user: MessageTemplate
    # What the model reads about a tool, by tool name, when the template sets it: the
    # agent's `finish` (the answer tool) is described here, not in code. A decision
    # model's template keeps its questions' texts here, by key (`gate_decision`).
    tools: Mapping[str, str] = field(default_factory=dict[str, str])

    def describe(self) -> str:
        """`freight-agent-gate:prod (3f2a9c41d0be)`, for logs and errors."""
        return f"{self.ref} ({self.version})"


class PromptRegistry(Protocol):
    """Where the templates come from: the Hub, at the reference each role is pinned to,
    or the repository copy."""

    def get(self, role: PromptRole) -> ChatPrompt:
        """The role's template. Raises `PromptError` when it cannot be read or is not a
        two-message chat template."""
        ...


@dataclass(frozen=True)
class Prompts:
    """The template of every role, resolved once for the process."""

    gate: ChatPrompt
    agent: ChatPrompt
    extract: ChatPrompt
    judge: ChatPrompt
    gate_decision: ChatPrompt
    memory: ChatPrompt

    @classmethod
    def resolve(cls, registry: PromptRegistry) -> "Prompts":
        return cls(
            gate=registry.get(PromptRole.GATE),
            agent=registry.get(PromptRole.AGENT),
            extract=registry.get(PromptRole.EXTRACT),
            judge=registry.get(PromptRole.JUDGE),
            gate_decision=registry.get(PromptRole.GATE_DECISION),
            memory=registry.get(PromptRole.MEMORY),
        )

    def all(self) -> tuple[ChatPrompt, ...]:
        return (self.gate, self.agent, self.extract, self.judge, self.gate_decision, self.memory)

    def versions(self) -> dict[str, str]:
        """Role -> version, as a run record and an experiment record them."""
        return {p.role.value: p.version for p in self.all()}

    def refs(self) -> dict[str, str]:
        """Role -> the reference each version was resolved from."""
        return {p.role.value: p.ref for p in self.all()}
