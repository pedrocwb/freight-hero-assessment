"""The decision model interface: a typed decision about a state, not text.

A decision model (TypeSafe's Jev, on OpenRouter's Decisions API) reads a state (text
parts and images) and answers named questions with probabilities. It writes no prose and
no reasoning. There are three kinds of question:

- `YesNo`: is it so? The answer is the probability of yes (`YesNoAnswer.yes`).
- `Choice`: which of these labels? Each label comes with what it means; the answer is the
  most likely label and the probability of each one (`ChoiceAnswer`).
- `Score`: where on this ordered scale? The answer is the probability of each level, and
  their probability-weighted position (`ScoreAnswer.score`, 0 for the first level).

The caller turns the probabilities into what happens, with a policy in code (the gate's
threshold, `application/gate_decision.py`), so a run records what the model weighed next
to what code decided. A request's texts (the state's preamble, each question's
instructions and criteria) are prompt text: they come from the role's prompt template
(`interfaces.prompts`), whose version the request carries (`prompt_version`).

The adapter (`adapters/decisions/openrouter.py`) translates a request into the provider's
body and the reply back into answers. Every failure is a `DecisionError`, a kind of
`LlmError`, so the runtime handles it as any model failure: the run escalates to a Hero.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from freight_agent.domain.runs import Usage
from freight_agent.interfaces.llm import LlmError, ModelRole


@dataclass(frozen=True)
class ImagePart:
    """An image of the state: a `data:image/png;base64,...` or an https URL. An image puts
    the provider in its slower long-context mode."""

    url: str


# One part of a state, in order: a text, or an image.
StatePart = str | ImagePart


@dataclass(frozen=True)
class YesNo:
    """A yes/no question. `yes` and `no` say what makes each answer true."""

    instructions: str
    yes: str
    no: str


@dataclass(frozen=True)
class Choice:
    """A choice of one label. `options` maps each label to what it means, in the order
    the model reads them."""

    instructions: str
    options: Mapping[str, str]


@dataclass(frozen=True)
class Score:
    """A position on an ordered scale. `levels` go from the lowest to the highest."""

    instructions: str
    levels: tuple[str, ...]


Question = YesNo | Choice | Score


@dataclass(frozen=True)
class DecisionRequest:
    """One decision: every question is answered about the same `state`. `timeout_s`
    bounds the whole call, retries included."""

    role: ModelRole
    state: tuple[StatePart, ...]
    questions: Mapping[str, Question]
    timeout_s: float = 30.0
    prompt_version: str | None = None


@dataclass(frozen=True)
class YesNoAnswer:
    yes: float  # the probability of yes, 0 to 1


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str  # the most likely label
    probabilities: Mapping[str, float] = field(default_factory=dict[str, float])  # per label
    confidence: float | None = None


@dataclass(frozen=True)
class ScoreAnswer:
    score: float  # the probability-weighted position, 0 (first level) to len(levels) - 1
    probabilities: tuple[float, ...] = ()  # per level, in the question's order
    confidence: float | None = None

    @property
    def most_likely(self) -> int:
        """The index of the most likely level (the first one on a tie); the rounded score
        when no level has a probability."""
        if not self.probabilities:
            return round(self.score)
        return max(range(len(self.probabilities)), key=lambda i: self.probabilities[i])


Answer = YesNoAnswer | ChoiceAnswer | ScoreAnswer


class DecisionError(LlmError):
    """The decision model failed, timed out, or did not answer what it was asked."""


@dataclass(frozen=True)
class DecisionResponse:
    """Every question's answer, by the question's name, and what the call cost."""

    answers: Mapping[str, Answer]
    usage: Usage

    def answer[A: YesNoAnswer | ChoiceAnswer | ScoreAnswer](self, name: str, kind: type[A]) -> A:
        """The answer to `name`; `DecisionError` when it is missing or of another kind."""
        found = self.answers.get(name)
        if not isinstance(found, kind):
            got = "nothing" if found is None else type(found).__name__
            raise DecisionError(f"the question {name!r} got {got}, not a {kind.__name__}")
        return found


class DecisionModel(Protocol):
    async def decide(self, request: DecisionRequest) -> DecisionResponse: ...
