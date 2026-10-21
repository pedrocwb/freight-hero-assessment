"""Save a conversation played on the platform as an eval scenario (`POST /api/scenarios`).

The person chooses the id, title, suite and tags, and for each recorded step
(`api.recording`) its note, its expectations and its `forbid` lines; the injections and
times come from the recording, never from the client. The file is the repository's kind:
a header comment that tells the story, then the fields, validated by the scenario spec
(`evalkit.spec.Scenario`) and parsed back before it is written to
`evals/scenarios/<suite>/<id>.yaml`. From there it is in the Scenarios list, in
`agent eval list` and in the next LangSmith experiment of its suite.

The load is the fixture it was created from (`evals/fixtures/*.json`), with
`load_overrides` for the top-level keys that differ. A file sent during the
conversation is copied to `evals/fixtures/recorded/<id>/`, so the scenario sends the
same bytes.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from freight_agent.api.errors import BadRequest, Conflict, NotFound, Unprocessable
from freight_agent.api.recording import RecordedStep, Recording
from freight_agent.evalkit.spec import Scenario, load_scenario
from freight_agent.interfaces.documents import BlobStore

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_TIME = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


class _Body(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StepDraft(_Body):
    index: int = Field(description="The recorded step (`GET /api/loads/{id}/recording`)")
    note: str = ""
    expect: dict[str, Any] = Field(default_factory=dict[str, Any])
    forbid: tuple[dict[str, Any], ...] = ()


class ScenarioDraft(_Body):
    """`POST /api/scenarios`: a scenario made of a load's recorded steps."""

    load_id: str
    id: str
    title: str = Field(min_length=1)
    suite: str = "core"
    tags: tuple[str, ...] = ("regression",)
    description: str = ""
    steps: tuple[StepDraft, ...] = Field(min_length=1)


class SavedScenario(_Body):
    id: str
    suite: str
    path: str = Field(description="The file, relative to the evals directory")


def build(
    draft: ScenarioDraft,
    recording: Recording,
    *,
    metadata: Mapping[str, Any],
    fixtures_dir: Path,
    start: datetime,
    attachments: Mapping[tuple[int, int], str] | None = None,
) -> Scenario:
    """The scenario of the draft: the recorded injections at their load times, the
    draft's expectations, the load's fixture. `attachments` maps a recorded file to its
    fixture path (default: none expected). Raises `BadRequest` or `Unprocessable`."""
    for name, value in (("id", draft.id), ("suite", draft.suite)):
        if not _NAME.match(value):
            raise BadRequest(
                f"the scenario {name} {value!r} must be lowercase letters, digits and "
                "underscores, starting with a letter"
            )
    by_index = {step.index: step for step in recording.steps}
    steps: list[dict[str, Any]] = []
    for chosen in sorted(draft.steps, key=lambda s: s.index):
        recorded = by_index.get(chosen.index)
        if recorded is None:
            raise BadRequest(f"no recorded step {chosen.index} for load {draft.load_id}")
        if recorded.at.date() != start.date():
            raise BadRequest(
                "a scenario plays on one day: the conversation went past midnight "
                f"(step {chosen.index} is on {recorded.at.date()})"
            )
        steps.append(_step(recorded, chosen, attachments or {}))
    fixture, overrides = _fixture(metadata, fixtures_dir)
    latencies = [s.latency_seconds for s in recording.steps if s.latency_seconds]
    data: dict[str, Any] = {
        "id": draft.id,
        "title": draft.title.strip(),
        "suite": draft.suite,
        "tags": list(dict.fromkeys(draft.tags)),
        "description": draft.description.strip(),
        "load": f"fixtures/{fixture}",
        "load_overrides": overrides,
        "timezone": getattr(start.tzinfo, "key", "America/Los_Angeles"),
        "date": start.date().isoformat(),
        "start": _clock(start),
        "run_latency_seconds": int(latencies[0]) if latencies else 60,
        "steps": steps,
    }
    try:
        return Scenario.model_validate(data)
    except ValidationError as exc:
        raise Unprocessable(f"the scenario is not valid: {_errors(exc)}") from exc


def render(scenario: Scenario, *, load_id: str, broker: str) -> str:
    """The scenario as the repository writes one: the story in a header comment, then
    the fields, small mappings and lists inline."""
    header = [
        f"# {scenario.title}",
        "#",
        f"# Recorded on the platform from a conversation with load {load_id} ({broker}).",
        '# Timing (docs/decisions.md, "Eval timing"): the run of an injected event happens '
        f"{scenario.run_latency_seconds} s",
        "# after it; timers fire with zero latency. "
        f"Times are {scenario.timezone}, {scenario.date.isoformat()}.",
        "#",
    ]
    for index, step in enumerate(scenario.steps):
        header.append(f"# Step {index}, {step.at[:5]}. {_story(step.model_dump(mode='json'))}")
        if step.note:
            header.append(f"#   {step.note}")
    dumped = scenario.model_dump(mode="json", exclude_defaults=True)
    dumped["tags"] = list(scenario.tags)
    dumped["date"] = scenario.date  # unquoted, as the files write it
    data = {k: dumped[k] for k in Scenario.model_fields if k in dumped}
    body = _dump(data).replace("\nsteps:\n", "\n\nsteps:\n", 1)
    return "\n".join(header) + "\n\n" + body


async def save(
    draft: ScenarioDraft,
    recording: Recording,
    *,
    evals_dir: Path,
    metadata: Mapping[str, Any] | None,
    start: datetime,
    broker: str,
    blobs: BlobStore,
    known_ids: Sequence[str],
) -> SavedScenario:
    """Validate the draft and write its file, and the files its steps send. 409 for an id
    that is taken; nothing stays written unless the scenario parses back as it was built."""
    if metadata is None:
        raise BadRequest(
            f"load {draft.load_id} was not created on this simulation, so a scenario cannot "
            "rebuild it from a fixture"
        )
    if not recording.steps:
        raise NotFound(f"nothing was recorded for load {draft.load_id}: play it first")
    path = evals_dir / "scenarios" / draft.suite / f"{draft.id}.yaml"
    chosen = {s.index for s in draft.steps}
    files: dict[tuple[int, int], tuple[str, str]] = {}  # fixture path, blob reference
    for step in recording.steps:
        if step.index not in chosen or step.message is None:
            continue
        for number, attachment in enumerate(step.message.attachments):
            ref = recording.blob_refs.get((step.index, number))
            if ref is None:
                raise BadRequest(f"the file of step {step.index} is no longer stored")
            fixture = f"fixtures/recorded/{draft.id}/{_safe(attachment.filename)}"
            files[(step.index, number)] = (fixture, ref)
    scenario = build(
        draft,
        recording,
        metadata=metadata,
        fixtures_dir=evals_dir / "fixtures",
        start=start,
        attachments={key: fixture for key, (fixture, _) in files.items()},
    )
    if draft.id in known_ids or path.exists():
        raise Conflict(f"a scenario {draft.id!r} already exists: choose another id")
    written: list[Path] = []
    try:
        for fixture, ref in files.values():
            target = evals_dir / fixture
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await blobs.get(ref))
            written.append(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(scenario, load_id=draft.load_id, broker=broker), encoding="utf-8")
        written.append(path)
        if load_scenario(path) != scenario:
            raise Unprocessable("the written scenario does not read back as it was built")
    except (ValidationError, yaml.YAMLError) as exc:
        _remove(written)
        raise Unprocessable(f"the written scenario does not parse: {exc}") from exc
    except BaseException:
        _remove(written)
        raise
    return SavedScenario(
        id=scenario.id, suite=scenario.suite, path=f"scenarios/{draft.suite}/{draft.id}.yaml"
    )


def _remove(paths: Sequence[Path]) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


# --- helpers ------------------------------------------------------------------------------


def _step(
    recorded: RecordedStep, chosen: StepDraft, attachments: Mapping[tuple[int, int], str]
) -> dict[str, Any]:
    step: dict[str, Any] = {"at": _clock(recorded.at), "note": chosen.note.strip()}
    if recorded.message is not None:
        message = recorded.message
        step["message"] = {
            "channel": message.channel.value,
            "sender": message.sender,
            "body": message.body,
        }
        files = [
            {
                "filename": a.filename,
                "fixture": attachments.get((recorded.index, n), ""),
                "content_type": a.content_type,
            }
            for n, a in enumerate(message.attachments)
        ]
        if files:
            step["message"]["attachments"] = files
    if recorded.ping is not None:
        step["ping"] = {"delay_risk": recorded.ping.delay_risk.value}
    if chosen.expect:
        step["expect"] = chosen.expect
    if chosen.forbid:
        step["forbid"] = list(chosen.forbid)
    return step


def _fixture(metadata: Mapping[str, Any], fixtures_dir: Path) -> tuple[str, dict[str, Any]]:
    """The fixture file closest to the load's metadata, and the keys that differ."""
    best: tuple[int, str, dict[str, Any]] | None = None
    for path in sorted(fixtures_dir.glob("*.json")):
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        fixture = cast(dict[str, Any], data)
        if set(fixture) - set(metadata):
            continue  # an override replaces keys, it cannot remove one
        overrides = {k: v for k, v in metadata.items() if fixture.get(k) != v}
        if best is None or len(overrides) < best[0]:
            best = (len(overrides), path.name, overrides)
    if best is None:
        raise BadRequest(f"no fixture in {fixtures_dir} matches the load's metadata")
    return best[1], best[2]


def _clock(at: datetime) -> str:
    return at.strftime("%H:%M") if at.second == 0 else at.strftime("%H:%M:%S")


def _safe(filename: str) -> str:
    return _FILENAME.sub("_", filename).strip("._") or "attachment"


def _story(step: Mapping[str, Any]) -> str:
    message, ping = step.get("message"), step.get("ping")
    if message:
        files = [a["filename"] for a in message.get("attachments") or ()]
        sent = f" with {', '.join(files)}" if files else ""
        who = message.get("sender") or "an unknown sender"
        return f"{str(message['channel']).upper()} from {who}{sent}: {message['body']!r}."
    if ping:
        return f"Ping, delay risk {ping['delay_risk']}."
    return "No injection: the clock moves and the timers due fire."


class _Dumper(yaml.SafeDumper):
    """Quotes a time ("10:30"), which YAML 1.1 would read back as a number of minutes."""


def _string(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = '"' if _TIME.match(value) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _string)


def _dump(data: Mapping[str, Any]) -> str:
    return yaml.dump(
        dict(data),
        Dumper=_Dumper,
        sort_keys=False,
        default_flow_style=None,
        allow_unicode=True,
        width=100,
    )


def _errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'scenario'}: {e['msg']}" for e in exc.errors()
    )
