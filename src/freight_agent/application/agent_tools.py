"""The agent's tools: read tools, one action tool per `Action` type, and `finish`.

Read tools (`search_sop`, `read_sop_section`, `get_history`, `get_ledger`, `get_load`,
`read_document`, `list_document_requirements`) answer from the run's context
(`RunContext`, loaded before the think phase) or from the knowledge index
(`RuntimeDeps.knowledge`): the agent thinks outside any transaction, so no tool touches a
unit of work. A bad argument is an answer, not an exception: the tool returns the valid
values so a small model can recover in its next round.

Action tools (`send_sms`, `send_email`, `post_broker_slack`, `write_tms_note`,
`create_hero_task`, `post_internal_slack`, `update_stage`, `schedule_timer`,
`defer_obligation`, `close_obligation`, `open_obligation`) are specs only: the run
graph's `tools` node builds the `Action` from a call's arguments (`build_action`) and
stages it (`application.agent.stage`), which checks it with
`domain.validation.check_action` against the actions accepted so far and answers
`accepted as action #n` or `rejected by <rule>: <why>`. A tool never executes anything:
code executes the staged plan after the run.

`finish` is the answer tool: `AgentOutcome`, whose schema title is `finish`, offered by
the adapter next to these tools (`ports.llm.LanguageModel.chat`).
"""

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from freight_agent.application.context import RunContext
from freight_agent.application.prompts import (
    render_document,
    render_history,
    render_ledger,
    render_load,
    render_requirements,
)
from freight_agent.application.tools import (
    ACTION_CHECKED,
    ACTION_DESCRIPTIONS,
    FIELD_DESCRIPTIONS,
    GET_HISTORY,
    GET_LEDGER,
    GET_LOAD,
    LIST_DOCUMENT_REQUIREMENTS,
    READ_DESCRIPTIONS,
    READ_DOCUMENT,
    READ_SOP_SECTION,
    READ_TOOLS,
    SEARCH_SOP,
)
from freight_agent.domain.actions import (
    FINISH,
    Action,
    CloseObligation,
    CreateHeroTask,
    DeferObligation,
    OpenObligation,
    PostBrokerSlack,
    PostInternalSlack,
    ScheduleTimer,
    SendEmail,
    SendSms,
    UpdateStage,
    WriteTmsNote,
)
from freight_agent.domain.events import Attachment
from freight_agent.domain.sop import ResolvedSection
from freight_agent.interfaces.knowledge import SopIndex
from freight_agent.interfaces.llm import ToolSpec

__all__ = [
    "ACTION_TOOLS",
    "FINISH",
    "READ_SOP_SECTION",
    "READ_TOOLS",
    "SEARCH_SOP",
    "ReadTool",
    "Toolbox",
    "agent_tools",
    "build_action",
    "invalid_arguments",
]

SEARCH_HITS = 5
HISTORY_DEFAULT = 50

# The action tools, by name: each stages one action of that type.
_ACTION_MODELS: Mapping[str, type[BaseModel]] = {
    "send_sms": SendSms,
    "send_email": SendEmail,
    "post_broker_slack": PostBrokerSlack,
    "write_tms_note": WriteTmsNote,
    "create_hero_task": CreateHeroTask,
    "post_internal_slack": PostInternalSlack,
    "update_stage": UpdateStage,
    "schedule_timer": ScheduleTimer,
    "defer_obligation": DeferObligation,
    "close_obligation": CloseObligation,
    "open_obligation": OpenObligation,
}
ACTION_TOOLS = tuple(_ACTION_MODELS)

_ACTION = TypeAdapter[Action](Action)

# A read tool: its arguments in, its answer to the model out.
ReadTool = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class _Reader:
    spec: ToolSpec
    run: ReadTool


@dataclass(frozen=True)
class Toolbox:
    """The tools of one agent round: what the model is offered (`specs`, the answer tool
    aside, read tools first) and how each read tool answers (`readers`, by name)."""

    specs: tuple[ToolSpec, ...]
    readers: Mapping[str, ReadTool]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self.specs)


def agent_tools(
    ctx: RunContext, visible_sections: Sequence[ResolvedSection], knowledge: SopIndex
) -> Toolbox:
    """The tools of one run. A pure function of the run's context, so the graph builds the
    same toolbox for the model's round and for the tools node that answers it.

    `read_document` is offered only when an attachment or a document is in view, and
    `list_document_requirements` only when the broker requires a document, so a small
    model is not tempted by a tool that cannot help.
    """
    readers = [
        _search_sop_tool(ctx, knowledge),
        _read_sop_section_tool(visible_sections),
        _get_history_tool(ctx),
        _get_ledger_tool(ctx),
        _get_load_tool(ctx),
    ]
    readable = _readable_ids(ctx)
    if readable:
        readers.append(_read_document_tool(ctx, readable))
    if ctx.broker.documents:
        readers.append(_requirements_tool(ctx))
    actions = tuple(_action_tool(name, ctx) for name in ACTION_TOOLS)
    return Toolbox(
        specs=(*(r.spec for r in readers), *actions),
        readers={r.spec.name: r.run for r in readers},
    )


# --- read tools ---------------------------------------------------------------------------


def _search_sop_tool(ctx: RunContext, knowledge: SopIndex) -> _Reader:
    load = ctx.load

    async def run(args: dict[str, Any]) -> str:
        query = _text_arg(args, "query")
        if not query:
            return "Missing query: give the words to look for, like 'trailer temperature'."
        hits = await knowledge.search(load.broker_id, load.load_types, query, k=SEARCH_HITS)
        if not hits:
            return (
                f"No SOP section matches {_json(query)}. Try other words, or read the catalog "
                "of sections in the prompt."
            )
        lines = [f"- {h.section_id}: {h.title}. {h.description}" for h in hits]
        return (
            f"Sections that match {_json(query)}, best first (read one with "
            f"{READ_SOP_SECTION}):\n" + "\n".join(lines)
        )

    spec = ToolSpec(
        name=SEARCH_SOP,
        description=READ_DESCRIPTIONS[SEARCH_SOP],
        parameters=_object(
            {"query": {"type": "string", "description": "The words to look for."}}, ["query"]
        ),
    )
    return _Reader(spec, run)


def _read_sop_section_tool(sections: Sequence[ResolvedSection]) -> _Reader:
    by_id = {s.section_id: s for s in sections}

    async def run(args: dict[str, Any]) -> str:
        if not by_id:
            return "No SOP sections are available for this load."
        section = by_id.get(_text_arg(args, "section_id"))
        if section is None:
            return _unknown("section_id", args, by_id)
        return f"# {section.section_id}: {section.title}\n\n{section.body.strip()}"

    spec = ToolSpec(
        name=READ_SOP_SECTION,
        description=READ_DESCRIPTIONS[READ_SOP_SECTION],
        parameters=_object(
            {"section_id": _id_property("The id of the section, as listed.", by_id)},
            ["section_id"],
        ),
    )
    return _Reader(spec, run)


def _get_history_tool(ctx: RunContext) -> _Reader:
    available = len(ctx.long_history)

    async def run(args: dict[str, Any]) -> str:
        limit = args.get("limit", HISTORY_DEFAULT)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            limit = HISTORY_DEFAULT
        return render_history(ctx, limit)

    spec = ToolSpec(
        name=GET_HISTORY,
        description=f"{READ_DESCRIPTIONS[GET_HISTORY]} {available} events are available.",
        parameters=_object(
            {
                "limit": {
                    "type": "integer",
                    "description": f"How many of the latest events (default {HISTORY_DEFAULT}).",
                }
            },
            [],
        ),
    )
    return _Reader(spec, run)


def _get_ledger_tool(ctx: RunContext) -> _Reader:
    kinds = sorted({o.kind for o in ctx.obligations})

    async def run(args: dict[str, Any]) -> str:
        kind = _text_arg(args, "kind")
        return render_ledger(ctx, kind or None)

    spec = ToolSpec(
        name=GET_LEDGER,
        description=READ_DESCRIPTIONS[GET_LEDGER],
        parameters=_object(
            {
                "kind": {
                    "type": "string",
                    "description": "Optional: the obligation kind you care about.",
                    **({"enum": kinds} if kinds else {}),
                }
            },
            [],
        ),
    )
    return _Reader(spec, run)


def _get_load_tool(ctx: RunContext) -> _Reader:
    async def run(args: dict[str, Any]) -> str:
        return render_load(ctx, _text_arg(args, "part") or "all")

    spec = ToolSpec(
        name=GET_LOAD,
        description=READ_DESCRIPTIONS[GET_LOAD],
        parameters=_object(
            {
                "part": {
                    "type": "string",
                    "enum": ["all", "stops", "contacts"],
                    "description": "Optional: the part you need (default all).",
                }
            },
            [],
        ),
    )
    return _Reader(spec, run)


def _readable_ids(ctx: RunContext) -> dict[str, Attachment | None]:
    """The ids `read_document` accepts: every document of the load and every attachment in
    view, by document id and by attachment id. An attachment maps to itself, a document
    id to None (it is read from `ctx.documents`)."""
    ids: dict[str, Attachment | None] = {}
    for event in (ctx.event, *ctx.recent_events):
        for attachment in event.attachments:
            ids.setdefault(attachment.attachment_id, attachment)
    for document in ctx.documents:
        ids.setdefault(document.document_id, None)
    return ids


def _read_document_tool(ctx: RunContext, readable: Mapping[str, Attachment | None]) -> _Reader:
    async def run(args: dict[str, Any]) -> str:
        # `id` is the parameter; a model that names it after the id it holds is understood.
        wanted = next(
            (v for k in ("id", "document_id", "attachment_id") if (v := _text_arg(args, k))), ""
        )
        if wanted not in readable:
            return _unknown("id", {"id": wanted} if wanted else {}, readable)
        document = ctx.document(wanted)
        if document is not None:
            return render_document(document)
        attachment = readable[wanted]
        assert attachment is not None  # a document id is in `ctx.documents`
        # Every value comes from outside (a filename is whatever the sender chose), so each
        # is a JSON string: a newline in one cannot start a line of its own.
        return (
            f"attachment {_json(attachment.attachment_id)}\n"
            f"filename: {_json(attachment.filename)}\n"
            f"content type: {_json(attachment.content_type)}\n"
            "The document pipeline has no record of this attachment, so its type and values "
            "are unknown. Do not guess them: create a Hero task to review the document, with "
            "the attachment id and the filename."
        )

    spec = ToolSpec(
        name=READ_DOCUMENT,
        description=READ_DESCRIPTIONS[READ_DOCUMENT],
        parameters=_object(
            {"id": _id_property("The document id or the attachment id.", readable)}, ["id"]
        ),
    )
    return _Reader(spec, run)


def _requirements_tool(ctx: RunContext) -> _Reader:
    types = sorted({r.type_id for r in ctx.broker.documents})

    async def run(args: dict[str, Any]) -> str:
        return render_requirements(ctx, _text_arg(args, "type_id") or None)

    spec = ToolSpec(
        name=LIST_DOCUMENT_REQUIREMENTS,
        description=READ_DESCRIPTIONS[LIST_DOCUMENT_REQUIREMENTS],
        parameters=_object(
            {
                "type_id": {
                    "type": "string",
                    "enum": types,
                    "description": "Optional: the document type you care about.",
                }
            },
            [],
        ),
    )
    return _Reader(spec, run)


# --- action tools -------------------------------------------------------------------------


def build_action(tool: str, args: Mapping[str, Any]) -> Action:
    """The action an action tool call describes. Arguments the action does not have, and
    nulls, are dropped (a small model fills fields of other actions too); anything else
    goes to pydantic, whose `ValidationError` (a `ValueError`) names the bad field."""
    model = _ACTION_MODELS[tool]
    fields = {
        name: value
        for name, value in args.items()
        if name in model.model_fields and name != "type" and value is not None
    }
    return _ACTION.validate_python({**fields, "type": tool})


def invalid_arguments(tool: str, error: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'][1:] if p != tool) or '(arguments)'}: {e['msg']}"
        for e in error.errors()[:5]
    )
    return f"rejected: invalid arguments for {tool}: {problems}. Fix them and call it again."


def _action_tool(name: str, ctx: RunContext) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{ACTION_DESCRIPTIONS[name]} {ACTION_CHECKED}",
        parameters=_action_parameters(_ACTION_MODELS[name], _enums(ctx)),
    )


def _enums(ctx: RunContext) -> dict[str, list[str]]:
    """Values a small model should pick from. The rules still check every value: an enum
    here is help, not the guard (a provider may not enforce it)."""
    contacts = [c.id for c in ctx.load.contacts]
    kinds = list(dict.fromkeys([o.kind for o in ctx.broker.obligations]))
    return {
        "contact_id": contacts,
        "to_contact_ids": contacts,
        "kind": kinds,
        "requests": kinds,
    }


def _action_parameters(model: type[BaseModel], enums: Mapping[str, list[str]]) -> dict[str, Any]:
    """The action's JSON schema, flat and portable: no `$ref`, no title, no default, a
    nullable field as its plain type, and no `type` (the tool's name says it)."""
    schema = model.model_json_schema()
    defs = cast("dict[str, Any]", schema.get("$defs", {}))
    properties: dict[str, Any] = {}
    for name, prop in cast("dict[str, Any]", schema["properties"]).items():
        if name == "type":
            continue
        clean = _clean(prop, defs)
        values = enums.get(name)
        if values:
            target = cast("dict[str, Any]", clean["items"]) if "items" in clean else clean
            target["enum"] = values
        if name in FIELD_DESCRIPTIONS:
            clean["description"] = FIELD_DESCRIPTIONS[name]
        properties[name] = clean
    required = [r for r in cast("list[str]", schema.get("required", [])) if r != "type"]
    return _object(properties, required)


def _clean(node: Any, defs: Mapping[str, Any]) -> dict[str, Any]:
    prop = dict(cast("dict[str, Any]", node))
    ref = prop.pop("$ref", None)
    if isinstance(ref, str):
        prop = {**cast("dict[str, Any]", defs[ref.rsplit("/", 1)[-1]]), **prop}
    variants = prop.pop("anyOf", None)
    if isinstance(variants, list):
        typed = [v for v in cast("list[Any]", variants) if v != {"type": "null"}]
        prop = {**_clean(typed[0], defs), **prop}
    if "items" in prop:
        prop["items"] = _clean(prop["items"], defs)
    for key in ("title", "default"):
        prop.pop(key, None)
    return prop


# --- helpers ------------------------------------------------------------------------------


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _id_property(description: str, valid: Mapping[str, Any]) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "string", "description": description}
    if valid:
        prop["enum"] = list(valid)
    return prop


def _text_arg(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _unknown(name: str, args: Mapping[str, Any], valid: Mapping[str, Any]) -> str:
    given = args.get(name)
    problem = f"Unknown {name} {given!r}." if name in args else f"Missing {name}."
    return f"{problem} Valid {name} values: {', '.join(_json(v) for v in valid)}."


def _json(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)
