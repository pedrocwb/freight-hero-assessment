"""Suites: find a suite's scenarios and pick some of them.

A suite is a folder, `evals/scenarios/<suite>/*.yaml`, and a LangSmith dataset
(`freight-agent-<suite>`, synced from the folder by `evalkit.experiment`).

Scenarios tagged `capability` are measured, not enforced: they start low and graduate to
`regression` once they pass reliably. Every other scenario gates the run: `agent eval
run` exits 1 when one of them fails pass^k.
"""

from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml
from pydantic import ValidationError

from freight_agent.evalkit.spec import Scenario, load_scenario

REGRESSION = "regression"
CAPABILITY = "capability"


class SuiteError(Exception):
    """The suite cannot run as asked: no such suite, a bad scenario file, an empty selection."""


def gating(scenario: Scenario) -> bool:
    """Whether a failure fails the run: every scenario but a `capability` one."""
    return CAPABILITY not in scenario.tags


# --- discovery and selection ----------------------------------------------------------


def suites(evals_dir: Path) -> list[str]:
    """The suite folders under `evals/scenarios`, by name."""
    root = evals_dir / "scenarios"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and any(p.glob("*.yaml")))


def discover(evals_dir: Path, suite: str) -> list[Scenario]:
    """Every scenario of the suite, in file-name order.

    All bad files are reported in one error, so a broken suite is fixed in one pass. A
    scenario must name the suite of its folder, and ids are unique within a suite.
    """
    folder = evals_dir / "scenarios" / suite
    paths = sorted(folder.glob("*.yaml")) if folder.is_dir() else []
    if not paths:
        known = ", ".join(suites(evals_dir)) or "none"
        raise SuiteError(f"no suite {suite!r} in {evals_dir / 'scenarios'} (suites: {known})")
    scenarios: list[Scenario] = []
    problems: list[str] = []
    first_file: dict[str, str] = {}
    for path in paths:
        try:
            scenario = load_scenario(path)
        except (OSError, yaml.YAMLError, ValidationError) as exc:
            problems.append(f"{path.name}: {_describe_error(exc)}")
            continue
        if scenario.suite != suite:
            problems.append(
                f"{path.name}: suite is {scenario.suite!r}, but the file is in {suite}/"
            )
        elif scenario.id in first_file:
            problems.append(
                f"{path.name}: the id {scenario.id!r} is also used by {first_file[scenario.id]}"
            )
        else:
            first_file[scenario.id] = path.name
            scenarios.append(scenario)
    if problems:
        raise SuiteError(f"bad scenario files in {folder}:\n  " + "\n  ".join(problems))
    return scenarios


def select(
    scenarios: Sequence[Scenario], *, tags: Iterable[str] = (), ids: Iterable[str] = ()
) -> list[Scenario]:
    """The scenarios that have any of `tags` and are among `ids`; an empty filter keeps all."""
    wanted_tags, wanted_ids = set(tags), set(ids)
    unknown = sorted(wanted_ids - {s.id for s in scenarios})
    if unknown:
        raise SuiteError(
            f"no scenario {', '.join(repr(i) for i in unknown)} in this suite "
            "(`agent eval list` shows the ids)"
        )
    chosen = [
        s
        for s in scenarios
        if (not wanted_tags or wanted_tags & set(s.tags)) and (not wanted_ids or s.id in wanted_ids)
    ]
    if not chosen:
        raise SuiteError("no scenario matches the filters")
    return chosen


def _describe_error(exc: Exception) -> str:
    """One line per file: pydantic's own message spans many lines and links to its docs."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'file'}: {error['msg']}"
            for error in exc.errors()
        )
    return " ".join(str(exc).split())


def find(
    evals_dir: Path,
    *,
    ids: Iterable[str] = (),
    suite: str | None = None,
    tags: Iterable[str] = (),
) -> list[Scenario]:
    """The scenarios `agent play` asks for: by id (searched in every suite unless `suite`
    names one), and or by suite and tag. Nothing asked for, an unknown or ambiguous id, or
    nothing that matches is a `SuiteError`."""
    wanted_ids, wanted_tags = list(dict.fromkeys(ids)), set(tags)
    if not wanted_ids and suite is None and not wanted_tags:
        raise SuiteError(
            "say which scenarios to play: their ids, --suite or --tag "
            "(`agent eval list` shows them)"
        )
    names = [suite] if suite is not None else suites(evals_dir)
    found = [s for name in names for s in discover(evals_dir, name)]
    by_id: dict[str, list[Scenario]] = {}
    for scenario in found:
        by_id.setdefault(scenario.id, []).append(scenario)
    where = f"suite {suite!r}" if suite is not None else "any suite"
    if unknown := [i for i in wanted_ids if i not in by_id]:
        raise SuiteError(
            f"no scenario {', '.join(repr(i) for i in unknown)} in {where} "
            "(`agent eval list` shows the ids)"
        )
    if twice := [i for i in wanted_ids if len(by_id[i]) > 1]:
        raise SuiteError(
            f"the id {', '.join(repr(i) for i in twice)} is in more than one suite: add --suite"
        )
    chosen = [by_id[i][0] for i in wanted_ids] if wanted_ids else found
    chosen = [s for s in chosen if not wanted_tags or wanted_tags & set(s.tags)]
    if not chosen:
        tags_text = ", ".join(sorted(wanted_tags))
        raise SuiteError(f"no scenario in {where} has the tag {tags_text}")
    return chosen
