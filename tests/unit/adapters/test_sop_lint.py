import shutil
from pathlib import Path

import pytest

from freight_agent.adapters.sop.lint import LintIssue, Severity, lint

FIXTURES = Path(__file__).parent / "sop_fixtures"
ALL_TAGS = (
    "regression",
    "sop:communication_rules",
    "sop:delay_risk_high",
    "sop:driver_departed",
)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "sops"
    shutil.copytree(FIXTURES, root)
    return root


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _only(issues: list[LintIssue], file: str) -> LintIssue:
    assert len(issues) == 1, issues
    issue = issues[0]
    assert issue.file == file
    return issue


def _single_error(tmp_path: Path, relative: str, text: str) -> LintIssue:
    root = _tree(tmp_path)
    _write(root, relative, text)
    issue = _only(lint(root), relative)
    assert issue.severity is Severity.ERROR
    return issue


@pytest.mark.unit
def test_the_fixture_tree_is_clean() -> None:
    assert lint(FIXTURES, ALL_TAGS) == []


@pytest.mark.unit
def test_a_file_without_a_header_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", "# Escalation\n\nCall a Hero.")

    assert "no header" in issue.message
    assert "---" in issue.message


@pytest.mark.unit
def test_a_header_that_is_not_closed_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", "---\nid: escalation\nCall a Hero.")

    assert "not closed" in issue.message


@pytest.mark.unit
def test_a_header_that_is_not_valid_yaml_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "standard/escalation.md", "---\nid: escalation\nintents: [question\n---\nBody."
    )

    assert "could not be read near line 3" in issue.message


@pytest.mark.unit
def test_a_header_with_a_control_character_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", "---\nid: esc\x07\n---\nBody.")

    assert "could not be read" in issue.message


@pytest.mark.unit
def test_a_header_that_is_not_a_list_of_settings_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", "---\njust some words\n---\nBody.")

    assert "name: value" in issue.message


@pytest.mark.unit
def test_an_unknown_setting_is_an_error_with_a_suggestion(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "standard/escalation.md", "---\nid: escalation\nintent: [question]\n---\nBody."
    )

    assert "`intent`" in issue.message
    assert "did you mean `intents`" in issue.message


@pytest.mark.unit
def test_a_title_that_is_not_text_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "standard/escalation.md", "---\nid: escalation\ntitle: yes\n---\nBody."
    )

    assert "`title` must be text" in issue.message


@pytest.mark.unit
def test_a_missing_id_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", "---\ntitle: Escalation\n---\nBody.")

    assert "no `id`" in issue.message
    assert "id: " in issue.message


@pytest.mark.unit
def test_an_empty_header_is_a_missing_id(tmp_path: Path) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", "---\n---\nBody.")

    assert "no `id`" in issue.message


@pytest.mark.unit
@pytest.mark.parametrize("bad_id", ["Escalation", "escalation-rules", "2fast", "a__b", "x_", "7"])
def test_an_id_that_is_not_snake_case_is_an_error(tmp_path: Path, bad_id: str) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", f"---\nid: {bad_id}\n---\nBody.")

    assert f"the id '{bad_id}' is not valid" in issue.message
    assert "lowercase letters, digits and underscores" in issue.message


@pytest.mark.unit
def test_an_unknown_intent_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "standard/escalation.md", "---\nid: escalation\nintents: [late_truck]\n---\nB."
    )

    assert "'late_truck' in `intents` is not a known intent" in issue.message
    assert "delay_risk_high" in issue.message


@pytest.mark.unit
def test_intents_that_are_not_a_list_are_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "standard/escalation.md", "---\nid: escalation\nintents: {a: b}\n---\nB."
    )

    assert "`intents` must be a list" in issue.message


@pytest.mark.unit
def test_an_unknown_stage_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "standard/escalation.md", "---\nid: escalation\nstages: [parked]\n---\nB."
    )

    assert "'parked' in `stages` is not a known stage" in issue.message
    assert "to_pickup" in issue.message


@pytest.mark.unit
def test_an_invalid_mode_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "clients/northline/escalation.md", "---\nid: escalation\nmode: merge\n---\nB."
    )

    assert "`mode: merge` is not valid" in issue.message
    assert "replace" in issue.message
    assert "extend" in issue.message


@pytest.mark.unit
def test_a_duplicate_id_in_the_same_layer_client_and_type_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path,
        "clients/keystone/types/reefer/temperature.md",
        "---\nid: driver_departed\nmode: extend\n---\nAlso check the doors.",
    )

    assert "already used by clients/keystone/types/reefer/driver_departed.md" in issue.message


@pytest.mark.unit
def test_the_same_id_in_different_clients_and_layers_is_fine(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/delay_risk_high.md", "---\nid: delay_risk_high\n---\nCall.")
    _write(
        root,
        "clients/keystone/types/dry_van/delay_risk_high.md",
        "---\nid: delay_risk_high\n---\nCall.",
    )

    assert lint(root) == []


@pytest.mark.unit
def test_extend_in_standard_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "standard/escalation.md", "---\nid: escalation\nmode: extend\n---\nB."
    )

    assert "`mode: extend` cannot be used in standard/" in issue.message
    assert "Remove the `mode` line" in issue.message


@pytest.mark.unit
def test_a_client_extend_without_a_standard_base_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(
        tmp_path, "clients/northline/escalation.md", "---\nid: escalation\nmode: extend\n---\nB."
    )

    assert "extends 'escalation'" in issue.message
    assert "standard/" in issue.message


@pytest.mark.unit
def test_a_load_type_extend_can_build_on_its_clients_section(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/seal.md", "---\nid: seal\n---\nCheck the seal.")
    _write(root, "clients/acme/types/reefer/seal.md", "---\nid: seal\nmode: extend\n---\nAnd temp.")

    assert lint(root) == []


@pytest.mark.unit
def test_a_load_type_extend_cannot_build_on_another_clients_section(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/seal.md", "---\nid: seal\n---\nCheck the seal.")
    _write(root, "clients/keystone/types/reefer/seal.md", "---\nid: seal\nmode: extend\n---\nB.")

    issue = _only(lint(root), "clients/keystone/types/reefer/seal.md")

    assert issue.severity is Severity.ERROR
    assert "extends 'seal'" in issue.message
    assert "clients/keystone/" in issue.message


@pytest.mark.unit
def test_an_empty_body_is_an_error(tmp_path: Path) -> None:
    issue = _single_error(tmp_path, "standard/escalation.md", "---\nid: escalation\n---\n   \n")

    assert "no text" in issue.message


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative",
    [
        "delay_risk_high.md",
        "standard/extra/escalation.md",
        "clients/keystone.md",
        "clients/keystone/reefer/seal.md",
        "clients/keystone/types/seal.md",
        "clients/keystone/types/reefer/cold/seal.md",
    ],
)
def test_a_markdown_file_outside_the_layout_is_an_error(tmp_path: Path, relative: str) -> None:
    issue = _single_error(tmp_path, relative, "---\nid: seal\n---\nCheck the seal.")

    assert "not in a folder the agent reads" in issue.message
    assert "clients/<client_id>/types/<load_type>/<topic>.md" in issue.message


@pytest.mark.unit
def test_every_problem_of_a_file_is_reported(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "standard/escalation.md", "---\nid: Escalation\nstages: [parked]\n---\n")

    issues = lint(root)

    assert len(issues) == 3
    assert {i.file for i in issues} == {"standard/escalation.md"}


@pytest.mark.unit
def test_a_section_without_a_scenario_gets_a_coverage_warning() -> None:
    tags = ["regression", "sop:communication_rules", "sop:delay_risk_high", "capability"]

    issue = _only(lint(FIXTURES, tags), "standard/driver_departed.md")

    assert issue.severity is Severity.WARNING
    assert issue.message.startswith("no eval scenario covers section driver_departed")
    assert "sop:driver_departed" in issue.message


@pytest.mark.unit
def test_coverage_is_not_checked_without_scenario_tags() -> None:
    assert lint(FIXTURES) == []


@pytest.mark.unit
def test_issues_are_sorted_by_file_and_printable(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "standard/zz.md", "no header")
    _write(root, "clients/acme/aa.md", "no header")

    issues = lint(root, ["sop:communication_rules", "sop:driver_departed"])

    assert [i.file for i in issues] == [
        "clients/acme/aa.md",
        "standard/delay_risk_high.md",
        "standard/zz.md",
    ]
    assert str(issues[1]) == ("warning: standard/delay_risk_high.md: " + issues[1].message)


@pytest.mark.unit
def test_a_missing_folder_is_an_error(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"

    issue = _only(lint(missing), str(missing))

    assert issue.severity is Severity.ERROR
    assert "does not exist" in issue.message
