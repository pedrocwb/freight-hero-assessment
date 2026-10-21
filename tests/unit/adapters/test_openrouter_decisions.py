"""The Decisions API adapter against `httpx.MockTransport`. No network.

The replies are the shapes a live call returned (`typesafe/jev-1.14`, 2026-10-20): a
`noul` answer is P(yes), a `choice` its label with every label's probability, a `score`
its weighted position with each level's probability keyed by index, next to `usage`
with the cost in USD.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import langsmith
import pytest
from langsmith import run_helpers
from pydantic import SecretStr

from freight_agent.adapters.decisions.openrouter import (
    DECISIONS_URL,
    OpenRouterDecisions,
    answer_metadata,
    request_body,
)
from freight_agent.adapters.decisions.unavailable import UnavailableDecisions
from freight_agent.adapters.llm.models_config import DecisionRoleConfig, ModelPricing
from freight_agent.ports.decisions import (
    Choice,
    ChoiceAnswer,
    DecisionError,
    DecisionRequest,
    ImagePart,
    Score,
    ScoreAnswer,
    YesNo,
    YesNoAnswer,
)
from freight_agent.ports.llm import LlmError, ModelRole

pytestmark = pytest.mark.unit

KEY = "sk-or-test-0123456789"
MODEL = "typesafe/jev-1.14"
CONFIG = DecisionRoleConfig(model=MODEL, attempt_timeout_s=5, max_attempts=3, backoff_s=0.5)
PRICING = ModelPricing(input=0.042, output=0)

REQUEST = DecisionRequest(
    role=ModelRole.GATE,
    state=("A used road bike, 54 cm.", ImagePart(url="data:image/png;base64,AAAA")),
    questions={
        "matches": YesNo(instructions="Is it a 54 cm road bike?", yes="it is", no="it is not"),
        "category": Choice(
            instructions="What kind of bike?",
            options={"road": "a road bike", "city": "a city bike"},
        ),
        "condition": Score(instructions="Its condition?", levels=("poor", "fair", "good")),
    },
    timeout_s=10,
    prompt_version="local:abc",
)

REPLY: dict[str, Any] = {
    "model": "typesafe/jev-1.14-20261014",
    "answers": {
        "matches": {"type": "noul", "noul": 0.97},
        "category": {
            "type": "choice",
            "choice": "road",
            "probabilities": {"road": 0.9, "city": 0.1},
            "confidence": 0.9,
        },
        "condition": {
            "type": "score",
            "score": 1.87,
            "legend": {"0": "poor", "1": "fair", "2": "good"},
            "probabilities": {"0": 0, "1": 0.13, "2": 0.87},
            "confidence": 0.87,
        },
    },
    "usage": {"input_tokens": 473, "output_tokens": 71, "cost": 1.9866e-05},
    "id": "gen-dec-1",
    "provider": "TypeSafe",
}


class Script:
    """The transport's replies, in order, and every request it got."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def ok(body: dict[str, Any] = REPLY) -> httpx.Response:
    return httpx.Response(200, json=body)


def adapter(
    script: Script,
    *,
    config: DecisionRoleConfig = CONFIG,
    sleeps: list[float] | None = None,
    timer: Callable[[], float] | None = None,
) -> OpenRouterDecisions:
    async def sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    ticks = iter(float(i) for i in range(100))
    return OpenRouterDecisions(
        config,
        PRICING,
        SecretStr(KEY),
        transport=httpx.MockTransport(script),
        sleep=sleep,
        timer=timer or (lambda: next(ticks) * 0.25),
    )


# --- the request ------------------------------------------------------------------------


async def test_the_request_is_the_decisions_body_with_the_key_and_the_app_title() -> None:
    script = Script(ok())

    await adapter(script).decide(REQUEST)

    (sent,) = script.requests
    assert (sent.method, str(sent.url)) == ("POST", DECISIONS_URL)
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    assert sent.headers["content-type"] == "application/json"
    assert sent.headers["x-openrouter-title"] == "freight-agent"
    assert script.bodies()[0] == {
        "model": MODEL,
        "state": [
            "A used road bike, 54 cm.",
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        "questions": {
            "matches": {
                "type": "noul",
                "instructions": "Is it a 54 cm road bike?",
                "criteria": {"true": "it is", "false": "it is not"},
            },
            "category": {
                "type": "choice",
                "instructions": "What kind of bike?",
                "criteria": {"road": "a road bike", "city": "a city bike"},
            },
            "condition": {
                "type": "score",
                "instructions": "Its condition?",
                "criteria": ["poor", "fair", "good"],
            },
        },
    }


def test_the_body_keeps_the_options_in_the_question_order() -> None:
    body = request_body(MODEL, REQUEST)

    assert list(body["questions"]["category"]["criteria"]) == ["road", "city"]


# --- the reply --------------------------------------------------------------------------


async def test_each_answer_is_typed_and_the_usage_is_the_providers_cost() -> None:
    response = await adapter(Script(ok())).decide(REQUEST)

    assert response.answer("matches", YesNoAnswer) == YesNoAnswer(yes=0.97)
    category = response.answer("category", ChoiceAnswer)
    assert (category.choice, dict(category.probabilities), category.confidence) == (
        "road",
        {"road": 0.9, "city": 0.1},
        0.9,
    )
    condition = response.answer("condition", ScoreAnswer)
    assert (condition.score, condition.probabilities, condition.most_likely) == (
        1.87,
        (0.0, 0.13, 0.87),
        2,
    )
    usage = response.usage
    assert (usage.role, usage.model, usage.input_tokens, usage.output_tokens) == (
        "gate",
        MODEL,
        473,
        71,
    )
    assert usage.cost_usd == pytest.approx(1.9866e-05)
    assert usage.latency_ms == 250


async def test_a_label_the_model_did_not_weigh_is_zero_and_an_unasked_one_is_dropped() -> None:
    reply = json.loads(json.dumps(REPLY))
    reply["answers"]["category"]["probabilities"] = {"road": 1.0, "gravel": 0.0}

    response = await adapter(Script(ok(reply))).decide(REQUEST)

    assert dict(response.answer("category", ChoiceAnswer).probabilities) == {
        "road": 1.0,
        "city": 0.0,
    }


async def test_without_a_reported_cost_the_tokens_are_priced() -> None:
    reply = {**REPLY, "usage": {"input_tokens": 1_000_000, "output_tokens": 50}}

    response = await adapter(Script(ok(reply))).decide(REQUEST)

    assert response.usage.cost_usd == pytest.approx(0.042)


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({"matches": {"type": "noul", "noul": 0.5}}, "no choice answer to 'category'"),
        (
            {**REPLY["answers"], "matches": {"type": "choice", "choice": "road"}},
            "no noul answer to 'matches'",
        ),
    ],
)
async def test_a_reply_that_misses_a_question_or_its_kind_fails(
    answers: dict[str, Any], message: str
) -> None:
    with pytest.raises(DecisionError, match=message):
        await adapter(Script(ok({**REPLY, "answers": answers}))).decide(REQUEST)


@pytest.mark.parametrize(
    "body",
    [
        b"<html>gateway</html>",
        json.dumps({"answers": {"matches": {"type": "noul", "noul": 1.5}}}).encode(),
        json.dumps({"usage": {}}).encode(),
    ],
)
async def test_a_reply_that_is_not_a_decision_fails(body: bytes) -> None:
    with pytest.raises(DecisionError, match="the reply is not a decision"):
        await adapter(Script(httpx.Response(200, content=body))).decide(REQUEST)


def test_a_score_with_no_level_weighed_is_its_rounded_score() -> None:
    assert ScoreAnswer(score=1.6).most_likely == 2


# --- retries and errors ------------------------------------------------------------------


async def test_a_rate_limit_and_a_server_error_are_sent_again_with_a_doubled_backoff() -> None:
    sleeps: list[float] = []
    script = Script(httpx.Response(429), httpx.Response(503), ok())

    response = await adapter(script, sleeps=sleeps).decide(REQUEST)

    assert response.answer("matches", YesNoAnswer).yes == 0.97
    assert len(script.requests) == 3
    assert sleeps == [0.5, 1.0]


async def test_retry_after_sets_the_wait_within_a_cap() -> None:
    sleeps: list[float] = []
    script = Script(
        httpx.Response(429, headers={"Retry-After": "2"}),
        httpx.Response(429, headers={"Retry-After": "3600"}),
        ok(),
    )

    await adapter(script, sleeps=sleeps).decide(REQUEST)

    assert sleeps == [2.0, 10.0]


async def test_a_dropped_connection_is_sent_again() -> None:
    sleeps: list[float] = []
    script = Script(httpx.ConnectError("connection refused"), ok())

    await adapter(script, sleeps=sleeps).decide(REQUEST)

    assert (len(script.requests), sleeps) == (2, [0.5])


async def test_the_retries_are_bounded_and_the_last_status_is_named() -> None:
    script = Script(*(httpx.Response(502, json={"error": {"message": "upstream down"}}),) * 3)

    with pytest.raises(DecisionError, match=r"HTTP 502 after 3 attempts: upstream down"):
        await adapter(script).decide(REQUEST)

    assert len(script.requests) == 3


async def test_no_reply_after_every_attempt_fails() -> None:
    script = Script(*(httpx.ReadTimeout("slow"),) * 3)

    with pytest.raises(DecisionError, match="no reply after 3 attempts: ReadTimeout"):
        await adapter(script).decide(REQUEST)


async def test_a_client_error_is_not_sent_again_and_names_the_providers_message() -> None:
    body = {"error": {"message": "Invalid discriminator value", "code": 400}}
    script = Script(httpx.Response(400, json=body))

    with pytest.raises(DecisionError) as raised:
        await adapter(script).decide(REQUEST)

    assert str(raised.value) == f"{MODEL} (gate): HTTP 400: Invalid discriminator value"
    assert isinstance(raised.value, LlmError)  # the runtime escalates it as a model failure
    assert len(script.requests) == 1


async def test_an_error_body_that_is_not_json_is_quoted() -> None:
    script = Script(httpx.Response(401, text="  no\n auth  "))

    with pytest.raises(DecisionError, match=r"HTTP 401: no auth$"):
        await adapter(script).decide(REQUEST)


async def test_the_key_never_shows_in_an_error() -> None:
    script = Script(httpx.Response(401, json={"error": {"message": f"bad key {KEY}"}}))

    with pytest.raises(DecisionError) as raised:
        await adapter(script).decide(REQUEST)

    assert KEY not in str(raised.value)
    assert "[redacted]" in str(raised.value)


async def test_the_call_budget_bounds_every_attempt() -> None:
    async def slow(seconds: float) -> None:
        await asyncio.sleep(1)

    model = OpenRouterDecisions(
        CONFIG,
        PRICING,
        SecretStr(KEY),
        transport=httpx.MockTransport(Script(*(httpx.Response(503),) * 3)),
        sleep=slow,
    )
    request = DecisionRequest(role=ModelRole.GATE, state=("x",), questions={}, timeout_s=0.05)

    with pytest.raises(DecisionError, match=r"timed out after 0\.05 s"):
        await model.decide(request)


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(ValueError, match="empty"):
        OpenRouterDecisions(CONFIG, PRICING, SecretStr("  "))


# --- the client, the repr and tracing ---------------------------------------------------


async def test_one_client_serves_the_calls_of_a_loop_until_it_is_closed() -> None:
    model = adapter(Script(ok(), ok()))

    await model.decide(REQUEST)
    await model.decide(REQUEST)
    clients = list(model._clients.values())  # pyright: ignore[reportPrivateUsage]
    await model.aclose()

    assert len(clients) == 1 and clients[0].is_closed
    assert not model._clients  # pyright: ignore[reportPrivateUsage]


def test_the_repr_names_the_model_and_not_the_key() -> None:
    assert repr(adapter(Script())) == f"OpenRouterDecisions(model={MODEL!r})"


async def test_with_tracing_on_the_call_is_an_llm_span_with_the_answers_in_its_metadata() -> None:
    client = MagicMock(spec=langsmith.Client)

    with run_helpers.tracing_context(enabled=True, client=client, project_name="test"):
        await adapter(Script(ok())).decide(REQUEST)

    update = next(c for c in client.mock_calls if c[0] == "update_run").kwargs
    metadata = update["extra"]["metadata"]
    assert (update["name"], update["run_type"]) == (f"gate · {MODEL}", "llm")
    assert metadata["ls_model_name"] == MODEL and metadata["prompt_version"] == "local:abc"
    assert (metadata["decision_matches"], metadata["decision_category"]) == (0.97, "road")
    assert metadata["served_model"] == "typesafe/jev-1.14-20261014"
    assert update["outputs"]["usage_metadata"]["total_cost"] == pytest.approx(1.9866e-05)
    created = next(c for c in client.mock_calls if c[0] == "create_run").kwargs
    assert created["inputs"]["state"][1] == {"image_url": "data:image/png;base64,AAAA"}


def test_the_answers_flatten_into_span_metadata() -> None:
    metadata = answer_metadata(
        {
            "act": YesNoAnswer(yes=0.04),
            "intent": ChoiceAnswer(choice="question", probabilities={"question": 0.99}),
            "level": ScoreAnswer(score=1.5),
        }
    )

    assert metadata == {
        "decision_act": 0.04,
        "decision_intent": "question",
        "decision_intent_probabilities": {"question": 0.99},
        "decision_level": 1.5,
    }


async def test_a_process_without_a_decision_model_fails_every_call_with_the_reason() -> None:
    model = UnavailableDecisions("OPENROUTER_API_KEY is not set")

    with pytest.raises(DecisionError, match="OPENROUTER_API_KEY is not set"):
        await model.decide(REQUEST)
