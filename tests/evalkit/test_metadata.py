"""Reproducibility metadata: what an experiment records so it can be explained later.

A changed prompt must show as a changed hash, per broker, and collecting must never fail
a run: git and the models file are read through seams (a fake git runner, a missing
file), and what cannot be read is recorded as unknown.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.llm.models_config import load_models_config
from freight_agent.domain.broker import Broker, standard_broker
from freight_agent.evalkit import metadata as metadata_module
from freight_agent.evalkit.metadata import (
    RunMetadata,
    brokers,
    collect_metadata,
    git_state,
    prompt_hashes,
    run_git,
)

ROOT = Path(__file__).resolve().parents[2]
SOPS = ROOT / "sops"
MODELS = ROOT / "config" / "models.yaml"
CREATED = datetime(2026, 10, 20, 1, 30, tzinfo=UTC)

pytestmark = pytest.mark.unit


def named(broker_id: str, **changes: object) -> Broker:
    return standard_broker().model_copy(update={"broker_id": broker_id, **changes})


class FakeGit:
    """Answers `rev-parse` and `status` like git would, and records the calls."""

    def __init__(self, sha: str | None = "abc1234\n", status: str | None = "") -> None:
        self.answers = {"rev-parse": sha, "status": status}
        self.calls: list[tuple[Path, list[str]]] = []

    def __call__(self, repo: Path, args: list[str]) -> str | None:
        self.calls.append((repo, args))
        return self.answers[args[0]]


# --- prompt hashes ------------------------------------------------------------------------


def test_every_prompt_is_hashed_per_broker_with_extract_for_brokers_with_documents() -> None:
    bare = named("bare", document_types=(), documents=())

    hashes = prompt_hashes([standard_broker(), bare])

    assert list(hashes) == [
        "judge",
        "gate:standard",
        "agent:standard",
        "extract:standard",
        "gate:bare",
        "agent:bare",
    ]
    assert all(len(h) == 12 for h in hashes.values())


def changed(name: str) -> dict[str, str]:
    """Which prompt hashes of the standard broker differ from the unchanged one's."""
    base, broker = prompt_hashes([standard_broker()]), standard_broker()
    if name == "intent":
        first = broker.intents[0].model_copy(update={"description": "Something else."})
        broker = broker.model_copy(update={"intents": (first, *broker.intents[1:])})
    elif name == "limit":
        limits = broker.limits.model_copy(update={"max_steps": 3})
        broker = broker.model_copy(update={"limits": limits})
    elif name == "document":
        first = broker.document_types[0].model_copy(update={"description": "x y z"})
        broker = broker.model_copy(update={"document_types": (first, *broker.document_types[1:])})
    after = prompt_hashes([broker])
    return {key: after[key] for key in base if after[key] != base[key]}


def test_the_hashes_are_stable() -> None:
    assert prompt_hashes([standard_broker()]) == prompt_hashes([standard_broker()])
    assert changed("nothing") == {}


@pytest.mark.parametrize(
    ("change", "prompts"),
    [
        ("intent", {"gate:standard", "agent:standard"}),  # the catalog is in both
        ("limit", {"agent:standard"}),  # the agent's budget
        ("document", {"extract:standard"}),
    ],
)
def test_a_changed_catalog_changes_the_hash_of_each_prompt_that_shows_it(
    change: str, prompts: set[str]
) -> None:
    assert set(changed(change)) == prompts


def test_a_changed_judge_prompt_changes_its_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    before = prompt_hashes([standard_broker()])
    monkeypatch.setattr(metadata_module, "JUDGE_SYSTEM_PROMPT", "Grade harshly.")

    after = prompt_hashes([standard_broker()])

    assert {k for k in before if after[k] != before[k]} == {"judge"}


async def test_the_brokers_are_the_standard_one_and_each_client_folders(tmp_path: Path) -> None:
    clients = tmp_path / "clients"
    for client in ("acme", "northline"):
        (clients / client).mkdir(parents=True)
    (clients / "README.md").write_text("not a client")
    acme = named("acme", name="Acme Freight")
    knowledge = MemorySopIndex((), brokers=[acme])

    found = await brokers(knowledge, tmp_path)

    # northline has no catalog of its own: the index answers the standard one, kept once
    assert [b.broker_id for b in found] == ["standard", "acme"]
    assert found[1] is acme
    assert [b.broker_id for b in await brokers(knowledge, tmp_path / "none")] == ["standard"]


async def test_the_shipped_sop_tree_hashes_the_standard_and_acme_prompts() -> None:
    found = await brokers(MemorySopIndex.from_files(SOPS), SOPS)

    assert {b.broker_id for b in found} == {"standard", "acme"}
    assert {"extract:standard", "extract:acme"} <= set(prompt_hashes(found))


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


async def test_collect_metadata_records_the_code_the_sops_the_models_and_the_prompts(
    tmp_path: Path,
) -> None:
    git = FakeGit(status=" M src/freight_agent/cli.py\n")
    knowledge = MemorySopIndex.from_files(SOPS)

    found = await collect_metadata(
        created_at=CREATED,
        suite="core",
        k=3,
        cost_cap_usd=0.25,
        knowledge=knowledge,
        sops_dir=SOPS,
        sop_version="sop-v1",
        models_config=MODELS,
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
    assert found.models == {role.value: [c.model, *c.fallbacks] for role, c in config.roles.items()}
    assert found.models["gate"][0] == "openai/gpt-4.1-nano"
    assert found.prompts == prompt_hashes(await brokers(knowledge, SOPS))
    assert found.python is not None


async def test_a_broken_models_file_is_recorded_as_unknown(tmp_path: Path) -> None:
    broken = tmp_path / "models.yaml"
    broken.write_text("roles: [not, a, mapping]\n")

    found = await collect_metadata(
        created_at=CREATED,
        suite="core",
        k=1,
        cost_cap_usd=0.25,
        knowledge=MemorySopIndex(()),
        sops_dir=tmp_path,
        sop_version=None,
        models_config=broken,
        repo=tmp_path,
        git=FakeGit(sha=None),
    )

    assert found.models == {}
    assert (found.git_sha, found.git_dirty, found.sop_version) == (None, None, None)
    assert set(found.prompts) == {"judge", "gate:standard", "agent:standard", "extract:standard"}


def test_for_langsmith_flattens_the_models_and_the_prompts() -> None:
    found = RunMetadata(
        created_at=CREATED,
        suite="core",
        k=2,
        cost_cap_usd=0.5,
        git_sha="abc1234",
        git_dirty=False,
        models={"gate": ["openai/gpt-4.1-nano"], "agent": ["openai/gpt-oss-120b", "x/y"]},
        prompts={"judge": "111", "gate:acme": "222"},
    )

    assert found.for_langsmith() == {
        "created_at": "2026-10-20T01:30:00Z",
        "suite": "core",
        "k": 2,
        "cost_cap_usd": 0.5,
        "git_sha": "abc1234",
        "git_dirty": False,
        "sop_version": None,
        "python": None,
        "model_gate": "openai/gpt-4.1-nano",
        "model_agent": "openai/gpt-oss-120b, x/y",
        "prompt_judge": "111",
        "prompt_gate:acme": "222",
    }
