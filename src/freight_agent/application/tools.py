"""Read-only tools for the decide model.

Tools never act; actions are proposed in the plan. They let the model read an
SOP section it was not shown in full, or an attachment's details. A bad
argument is an answer, not an exception: the tool returns the valid ids so a
small model can recover in its next tool round instead of failing the run.
"""

from collections.abc import Sequence
from typing import Any

from freight_agent.application.context import RunContext
from freight_agent.domain.events import Attachment
from freight_agent.domain.sop import ResolvedSection
from freight_agent.ports.llm import ToolSpec

READ_SOP_SECTION = "read_sop_section"
READ_DOCUMENT = "read_document"


def decide_tools(
    ctx: RunContext, visible_sections: Sequence[ResolvedSection]
) -> tuple[ToolSpec, ...]:
    """`read_document` is offered only when there is an attachment to read, so a small
    model is not tempted to call a tool that cannot help."""
    tools = [read_sop_section_tool(visible_sections)]
    attachments = attachments_in_view(ctx)
    if attachments:
        tools.append(read_document_tool(attachments))
    return tuple(tools)


def attachments_in_view(ctx: RunContext) -> tuple[Attachment, ...]:
    """The event's attachments first, then those of the recent events, each once."""
    seen: dict[str, Attachment] = {}
    for event in (ctx.event, *ctx.recent_events):
        for attachment in event.attachments:
            seen.setdefault(attachment.attachment_id, attachment)
    return tuple(seen.values())


def read_sop_section_tool(sections: Sequence[ResolvedSection]) -> ToolSpec:
    by_id = {s.section_id: s for s in sections}

    async def run(args: dict[str, Any]) -> str:
        if not by_id:
            return "No SOP sections are available for this load."
        section = by_id.get(_text_arg(args, "section_id"))
        if section is None:
            return _unknown("section_id", args, by_id)
        return f"# {section.section_id}: {section.title}\n\n{section.body.strip()}"

    return ToolSpec(
        name=READ_SOP_SECTION,
        description=(
            "Read the full text of one SOP section from the catalog of other SOP sections. "
            "Use it only when this event needs a section that is not shown in full."
        ),
        parameters=_id_schema(
            "section_id", "The id of the section, as listed in the catalog.", by_id
        ),
        run=run,
    )


def read_document_tool(attachments: Sequence[Attachment]) -> ToolSpec:
    by_id = {a.attachment_id: a for a in attachments}

    async def run(args: dict[str, Any]) -> str:
        if not by_id:
            return "No attachments are available on this load."
        attachment = by_id.get(_text_arg(args, "attachment_id"))
        if attachment is None:
            return _unknown("attachment_id", args, by_id)
        return (
            f"attachment {attachment.attachment_id}\n"
            f"filename: {attachment.filename}\n"
            f"content type: {attachment.content_type}\n"
            f"reference: {attachment.ref}\n"
            "Content extraction is not available, so the type and the values of this document "
            "are unknown. Do not guess them: create a Hero task to review the document, with "
            "the attachment id and the filename."
        )

    return ToolSpec(
        name=READ_DOCUMENT,
        description=(
            "Read the details of an attachment (BOL, POD, rate confirmation, receipt) "
            "by its attachment id."
        ),
        parameters=_id_schema("attachment_id", "The id of the attachment.", by_id),
        run=run,
    )


def _text_arg(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _unknown(name: str, args: dict[str, Any], valid: dict[str, Any]) -> str:
    given = args.get(name)
    problem = f"Unknown {name} {given!r}." if name in args else f"Missing {name}."
    return f"{problem} Valid {name} values: {', '.join(valid)}."


def _id_schema(name: str, description: str, valid: dict[str, Any]) -> dict[str, Any]:
    prop: dict[str, Any] = {"type": "string", "description": description}
    if valid:
        prop["enum"] = list(valid)
    return {
        "type": "object",
        "properties": {name: prop},
        "required": [name],
        "additionalProperties": False,
    }
