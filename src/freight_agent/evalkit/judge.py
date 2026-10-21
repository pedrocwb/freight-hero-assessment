"""The LLM judge: grades the wording of a step against the scenario's rubric.

Code graders check facts (who got what, the ledger, the timers); some things only a
reader can judge: whether an SMS is polite, whether a Hero task says what the Hero needs
to do. A step that sets `expect.judge {rubric, min_score}` gets one call on the `judge`
role, with the rubric and every text the step created (SMS, emails, Slack posts, Hero
tasks, TMS notes), and passes when the verdict's score reaches `min_score`. The grading
instructions are the judge's template (`ports.prompts`, `prompts/judge.yaml`); the
rubric and the texts are its variables.

The player calls the judge right after the step, on the uncounted model, never through
the counting wrapper, so a judge call is not one of the agent's model calls:
`expect.llm_calls` means the same with or without a judge. Its usage is reported apart
(`Played.judge_usage`) and still counts toward the experiment's cost cap. The verdict is
part of the step's observation, so the `judge` evaluator reads it from the outputs like
the code graders read theirs. The judge model is `config/models.yaml`'s, with no
fallback: a verdict from another model is not comparable.
"""

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from freight_agent.application.prompts import check_variables
from freight_agent.domain.outbox import OutboxItem, OutboxKind
from freight_agent.domain.runs import Usage
from freight_agent.interfaces.llm import LanguageModel, LlmError, LlmRequest, ModelRole
from freight_agent.interfaces.prompts import ChatPrompt

DEFAULT_MIN_SCORE = 0.7
JUDGE_SYSTEM_VARIABLES: frozenset[str] = frozenset()
JUDGE_USER_VARIABLES = frozenset({"rubric", "count", "texts"})
_TEXT_FIELDS = ("subject", "text", "body", "title", "description")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class JudgeSpec(_Frozen):
    """`expect.judge`: what a reader checks in the step's texts, and the passing score."""

    rubric: str = Field(min_length=1, description="What the texts must do, point by point")
    min_score: float = Field(default=DEFAULT_MIN_SCORE, ge=0, le=1)


class JudgeVerdict(_Frozen):
    """The judge's structured answer. The reasons come first: small models fill fields in
    schema order and give a better score after writing down why."""

    reasons: tuple[str, ...] = Field(
        default=(),
        description="One short sentence per rubric point: met or not, and the words that show it",
    )
    score: float = Field(
        ge=0,
        le=1,
        description="1.0 when every rubric point is met, 0.0 when none is; in steps of 0.1",
    )


class JudgeResult(_Frozen):
    """What the player records for a step with a rubric. Exactly one of `verdict` and
    `error` is set."""

    rubric: str
    min_score: float
    verdict: JudgeVerdict | None = None
    error: str | None = None  # could not judge: a failure (nothing to judge, model error)
    usage: Usage | None = None

    @property
    def passed(self) -> bool:
        return self.verdict is not None and self.verdict.score >= self.min_score


def judged_texts(outbox: Sequence[OutboxItem]) -> list[str]:
    """One line per outbox item that carries text, labelled with its kind and recipient.

    Stage updates carry no text and are left out."""
    texts: list[str] = []
    for item in outbox:
        parts = [f"{name}: {value}" for name in _TEXT_FIELDS if (value := _str(item, name))]
        if not parts:
            continue
        head = item.kind.value + _recipient(item)
        texts.append(f"[{head}] " + " | ".join(parts))
    return texts


def _str(item: OutboxItem, name: str) -> str:
    value = item.payload.get(name)
    return " ".join(value.split()) if isinstance(value, str) else ""


def _recipient(item: OutboxItem) -> str:
    if item.kind is OutboxKind.SMS and isinstance(contact := item.payload.get("contact_id"), str):
        return f" to {contact}"
    if item.kind is OutboxKind.EMAIL:
        ids = item.payload.get("to_contact_ids")
        if isinstance(ids, list | tuple) and ids:
            return " to " + ", ".join(str(i) for i in ids)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if item.kind is OutboxKind.HERO_TASK and item.payload.get("urgent") is True:
        return " (urgent)"
    return ""


def render_judge_prompt(prompt: ChatPrompt, rubric: str, texts: Sequence[str]) -> str:
    listed = "\n".join(f"{n}. {text}" for n, text in enumerate(texts, 1))
    return prompt.user.format({"rubric": rubric.strip(), "count": str(len(texts)), "texts": listed})


def check_judge_prompt(prompt: ChatPrompt) -> ChatPrompt:
    """`prompt`, or `PromptError` when it names a variable the judge does not compute."""
    return check_variables(prompt, system=JUDGE_SYSTEM_VARIABLES, user=JUDGE_USER_VARIABLES)


class Judge:
    """Grades one step's texts on the `judge` role of `model` (the uncounted model), with
    the judge's template `prompt`."""

    def __init__(
        self, model: LanguageModel, prompt: ChatPrompt, *, timeout_s: float = 60.0
    ) -> None:
        """`PromptError` when the template does not fit the judge (`check_judge_prompt`)."""
        self._model = model
        self.prompt = check_judge_prompt(prompt)
        self._timeout_s = timeout_s

    async def grade(self, spec: JudgeSpec, outbox: Sequence[OutboxItem]) -> JudgeResult:
        """`LlmError` becomes an error result (a failed check); anything else propagates:
        a bug is a broken harness, not a bad answer."""
        texts = judged_texts(outbox)
        if not texts:
            return JudgeResult(
                rubric=spec.rubric,
                min_score=spec.min_score,
                error="nothing to judge: the step created no text",
            )
        request = LlmRequest(
            role=ModelRole.JUDGE,
            system=self.prompt.system.format({}),
            user=render_judge_prompt(self.prompt, spec.rubric, texts),
            timeout_s=self._timeout_s,
            prompt_version=self.prompt.version,
        )
        try:
            response = await self._model.generate(request, JudgeVerdict)
        except LlmError as exc:
            return JudgeResult(
                rubric=spec.rubric,
                min_score=spec.min_score,
                error=f"the judge model failed: {type(exc).__name__}: {exc}",
            )
        return JudgeResult(
            rubric=spec.rubric,
            min_score=spec.min_score,
            verdict=response.parsed,
            usage=response.usage,
        )
