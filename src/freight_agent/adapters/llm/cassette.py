"""Cassettes: record a real model's answers once, replay them offline on every PR.

A cassette is keyed by everything that shapes the model's answer: the model id,
the role, the prompts, the tools offered, the output schema and the tool-round
limit. Change any of them and the key changes, so a recording is never replayed
against a prompt it was not made for: the eval stops with `CassetteMissing` and
says how to record again. The timeout is not in the key because it does not
change what the model says.

In replay the recorded transcript stands in for the tool calls; tools do not
run. Cassettes are committed, so a file holds hashes of the prompts rather than
the prompts, and the transcript is scrubbed of credentials before it is written.
"""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from freight_agent.domain.runs import Usage
from freight_agent.ports.llm import LanguageModel, LlmRequest, LlmResponse, SchemaT

CassetteMode = Literal["replay", "record"]

RECORD_COMMAND = "uv run agent eval run --mode record"
FORMAT_VERSION = 1  # bump when the file layout changes, so old files read as corrupt, not wrong


class CassetteError(Exception):
    """A cassette cannot be replayed.

    Deliberately not an `LlmError`: the runtime turns `LlmError` into a Hero
    escalation, which would make a broken eval setup look like a model outage
    the agent handled well. A cassette problem must fail the eval loudly.
    """


class CassetteMissing(CassetteError):
    """No recording for this exact request: never recorded, or the request changed."""

    def __init__(self, *, role: str, key: str, model_id: str, path: Path) -> None:
        super().__init__(
            f"no cassette for the {role} call of model {model_id} (key {key[:16]}, "
            f"expected at {path}). The prompt, tools or schema changed, or this call was "
            f"never recorded: run `{RECORD_COMMAND}` to record it."
        )
        self.role = role
        self.key = key
        self.model_id = model_id
        self.path = path


class CassetteCorrupt(CassetteError):
    """The file exists but does not hold a usable recording for this request."""


def cassette_key(model_id: str, request: LlmRequest, schema: type[BaseModel]) -> str:
    """sha256 of canonical JSON over every input that shapes the model's answer."""
    material = {
        "model_id": model_id,
        "role": request.role.value,
        "system": request.system,
        "user": request.user,
        "tools": [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in request.tools
        ],
        "schema": {"name": schema.__name__, "json_schema": schema.model_json_schema()},
        "max_tool_rounds": request.max_tool_rounds,
    }
    return _sha256(json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


_UNSAFE_PATH_CHARS = re.compile(r"[^a-z0-9._-]+")


def model_slug(model_id: str) -> str:
    """One safe directory name per model: `openai/gpt-4o-mini` -> `openai-gpt-4o-mini`."""
    slug = _UNSAFE_PATH_CHARS.sub("-", model_id.lower()).strip("-.")
    if not slug:
        raise ValueError(f"model id {model_id!r} gives no usable cassette directory name")
    return slug


class CassetteModel:
    """A `LanguageModel` that replays recorded answers, or records a real model's answers.

    `replay` never calls a model; `record` calls `inner` every time and overwrites
    the cassette, so re-recording after a prompt change is one live run.
    """

    def __init__(
        self,
        directory: Path,
        model_id: str,
        mode: CassetteMode,
        inner: LanguageModel | None = None,
    ) -> None:
        if mode not in ("replay", "record"):
            raise ValueError(f"unknown cassette mode {mode!r}")
        if mode == "record" and inner is None:
            raise ValueError("record mode needs the inner model to record from")
        if mode == "replay" and inner is not None:
            raise ValueError("replay never calls a model; do not pass an inner model")
        self._directory = directory / model_slug(model_id)
        self._model_id = model_id
        self._inner = inner

    def path_for(self, request: LlmRequest, schema: type[BaseModel]) -> Path:
        return self._path(cassette_key(self._model_id, request, schema))

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        key = cassette_key(self._model_id, request, schema)
        path = self._path(key)
        if self._inner is None:  # replay: the constructor allows no inner model in record mode
            return _replay(path, key, self._model_id, request, schema)
        response = await self._inner.generate(request, schema)
        _write(path, self._document(key, request, schema, response))
        return response

    def _path(self, key: str) -> Path:
        return self._directory / f"{key[:16]}.json"

    def _document(
        self,
        key: str,
        request: LlmRequest,
        schema: type[BaseModel],
        response: LlmResponse[Any],
    ) -> dict[str, Any]:
        return {
            "version": FORMAT_VERSION,
            "key": key,
            "model_id": self._model_id,
            "request": {
                "role": request.role.value,
                "schema": schema.__name__,
                "system_sha256": _sha256(request.system),
                "user_sha256": _sha256(request.user),
                "tools": [tool.name for tool in request.tools],
                "max_tool_rounds": request.max_tool_rounds,
            },
            "parsed": response.parsed.model_dump(mode="json"),
            "usage": response.usage.model_dump(mode="json"),
            "transcript": _scrub(
                to_jsonable_python(list(response.transcript), serialize_unknown=True)
            ),
        }


def _replay(
    path: Path, key: str, model_id: str, request: LlmRequest, schema: type[SchemaT]
) -> LlmResponse[SchemaT]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CassetteMissing(
            role=request.role.value, key=key, model_id=model_id, path=path
        ) from None
    try:
        document: Any = json.loads(text)
    except ValueError as exc:
        raise _corrupt(path, f"not valid JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise _corrupt(path, "not a JSON object")
    fields = cast(dict[str, Any], document)
    if fields.get("version") != FORMAT_VERSION:
        raise _corrupt(path, f"format version {fields.get('version')!r}, expected {FORMAT_VERSION}")
    if fields.get("key") != key:
        raise _corrupt(path, f"holds key {fields.get('key')!r}, expected {key}")
    try:
        parsed = schema.model_validate(fields.get("parsed"))
    except ValidationError as exc:
        raise _corrupt(path, f"the parsed output does not match {schema.__name__}: {exc}") from exc
    try:
        usage = Usage.model_validate(fields.get("usage"))
    except ValidationError as exc:
        raise _corrupt(path, f"bad usage: {exc}") from exc
    transcript = fields.get("transcript")
    if not isinstance(transcript, list) or not all(
        isinstance(entry, dict) for entry in cast(list[Any], transcript)
    ):
        raise _corrupt(path, "the transcript is not a list of objects")
    return LlmResponse(
        parsed=parsed, usage=usage, transcript=tuple(cast(list[dict[str, Any]], transcript))
    )


def _corrupt(path: Path, problem: str) -> CassetteCorrupt:
    return CassetteCorrupt(f"cassette {path}: {problem}; re-record it with `{RECORD_COMMAND}`")


def _write(path: Path, document: dict[str, Any]) -> None:
    """Pretty, key-sorted JSON, written atomically so a crash never leaves half a cassette."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Credentials a provider adapter might leave in a transcript (request headers, client
# settings). Matched on the key name, normalised to snake_case, and on the value shape.
# `token$` does not match usage keys such as `input_tokens`.
_SECRET_KEY = re.compile(r"(api_?key|authorization|password|secret|cookie|token)$")
_SECRET_VALUE = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}|\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_REDACTED = "[redacted]"


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _REDACTED if _is_secret_key(key) else _scrub(item)
            for key, item in cast(dict[str, Any], value).items()
        }
    if isinstance(value, list):
        return [_scrub(item) for item in cast(list[Any], value)]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(_REDACTED, value)
    return value


def _is_secret_key(key: str) -> bool:
    return _SECRET_KEY.search(key.lower().replace("-", "_")) is not None
