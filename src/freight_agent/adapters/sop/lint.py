"""Checks SOP editors run before a change reaches the agent.

Errors are what `FileSopRepository` would refuse to load (a section or a client's
catalog), plus the rules that span files (an `extend` must have something to extend). The
coverage warning keeps every section tied to at least one eval scenario, so a change to
its text is tested.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from freight_agent.adapters.sop.files import (
    CATALOG_FILE,
    STANDARD_INTENTS,
    SopFile,
    SopFormatError,
    catalog_paths,
    duplicate_problem,
    duplicates,
    intent_ids,
    is_catalog,
    locate,
    outside_layout_problem,
    read_catalog,
    read_sop_file,
    sop_paths,
)
from freight_agent.domain.sop import MergeMode, SopLayer

_COVERAGE_TAG = "sop:"
_LAYER_ORDER = tuple(SopLayer)  # least to most specific


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class LintIssue:
    severity: Severity
    file: str  # relative to the SOP root, POSIX form
    message: str

    def __str__(self) -> str:
        return f"{self.severity}: {self.file}: {self.message}"


def lint(root: Path, scenario_tags: Iterable[str] | None = None) -> list[LintIssue]:
    """Every problem in the tree, sorted by file. Coverage is checked only when tags are given."""
    try:
        paths = sop_paths(root)
    except SopFormatError as exc:
        return [_error(exc.source, exc.problem)]
    issues: list[LintIssue] = []
    catalogs = _catalogs(root, issues)
    files: list[SopFile] = []
    for source, path in paths:
        location = locate(source)
        if location is None:
            issues.append(_error(source, outside_layout_problem()))
            continue
        intents = STANDARD_INTENTS
        if location.client_id is not None:
            intents = catalogs.get(location.client_id, STANDARD_INTENTS)
        file = read_sop_file(path, location, intents=intents)
        files.append(file)
        issues.extend(_error(source, problem) for problem in file.problems)
    issues.extend(duplicate_issues(files))
    issues.extend(extend_issues(files))
    if scenario_tags is not None:
        issues.extend(_coverage_warnings(files, scenario_tags))
    return sorted(issues, key=lambda issue: issue.file)  # stable: a file keeps its order


def _error(file: str, message: str) -> LintIssue:
    return LintIssue(Severity.ERROR, file, message)


def _catalogs(root: Path, issues: list[LintIssue]) -> dict[str, tuple[str, ...]]:
    """The intents of each client's catalog, by client id. A catalog that cannot be read
    is an error, and its client's files are checked against the standard intents."""
    found: dict[str, tuple[str, ...]] = {}
    for source, _ in catalog_paths(root):
        if not is_catalog(source):
            issues.append(
                _error(
                    source,
                    "this catalog is not in a folder the agent reads, so it is never used. "
                    f"Move it to clients/<client_id>/{CATALOG_FILE}.",
                )
            )
            continue
        client_id = source.split("/")[1]
        try:
            found[client_id] = intent_ids(read_catalog(root, client_id))
        except SopFormatError as exc:
            issues.append(_error(exc.source, exc.problem))
    return found


def duplicate_issues(files: Sequence[SopFile]) -> list[LintIssue]:
    return [
        _error(repeat.location.source, duplicate_problem(section_id, first))
        for section_id, repeat, first in duplicates(files)
    ]


def extend_issues(files: Sequence[SopFile]) -> list[LintIssue]:
    """`extend` adds text after a less specific section of the same client, so one must exist."""
    ids_in: dict[tuple[SopLayer, str | None], set[str]] = {}
    for file in files:
        if file.section_id is not None:
            key = (file.location.layer, file.location.client_id)
            ids_in.setdefault(key, set()).add(file.section_id)
    issues: list[LintIssue] = []
    for file in files:
        if file.mode is not MergeMode.EXTEND or file.section_id is None:
            continue
        location = file.location
        if location.layer is SopLayer.STANDARD:
            issues.append(
                _error(
                    location.source,
                    "`mode: extend` cannot be used in standard/: a standard section has "
                    "nothing below it to extend. Remove the `mode` line.",
                )
            )
            continue
        bases: list[tuple[SopLayer, str | None]] = [(SopLayer.STANDARD, None)]
        if location.layer is SopLayer.LOAD_TYPE:
            bases.append((SopLayer.CLIENT, location.client_id))
        if any(file.section_id in ids_in.get(base, set()) for base in bases):
            continue
        searched = " and ".join(_folder(layer, client_id) for layer, client_id in bases)
        issues.append(
            _error(
                location.source,
                f"this section extends '{file.section_id}', but no section with that id exists "
                f"in {searched}. Fix the id so it matches the section to add to, or change "
                "`mode` to `replace`.",
            )
        )
    return issues


def _folder(layer: SopLayer, client_id: str | None) -> str:
    return "standard/" if layer is SopLayer.STANDARD else f"clients/{client_id}/"


def _coverage_warnings(files: Sequence[SopFile], scenario_tags: Iterable[str]) -> list[LintIssue]:
    """One warning per uncovered id, on its least specific file (where an editor looks first)."""
    covered = {tag.removeprefix(_COVERAGE_TAG) for tag in scenario_tags if tag.startswith("sop:")}
    first_file: dict[str, str] = {}
    ordered = sorted(files, key=lambda f: (_LAYER_ORDER.index(f.location.layer), f.location.source))
    for file in ordered:
        if file.section_id is not None:
            first_file.setdefault(file.section_id, file.location.source)
    return [
        LintIssue(
            Severity.WARNING,
            source,
            f"no eval scenario covers section {section_id}. Add the tag sop:{section_id} to an "
            "eval scenario that exercises this section, or write a new one, so that changes "
            "to it are tested.",
        )
        for section_id, source in first_file.items()
        if section_id not in covered
    ]
