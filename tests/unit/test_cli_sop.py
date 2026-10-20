"""`agent sop import`: the command line over the importer, and its exit codes.

The memory store only checks the tree (it goes away with the command).
"""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from freight_agent import bootstrap, cli
from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.settings import Settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture(autouse=True)
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No `.env` and no shell setting of the developer: offline, in memory."""
    for name in ("LLM_MODE", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MODELS_CONFIG", str(ROOT / "config" / "models.yaml"))
    monkeypatch.setenv("LLM_MODE", "replay")
    return tmp_path


def agent(*args: str) -> Result:
    return runner.invoke(cli.app, list(args))


def test_the_seed_tree_imports_one_version_per_layer() -> None:
    result = agent("sop", "import", str(ROOT / "sops"))

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("standard: published version ")
    assert lines[0].endswith("(11 sections)")
    assert lines[1].startswith("keystone: published version ")
    assert lines[2].startswith("northline: published version ")
    assert lines[-1] == "3 of 3 layers published to memory (checked only)"


def test_one_broker_imports_alone() -> None:
    result = agent("sop", "import", str(ROOT / "sops"), "--broker", "northline")

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "1 of 1 layers published to memory (checked only)"


def test_a_tree_with_problems_imports_nothing_and_exits_1(env: Path) -> None:
    sops = env / "sops"
    shutil.copytree(ROOT / "sops", sops)
    (sops / "standard" / "broken.md").write_text("no header\n", encoding="utf-8")

    result = agent("sop", "import", str(sops))

    assert result.exit_code == 1
    assert "error: standard/broken.md: the file has no header" in result.output
    assert result.output.splitlines()[-1] == f"1 errors in {sops}: nothing was imported"


def test_a_broker_with_no_files_exits_1() -> None:
    result = agent("sop", "import", str(ROOT / "sops"), "--broker", "acme")

    assert result.exit_code == 1
    assert "there are no SOP files for acme" in result.output


def test_lint_still_checks_the_files() -> None:
    result = runner.invoke(cli.app, ["sop", "lint"], env={"SOPS_DIR": str(ROOT / "sops")})

    assert result.exit_code == 0, result.output


def test_the_embedder_comes_from_the_composition_root_once_it_has_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Settings] = []

    def build_embedder(settings: Settings) -> FakeEmbedder:
        built.append(settings)
        return FakeEmbedder(dim=1536)

    monkeypatch.setattr(bootstrap, "build_embedder", build_embedder, raising=False)

    result = agent("sop", "import", str(ROOT / "sops"))

    assert result.exit_code == 0, result.output
    assert len(built) == 1
