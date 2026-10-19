"""Every scenario file parses, has a unique id, points at a fixture that exists and
declares the SOP sections it covers."""

from pathlib import Path

import pytest

from freight_agent.evalkit.spec import load_scenario

EVALS = Path(__file__).resolve().parents[2] / "evals"
FILES = sorted((EVALS / "scenarios").rglob("*.yaml"))


@pytest.mark.unit
@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
def test_scenario_file_is_valid(path: Path) -> None:
    scenario = load_scenario(path)

    assert (EVALS / scenario.load).is_file(), f"fixture {scenario.load} not found"
    assert any(t.startswith("sop:") for t in scenario.tags), "tag the SOP sections covered"
    assert scenario.suite == path.parent.name, "the suite is the scenario's directory name"


@pytest.mark.unit
def test_scenario_ids_are_unique() -> None:
    ids = [load_scenario(p).id for p in FILES]

    assert len(ids) == len(set(ids))
