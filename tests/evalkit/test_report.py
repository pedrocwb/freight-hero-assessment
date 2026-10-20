"""Reports: results.json, report.md, the terminal summary, the baseline comparison and
the reproducibility metadata they carry."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.domain.runs import Usage
from freight_agent.domain.validation import ValidationConfig
from freight_agent.evalkit.metadata import (
    EVAL_RUNTIME_CONFIG,
    RunMetadata,
    collect_metadata,
    config_sha256,
    git_state,
    prompts_sha256,
)
from freight_agent.evalkit.report import (
    REPORT_FILE,
    RESULTS_FILE,
    ReportError,
    compare,
    load_results,
    render_markdown,
    render_scenario_line,
    render_summary,
    write_report,
    write_run,
)
from freight_agent.evalkit.results import Failure, ScenarioResult
from freight_agent.evalkit.suite import ScenarioSummary, SuiteResult

ROOT = Path(__file__).resolve().parents[2]
CREATED = datetime(2026, 10, 19, 15, 30, 5, tzinfo=UTC)


def meta() -> RunMetadata:
    return RunMetadata(
        created_at=CREATED,
        mode="live",
        model_set="default",
        git_sha="abc123def456",
        git_dirty=True,
        sop_version="0123456789ab",
        models={"gate": ["openai/gpt-4.1-nano", "google/gemini-2.5-flash-lite"]},
        prompts_sha256="feedfacecafe",
        config_sha256="c0ffee000000",
        python="3.13.1",
        settings={"LLM_MODE": "live", "OPENROUTER_API_KEY": "set"},
    )


def trial(
    scenario_id: str,
    *,
    failures: tuple[Failure, ...] = (),
    cost_usd: float = 0.0,
    error: str | None = None,
    number: int = 1,
) -> ScenarioResult:
    usage = (Usage(role="gate", model="m", input_tokens=90, output_tokens=10, cost_usd=cost_usd),)
    return ScenarioResult(
        scenario_id=scenario_id,
        trial=number,
        passed=not failures and error is None,
        failures=failures,
        usage=usage,
        error=error,
    )


def scenario(
    scenario_id: str, *trials: ScenarioResult, tags: tuple[str, ...] = ("regression", "sop:x")
) -> ScenarioSummary:
    return ScenarioSummary(
        scenario_id=scenario_id, title=f"{scenario_id} title", tags=tags, k=1, trials=trials
    )


NO_ACTIONS = Failure(
    step_index=1, check="expect.no_actions", message="expected no actions, found 1: sms to drv"
)


def result(*scenarios: ScenarioSummary, **fields: object) -> SuiteResult:
    return SuiteResult.model_validate(
        {
            "suite": "core",
            "mode": "live",
            "k": 1,
            "metadata": meta(),
            "scenarios": scenarios,
            "cost_cap_usd": 5.0,
            **fields,
        }
    )


def mixed() -> SuiteResult:
    return result(
        scenario("a_good", trial("a_good", cost_usd=0.0125)),
        scenario("b_bad", trial("b_bad", failures=(NO_ACTIONS,), cost_usd=0.0025)),
        scenario("c_hard", trial("c_hard", failures=(NO_ACTIONS,)), tags=("capability",)),
    )


# --- report.md --------------------------------------------------------------------------


@pytest.mark.unit
def test_the_summary_table_has_one_row_per_scenario() -> None:
    text = render_markdown(mixed())

    assert (
        "| Scenario | Tags | Status | pass^k | Trials passed | Failures | Cost (USD) | Tokens "
        "| Model calls |" in text
    )
    assert "| a_good | regression, sop:x | pass | yes | 1/1 | 0 | 0.0125 | 100 | 0 |" in text
    assert "| b_bad | regression, sop:x | fail | no | 0/1 | 1 | 0.0025 | 100 | 0 |" in text
    assert "| c_hard | capability | fail | no | 0/1 | 1 | 0.0000 | 100 | 0 |" in text


@pytest.mark.unit
def test_the_verdict_and_the_groups_lead_the_report() -> None:
    text = render_markdown(mixed())

    assert text.startswith("# Eval report: core suite, live mode\n")
    assert "**Verdict: failed** (exit code 1)" in text
    assert "| regression | 2 | 50% | 50% |" in text
    assert "| capability | 1 | 0% | 0% |" in text


@pytest.mark.unit
def test_failures_are_listed_with_their_step_check_and_message() -> None:
    text = render_markdown(mixed())

    failures = text.split("## Failures", 1)[1]
    assert "### b_bad (fail, gating)" in failures
    assert "### c_hard (fail, capability)" in failures
    assert "- step 1, `expect.no_actions`: expected no actions, found 1: sms to drv" in failures
    assert "a_good" not in failures.split("## ", 1)[0]


@pytest.mark.unit
def test_a_harness_error_is_shown_as_the_trials_error() -> None:
    broken = result(scenario("x", trial("x", error="step 0: CassetteMissing: no cassette")))

    text = render_markdown(broken)

    assert "**Verdict: error** (exit code 2)" in text
    assert "- error: step 0: CassetteMissing: no cassette" in text


@pytest.mark.unit
def test_the_cost_total_and_the_cap_are_reported() -> None:
    text = render_markdown(mixed())

    assert "Total: $0.0150 over 3 scenarios, 300 tokens, 0 model calls. Cost cap: $5.00." in text


@pytest.mark.unit
def test_a_stopped_run_says_why_and_lists_what_did_not_run() -> None:
    stopped = result(
        scenario("a_good", trial("a_good", cost_usd=6.0)),
        stopped="stopped by the cost cap: $6.0000 spent, over the cap of $5.00",
        not_run=("b_next", "c_last"),
        warnings=("a warning",),
    )

    text = render_markdown(stopped)

    assert "**Verdict: aborted** (exit code 3)" in text
    assert "stopped by the cost cap: $6.0000 spent" in text
    assert "Not run: b_next, c_last" in text
    assert "Warning: a warning" in text


@pytest.mark.unit
def test_the_metadata_section_names_every_input_of_the_run() -> None:
    text = render_markdown(mixed())

    metadata = text.split("## Metadata", 1)[1]
    for value in (
        "abc123def456 (uncommitted changes)",
        "0123456789ab",
        "gate: openai/gpt-4.1-nano, then google/gemini-2.5-flash-lite",
        "feedfacecafe",
        "c0ffee000000",
        "2026-10-19T15:30:05+00:00",
        "OPENROUTER_API_KEY=set",
    ):
        assert value in metadata


# --- files ------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_run_is_written_to_a_folder_named_by_time_suite_and_mode(tmp_path: Path) -> None:
    folder = write_run(mixed(), tmp_path / "results")

    assert folder == tmp_path / "results" / "20261019T153005Z-core-live"
    assert (folder / RESULTS_FILE).is_file()
    assert (folder / REPORT_FILE).read_text(encoding="utf-8") == render_markdown(mixed())


@pytest.mark.unit
def test_two_runs_in_the_same_second_get_two_folders(tmp_path: Path) -> None:
    first = write_run(mixed(), tmp_path)
    second = write_run(mixed(), tmp_path)

    assert first != second
    assert second.name == "20261019T153005Z-core-live-2"


@pytest.mark.unit
def test_results_json_holds_every_trial_and_reads_back_the_same(tmp_path: Path) -> None:
    original = mixed()
    folder = write_run(original, tmp_path)

    data = json.loads((folder / RESULTS_FILE).read_text(encoding="utf-8"))
    assert data["exit_code"] == 1
    assert data["scenarios"][1]["trials"][0]["failures"][0]["check"] == "expect.no_actions"
    assert data["scenarios"][0]["trials"][0]["usage"][0]["cost_usd"] == 0.0125
    assert data["metadata"]["git_sha"] == "abc123def456"
    assert load_results(folder) == original
    assert load_results(folder / RESULTS_FILE) == original


@pytest.mark.unit
def test_reading_something_that_is_not_a_results_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match=r"no results\.json"):
        load_results(tmp_path)
    (tmp_path / RESULTS_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(ReportError, match="not an eval results file"):
        load_results(tmp_path)


@pytest.mark.unit
def test_the_report_can_be_rendered_again_from_its_results(tmp_path: Path) -> None:
    folder = write_run(mixed(), tmp_path)
    (folder / REPORT_FILE).unlink()

    path = write_report(load_results(folder), folder)

    assert path.read_text(encoding="utf-8") == render_markdown(mixed())


# --- baseline comparison ----------------------------------------------------------------


def baseline(tmp_path: Path) -> Path:
    """Before: a_good failed, b_bad passed, gone_now existed; it cost $0.0100."""
    before = result(
        scenario("a_good", trial("a_good", failures=(NO_ACTIONS,), cost_usd=0.004)),
        scenario("b_bad", trial("b_bad", cost_usd=0.006)),
        scenario("gone_now", trial("gone_now")),
    )
    return write_run(before, tmp_path / "baseline")


@pytest.mark.unit
def test_comparison_names_new_failures_fixed_scenarios_and_the_cost_delta(
    tmp_path: Path,
) -> None:
    comparison = compare(mixed(), baseline(tmp_path))

    assert comparison.new_failures == ("b_bad",)
    assert comparison.fixed == ("a_good",)
    assert comparison.added == ("c_hard",)
    assert comparison.missing == ("gone_now",)
    assert comparison.cost_delta_usd == pytest.approx(0.005)
    assert comparison.baseline_git_sha == "abc123def456"


@pytest.mark.unit
def test_the_baseline_can_be_a_results_folder_or_its_json(tmp_path: Path) -> None:
    folder = baseline(tmp_path)

    assert (
        compare(mixed(), folder).new_failures
        == compare(mixed(), folder / RESULTS_FILE).new_failures
    )


@pytest.mark.unit
def test_the_comparison_is_part_of_the_report(tmp_path: Path) -> None:
    comparison = compare(mixed(), baseline(tmp_path))

    text = render_markdown(mixed(), comparison)

    section = text.split("## Comparison with the baseline", 1)[1]
    assert "New failures: b_bad" in section
    assert "Fixed: a_good" in section
    assert "Not in the baseline: c_hard" in section
    assert "Not in this run: gone_now" in section
    assert "Cost: $0.0150 now, $0.0100 in the baseline (+$0.0050)" in section


@pytest.mark.unit
def test_a_missing_baseline_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ReportError, match="baseline"):
        compare(mixed(), tmp_path / "nothing")


# --- terminal ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_scenario_line_shows_status_trials_and_cost_and_the_failures() -> None:
    good, bad, _ = mixed().scenarios

    assert render_scenario_line(good).split() == ["PASS", "a_good", "1/1", "$0.0125"]
    lines = render_scenario_line(bad).splitlines()
    assert lines[0].split() == ["FAIL", "b_bad", "0/1", "$0.0025"]
    assert lines[1].strip() == "step 1 expect.no_actions: expected no actions, found 1: sms to drv"


@pytest.mark.unit
def test_the_summary_gives_counts_cost_comparison_verdict_and_report_path(
    tmp_path: Path,
) -> None:
    comparison = compare(mixed(), baseline(tmp_path))

    text = render_summary(mixed(), comparison, report=Path("evals/results/x/report.md"))

    assert (
        "3 scenarios: 1 passed, 2 failed, 0 errors; regression 1/2 pass^k, capability 0/1 pass^k"
        in text
    )
    assert "cost $0.0150, 300 tokens, 0 model calls (cap $5.00)" in text
    assert "vs baseline: 1 new failure (b_bad), 1 fixed (a_good), cost +$0.0050" in text
    assert "verdict: failed (exit code 1)" in text
    assert "report: evals/results/x/report.md" in text


# --- metadata ---------------------------------------------------------------------------


class FakeGit:
    def __init__(self, sha: str | None, status: str | None = "") -> None:
        self.sha = sha
        self.status = status

    def __call__(self, repo: Path, args: list[str]) -> str | None:
        return self.sha if args[0] == "rev-parse" else self.status


@pytest.mark.unit
def test_git_state_reads_the_sha_and_whether_the_tree_is_dirty() -> None:
    assert git_state(ROOT, FakeGit("abc\n", "")) == ("abc", False)
    assert git_state(ROOT, FakeGit("abc\n", " M src/x.py\n")) == ("abc", True)
    assert git_state(ROOT, FakeGit(None)) == (None, None)


@pytest.mark.unit
def test_git_state_tolerates_a_folder_outside_git(tmp_path: Path) -> None:
    assert git_state(tmp_path) == (None, None)


@pytest.mark.unit
def test_metadata_records_the_sop_version_models_and_hashes() -> None:
    data = collect_metadata(
        created_at=CREATED,
        mode="scripted",
        model_set="default",
        sop_version=FileSopRepository(ROOT / "sops").version(),
        models_config=ROOT / "config" / "models.yaml",
        repo=ROOT,
        settings={"LLM_MODE": "replay"},
        git=FakeGit("abc\n"),
    )

    assert data.git_sha == "abc"
    assert data.git_dirty is False
    assert data.sop_version == FileSopRepository(ROOT / "sops").version()
    assert data.models["gate"][0] == "openai/gpt-4.1-nano"
    assert set(data.models) == {"gate", "agent", "judge", "driver_sim", "extract"}
    assert data.models_config_sha256 is not None
    assert data.prompts_sha256 == prompts_sha256()
    assert data.config_sha256 == config_sha256(EVAL_RUNTIME_CONFIG)
    assert data.settings == {"LLM_MODE": "replay"}


@pytest.mark.unit
def test_metadata_records_unknown_values_instead_of_failing(tmp_path: Path) -> None:
    data = collect_metadata(
        created_at=CREATED,
        mode="scripted",
        model_set="default",
        sop_version=None,
        models_config=tmp_path / "no_models.yaml",
        repo=tmp_path,
        git=FakeGit(None),
    )

    assert (data.git_sha, data.git_dirty, data.sop_version) == (None, None, None)
    assert (data.models, data.models_config_sha256) == ({}, None)


@pytest.mark.unit
def test_the_hashes_are_short_stable_and_follow_the_config() -> None:
    stricter = RuntimeConfig(validation=ValidationConfig(sms_max_per_window=1))

    assert prompts_sha256() == prompts_sha256()
    assert len(prompts_sha256()) == 12
    assert config_sha256(RuntimeConfig()) == config_sha256(RuntimeConfig())
    assert config_sha256(stricter) != config_sha256(RuntimeConfig())
