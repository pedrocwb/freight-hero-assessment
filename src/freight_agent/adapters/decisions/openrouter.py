"""A `DecisionModel` on OpenRouter's Decisions API: TypeSafe's Jev.

Not the chat-completions API: `POST https://openrouter.ai/api/alpha/decisions`, with the
OpenRouter key, a body of `model`, `state` (texts, and `image_url` parts) and `questions`
keyed by name. The reply's `answers` are keyed by the same names:

    noul    {"instructions", "criteria": {"true", "false"}}
            -> {"type": "noul", "noul": P(yes)}
    choice  {"instructions", "criteria": {label: meaning, ...}}
            -> {"type": "choice", "choice": label, "probabilities": {label: p}, "confidence"}
    score   {"instructions", "criteria": [level, ...]}
            -> {"type": "score", "score": position, "probabilities": {"0": p, ...},
                "legend": {"0": level, ...}, "confidence"}

next to `usage` (`input_tokens`, `output_tokens`, `cost` in USD) and the dated model that
served the call (`typesafe/jev-1.14-20261014`). An error reply is
`{"error": {"message", "code"}}`.

- One `httpx.AsyncClient` per event loop, kept: calls reuse the connection, as the chat
  models' clients do, so a call's latency is the model's, not a new TLS handshake.
- A transient failure (429, 502, 503, 529, as for the chat models, or a dropped
  connection) is sent again on the chat models' `RetryPolicy` (`adapters/llm/resilient.py`):
  exponential backoff from `backoff_s` with jitter, a short `Retry-After` as the least
  wait and a long one as no retry, up to `max_attempts` sends, all within the request's
  `timeout_s`. The resilient wrapper serves chat models only, so the adapter runs the
  policy itself. Any other status, the budget running out, or a reply that does not
  answer every question with its kind is a `DecisionError` naming the model and the role.
  Each retry is logged as the chat models' are (`...; retry 1 of 2 in 0.5 s`). The key
  is only in the request headers: it is never logged and is scrubbed from every error
  text.
- A call's cost is the provider's `usage.cost`; without one, its tokens at the configured
  price. Its `Usage.provider` is the reply's `provider`.
- Each call is an `llm` span in LangSmith when tracing is on, under the graph node that
  made it: the state and the questions in, the answers out, and the answers flattened
  into the span's metadata (`decision_act`, `decision_intent`,
  `decision_intent_probabilities`), so traces can be filtered by them.
"""

import asyncio
import logging
import random
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from typing import Annotated, Any, Literal

import httpx
from langsmith import run_helpers
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from freight_agent.adapters.llm.models_config import DecisionRoleConfig, ModelPricing
from freight_agent.adapters.llm.resilient import RetryPolicy
from freight_agent.domain.runs import Usage
from freight_agent.ports.decisions import (
    Answer,
    Choice,
    ChoiceAnswer,
    DecisionError,
    DecisionRequest,
    DecisionResponse,
    ImagePart,
    Question,
    Score,
    ScoreAnswer,
    StatePart,
    YesNo,
    YesNoAnswer,
)

logger = logging.getLogger(__name__)

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
_APP_TITLE = "freight-agent"
_MAX_CAUSE = 500  # characters of a provider's error text kept in a DecisionError
_MAX_DELAY_S = 8.0  # the longest backoff; a longer Retry-After is no retry
_IMAGE_PREVIEW = 64  # characters of an image URL a trace shows
# Rate limited (429), the providers down or overloaded behind OpenRouter (502, 503) or
# saying so (529): the chat models' transient statuses (`adapters/llm/openrouter.py`).
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 529})

Sleep = Callable[[float], Awaitable[None]]
Timer = Callable[[], float]
Jitter = Callable[[], float]  # 0 to 1


# --- the provider's reply -------------------------------------------------------------


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")  # a field the provider adds is not an error


class _Noul(_Wire):
    type: Literal["noul"]
    noul: float = Field(ge=0, le=1)


class _Choice(_Wire):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float] = {}
    confidence: float | None = None


class _Score(_Wire):
    type: Literal["score"]
    score: float
    probabilities: dict[str, float] = {}
    confidence: float | None = None


class _Usage(_Wire):
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float | None = None


_WireAnswer = Annotated[_Noul | _Choice | _Score, Field(discriminator="type")]


class _Reply(_Wire):
    model: str | None = None
    provider: str | None = None  # the endpoint that served the call: "TypeSafe"
    answers: dict[str, _WireAnswer]
    usage: _Usage = _Usage()


# --- the request body -----------------------------------------------------------------


def request_body(model_id: str, request: DecisionRequest) -> dict[str, Any]:
    """The Decisions API body of `request`."""
    return {
        "model": model_id,
        "state": [_part(p) for p in request.state],
        "questions": {name: _question(q) for name, q in request.questions.items()},
    }


def _part(part: StatePart) -> str | dict[str, Any]:
    if isinstance(part, ImagePart):
        return {"type": "image_url", "image_url": {"url": part.url}}
    return part


def _question(question: Question) -> dict[str, Any]:
    match question:
        case YesNo():
            criteria: Any = {"true": question.yes, "false": question.no}
        case Choice():
            criteria = dict(question.options)
        case Score():
            criteria = list(question.levels)
    return {"type": _kind(question), "instructions": question.instructions, "criteria": criteria}


def _kind(question: Question) -> str:
    """The Decisions API's name of the question's kind."""
    match question:
        case YesNo():
            return "noul"
        case Choice():
            return "choice"
        case Score():
            return "score"


# --- the adapter ----------------------------------------------------------------------


class OpenRouterDecisions:
    """`DecisionModel` for one decision model of one role. The key is held as a
    `SecretStr`; `repr` shows the model only."""

    def __init__(
        self,
        config: DecisionRoleConfig,
        pricing: ModelPricing,
        api_key: SecretStr,
        *,
        url: str = DECISIONS_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        timer: Timer = time.perf_counter,
        jitter: Jitter = random.random,
    ) -> None:
        if not api_key.get_secret_value().strip():
            raise ValueError("the OpenRouter API key is empty")
        self._config = config
        self._retry = RetryPolicy(
            max_retries=config.max_attempts - 1,
            base_delay_s=min(config.backoff_s, _MAX_DELAY_S),
            max_delay_s=_MAX_DELAY_S,
        )
        self._jitter = jitter
        self._pricing = pricing
        self._api_key = api_key
        self._url = url
        self._transport = transport
        self._sleep = sleep
        self._timer = timer
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]
        self._clients = weakref.WeakKeyDictionary()

    @property
    def model_id(self) -> str:
        return self._config.model

    def __repr__(self) -> str:
        return f"OpenRouterDecisions(model={self.model_id!r})"

    async def decide(self, request: DecisionRequest) -> DecisionResponse:
        body = request_body(self.model_id, request)
        role = request.role.value
        metadata: dict[str, Any] = {
            "role": role,
            "ls_provider": "openrouter",
            "ls_model_name": self.model_id,
        }
        if request.prompt_version is not None:
            metadata["prompt_version"] = request.prompt_version
        async with run_helpers.trace(
            f"{role} · {self.model_id}",
            run_type="llm",
            inputs=_trace_inputs(body),
            tags=[f"role:{role}"],
            metadata=metadata,
        ) as span:
            started = self._timer()
            reply = await self._send(request, body)
            latency_ms = round((self._timer() - started) * 1000)
            response = DecisionResponse(
                answers=self._answers(request, reply),
                usage=self._usage(request, reply, latency_ms),
            )
            span.add_metadata(
                {**answer_metadata(response.answers), "served_model": reply.model or ""}
            )
            span.end(outputs=_trace_outputs(response))
        return response

    async def aclose(self) -> None:
        """Close the clients of every event loop (a process that ends does not need to)."""
        clients, self._clients = list(self._clients.values()), weakref.WeakKeyDictionary()
        for client in clients:
            await client.aclose()

    # --- sending ----------------------------------------------------------------------

    async def _send(self, request: DecisionRequest, body: dict[str, Any]) -> _Reply:
        try:
            async with asyncio.timeout(request.timeout_s):
                return await self._attempts(request, body)
        except TimeoutError:
            raise self._error(request, f"timed out after {request.timeout_s:g} s") from None

    async def _attempts(self, request: DecisionRequest, body: dict[str, Any]) -> _Reply:
        client = self._client()
        attempt = 0
        while True:
            attempt += 1
            retry_after: float | None = None
            try:
                response = await client.post(self._url, json=body)
            except httpx.TransportError as exc:  # timeouts and dropped connections
                cause = f"no reply: {type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if status == httpx.codes.OK:
                    return self._parse(request, response)
                cause = f"HTTP {status}: {_error_text(response)}"
                if status not in _TRANSIENT_STATUSES:
                    raise self._error(request, cause)
                retry_after = _retry_after_s(response)
            delay = self._retry.delay_s(attempt, retry_after, self._jitter())
            if delay is None:
                tried = f" (attempt {attempt} of {self._config.max_attempts})"
                raise self._error(request, cause + tried) from None
            retries = self._config.max_attempts - 1
            error = self._error(request, cause)
            logger.warning("%s; retry %d of %d in %.1f s", error, attempt, retries, delay)
            await self._sleep(delay)

    def _client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self._config.attempt_timeout_s),
                headers={
                    "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                    "X-OpenRouter-Title": _APP_TITLE,
                },
            )
            self._clients[loop] = client
        return client

    # --- reading the reply ------------------------------------------------------------

    def _parse(self, request: DecisionRequest, response: httpx.Response) -> _Reply:
        try:
            return _Reply.model_validate_json(response.content)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            raise self._error(request, f"the reply is not a decision: {problems}") from None

    def _answers(self, request: DecisionRequest, reply: _Reply) -> dict[str, Answer]:
        answers: dict[str, Answer] = {}
        for name, question in request.questions.items():
            answer = _answer(question, reply.answers.get(name))
            if answer is None:
                kind = _kind(question)
                raise self._error(request, f"the reply has no {kind} answer to {name!r}")
            answers[name] = answer
        return answers

    def _usage(self, request: DecisionRequest, reply: _Reply, latency_ms: int) -> Usage:
        tokens = reply.usage
        cost = tokens.cost
        if cost is None:
            cost = self._pricing.cost_usd(
                input_tokens=tokens.input_tokens, output_tokens=tokens.output_tokens
            )
        return Usage(
            role=request.role.value,
            model=self.model_id,
            provider=reply.provider,
            input_tokens=tokens.input_tokens,
            output_tokens=tokens.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )

    def _error(self, request: DecisionRequest, cause: str) -> DecisionError:
        text = f"{self.model_id} ({request.role.value}): {cause}"
        return DecisionError(text.replace(self._api_key.get_secret_value(), "[redacted]"))


def _answer(question: Question, raw: _Noul | _Choice | _Score | None) -> Answer | None:
    """The port's answer to `question`, or None when `raw` is not of its kind."""
    match question, raw:
        case YesNo(), _Noul():
            return YesNoAnswer(yes=raw.noul)
        case Choice(), _Choice():
            # Every option, in the question's order; a label the model did not weigh is 0.
            probabilities = {label: raw.probabilities.get(label, 0.0) for label in question.options}
            return ChoiceAnswer(
                choice=raw.choice, probabilities=probabilities, confidence=raw.confidence
            )
        case Score(), _Score():
            probabilities = tuple(
                raw.probabilities.get(str(i), 0.0) for i in range(len(question.levels))
            )
            return ScoreAnswer(
                score=raw.score, probabilities=probabilities, confidence=raw.confidence
            )
        case _:
            return None


def _retry_after_s(response: httpx.Response) -> float | None:
    """The reply's `Retry-After` in seconds, when it gives a number."""
    try:
        return max(float(response.headers.get("retry-after", "")), 0.0)
    except ValueError:
        return None


def _error_text(response: httpx.Response) -> str:
    """The provider's error message, else the start of the body."""
    try:
        data: Any = response.json()
        message = data["error"]["message"]
        text = message if isinstance(message, str) else str(message)
    except (ValueError, KeyError, TypeError):
        text = response.text
    return " ".join(text.split())[:_MAX_CAUSE] or "(empty body)"


# --- tracing --------------------------------------------------------------------------


def answer_metadata(answers: Mapping[str, Answer]) -> dict[str, Any]:
    """The answers as flat span metadata: `decision_<question>` is P(yes), the chosen
    label (with `decision_<question>_probabilities`) or the score."""
    metadata: dict[str, Any] = {}
    for name, answer in answers.items():
        key = f"decision_{name}"
        match answer:
            case YesNoAnswer():
                metadata[key] = answer.yes
            case ChoiceAnswer():
                metadata[key] = answer.choice
                metadata[f"{key}_probabilities"] = dict(answer.probabilities)
            case ScoreAnswer():
                metadata[key] = answer.score
    return metadata


def _trace_inputs(body: dict[str, Any]) -> dict[str, Any]:
    """The body as a trace shows it: an image's data URL cut short."""
    state = [
        p if isinstance(p, str) else {"image_url": str(p["image_url"]["url"])[:_IMAGE_PREVIEW]}
        for p in body["state"]
    ]
    return {"state": state, "questions": body["questions"]}


def _trace_outputs(response: DecisionResponse) -> dict[str, Any]:
    usage = response.usage
    answers: dict[str, Any] = {}
    for name, answer in response.answers.items():
        match answer:
            case YesNoAnswer():
                answers[name] = {"yes": answer.yes}
            case ChoiceAnswer():
                answers[name] = {
                    "choice": answer.choice,
                    "probabilities": dict(answer.probabilities),
                    "confidence": answer.confidence,
                }
            case ScoreAnswer():
                answers[name] = {
                    "score": answer.score,
                    "probabilities": list(answer.probabilities),
                    "confidence": answer.confidence,
                }
    return {
        "answers": answers,
        "usage_metadata": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.output_tokens,
            "total_cost": usage.cost_usd,
        },
    }
