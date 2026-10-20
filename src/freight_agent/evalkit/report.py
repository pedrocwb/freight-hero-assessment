"""Eval reports: `results.json` for tools, `report.md` for people, a summary for the terminal.

Every run gets its own folder, `<out>/<UTC time>-<suite>-<mode>/`, so runs never
overwrite each other and a folder name says what it holds. `results.json` keeps every
trial with its observations, failures and model usage, plus the metadata; `report.md`
is rendered from it alone, so a report can be rendered again later (`agent eval report`).

A baseline is an earlier `results.json`. The comparison reads only scenario ids,
pass^k and cost from it, so a baseline written by an older version of this code still
compares.
"""

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError, computed_field

from freight_agent.evalkit.graders import rejections, sections_read, tool_calls
from freight_agent.evalkit.metadata import RunMetadata
from freight_agent.evalkit.results import Failure, ScenarioResult
from freight_agent.evalkit.suite import (
    CAPABILITY,
    REGRESSION,
    ExitCode,
    ScenarioStatus,
    ScenarioSummary,
    SuiteResult,
)

RESULTS_FILE = "results.json"
REPORT_FILE = "report.md"

_VERDICTS = {
    ExitCode.PASSED: "passed",
    ExitCode.FAILED: "failed",
    ExitCode.HARNESS_ERROR: "error",
    ExitCode.COST_CAP: "aborted",
}
_TERMINAL_FAILURES = 5  # per scenario; the report has them all
_TERMINAL_WIDTH = 160


class ReportError(Exception):
    """A results file or a baseline cannot be read."""


# --- baseline comparison ----------------------------------------------------------------


class _Lenient(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class _BaselineScenario(_Lenient):
    scenario_id: str
    passed: bool
    cost_usd: float = 0.0


class _BaselineMetadata(_Lenient):
    git_sha: str | None = None
    created_at: datetime | None = None


class Baseline(_Lenient):
    """The parts of an earlier results.json a comparison reads."""

    source: str = ""  # the path it was read from
    scenarios: tuple[_BaselineScenario, ...]
    cost_usd: float = 0.0
    metadata: _BaselineMetadata = _BaselineMetadata()


def read_baseline(path: Path) -> Baseline:
    """Read before the run starts, so a wrong path fails at once rather than after it."""
    file = results_path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"cannot read the baseline {path}: {exc}") from exc
    try:
        baseline = Baseline.model_validate_json(text)
    except ValidationError as exc:
        raise ReportError(f"the baseline {file} is not an eval results file") from exc
    return baseline.model_copy(update={"source": str(path)})


class Comparison(_Lenient):
    baseline: str  # the path it was read from
    baseline_git_sha: str | None
    baseline_created_at: datetime | None
    new_failures: tuple[str, ...]  # passed in the baseline, not now
    fixed: tuple[str, ...]  # failed in the baseline, pass now
    added: tuple[str, ...]  # not in the baseline
    missing: tuple[str, ...]  # in the baseline, not in this run
    cost_usd: float
    baseline_cost_usd: float

    @computed_field
    @property
    def cost_delta_usd(self) -> float:
        return self.cost_usd - self.baseline_cost_usd


def compare(result: SuiteResult, baseline: Baseline | Path) -> Comparison:
    before = baseline if isinstance(baseline, Baseline) else read_baseline(baseline)
    was = {s.scenario_id: s.passed for s in before.scenarios}
    now = {s.scenario_id: s.passed for s in result.scenarios}
    both = [i for i in now if i in was]
    return Comparison(
        baseline=before.source,
        baseline_git_sha=before.metadata.git_sha,
        baseline_created_at=before.metadata.created_at,
        new_failures=tuple(i for i in both if was[i] and not now[i]),
        fixed=tuple(i for i in both if not was[i] and now[i]),
        added=tuple(i for i in now if i not in was),
        missing=tuple(i for i in was if i not in now),
        cost_usd=result.cost_usd,
        baseline_cost_usd=before.cost_usd,
    )


# --- files ------------------------------------------------------------------------------


def run_folder_name(result: SuiteResult) -> str:
    started = result.metadata.created_at.astimezone(UTC)
    return f"{started:%Y%m%dT%H%M%SZ}-{result.suite}-{result.mode}"


def write_run(result: SuiteResult, out: Path, comparison: Comparison | None = None) -> Path:
    """Write `results.json` and `report.md` to a new folder under `out`; return the folder."""
    out.mkdir(parents=True, exist_ok=True)
    name = run_folder_name(result)
    folder, attempt = out / name, 1
    while True:
        try:
            folder.mkdir()
            break
        except FileExistsError:  # two runs in the same second
            attempt += 1
            folder = out / f"{name}-{attempt}"
    (folder / RESULTS_FILE).write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    write_report(result, folder, comparison)
    return folder


def write_report(result: SuiteResult, folder: Path, comparison: Comparison | None = None) -> Path:
    path = folder / REPORT_FILE
    path.write_text(render_markdown(result, comparison), encoding="utf-8")
    return path


def results_path(path: Path) -> Path:
    """A results folder or its `results.json`."""
    return path / RESULTS_FILE if path.is_dir() else path


def load_results(path: Path) -> SuiteResult:
    file = results_path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ReportError(f"no {RESULTS_FILE} at {path}") from None
    except OSError as exc:
        raise ReportError(f"cannot read {file}: {exc}") from exc
    try:
        return SuiteResult.model_validate_json(text)
    except ValidationError as exc:
        raise ReportError(
            f"{file} is not an eval results file ({exc.error_count()} problems)"
        ) from exc


# --- report.md --------------------------------------------------------------------------


def render_markdown(result: SuiteResult, comparison: Comparison | None = None) -> str:
    sections = [
        _heading(result),
        _scenario_table(result),
        _group_table(result),
        _failures(result),
        *_agent_loop(result),
        _cost(result),
    ]
    if comparison is not None:
        sections.append(_comparison(comparison))
    sections.append(_metadata(result.metadata))
    return "\n\n".join(sections) + "\n"


def verdict(result: SuiteResult) -> str:
    return _VERDICTS[result.exit_code]


def _heading(result: SuiteResult) -> str:
    gating = [s for s in result.scenarios if s.gating]
    capability = [s for s in result.scenarios if not s.gating]
    lines = [
        f"# Eval report: {result.suite} suite, {result.mode} mode",
        "",
        f"**Verdict: {verdict(result)}** (exit code {int(result.exit_code)}). "
        f"{len(result.scenarios)} scenarios, k = {result.k}: "
        f"{_passed_of(gating)} {REGRESSION} and {_passed_of(capability)} {CAPABILITY} "
        "scenarios pass every trial (pass^k).",
    ]
    if result.stopped is not None:
        lines += ["", f"> **Stopped:** {result.stopped}."]
    if result.not_run:
        lines += ["", f"> Not run: {', '.join(result.not_run)}"]
    for warning in result.warnings:
        lines += ["", f"> Warning: {warning}"]
    return "\n".join(lines)


def _passed_of(scenarios: Sequence[ScenarioSummary]) -> str:
    return f"{sum(s.passed for s in scenarios)}/{len(scenarios)}"


def _scenario_table(result: SuiteResult) -> str:
    rows = [
        "## Scenarios",
        "",
        "| Scenario | Tags | Status | pass^k | Trials passed | Failures | Cost (USD) | Tokens "
        "| Model calls |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in result.scenarios:
        cells = (
            s.scenario_id,
            ", ".join(s.tags),
            s.status.value,
            "yes" if s.passed else "no",
            f"{s.trials_passed}/{s.k}",
            str(s.failure_count),
            f"{s.cost_usd:.4f}",
            str(s.tokens),
            str(s.model_calls),
        )
        rows.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(rows)


def _group_table(result: SuiteResult) -> str:
    rows = [
        "## Groups",
        "",
        "Per tag, the share of its scenarios that passed at least one trial (pass@k) and "
        "every trial (pass^k).",
        "",
        "| Tag | Scenarios | pass@k | pass^k |",
        "|---|---|---|---|",
    ]
    rows += [
        f"| {_cell(g.tag)} | {g.scenarios} | {g.pass_at_k:.0%} | {g.pass_hat_k:.0%} |"
        for g in result.groups
    ]
    return "\n".join(rows)


def _failures(result: SuiteResult) -> str:
    blocks = ["## Failures"]
    failing = [s for s in result.scenarios if s.status is not ScenarioStatus.PASSED]
    if not failing:
        blocks.append("No failures.")
    for s in failing:
        role = "gating" if s.gating else CAPABILITY
        lines = [f"### {s.scenario_id} ({s.status.value}, {role})", "", s.title]
        for t in s.trials:
            if t.passed:
                continue
            lines += ["", f"Trial {t.trial}:", "", *(f"- {_failure_md(f)}" for f in _all(t))]
        if s.status is ScenarioStatus.INCOMPLETE:
            lines += ["", f"Only {len(s.trials)} of {s.k} trials ran."]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _all(trial: ScenarioResult) -> Iterable[Failure | str]:
    if trial.error is not None:
        yield trial.error
    yield from trial.failures


def _failure_md(item: Failure | str) -> str:
    if isinstance(item, str):
        return f"error: {_flat(item)}"
    where = f"step {item.step_index}, " if item.step_index is not None else ""
    return f"{where}`{item.check}`: {_flat(item.message)}"


def _agent_loop(result: SuiteResult) -> list[str]:
    """Per scenario, what the agent loop did over its trials: the judge's scores, the tool
    calls, the refused ones and the sections read. Left out when no trial has any."""
    rows: list[str] = []
    for s in result.scenarios:
        runs = [run for t in s.trials for o in t.observations for run in o.runs]
        calls = tool_calls(runs)
        scores = [
            o.judge.verdict.score
            for t in s.trials
            for o in t.observations
            if o.judge is not None and o.judge.verdict is not None
        ]
        if not calls and not scores:
            continue
        judge = f"{min(scores):.2f} min, {sum(scores) / len(scores):.2f} mean" if scores else "-"
        cells = (
            s.scenario_id,
            judge,
            str(len(calls)),
            str(rejections(runs)),
            ", ".join(sections_read(runs)) or "-",
        )
        rows.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    if not rows:
        return []
    head = [
        "## Agent loop",
        "",
        "Over every trial: the judge's scores of the judged steps, the agent's tool calls, "
        "the calls a rule refused and the SOP sections it searched up or read.",
        "",
        "| Scenario | Judge | Tool calls | Refused | Sections read |",
        "|---|---|---|---|---|",
    ]
    return ["\n".join(head + rows)]


def _cost(result: SuiteResult) -> str:
    cap = "none" if result.cost_cap_usd is None else f"${result.cost_cap_usd:.2f}"
    judge = sum(t.judge_cost_usd for s in result.scenarios for t in s.trials)
    judged = f" The judge's share: ${judge:.4f}." if judge else ""
    return (
        "## Cost\n\n"
        f"Total: ${result.cost_usd:.4f} over {len(result.scenarios)} scenarios, "
        f"{result.tokens} tokens, {result.model_calls} model calls. Cost cap: {cap}.{judged}"
    )


def _comparison(comparison: Comparison) -> str:
    when = comparison.baseline_created_at.isoformat() if comparison.baseline_created_at else "?"
    lines = [
        "## Comparison with the baseline",
        "",
        f"Baseline: `{comparison.baseline}` (git {comparison.baseline_git_sha or 'unknown'}, "
        f"run at {when}).",
        "",
        f"- New failures: {_names(comparison.new_failures)}",
        f"- Fixed: {_names(comparison.fixed)}",
        f"- Not in the baseline: {_names(comparison.added)}",
        f"- Not in this run: {_names(comparison.missing)}",
        f"- Cost: ${comparison.cost_usd:.4f} now, ${comparison.baseline_cost_usd:.4f} in the "
        f"baseline ({_signed(comparison.cost_delta_usd)})",
    ]
    return "\n".join(lines)


def _metadata(meta: RunMetadata) -> str:
    dirty = {True: " (uncommitted changes)", False: "", None: ""}[meta.git_dirty]
    models = "; ".join(f"{role}: {', then '.join(chain)}" for role, chain in meta.models.items())
    settings = ", ".join(f"{k}={v}" for k, v in meta.settings.items())
    rows = [
        ("Run at (UTC)", meta.created_at.isoformat()),
        ("Git commit", f"{meta.git_sha or 'unknown'}{dirty}"),
        ("SOP version", meta.sop_version or "unknown"),
        ("Broker", meta.broker or "each load fixture's own"),
        ("Mode", meta.mode),
        ("Model set", meta.model_set),
        ("Models", models or "unknown"),
        ("Models config hash", meta.models_config_sha256 or "unknown"),
        ("Prompts hash", meta.prompts_sha256 or "unknown"),
        ("Runtime config hash", meta.config_sha256 or "unknown"),
        ("Python", meta.python or "unknown"),
        ("Settings", settings or "unknown"),
    ]
    table = "\n".join(f"| {name} | {_cell(value)} |" for name, value in rows)
    note = (
        "\n\nScripted mode calls no real model: each scenario's script answers the model "
        "calls, at no cost, so the model ids above did not run."
        if meta.mode == "scripted"
        else ""
    )
    return f"## Metadata\n\n| | |\n|---|---|\n{table}{note}"


def _cell(text: str) -> str:
    return _flat(text).replace("|", "\\|")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _names(ids: Sequence[str]) -> str:
    return ", ".join(ids) if ids else "none"


def _signed(amount: float) -> str:
    return f"{'+' if amount >= 0 else '-'}${abs(amount):.4f}"


# --- terminal ---------------------------------------------------------------------------


def render_scenario_line(summary: ScenarioSummary) -> str:
    """One line per scenario, then its first failures, indented."""
    note = "  capability" if not summary.gating else ""
    head = (
        f"{summary.status.value.upper():<10} {summary.scenario_id:<40} "
        f"{summary.trials_passed}/{summary.k}  ${summary.cost_usd:.4f}{note}"
    )
    details: list[str] = []
    for t in summary.trials:
        prefix = f"trial {t.trial}, " if summary.k > 1 else ""
        details += [f"{prefix}{_failure_terminal(item)}" for item in _all(t)]
    shown = details[:_TERMINAL_FAILURES]
    if len(details) > len(shown):
        shown.append(f"... {len(details) - len(shown)} more in the report")
    return "\n".join([head, *(f"    {_clip(line)}" for line in shown)])


def _failure_terminal(item: Failure | str) -> str:
    if isinstance(item, str):
        return f"error: {_flat(item)}"
    where = f"step {item.step_index} " if item.step_index is not None else ""
    return f"{where}{item.check}: {_flat(item.message)}"


def _clip(line: str) -> str:
    return line if len(line) <= _TERMINAL_WIDTH else line[: _TERMINAL_WIDTH - 3] + "..."


def render_summary(
    result: SuiteResult, comparison: Comparison | None = None, *, report: Path | None = None
) -> str:
    """The totals after the scenario lines: counts, cost, the comparison and the verdict."""
    by_status = dict.fromkeys(ScenarioStatus, 0)
    for s in result.scenarios:
        by_status[s.status] += 1
    failed = by_status[ScenarioStatus.FAILED] + by_status[ScenarioStatus.INCOMPLETE]
    gating = [s for s in result.scenarios if s.gating]
    capability = [s for s in result.scenarios if not s.gating]
    cap = "" if result.cost_cap_usd is None else f" (cap ${result.cost_cap_usd:.2f})"
    lines = [
        f"{len(result.scenarios)} scenarios: {by_status[ScenarioStatus.PASSED]} passed, "
        f"{failed} failed, {by_status[ScenarioStatus.ERROR]} errors; "
        f"{REGRESSION} {_passed_of(gating)} pass^k, {CAPABILITY} {_passed_of(capability)} pass^k",
        f"cost ${result.cost_usd:.4f}, {result.tokens} tokens, {result.model_calls} model "
        f"calls{cap}",
    ]
    if result.stopped is not None:
        lines.append(f"{result.stopped}; not run: {_names(result.not_run)}")
    lines += [f"warning: {w}" for w in result.warnings]
    if comparison is not None:
        lines.append(
            f"vs baseline: {_count(comparison.new_failures, 'new failure')}, "
            f"{_count(comparison.fixed, 'fixed', plural='fixed')}, "
            f"cost {_signed(comparison.cost_delta_usd)}"
        )
    lines.append(f"verdict: {verdict(result)} (exit code {int(result.exit_code)})")
    if report is not None:
        lines.append(f"report: {report}")
    return "\n".join(lines)


def _count(ids: Sequence[str], noun: str, *, plural: str | None = None) -> str:
    word = noun if len(ids) == 1 else (plural or f"{noun}s")
    listed = f" ({', '.join(ids)})" if ids else ""
    return f"{len(ids)} {word}{listed}"
