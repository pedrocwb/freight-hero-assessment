"""Reproducibility metadata: what an experiment records so it can be explained later.

The prompts are each role's template version and the pin it came from, and collecting
must never fail a run: git and the models file are read through seams (a fake git
runner, a missing file), and what cannot be read is recorded as unknown.
"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.llm.models_config import load_models_config
from freight_agent.evalkit.metadata import (
    RunMetadata,
    collect_metadata,
    git_state,
    run_git,
)
from tests.prompts import PROMPTS, PROMPTS_DIR

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "config" / "models.yaml"
CREATED = datetime(2026, 10, 20, 1, 30, tzinfo=UTC)

pytestmark = pytest.mark.unit


class FakeGit:
    """Answers `rev-parse` and `status` like git would, and records the calls."""

    def __init__(self, sha: str | None = "abc1234\n", status: str | None = "") -> None:
        self.answers = {"rev-parse": sha, "status": status}
        self.calls: list[tuple[Path, list[str]]] = []

    def __call__(self, repo: Path, args: list[str]) -> str | None:
        self.calls.append((repo, args))
        return self.answers[args[0]]


# --- prompts -----------------------------------------------------------------------------


def test_the_prompts_are_each_roles_version_and_the_reference_it_came_from() -> None:
    found = collect(prompts=PROMPTS)

    assert found.prompts == PROMPTS.versions()
    roles = ("gate", "agent", "extract", "judge", "gate_decision", "memory")
    assert set(found.prompts) == set(roles)
    assert all(v.startswith("local:") and len(v) == 18 for v in found.prompts.values())
    assert found.prompt_refs == {role: str(PROMPTS_DIR / f"{role}.yaml") for role in roles}


def test_a_hub_version_is_recorded_with_its_pin() -> None:
    hub = replace(PROMPTS.agent, ref="freight-agent-agent:candidate", version="3f2a9c41d0be")
    prompts = replace(PROMPTS, agent=hub)

    found = collect(prompts=prompts).for_langsmith()

    assert (found["prompt_agent"], found["prompt_ref_agent"]) == (
        "3f2a9c41d0be",
        "freight-agent-agent:candidate",
    )
    assert found["prompt_gate"] == PROMPTS.gate.version


def collect(**changes: object) -> RunMetadata:
    options: dict[str, object] = {
        "created_at": CREATED,
        "suite": "core",
        "k": 1,
        "cost_cap_usd": 0.25,
        "sop_version": None,
        "models_config": MODELS,
        "prompts": PROMPTS,
        "repo": ROOT,
        "git": FakeGit(),
        **changes,
    }
    return collect_metadata(**options)  # pyright: ignore[reportArgumentType]


# --- git ----------------------------------------------------------------------------------


def test_git_state_is_the_commit_and_whether_the_tree_has_changes(tmp_path: Path) -> None:
    clean, dirty = FakeGit(), FakeGit(status="?? evals/new.yaml\n")

    assert git_state(tmp_path, clean) == ("abc1234", False)
    assert git_state(tmp_path, dirty) == ("abc1234", True)  # untracked files count
    assert clean.calls == [(tmp_path, ["rev-parse", "HEAD"]), (tmp_path, ["status", "--porcelain"])]


def test_outside_git_both_are_unknown(tmp_path: Path) -> None:
    git = FakeGit(sha=None)

    assert git_state(tmp_path, git) == (None, None)
    assert len(git.calls) == 1  # no status asked
    assert git_state(tmp_path, FakeGit(status=None)) == ("abc1234", None)


def test_run_git_is_none_when_git_cannot_run(tmp_path: Path) -> None:
    assert run_git(tmp_path / "missing", ["rev-parse", "HEAD"]) is None


# --- collecting and flattening ------------------------------------------------------------


def test_collect_metadata_records_the_code_the_sops_the_models_and_the_prompts(
    tmp_path: Path,
) -> None:
    git = FakeGit(status=" M src/freight_agent/cli.py\n")

    found = collect_metadata(
        created_at=CREATED,
        suite="core",
        k=3,
        cost_cap_usd=0.25,
        sop_version="sop-v1",
        models_config=MODELS,
        prompts=PROMPTS,
        repo=tmp_path,
        git=git,
    )

    assert (found.created_at, found.suite, found.k, found.cost_cap_usd) == (
        CREATED,
        "core",
        3,
        0.25,
    )
    assert (found.git_sha, found.git_dirty, found.sop_version) == ("abc1234", True, "sop-v1")
    assert git.calls[0][0] == tmp_path
    config = load_models_config(MODELS)
    assert found.models == {
        **{role.value: list(c.models) for role, c in config.roles.items()},
        "decision_gate": ["typesafe/jev-1.14"],
    }
    assert found.models["gate"][0] == "openai/gpt-4.1-nano"
    assert found.prompts == PROMPTS.versions()
    assert found.python is not None


def test_a_broken_models_file_is_recorded_as_unknown(tmp_path: Path) -> None:
    broken = tmp_path / "models.yaml"
    broken.write_text("roles: [not, a, mapping]\n")

    found = collect(models_config=broken, repo=tmp_path, git=FakeGit(sha=None))

    assert found.models == {}
    assert (found.git_sha, found.git_dirty, found.sop_version) == (None, None, None)
    assert found.prompts == PROMPTS.versions()


def test_for_langsmith_flattens_the_models_and_the_prompts() -> None:
    found = RunMetadata(
        created_at=CREATED,
        suite="core",
        k=2,
        cost_cap_usd=0.5,
        git_sha="abc1234",
        git_dirty=False,
        models={"gate": ["openai/gpt-4.1-nano"], "agent": ["openai/gpt-oss-120b", "x/y"]},
        prompts={"judge": "111", "gate": "222"},
        prompt_refs={"judge": "freight-agent-judge:prod", "gate": "freight-agent-gate:prod"},
    )

    assert found.for_langsmith() == {
        "created_at": "2026-10-20T01:30:00Z",
        "suite": "core",
        "k": 2,
        "cost_cap_usd": 0.5,
        "git_sha": "abc1234",
        "git_dirty": False,
        "sop_version": None,
        "gate_engine": None,
        "gate_act_threshold": None,
        "python": None,
        "model_gate": "openai/gpt-4.1-nano",
        "model_agent": "openai/gpt-oss-120b, x/y",
        "prompt_judge": "111",
        "prompt_gate": "222",
        "prompt_ref_judge": "freight-agent-judge:prod",
        "prompt_ref_gate": "freight-agent-gate:prod",
    }


# --- the gate's engine ------------------------------------------------------------------


def test_the_gate_engine_is_recorded_and_its_threshold_only_on_jev() -> None:
    jev = collect(gate_engine="jev", gate_act_threshold=0.2).for_langsmith()
    llm = collect(gate_engine="llm", gate_act_threshold=0.2).for_langsmith()

    assert (jev["gate_engine"], jev["gate_act_threshold"]) == ("jev", 0.2)
    assert jev["model_decision_gate"] == "typesafe/jev-1.14"
    assert (llm["gate_engine"], llm["gate_act_threshold"]) == ("llm", None)
