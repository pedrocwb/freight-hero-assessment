"""The `extract` prompt's variables: one document to classify among the broker's types
and read.

Owner: card W3-DOC.

The answer schema is generic (`domain.documents.Extraction`: a type id, a confidence and
`{name, value}` pairs), so it is the same for every broker. The extract template
(`interfaces.prompts`, `prompts/extract.yaml`) holds the job and the format of each field
kind; code renders into it the broker's `DocumentTypeDef`s, with their fields, and the
document: its pages' text between markers and, for pages whose text is too thin (a scan,
a photo), which pages come as images (`LlmRequest.images`).

The prompt carries no value of the load (no PO, no stop names): the checks compare the
document with the load in code, and a model told the load's PO could "read" it into a
document that has another one.
"""

import json
import re
from collections.abc import Sequence

from freight_agent.application.prompts import check_variables
from freight_agent.domain.broker import DocumentTypeDef, FieldDef
from freight_agent.domain.documents import Page
from freight_agent.interfaces.prompts import ChatPrompt

# The document's text the model sees at most, all pages together (the stored text is
# whole); a longer document is cut and the cut is said.
MAX_PROMPT_TEXT_CHARS = 24_000

EXTRACT_SYSTEM_VARIABLES = frozenset({"types"})
EXTRACT_USER_VARIABLES = frozenset({"filename", "content_type", "page_count", "images", "pages"})


def check_extract_prompt(prompt: ChatPrompt) -> ChatPrompt:
    """`prompt`, or `PromptError` when it names a variable the pipeline does not compute."""
    return check_variables(prompt, system=EXTRACT_SYSTEM_VARIABLES, user=EXTRACT_USER_VARIABLES)


def extract_system(prompt: ChatPrompt, types: Sequence[DocumentTypeDef]) -> str:
    """The system prompt for a broker's document types."""
    return prompt.system.format({"types": "\n".join(_type(t) for t in types)})


def _type(type_def: DocumentTypeDef) -> str:
    lines = [f"- `{type_def.type_id}` ({type_def.title}): {type_def.description}", "  Fields:"]
    lines.extend(f"  - {_field(f)}" for f in type_def.fields)
    return "\n".join(lines)


def _field(field: FieldDef) -> str:
    return f"`{field.name}` ({field.kind}): {field.description}"


def extract_user(
    prompt: ChatPrompt,
    *,
    filename: str,
    content_type: str,
    pages: Sequence[Page],
    image_pages: Sequence[int],
) -> str:
    """The document: its pages' text in order, and which pages come as images.

    `image_pages` are the numbers of the pages whose image goes with the request, in the
    order of the images. The filename is the sender's choice, so it is quoted like data.
    """
    images = ""
    if image_pages:
        numbers = ", ".join(str(n) for n in image_pages)
        images = (
            f"\nThe images attached are pages {numbers}, in that order: read those pages "
            "from their images."
        )
    return prompt.user.format(
        {
            "filename": json.dumps(filename, ensure_ascii=False),
            "content_type": json.dumps(content_type, ensure_ascii=False),
            "page_count": str(len(pages)),
            "images": images,
            "pages": _pages_text(pages, frozenset(image_pages)),
        }
    )


def _pages_text(pages: Sequence[Page], imaged: frozenset[int]) -> str:
    parts: list[str] = []
    budget = MAX_PROMPT_TEXT_CHARS
    for page in pages:
        text = _unmarked(page.text.strip())
        note = " (see its image)" if page.number in imaged else ""
        if not text:
            parts.append(f"--- page {page.number}: no text{note} ---")
            continue
        parts.append(f"--- page {page.number}{note} ---")
        if len(text) > budget:
            parts.append(text[:budget])
            parts.append("[the rest of the document's text is cut]")
            break
        parts.append(text)
        budget -= len(text)
    return "\n".join(parts)


def _unmarked(text: str) -> str:
    """The text without the markers, so a document cannot end its own block."""
    return _MARKER.sub("[document]", text)


_MARKER = re.compile(r"<\s*/?\s*document\s*>", re.IGNORECASE)
