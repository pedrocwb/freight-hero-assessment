"""Reproducibility metadata: what an experiment needs to be explained or repeated later.

Two experiments can only be compared when it is known what changed between them: the
code (git sha, uncommitted edits), the SOP text (the tree's content hash), the model of
each role and the prompts the models read. Each prompt is hashed as rendered, for every
broker of the SOP tree (the gate's and the agent's prompts are rendered from the
broker's catalog and limits), so a changed word, intent or limit shows as a changed hash.
LangSmith records this on the experiment, next to its results.

Collecting never fails a run: a value that cannot be read (no git, a broken models
file) is recorded as unknown (`None`), and the run itself reports the real problem.
"""

import hashlib
import platform
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from freight_agent.adapters.llm.models_config import load_models_config
from freight_agent.application.prompts import render_agent_system_prompt, render_gate_system_prompt
from freight_agent.application.prompts_documents import extract_system
from freight_agent.domain.broker import Broker, standard_broker
from freight_agent.evalkit.judge import JUDGE_SYSTEM_PROMPT
from freight_agent.ports.knowledge import SopIndex

_HASH_LENGTH = 12
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
    models: dict[str, list[str]] = {}  # role -> [primary, *fallbacks]
    prompts: dict[str, str] = {}  # "<prompt>:<broker>" -> hash of the rendered text
    python: str | None = None

    def for_langsmith(self) -> dict[str, Any]:
        """Flat values, as LangSmith shows and filters an experiment's metadata."""
        data: dict[str, Any] = self.model_dump(mode="json", exclude={"models", "prompts"})
        data.update({f"model_{role}": ", ".join(chain) for role, chain in self.models.items()})
        data.update({f"prompt_{name}": sha for name, sha in self.prompts.items()})
        return data


async def collect_metadata(
    *,
    created_at: datetime,
    suite: str,
    k: int,
    cost_cap_usd: float,
    knowledge: SopIndex,
    sops_dir: Path,
    sop_version: str | None,
    models_config: Path,
    repo: Path,
    git: GitRunner | None = None,
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
        prompts=prompt_hashes(await brokers(knowledge, sops_dir)),
        python=platform.python_version(),
    )


async def brokers(knowledge: SopIndex, sops_dir: Path) -> list[Broker]:
    """The standard catalog and every client folder's catalog, by id."""
    clients = sops_dir / "clients"
    ids = sorted(p.name for p in clients.iterdir() if p.is_dir()) if clients.is_dir() else []
    found = [standard_broker(), *[await knowledge.broker(i) for i in ids]]
    return [*{b.broker_id: b for b in found}.values()]


def prompt_hashes(catalogs: list[Broker]) -> dict[str, str]:
    """A hash of each system prompt as the models read it, per broker."""
    prompts = {"judge": JUDGE_SYSTEM_PROMPT}
    for broker in catalogs:
        prompts[f"gate:{broker.broker_id}"] = render_gate_system_prompt(broker)
        prompts[f"agent:{broker.broker_id}"] = render_agent_system_prompt(broker)
        if broker.document_types:
            prompts[f"extract:{broker.broker_id}"] = extract_system(broker.document_types)
    return {name: _short_hash(text) for name, text in prompts.items()}


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
    return {
        role.value: [role_config.model, *role_config.fallbacks]
        for role, role_config in config.roles.items()
    }


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
