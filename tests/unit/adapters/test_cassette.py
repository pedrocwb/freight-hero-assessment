import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from freight_agent.adapters.llm.cassette import (
    RECORD_COMMAND,
    CassetteCorrupt,
    CassetteMissing,
    CassetteModel,
    cassette_key,
    model_slug,
)
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import (
    ImageInput,
    LlmError,
    LlmRequest,
    LlmResponse,
    ModelRole,
    SchemaT,
    ToolSpec,
)

MODEL_ID = "openai/gpt-4o-mini"
PAGE = ImageInput(data=b"a scanned page", media_type="image/png")
ACK = GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="just ok")
USAGE = Usage(role="gate", model=MODEL_ID, input_tokens=120, output_tokens=14, cost_usd=0.0001)
TRANSCRIPT: tuple[dict[str, Any], ...] = (
    {"type": "ai", "content": "", "tool_calls": [{"name": "read_sop_section", "args": {}}]},
    {"type": "tool", "content": "Section text", "name": "read_sop_section"},
)


async def _no_run(_: dict[str, Any]) -> str:
    raise AssertionError("tools must not run during replay")


def _tool(
    name: str = "read_sop_section",
    description: str = "Read one SOP section.",
    parameters: dict[str, Any] | None = None,
) -> ToolSpec:
    schema = parameters or {"type": "object", "properties": {"section_id": {"type": "string"}}}
    return ToolSpec(name=name, description=description, parameters=schema, run=_no_run)


def _request(**changes: Any) -> LlmRequest:
    base = LlmRequest(
        role=ModelRole.GATE,
        system="You classify driver messages.",
        user="Driver: ok thanks",
        tools=(_tool(),),
        max_tool_rounds=0,
    )
    return replace(base, **changes)


class FixedModel:
    """Returns one response and counts calls, so tests can see when the inner model runs."""

    def __init__(self, response: LlmResponse[Any]) -> None:
        self._response = response
        self.calls = 0

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        self.calls += 1
        return LlmResponse(
            parsed=schema.model_validate(self._response.parsed.model_dump()),
            usage=self._response.usage,
            transcript=self._response.transcript,
        )


def _recorder(transcript: tuple[dict[str, Any], ...] = TRANSCRIPT) -> FixedModel:
    return FixedModel(LlmResponse(parsed=ACK, usage=USAGE, transcript=transcript))


class OtherDecision(BaseModel):
    intent: Intent
    act: bool


# --- key ---------------------------------------------------------------------


@pytest.mark.unit
def test_key_is_stable_for_identical_inputs() -> None:
    first = cassette_key(MODEL_ID, _request(), GateDecision)
    second = cassette_key(MODEL_ID, _request(tools=(_tool(),)), GateDecision)

    assert first == second
    assert len(first) == 64


@pytest.mark.unit
def test_key_ignores_the_timeout_and_the_tool_implementation() -> None:
    async def other_run(_: dict[str, Any]) -> str:
        return "x"

    base = cassette_key(MODEL_ID, _request(), GateDecision)
    slower = cassette_key(MODEL_ID, _request(timeout_s=5.0), GateDecision)
    other_impl = cassette_key(
        MODEL_ID, _request(tools=(replace(_tool(), run=other_run),)), GateDecision
    )

    assert base == slower == other_impl


@pytest.mark.unit
@pytest.mark.parametrize(
    "change",
    [
        {"role": ModelRole.AGENT},
        {"system": "You classify driver messages. Be brief."},
        {"user": "Driver: ok thanks!"},
        {"max_tool_rounds": 2},
        {"tools": ()},
        {"tools": (_tool(), _tool(name="read_document"))},
        {"tools": (_tool(name="read_document"),)},
        {"tools": (_tool(description="Read a section."),)},
        {"tools": (_tool(parameters={"type": "object", "properties": {}}),)},
        {"images": (PAGE,)},
        {"max_cost_usd": 0.05},
    ],
    ids=lambda change: next(iter(change)),
)
def test_any_request_change_changes_the_key(change: dict[str, Any]) -> None:
    base = cassette_key(MODEL_ID, _request(), GateDecision)

    assert cassette_key(MODEL_ID, _request(**change), GateDecision) != base


@pytest.mark.unit
def test_images_are_keyed_by_their_bytes_and_type_in_order() -> None:
    other = ImageInput(data=b"another page", media_type="image/png")
    jpeg = replace(PAGE, media_type="image/jpeg")

    def key(*images: ImageInput) -> str:
        return cassette_key(MODEL_ID, _request(images=images), GateDecision)

    assert key(PAGE) == key(ImageInput(data=PAGE.data, media_type=PAGE.media_type))
    assert len({key(PAGE), key(other), key(jpeg), key(PAGE, other), key(other, PAGE)}) == 5


@pytest.mark.unit
def test_cost_caps_are_part_of_the_key() -> None:
    def key(cap: float) -> str:
        return cassette_key(MODEL_ID, _request(max_cost_usd=cap), GateDecision)

    assert key(0.05) == key(0.05) != key(0.01)


@pytest.mark.unit
def test_a_request_without_images_or_cap_keeps_its_pre_wave_3_key() -> None:
    """The key material grows only when a request has images or a cap, so a gate call
    recorded before wave 3 still replays (its prompt is rendered the same)."""
    request = _request()
    material = {
        "model_id": MODEL_ID,
        "role": request.role.value,
        "system": request.system,
        "user": request.user,
        "tools": [
            {"name": t.name, "description": t.description, "parameters": t.parameters}
            for t in request.tools
        ],
        "schema": {"name": "GateDecision", "json_schema": GateDecision.model_json_schema()},
        "max_tool_rounds": request.max_tool_rounds,
    }
    text = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    assert (
        cassette_key(MODEL_ID, request, GateDecision) == hashlib.sha256(text.encode()).hexdigest()
    )


@pytest.mark.unit
async def test_the_summary_names_the_image_hashes_and_the_cap_never_the_bytes(
    tmp_path: Path,
) -> None:
    request = _request(images=(PAGE,), max_cost_usd=0.05)
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder())
    await model.generate(request, GateDecision)

    text = model.path_for(request, GateDecision).read_text(encoding="utf-8")
    summary = json.loads(text)["request"]

    assert summary["images"] == [f"image/png:{hashlib.sha256(PAGE.data).hexdigest()}"]
    assert summary["max_cost_usd"] == 0.05
    assert "scanned page" not in text


@pytest.mark.unit
async def test_replay_returns_the_recorded_transcript_for_the_agent_to_fold(
    tmp_path: Path,
) -> None:
    """Tools never run in replay: the agent stages its plan from the recorded tool calls."""
    staged: tuple[dict[str, Any], ...] = (
        {"role": "system", "content": "s"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "send_sms", "args": {"contact_id": "drv"}}],
            "usage": {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 0, "cost_usd": 1e-6},
        },
        {"role": "tool", "tool_call_id": "c1", "name": "send_sms", "content": "accepted"},
    )
    request = _request(tools=(_tool(name="send_sms"),), max_tool_rounds=8, max_cost_usd=0.05)
    await CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder(staged)).generate(
        request, GateDecision
    )

    replayed = await CassetteModel(tmp_path, MODEL_ID, "replay").generate(request, GateDecision)

    assert replayed.transcript == staged


@pytest.mark.unit
def test_tool_order_is_part_of_the_key() -> None:
    a, b = _tool(name="a"), _tool(name="b")

    assert cassette_key(MODEL_ID, _request(tools=(a, b)), GateDecision) != cassette_key(
        MODEL_ID, _request(tools=(b, a)), GateDecision
    )


@pytest.mark.unit
def test_schema_and_model_id_are_part_of_the_key() -> None:
    base = cassette_key(MODEL_ID, _request(), GateDecision)

    assert cassette_key(MODEL_ID, _request(), OtherDecision) != base
    assert cassette_key("google/gemini-2.5-flash", _request(), GateDecision) != base


@pytest.mark.unit
def test_same_schema_name_with_other_fields_changes_the_key() -> None:
    class GateDecisionV2(GateDecision):
        confidence: float = 1.0

    GateDecisionV2.__name__ = "GateDecision"

    assert cassette_key(MODEL_ID, _request(), GateDecisionV2) != cassette_key(
        MODEL_ID, _request(), GateDecision
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model_id", "slug"),
    [
        ("openai/gpt-4o-mini", "openai-gpt-4o-mini"),
        ("anthropic/claude-3.5-haiku:beta", "anthropic-claude-3.5-haiku-beta"),
        ("Meta-Llama/Llama-3.1-8B", "meta-llama-llama-3.1-8b"),
        ("../../etc", "etc"),
    ],
)
def test_model_slug_is_one_safe_path_component(model_id: str, slug: str) -> None:
    assert model_slug(model_id) == slug


@pytest.mark.unit
@pytest.mark.parametrize("model_id", ["", "///", ".."])
def test_model_id_without_a_usable_slug_is_rejected(model_id: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="model id"):
        CassetteModel(tmp_path, model_id, "replay")


# --- construction ------------------------------------------------------------


@pytest.mark.unit
def test_record_mode_needs_an_inner_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="record mode needs"):
        CassetteModel(tmp_path, MODEL_ID, "record")


@pytest.mark.unit
def test_unknown_mode_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown cassette mode"):
        CassetteModel(tmp_path, MODEL_ID, "live")  # pyright: ignore[reportArgumentType]


@pytest.mark.unit
def test_replay_mode_refuses_an_inner_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="replay never calls"):
        CassetteModel(tmp_path, MODEL_ID, "replay", inner=ScriptedModel())


# --- replay ------------------------------------------------------------------


@pytest.mark.unit
async def test_missing_cassette_fails_loudly_with_how_to_record(tmp_path: Path) -> None:
    model = CassetteModel(tmp_path, MODEL_ID, "replay")
    key = cassette_key(MODEL_ID, _request(), GateDecision)

    with pytest.raises(CassetteMissing) as caught:
        await model.generate(_request(), GateDecision)

    message = str(caught.value)
    assert "gate" in message
    assert key[:16] in message
    assert MODEL_ID in message
    assert RECORD_COMMAND in message
    assert caught.value.path == model.path_for(_request(), GateDecision)


@pytest.mark.unit
def test_missing_cassette_is_not_a_model_failure() -> None:
    """The runtime turns LlmError into a Hero escalation; a missing cassette must not."""
    assert not issubclass(CassetteMissing, LlmError)
    assert not issubclass(CassetteCorrupt, LlmError)


@pytest.mark.unit
async def test_record_then_replay_round_trip(tmp_path: Path) -> None:
    inner = _recorder()
    recorded = await CassetteModel(tmp_path, MODEL_ID, "record", inner=inner).generate(
        _request(), GateDecision
    )

    replayed = await CassetteModel(tmp_path, MODEL_ID, "replay").generate(
        _request(timeout_s=1.0), GateDecision
    )

    assert inner.calls == 1
    assert recorded.parsed == ACK
    assert replayed.parsed == ACK
    assert isinstance(replayed.parsed, GateDecision)
    assert replayed.usage == USAGE
    assert replayed.transcript == TRANSCRIPT


@pytest.mark.unit
async def test_a_changed_prompt_is_a_missing_cassette(tmp_path: Path) -> None:
    await CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder()).generate(
        _request(), GateDecision
    )

    with pytest.raises(CassetteMissing):
        await CassetteModel(tmp_path, MODEL_ID, "replay").generate(
            _request(user="Driver: ok thanks, see you"), GateDecision
        )


@pytest.mark.unit
async def test_another_model_does_not_replay_this_models_cassette(tmp_path: Path) -> None:
    await CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder()).generate(
        _request(), GateDecision
    )

    with pytest.raises(CassetteMissing):
        await CassetteModel(tmp_path, "google/gemini-2.5-flash", "replay").generate(
            _request(), GateDecision
        )


@pytest.mark.unit
async def test_corrupt_cassette_names_the_file(tmp_path: Path) -> None:
    model = CassetteModel(tmp_path, MODEL_ID, "replay")
    path = model.path_for(_request(), GateDecision)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(CassetteCorrupt, match=str(path.name)):
        await model.generate(_request(), GateDecision)


def _doctor(document: dict[str, Any], field: str, value: Any) -> Any:
    if field == "<root>":
        return value
    return {**document, field: value}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("<root>", ["not", "an", "object"], "not a JSON object"),
        ("version", 99, "format version 99"),
        ("key", "0" * 64, "holds key"),
        ("parsed", {"intent": 42, "act": False, "reason": "x"}, "parsed output"),
        ("usage", {"role": "gate"}, "bad usage"),
        ("transcript", "not a list", "transcript"),
        ("transcript", ["not an object"], "transcript"),
    ],
)
async def test_a_doctored_cassette_is_corrupt_not_replayed(
    field: str, value: Any, problem: str, tmp_path: Path
) -> None:
    await CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder()).generate(
        _request(), GateDecision
    )
    model = CassetteModel(tmp_path, MODEL_ID, "replay")
    path = model.path_for(_request(), GateDecision)
    document = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps(_doctor(document, field, value)), encoding="utf-8")

    with pytest.raises(CassetteCorrupt, match=problem) as caught:
        await model.generate(_request(), GateDecision)

    assert RECORD_COMMAND in str(caught.value)


# --- record ------------------------------------------------------------------


@pytest.mark.unit
async def test_record_writes_one_file_per_model_and_key(tmp_path: Path) -> None:
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder())
    key = cassette_key(MODEL_ID, _request(), GateDecision)

    await model.generate(_request(), GateDecision)

    path = tmp_path / "openai-gpt-4o-mini" / f"{key[:16]}.json"
    assert model.path_for(_request(), GateDecision) == path
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == [path.name]


@pytest.mark.unit
async def test_cassette_holds_a_request_summary_not_the_prompts(tmp_path: Path) -> None:
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder())
    await model.generate(_request(), GateDecision)

    text = model.path_for(_request(), GateDecision).read_text(encoding="utf-8")
    document = json.loads(text)

    assert "You classify driver messages." not in text
    assert "Driver: ok thanks" not in text
    assert document["key"] == cassette_key(MODEL_ID, _request(), GateDecision)
    assert document["model_id"] == MODEL_ID
    summary = document["request"]
    assert summary["role"] == "gate"
    assert summary["schema"] == "GateDecision"
    assert summary["tools"] == ["read_sop_section"]
    assert summary["max_tool_rounds"] == 0
    assert len(summary["system_sha256"]) == len(summary["user_sha256"]) == 64
    assert document["parsed"] == ACK.model_dump(mode="json")
    assert document["usage"] == USAGE.model_dump(mode="json")


@pytest.mark.unit
async def test_cassette_files_are_byte_stable_and_pretty(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    for directory in (first, second):
        model = CassetteModel(directory, MODEL_ID, "record", inner=_recorder())
        await model.generate(_request(), GateDecision)
    name = Path("openai-gpt-4o-mini") / f"{cassette_key(MODEL_ID, _request(), GateDecision)[:16]}"

    a = (first / name).with_suffix(".json").read_bytes()
    b = (second / name).with_suffix(".json").read_bytes()

    assert a == b
    assert a.endswith(b"}\n")
    assert b'\n  "key": ' in a  # two-space indent, sorted keys


@pytest.mark.unit
async def test_rerecording_the_same_response_rewrites_the_same_bytes(tmp_path: Path) -> None:
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder())
    await model.generate(_request(), GateDecision)
    path = model.path_for(_request(), GateDecision)
    before = path.read_bytes()

    await model.generate(_request(), GateDecision)

    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left


@pytest.mark.unit
async def test_record_propagates_a_model_failure_and_writes_nothing(tmp_path: Path) -> None:
    inner = ScriptedModel()
    inner.push(ModelRole.GATE, LlmError("provider down"))
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=inner)

    with pytest.raises(LlmError, match="provider down"):
        await model.generate(_request(), GateDecision)

    assert not any(tmp_path.rglob("*.json"))


@pytest.mark.unit
async def test_a_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def disk_full(src: str, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("freight_agent.adapters.llm.cassette.os.replace", disk_full)
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder())

    with pytest.raises(OSError, match="disk full"):
        await model.generate(_request(), GateDecision)

    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []


@pytest.mark.unit
async def test_record_never_stores_secrets_from_the_transcript(tmp_path: Path) -> None:
    leaky: tuple[dict[str, Any], ...] = (
        {
            "type": "request",
            "headers": {"Authorization": "Bearer sk-or-v1-0123456789abcdef0123", "X-Title": "fa"},
            "api_key": "sk-or-v1-0123456789abcdef0123",
            "usage_metadata": {"input_tokens": 10, "output_tokens": 2},
        },
        {"type": "ai", "content": "key is sk-or-v1-abcdefabcdefabcdefabcdef, ok"},
    )
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder(leaky))

    await model.generate(_request(), GateDecision)

    text = model.path_for(_request(), GateDecision).read_text(encoding="utf-8")
    assert "sk-or-v1" not in text
    document = json.loads(text)
    request = document["transcript"][0]
    assert request["headers"]["X-Title"] == "fa"
    assert request["usage_metadata"] == {"input_tokens": 10, "output_tokens": 2}
    assert document["transcript"][1]["content"] == "key is [redacted], ok"


@pytest.mark.unit
async def test_record_stores_non_json_transcript_values_as_json(tmp_path: Path) -> None:
    rich: tuple[dict[str, Any], ...] = ({"type": "ai", "parsed": ACK, "tags": ("a", "b")},)
    model = CassetteModel(tmp_path, MODEL_ID, "record", inner=_recorder(rich))
    await model.generate(_request(), GateDecision)

    replayed = await CassetteModel(tmp_path, MODEL_ID, "replay").generate(_request(), GateDecision)

    assert replayed.transcript == (
        {"type": "ai", "parsed": ACK.model_dump(mode="json"), "tags": ["a", "b"]},
    )
