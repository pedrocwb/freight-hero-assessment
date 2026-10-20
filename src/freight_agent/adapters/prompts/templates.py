"""Prompt templates as LangChain `ChatPromptTemplate`s, and their copy in the repository.

The Hub stores each prompt as a `ChatPromptTemplate` in mustache, a system and a human
message. The repository keeps the same template as YAML, `prompts/<role>.yaml`, one block
scalar per message, so a reviewer reads a prompt in git and a diff shows a changed word.
Both registries (`local.py`, `hub.py`) and the `agent prompts` commands build a template
through this module, so it is checked the same way wherever it comes from:

- mustache, two messages: system, then human;
- every variable written `{{{name}}}`, unescaped. `{{name}}` would HTML-escape its value
  (`"` becomes `&quot;`, and the message text in a prompt is quoted JSON), so it is
  refused, and so are sections and partials, which code never fills;
- the tool descriptions a template sets are its metadata (`tools`), which the Hub keeps
  with each commit.

A template's content is its two messages and its tool descriptions; its hash
(`PromptFile.content_hash`) is the repository copy's version and what `agent prompts
status` compares with a Hub commit. The name and the description are the Hub prompt's,
not a commit's, so they are not part of it.

Rendering is LangChain's (`PromptTemplate.format`), the same as in the Hub's playground.
"""

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    PromptTemplate,
    SystemMessagePromptTemplate,
)
from langchain_core.utils import mustache
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from freight_agent.ports.prompts import ChatPrompt, PromptError, PromptRole

HASH_LENGTH = 12
TOOLS_KEY = "tools"  # the template metadata key of its tool descriptions
_MUSTACHE = "mustache"
_ALLOWED_TAGS = ("literal", "no escape")


class PromptFile(BaseModel):
    """A template with its Hub prompt's name and description: `prompts/<role>.yaml`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, description="The Hub prompt, freight-agent-<role>")
    description: str = ""
    system: str
    user: str
    tools: dict[str, str] = {}

    def content(self) -> dict[str, Any]:
        return {"system": self.system, "user": self.user, "tools": dict(sorted(self.tools.items()))}

    def content_hash(self) -> str:
        text = json.dumps(self.content(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]

    def same_content(self, other: "PromptFile") -> bool:
        return self.content() == other.content()


# --- LangChain -------------------------------------------------------------------------


def to_langchain(file: PromptFile) -> ChatPromptTemplate:
    """The template the Hub stores. Its tool descriptions go in its metadata."""
    _check_tags(file.system, f"{file.name} (system)")
    _check_tags(file.user, f"{file.name} (user)")
    template = ChatPromptTemplate.from_messages(
        [("system", file.system), ("human", file.user)], template_format=_MUSTACHE
    )
    if file.tools:
        template.metadata = {TOOLS_KEY: dict(file.tools)}
    return template


def from_langchain(template: object, *, name: str, description: str = "") -> PromptFile:
    """The repository form of a template pulled from the Hub. Raises `PromptError` for
    anything but a mustache chat template of a system and a human message."""
    if not isinstance(template, ChatPromptTemplate):
        raise PromptError(f"{name} is a {type(template).__name__}, not a chat prompt template")
    messages = template.messages
    kinds = [type(m).__name__ for m in messages]
    expected = [SystemMessagePromptTemplate.__name__, HumanMessagePromptTemplate.__name__]
    if kinds != expected:
        raise PromptError(
            f"{name} must have two messages, system then human (user), not {', '.join(kinds)}"
        )
    system, user = (_text(m, name) for m in messages)
    tools = (template.metadata or {}).get(TOOLS_KEY) or {}
    if not isinstance(tools, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str)
        for k, v in cast(Mapping[object, object], tools).items()
    ):
        raise PromptError(f"{name}: the metadata {TOOLS_KEY!r} must map tool names to text")
    return PromptFile(
        name=name,
        description=description,
        system=system,
        user=user,
        tools=dict(cast(Mapping[str, str], tools)),
    )


def _text(message: object, name: str) -> str:
    prompt = getattr(message, "prompt", None)
    if not isinstance(prompt, PromptTemplate) or prompt.template_format != _MUSTACHE:
        raise PromptError(f"{name}: each message must be a mustache template")
    return prompt.template


def _check_tags(template: str, where: str) -> None:
    """Only text and unescaped variables, `{{{name}}}`."""
    try:
        tags = list(mustache.tokenize(template))
    except mustache.ChevronError as exc:
        raise PromptError(f"{where} is not valid mustache: {exc}") from None
    for kind, key in tags:
        if kind == "variable":
            raise PromptError(
                f"{where}: write {{{{{{{key}}}}}}} for the variable {key}: {{{{{key}}}}} "
                "HTML-escapes its value"
            )
        if kind not in _ALLOWED_TAGS:
            raise PromptError(
                f"{where}: the mustache {kind} {key!r} is not supported; code fills only "
                "variables, written {{{name}}}"
            )


class _Message:
    """Implements `ports.prompts.MessageTemplate` with the message's LangChain template."""

    def __init__(self, template: PromptTemplate, where: str) -> None:
        self._template = template
        self._where = where
        self.variables = frozenset(template.input_variables)

    def format(self, variables: Mapping[str, str]) -> str:
        missing = sorted(self.variables - variables.keys())
        if missing:
            raise PromptError(f"{self._where} names {', '.join(missing)}, which it was not given")
        return self._template.format(**{name: variables[name] for name in self.variables})


def chat_prompt(file: PromptFile, *, role: PromptRole, ref: str, version: str) -> ChatPrompt:
    """The port's view of a checked template, rendered by LangChain."""
    template = to_langchain(file)
    system, user = (cast(SystemMessagePromptTemplate, m).prompt for m in template.messages)
    return ChatPrompt(
        role=role,
        name=file.name,
        ref=ref,
        version=version,
        system=_Message(cast(PromptTemplate, system), f"{ref} (system)"),
        user=_Message(cast(PromptTemplate, user), f"{ref} (user)"),
        tools=dict(file.tools),
    )


# --- the repository copy ---------------------------------------------------------------

_HEADER = (
    "# The {role} prompt: the repository copy of the LangSmith Hub prompt {name}.\n"
    "# `agent prompts push` publishes it, `agent prompts pull` writes the Hub's version\n"
    "# back, and config/prompts.yaml pins the version the runtime runs. Code computes\n"
    "# each {{{{{{variable}}}}}} (application/prompts.py, evalkit/judge.py).\n"
)


class _BlockDumper(yaml.SafeDumper):
    """Multi-line text as a literal block, so a prompt reads as written."""


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_BlockDumper.add_representer(str, _represent_str)


def dump_prompt_file(file: PromptFile, role: PromptRole) -> str:
    data: dict[str, Any] = {
        "name": file.name,
        "description": file.description,
        "system": file.system,
        "user": file.user,
    }
    if file.tools:
        data["tools"] = dict(file.tools)
    body = yaml.dump(
        data, Dumper=_BlockDumper, sort_keys=False, allow_unicode=True, width=1_000_000
    )
    return _HEADER.format(role=role.value, name=file.name) + body


def write_prompt_file(path: Path, file: PromptFile, role: PromptRole) -> bool:
    """Write the file; False when it already held this text."""
    text = dump_prompt_file(file, role)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def read_prompt_file(path: Path) -> PromptFile:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PromptError(f"the prompt file {path} does not exist (PROMPTS_DIR)") from None
    except (OSError, yaml.YAMLError) as exc:
        raise PromptError(f"the prompt file {path} cannot be read: {exc}") from None
    try:
        return PromptFile.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in exc.errors()
        )
        raise PromptError(f"the prompt file {path} is not valid: {problems}") from None
