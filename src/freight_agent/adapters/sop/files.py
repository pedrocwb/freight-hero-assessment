"""SOP sections read from Markdown files on disk.

The folder a file sits in gives its layer, client and load type ("SOP files" in
docs/decisions.md); the YAML front matter gives the rest. A file that cannot become one
unambiguous section fails loudly with `SopFormatError`: the agent must never act on a
procedure it misread. `lint` reports the same problems, plus the rules that span files.

Messages are written for the people who edit SOPs, not for engineers: each one says
what is wrong and how to fix it. They start in lowercase because they are shown after
the file name ("standard/escalation.md: the header has no `id`...").
"""

import difflib
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import MergeMode, SopLayer, SopSection

SETTINGS = ("id", "title", "description", "intents", "stages", "mode")
LAYOUT = (
    "standard/<topic>.md, clients/<client_id>/<topic>.md or "
    "clients/<client_id>/types/<load_type>/<topic>.md"
)
_FENCE = "---"
_SNAKE_CASE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_VERSION_LENGTH = 12

Scope = tuple[SopLayer, str | None, str | None]


class SopFormatError(Exception):
    """A SOP file could not be loaded. Names the file so an editor can find it."""

    def __init__(self, source: str, problem: str) -> None:
        super().__init__(f"{source}: {problem}")
        self.source = source
        self.problem = problem


@dataclass(frozen=True)
class SopLocation:
    """Where a file sits in the layout, which decides its layer, client and load type."""

    source: str  # relative to the SOP root, POSIX form
    layer: SopLayer
    client_id: str | None = None
    load_type: str | None = None

    @property
    def scope(self) -> Scope:
        """One folder of the layout. A section id may appear only once per scope."""
        return (self.layer, self.client_id, self.load_type)


@dataclass(frozen=True)
class SopFile:
    """One file, read and checked on its own.

    `section` is set only when the file has no problems. `section_id` and `mode` are kept
    even when other settings are wrong, so the rules that span files can still run.
    """

    location: SopLocation
    section_id: str | None
    mode: MergeMode | None  # as written, before standard/ ignores it
    section: SopSection | None
    problems: tuple[str, ...]


class _Unreadable(Exception):
    """The header cannot be read at all, so no setting can be checked."""


def sop_paths(root: Path) -> list[tuple[str, Path]]:
    """Every Markdown file of the tree as (relative POSIX path, path), sorted.

    README files are documentation for the editors, not sections.
    """
    if not root.is_dir():
        raise SopFormatError(str(root), "the SOP folder does not exist. Check the path.")
    found = (p for p in root.rglob("*.md") if p.is_file() and p.name.lower() != "readme.md")
    return sorted((p.relative_to(root).as_posix(), p) for p in found)


def locate(source: str) -> SopLocation | None:
    """The layer, client and load type a path stands for, or None outside the layout."""
    match PurePosixPath(source).parts:
        case ("standard", _):
            return SopLocation(source, SopLayer.STANDARD)
        case ("clients", client_id, _):
            return SopLocation(source, SopLayer.CLIENT, client_id)
        case ("clients", client_id, "types", load_type, _):
            return SopLocation(source, SopLayer.LOAD_TYPE, client_id, load_type)
        case _:
            return None


def outside_layout_problem() -> str:
    return (
        f"this file is not in a folder the agent reads, so it is never used. Move it to {LAYOUT}."
    )


def duplicate_problem(section_id: str, first: SopFile) -> str:
    return (
        f"the id '{section_id}' is already used by {first.location.source}. An id can appear "
        "only once in the same folder: rename one of the sections or merge the two files."
    )


def duplicates(files: Iterable[SopFile]) -> list[tuple[str, SopFile, SopFile]]:
    """(id, repeat, first) for every file that reuses an id already used in its folder."""
    first: dict[tuple[Scope, str], SopFile] = {}
    found: list[tuple[str, SopFile, SopFile]] = []
    for file in files:
        if file.section_id is None:
            continue
        key = (file.location.scope, file.section_id)
        if key in first:
            found.append((file.section_id, file, first[key]))
        else:
            first[key] = file
    return found


def read_sop_file(path: Path, location: SopLocation) -> SopFile:
    """Read one file and check every setting, collecting all problems instead of stopping."""
    try:
        meta, body = _split_front_matter(_read_text(path))
    except _Unreadable as exc:
        return SopFile(location, None, None, None, (str(exc),))
    problems: list[str] = _unknown_settings(meta)
    section_id = _section_id(meta, problems)
    title = _text(meta, "title", problems)
    description = _text(meta, "description", problems)
    intents = _members(meta, "intents", Intent, problems)
    stages = _members(meta, "stages", Stage, problems)
    mode = _mode(meta, problems)
    if not body:
        problems.append("the section has no text. Write the procedure below the closing --- line.")
    if problems or section_id is None or mode is None:
        return SopFile(location, section_id, mode, None, tuple(problems))
    section = SopSection(
        section_id=section_id,
        layer=location.layer,
        client_id=location.client_id,
        load_type=location.load_type,
        title=title or section_id,
        description=description or "",
        intents=intents,
        stages=stages,
        # decisions.md: mode is ignored in standard/, there is nothing below it to extend.
        mode=MergeMode.REPLACE if location.layer is SopLayer.STANDARD else mode,
        body=body,
        source=location.source,
    )
    return SopFile(location, section_id, mode, section, ())


class FileSopRepository:
    """`SopRepository` over a folder of Markdown files.

    Reads the disk on every call, so an edited SOP applies to the next run, and the
    version recorded with a run matches what that run read. Only the files a client can
    see are read: a broken file of one client never blocks the others.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def sections_for_client(self, client_id: str) -> list[SopSection]:
        files = [
            self._load(source, path)
            for source, path in sop_paths(self._root)
            if _visible_to(source, client_id)
        ]
        for section_id, repeat, first in duplicates(files):
            raise SopFormatError(repeat.location.source, duplicate_problem(section_id, first))
        return [file.section for file in files if file.section is not None]

    def version(self) -> str:
        """Short sha256 of the sorted (relative path, bytes) pairs: any edit or move changes it."""
        digest = hashlib.sha256()
        for source, path in sop_paths(self._root):
            data = path.read_bytes()
            # The length keeps the pairs unambiguous: no two trees hash the same bytes.
            digest.update(source.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
        return digest.hexdigest()[:_VERSION_LENGTH]

    def _load(self, source: str, path: Path) -> SopFile:
        location = locate(source)
        if location is None:
            raise SopFormatError(source, outside_layout_problem())
        file = read_sop_file(path, location)
        if file.section is None:
            raise SopFormatError(source, "; ".join(file.problems))
        return file


def _visible_to(source: str, client_id: str) -> bool:
    parts = PurePosixPath(source).parts
    return parts[0] == "standard" or (len(parts) > 2 and parts[:2] == ("clients", client_id))


def _read_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError:
        raise _Unreadable(
            "the file is not saved as UTF-8 text. Save it again as UTF-8 in your editor."
        ) from None


def _split_front_matter(text: str) -> tuple[dict[Any, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        raise _Unreadable(
            "the file has no header. Start it with a line that is only ---, then the settings "
            "(id, title, description, ...), then another line that is only ---, then the text."
        )
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == _FENCE), None)
    if end is None:
        raise _Unreadable(
            "the header is not closed. Add a line that is only --- after the last setting."
        )
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise _Unreadable(
            f"the header could not be read{_yaml_where(exc)}. Write each setting on its own "
            "line as `name: value`, and lists in brackets, like [to_pickup, to_delivery]."
        ) from None
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise _Unreadable(
            "the header must be a list of settings, one per line, written as `name: value`."
        )
    return meta, "\n".join(lines[end + 1 :]).strip()


def _yaml_where(exc: yaml.YAMLError) -> str:
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
        # +2: the mark counts from 0 inside the header, which starts after the first ---.
        return f" near line {exc.problem_mark.line + 2} ({exc.problem})"
    return ""


def _unknown_settings(meta: dict[Any, Any]) -> list[str]:
    problems: list[str] = []
    for key in meta:
        if key in SETTINGS:
            continue
        close = difflib.get_close_matches(str(key), SETTINGS, n=1)
        hint = f" (did you mean `{close[0]}`?)" if close else ""
        problems.append(
            f"the setting `{key}` is not known{hint}. "
            f"Use only these settings: {', '.join(SETTINGS)}."
        )
    return problems


def _section_id(meta: dict[Any, Any], problems: list[str]) -> str | None:
    value = meta.get("id")
    if value is None or value == "":
        problems.append(
            "the header has no `id`. Add a line like `id: delay_risk_high`; the id names the "
            "section and must be the same in every layer that changes it."
        )
        return None
    if not isinstance(value, str) or not _SNAKE_CASE.fullmatch(value):
        problems.append(
            f"the id '{value}' is not valid. Use only lowercase letters, digits and "
            "underscores, starting with a letter, for example delay_risk_high."
        )
        return None
    return value


def _text(meta: dict[Any, Any], key: str, problems: list[str]) -> str | None:
    value = meta.get(key)
    if value is None or isinstance(value, str):
        return value.strip() if value else None
    problems.append(
        f"`{key}` must be text. Put it in quotes if it looks like a number, a date or yes/no."
    )
    return None


def _members[E: StrEnum](
    meta: dict[Any, Any], key: str, kind: type[E], problems: list[str]
) -> tuple[E, ...]:
    """A list of enum values. A single value without brackets is accepted as a list of one."""
    noun = key.removesuffix("s")
    raw = meta.get(key)
    if raw is None:
        return ()
    items: object = [raw] if isinstance(raw, str) else raw
    known = {member.value: member for member in kind}
    if not isinstance(items, list):
        problems.append(
            f"`{key}` must be a list, like [{', '.join(list(known)[:2])}]. "
            f"Leave it out to apply the section to every {noun}."
        )
        return ()
    found: list[E] = []
    for item in items:
        member = known.get(item) if isinstance(item, str) else None
        if member is None:
            problems.append(
                f"'{item}' in `{key}` is not a known {noun}. Use one of: {', '.join(known)}; "
                f"or leave `{key}` out to apply the section to every {noun}."
            )
        else:
            found.append(member)
    return tuple(dict.fromkeys(found))


def _mode(meta: dict[Any, Any], problems: list[str]) -> MergeMode | None:
    raw = meta.get("mode")
    if raw is None:
        return MergeMode.REPLACE
    if isinstance(raw, str) and raw in {mode.value for mode in MergeMode}:
        return MergeMode(raw)
    problems.append(
        f"`mode: {raw}` is not valid. Use `replace` to replace the section of the less "
        "specific layer, or `extend` to add this text after it."
    )
    return None
