"""Reproducibility metadata: what an eval run needs to be explained or repeated later.

Two runs can only be compared when it is known what changed between them: the code
(git sha, uncommitted edits), the SOP text (its version hash), the models, the prompt
templates and the runtime's limits. Each of those is recorded as an id or a short hash,
so a report shows at a glance whether a difference in results comes with a difference
in inputs.

Collecting never fails a run: a value that cannot be read (no git, no SOP folder, a
broken models file) is recorded as unknown (`None`), and the run itself reports the real
problem.
"""

import dataclasses
import hashlib
import importlib
import json
import platform
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from freight_agent.adapters.llm.models_config import load_models_config
from freight_agent.application.runtime import RuntimeConfig
from freight_agent.evalkit import judge

# What `evalkit.runner.build_runtime` runs the agent with. Hashed, not imported from the
# runner, because the runner builds it inline.
EVAL_RUNTIME_CONFIG = RuntimeConfig(reraise_unexpected=True)

_HASH_LENGTH = 12
_GIT_TIMEOUT_S = 10

# The modules whose source shapes what a model reads: the prompt templates and the code
# that fills them, and the tool descriptions. Wave 3 moves the tools from `tools` to
# `agent_tools` (card W3-AGT) and adds the document prompts; a module that does not exist
# (yet, or any more) is left out of the hash.
PROMPT_MODULES = (
    "freight_agent.application.prompts",
    "freight_agent.application.tools",
    "freight_agent.application.agent_tools",
    "freight_agent.application.prompts_documents",
)

# Runs a git command in a folder: its stdout, or None when git is missing or fails.
GitRunner = Callable[[Path, list[str]], str | None]


class RunMetadata(BaseModel):
    """Everything but the results. A value of None means it could not be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    created_at: datetime  # UTC, when the run started
    mode: str
    model_set: str = "default"
    git_sha: str | None = None
    git_dirty: bool | None = None  # uncommitted or untracked files in the checkout
    sop_version: str | None = None
    models: dict[str, list[str]] = {}  # role -> [primary, *fallbacks], from the models config
    models_config_sha256: str | None = None
    prompts_sha256: str | None = None
    config_sha256: str | None = None  # the runtime and validation config
    python: str | None = None
    settings: dict[str, str] = {}  # `Settings.describe()`: secrets show only as set or unset
    broker: str | None = None  # `acme@v2` when the run played a broker export


def collect_metadata(
    *,
    created_at: datetime,
    mode: str,
    model_set: str,
    sop_version: str | None,
    models_config: Path,
    repo: Path,
    settings: Mapping[str, str] | None = None,
    git: GitRunner | None = None,
    broker: str | None = None,
) -> RunMetadata:
    git = git or run_git
    sha, dirty = git_state(repo, git)
    models, models_sha = _models(models_config)
    return RunMetadata(
        created_at=created_at,
        mode=mode,
        model_set=model_set,
        git_sha=sha,
        git_dirty=dirty,
        sop_version=sop_version,
        models=models,
        models_config_sha256=models_sha,
        prompts_sha256=prompts_sha256(),
        config_sha256=config_sha256(EVAL_RUNTIME_CONFIG),
        python=platform.python_version(),
        settings=dict(settings or {}),
        broker=broker,
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


def prompts_sha256(modules: tuple[str, ...] = PROMPT_MODULES) -> str:
    """Hash of the prompt sources: the templates and the rendering code that fills them
    shape the prompt alike, and so do the tool descriptions the agent reads and the
    judge's prompt."""
    digest = hashlib.sha256()
    for name in modules:
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:  # the module exists and one of its own imports is missing
                raise
            continue
        source = Path(module.__file__ or "").read_bytes()
        digest.update(module.__name__.encode() + b"\0" + source)
    digest.update(b"judge\0" + judge.prompt_fingerprint().encode())
    return digest.hexdigest()[:_HASH_LENGTH]


def config_sha256(config: RuntimeConfig) -> str:
    """Hash of every runtime setting, the validation limits and the ping filter included."""
    return _short_hash(json.dumps(_plain(config), sort_keys=True, default=str))


def _plain(config: RuntimeConfig) -> dict[str, Any]:
    plain: dict[str, Any] = {}
    for item in dataclasses.fields(config):
        value = getattr(config, item.name)
        plain[item.name] = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return plain


def _models(path: Path) -> tuple[dict[str, list[str]], str | None]:
    try:
        data = path.read_bytes()
        config = load_models_config(path)
    except (OSError, ValueError):
        return {}, None
    chains = {
        role.value: [settings.model, *settings.fallbacks] for role, settings in config.roles.items()
    }
    return chains, hashlib.sha256(data).hexdigest()[:_HASH_LENGTH]


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_HASH_LENGTH]
