"""Suites: discovery, selection and which scenarios gate a run.

Scenarios here are small one-step files written to a temporary evals folder, so each
test controls the ids, suites and tags. Playing and grading a suite is the experiment's
job (`test_experiment.py`).
"""

from pathlib import Path

import pytest

from freight_agent.evalkit.spec import Scenario
from freight_agent.evalkit.suite import (
    CAPABILITY,
    REGRESSION,
    SuiteError,
    discover,
    gating,
    select,
    suites,
)

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit

# One step: the driver says "ok" with nothing open.
ACK_STEPS = """\
steps:
  - at: "07:05"
    message: {channel: sms, sender: drv_20931, body: "ok"}
    expect:
      no_actions: true
      run_status: [noop]
      llm_calls: 1
"""


def write_scenario(
    evals: Path,
    scenario_id: str,
    *,
    suite: str = "core",
    tags: str = "[regression, sop:acknowledgements]",
    steps: str = ACK_STEPS,
) -> Path:
    folder = evals / "scenarios" / suite
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{scenario_id}.yaml"
    path.write_text(
        f"id: {scenario_id}\ntitle: {scenario_id} title\nsuite: {suite}\ntags: {tags}\n"
        f"load: fixtures/load_481207.json\ndate: 2026-10-13\nstart: '06:55'\n{steps}",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def evals(tmp_path: Path) -> Path:
    return tmp_path / "evals"


# --- discovery --------------------------------------------------------------------------


def test_discovery_reads_every_scenario_of_the_suite_folder_in_file_order(evals: Path) -> None:
    write_scenario(evals, "b_second")
    write_scenario(evals, "a_first")
    write_scenario(evals, "other", suite="extra")

    assert [s.id for s in discover(evals, "core")] == ["a_first", "b_second"]
    assert suites(evals) == ["core", "extra"]


def test_a_folder_without_scenario_files_is_not_a_suite(evals: Path) -> None:
    assert suites(evals) == []  # no scenarios folder at all
    (evals / "scenarios" / "empty").mkdir(parents=True)
    write_scenario(evals, "a_first")

    assert suites(evals) == ["core"]


def test_the_shipped_suites_and_their_sizes() -> None:
    evals = ROOT / "evals"

    assert suites(evals) == ["agent", "core", "docs"]
    assert [len(discover(evals, name)) for name in suites(evals)] == [1, 22, 3]


def test_an_unknown_suite_names_the_suites_that_exist(evals: Path) -> None:
    write_scenario(evals, "a_first")

    with pytest.raises(SuiteError, match=r"no suite 'nope'.*\(suites: core\)"):
        discover(evals, "nope")


def test_an_unknown_suite_in_an_empty_folder_says_there_is_none(evals: Path) -> None:
    with pytest.raises(SuiteError, match=r"\(suites: none\)"):
        discover(evals, "core")


def test_every_bad_scenario_file_is_named_in_one_error(evals: Path) -> None:
    write_scenario(evals, "good")
    write_scenario(evals, "broken_yaml").write_text("id: [unclosed\n", encoding="utf-8")
    write_scenario(evals, "bad_time", steps="steps:\n  - at: '7 am'\n")
    write_scenario(evals, "old_script", steps=ACK_STEPS + "    script: {gate: []}\n")

    with pytest.raises(SuiteError) as caught:
        discover(evals, "core")

    message = str(caught.value)
    assert "broken_yaml.yaml" in message
    assert "bad_time.yaml: steps.0: Value error, step time must be HH:MM" in message
    assert "old_script.yaml: steps.0.script: Extra inputs are not permitted" in message
    assert "good.yaml" not in message
    assert len(message.splitlines()) == 4  # the folder, then one line per bad file


def test_a_scenario_in_the_wrong_folder_or_with_a_repeated_id_is_an_error(evals: Path) -> None:
    write_scenario(evals, "a_first")
    write_scenario(evals, "a_copy").write_text(
        (evals / "scenarios" / "core" / "a_first.yaml").read_text(), encoding="utf-8"
    )
    misplaced = write_scenario(evals, "misplaced")
    misplaced.write_text(misplaced.read_text().replace("suite: core", "suite: other"))

    with pytest.raises(SuiteError) as caught:
        discover(evals, "core")

    message = str(caught.value)
    # a_copy.yaml comes first in file order, so a_first.yaml is the repeat
    assert "a_first.yaml: the id 'a_first' is also used by a_copy.yaml" in message
    assert "misplaced.yaml: suite is 'other', but the file is in core/" in message


# --- selection --------------------------------------------------------------------------


def _scenarios(evals: Path, tags: dict[str, str]) -> list[Scenario]:
    for scenario_id, tag_list in tags.items():
        write_scenario(evals, scenario_id, tags=tag_list)
    return discover(evals, "core")


def test_tags_select_scenarios_with_any_of_them(evals: Path) -> None:
    scenarios = _scenarios(
        evals,
        {
            "r1": "[regression, sop:a]",
            "c1": "[capability, sop:b]",
            "r2": "[regression, sop:b]",
        },
    )

    assert [s.id for s in select(scenarios, tags=["capability"])] == ["c1"]
    assert [s.id for s in select(scenarios, tags=["sop:b", "capability"])] == ["c1", "r2"]
    assert [s.id for s in select(scenarios)] == ["c1", "r1", "r2"]


def test_ids_select_in_suite_order(evals: Path) -> None:
    scenarios = _scenarios(evals, {"r1": "[regression]", "r2": "[regression]", "r3": "[x]"})

    assert [s.id for s in select(scenarios, ids=["r3", "r1"])] == ["r1", "r3"]


def test_scenario_ids_and_tags_combine(evals: Path) -> None:
    scenarios = _scenarios(evals, {"r1": "[regression, sop:a]", "c1": "[capability, sop:a]"})

    assert [s.id for s in select(scenarios, ids=["r1"], tags=["sop:a"])] == ["r1"]
    with pytest.raises(SuiteError, match="no scenario matches the filters"):
        select(scenarios, tags=["capability"], ids=["r1"])


def test_an_unknown_scenario_id_is_an_error(evals: Path) -> None:
    scenarios = _scenarios(evals, {"r1": "[regression, sop:a]"})

    with pytest.raises(SuiteError, match=r"no scenario 'r8', 'r9' in this suite"):
        select(scenarios, ids=["r9", "r1", "r8"])


def test_a_tag_no_scenario_has_is_an_empty_selection(evals: Path) -> None:
    scenarios = _scenarios(evals, {"r1": "[regression]"})

    with pytest.raises(SuiteError, match="no scenario matches the filters"):
        select(scenarios, tags=["sop:nope"])


# --- gating -----------------------------------------------------------------------------


def test_every_scenario_but_a_capability_one_gates_the_run(evals: Path) -> None:
    scenarios = _scenarios(
        evals,
        {
            "a_regression": f"[{REGRESSION}, sop:a]",
            "b_capability": f"[{CAPABILITY}, sop:a]",
            "c_untagged": "[sop:a]",
            "d_both": f"[{REGRESSION}, {CAPABILITY}]",
        },
    )

    assert {s.id: gating(s) for s in scenarios} == {
        "a_regression": True,
        "b_capability": False,
        "c_untagged": True,
        "d_both": False,
    }


def test_the_shipped_capability_scenarios_do_not_gate() -> None:
    measured = [s.id for s in discover(ROOT / "evals", "core") if not gating(s)]

    assert measured == ["c3_relative_eta", "d4_lumper_no_sop"]
