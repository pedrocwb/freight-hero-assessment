"""Reproducibility metadata: what an experiment needs to be explained or repeated later.

Two experiments can only be compared when it is known what changed between them: the
code (git sha, uncommitted edits), the SOP text (the tree's content hash), the model of
each role, the gate's engine (and its act threshold) and the prompts the models read. A
prompt is its template's version (the LangSmith Hub commit, or `local:<sha256 prefix>`
of the repository copy) and the reference it was resolved from
(`freight-agent-agent:candidate`): an experiment run on a candidate tag says which commit
it measured, and the catalog the templates are rendered with is the SOP tree's, in
`sop_version`. LangSmith records this on the experiment, next to its results.

Collecting never fails a run: a value that cannot be read (no git, a broken models
file) is recorded as unknown (`None`), and the run itself reports the real problem.
"""

import platform
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from freight_agent.adapters.llm.models_config import load_models_config
from freight_agent.ports.prompts import Prompts

_GIT_TIMEOUT_S = 10

# Runs a git command in a folder: its stdout, or None when git is missing or fails.
GitRunner = Callable[[Path, list[str]], str | None]


class RunMetadata(BaseModel):
    """Everything but the results. A value of None means it could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_at: datetime  # UTC, when the run started
    suite: str
    k: int
    cost_cap_usd: float
    git_sha: str | None = None
    git_dirty: bool | None = None  # uncommitted or untracked files in the checkout
    sop_version: str | None = None
    models: dict[str, list[str]] = {}  # role -> [primary, *fallbacks]; decision_<role>
    gate_engine: str | None = None  # llm or jev
    gate_act_threshold: float | None = None  # the jev engine's; None on llm
    prompts: dict[str, str] = {}  # role -> template version (Hub commit, or local:<hash>)
    prompt_refs: dict[str, str] = {}  # role -> what the version was resolved from
    python: str | None = None

    def for_langsmith(self) -> dict[str, Any]:
        """Flat values, as LangSmith shows and filters an experiment's metadata:
        `prompt_agent` is the commit, `prompt_ref_agent` the pin it came from."""
        nested = {"models", "prompts", "prompt_refs"}
        data: dict[str, Any] = self.model_dump(mode="json", exclude=nested)
        data.update({f"model_{role}": ", ".join(chain) for role, chain in self.models.items()})
        data.update({f"prompt_{role}": version for role, version in self.prompts.items()})
        data.update({f"prompt_ref_{role}": ref for role, ref in self.prompt_refs.items()})
        return data


def collect_metadata(
    *,
    created_at: datetime,
    suite: str,
    k: int,
    cost_cap_usd: float,
    sop_version: str | None,
    models_config: Path,
    prompts: Prompts,
    repo: Path,
    git: GitRunner | None = None,
    gate_engine: str | None = None,
    gate_act_threshold: float | None = None,
) -> RunMetadata:
    sha, dirty = git_state(repo, git or run_git)
    return RunMetadata(
        created_at=created_at,
        suite=suite,
        k=k,
        cost_cap_usd=cost_cap_usd,
        git_sha=sha,
        git_dirty=dirty,
        sop_version=sop_version,
        models=_models(models_config),
        gate_engine=gate_engine,
        gate_act_threshold=gate_act_threshold if gate_engine == "jev" else None,
        prompts=prompts.versions(),
        prompt_refs=prompts.refs(),
        python=platform.python_version(),
    )


def run_git(repo: Path, args: list[str]) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # no git binary, a hung git
        return None
    return done.stdout if done.returncode == 0 else None


def git_state(repo: Path, git: GitRunner = run_git) -> tuple[str | None, bool | None]:
    """The checked-out commit and whether the tree has changes, or (None, None) outside git.

    Untracked files count as changes: a new scenario not yet added changes the results.
    """
    sha = git(repo, ["rev-parse", "HEAD"])
    if sha is None:
        return None, None
    status = git(repo, ["status", "--porcelain"])
    return sha.strip(), (None if status is None else bool(status.strip()))


def _models(path: Path) -> dict[str, list[str]]:
    try:
        config = load_models_config(path)
    except (OSError, ValueError):
        return {}
    models = {role.value: list(role_config.models) for role, role_config in config.roles.items()}
    models.update({f"decision_{role.value}": [d.model] for role, d in config.decisions.items()})
    return models
