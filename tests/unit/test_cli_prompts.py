"""`agent prompts status|push|pull`, on the fake Hub of the Hub adapter's tests.

Each test copies the repository's templates into a temporary `prompts/` and walks the
flow the docs describe: publish, edit, push a candidate, promote it to prod, and pull an
edit made in the LangSmith playground.
"""

import shutil
from pathlib import Path

import pytest
from langsmith.utils import LangSmithConnectionError
from typer.testing import CliRunner, Result

from freight_agent import cli, cli_prompts
from freight_agent.adapters.prompts.hub import PromptHub, short_commit
from freight_agent.adapters.prompts.templates import read_prompt_file
from freight_agent.settings import Settings
from tests.prompts import PROMPTS_DIR
from tests.unit.adapters.test_prompt_hub import FakeHubClient

pytestmark = [
    pytest.mark.unit,
    pytest.mark.filterwarnings("ignore::langchain_core._api.beta_decorator.LangChainBetaWarning"),
]

ROOT = Path(__file__).resolve().parents[2]
runner = CliRunner()


@pytest.fixture
def prompts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
    copy = tmp_path / "prompts"
    shutil.copytree(PROMPTS_DIR, copy)
    monkeypatch.setenv("PROMPTS_DIR", str(copy))
    monkeypatch.setenv("PROMPTS_CONFIG", str(ROOT / "config" / "prompts.yaml"))
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-test")
    return copy


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, prompts_dir: Path) -> FakeHubClient:
    fake = FakeHubClient()
    monkeypatch.setattr(cli_prompts, "hub_for", lambda settings: PromptHub(fake))
    return fake


def agent(*args: str) -> Result:
    return runner.invoke(cli.app, ["prompts", *args])


def lines(result: Result) -> list[str]:
    return result.output.strip().splitlines()


def edit_agent(prompts_dir: Path) -> None:
    path = prompts_dir / "agent.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("# Finish\n", "# Finish (check it twice)\n"),
        encoding="utf-8",
    )


def test_before_the_first_push_nothing_is_in_the_hub(client: FakeHubClient) -> None:
    result = agent("status")

    assert result.exit_code == 1
    assert lines(result)[0].split() == ["role", "pin", "commit", "latest", "prompts/<role>.yaml"]
    assert "gate     freight-agent-gate:prod     not in the Hub" in result.output


def test_push_publishes_every_template_and_status_shows_them_matching(
    client: FakeHubClient,
) -> None:
    pushed = agent("push", "--tag", "prod")
    status = agent("status")

    assert pushed.exit_code == 0, pushed.output
    assert [line.split(" pushed ")[0] for line in lines(pushed)] == [
        "gate: freight-agent-gate",
        "agent: freight-agent-agent",
        "extract: freight-agent-extract",
        "judge: freight-agent-judge",
    ]
    assert all(line.endswith(", tagged prod") for line in lines(pushed))
    assert status.exit_code == 0, status.output
    assert [line.split()[-1] for line in lines(status)[1:]] == ["matches"] * 4
    commit = client.tags["freight-agent-agent"]["prod"]
    assert f"freight-agent-agent:prod    {short_commit(commit)}" in status.output


def test_pushing_again_without_an_edit_commits_nothing(client: FakeHubClient) -> None:
    agent("push", "--tag", "prod")

    again = agent("push", "--tag", "prod")

    assert again.exit_code == 0
    assert all(", already prod" in line for line in lines(again))
    assert len(client.pushes) == 4


def test_a_candidate_is_measured_then_promoted_to_prod(
    client: FakeHubClient, prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent("push", "--tag", "prod")
    edit_agent(prompts_dir)

    candidate = agent("push", "--tag", "candidate")
    behind = agent("status")
    monkeypatch.setenv("PROMPTS_TAG", "candidate")
    at_candidate = agent("status")
    monkeypatch.delenv("PROMPTS_TAG")
    promoted = agent("push", "--tag", "prod")
    after = agent("status")

    assert "agent: freight-agent-agent pushed" in candidate.output
    assert "gate: freight-agent-gate unchanged at" in candidate.output
    assert len(client.pushes) == 5  # only the edited template got a new commit
    assert behind.exit_code == 1 and "differs (local:" in behind.output
    assert at_candidate.exit_code == 0, at_candidate.output
    assert "agent: freight-agent-agent unchanged at" in promoted.output
    assert promoted.output.count(", tagged prod") == 1  # prod moved for the agent alone
    assert after.exit_code == 0
    tags = client.tags["freight-agent-agent"]
    assert tags["prod"] == tags["candidate"]


def test_pull_writes_back_an_edit_made_in_the_playground(
    client: FakeHubClient, prompts_dir: Path
) -> None:
    agent("push", "--tag", "prod")
    path = prompts_dir / "gate.yaml"
    shipped = read_prompt_file(path)
    PromptHub(client).push(shipped.model_copy(update={"user": shipped.user + "Be brief.\n"}))

    pulled = agent("pull", "--tag", "latest")
    again = agent("pull", "--tag", "latest")

    assert pulled.exit_code == 0, pulled.output
    assert f"gate: wrote {path} from freight-agent-gate:latest" in pulled.output
    assert "agent: unchanged" in pulled.output
    assert read_prompt_file(path).user.endswith("Be brief.\n")
    assert read_prompt_file(path).description == shipped.description
    assert "gate: unchanged" in again.output


def test_push_refuses_a_template_the_code_cannot_render(
    client: FakeHubClient, prompts_dir: Path
) -> None:
    path = prompts_dir / "gate.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("{{{text}}}", "{{{text}}} {{{weather}}}"),
        encoding="utf-8",
    )

    result = agent("push")

    assert result.exit_code == 2
    assert "the user message names weather" in result.stderr
    assert client.pushes == []


def test_public_without_a_hub_handle_stays_private(client: FakeHubClient) -> None:
    result = agent("push", "--public")

    assert result.exit_code == 0
    assert all(
        "(kept private: public prompts need a LangSmith Hub handle)" in line
        for line in lines(result)
    )


def test_pulling_a_pin_the_hub_does_not_have_exits_2(client: FakeHubClient) -> None:
    result = agent("pull")

    assert result.exit_code == 2
    assert "freight-agent-gate:prod is not in the LangSmith Hub" in result.stderr


def test_every_command_needs_the_langsmith_key(
    prompts_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY")

    for command in ("status", "push", "pull"):
        result = agent(command)
        assert result.exit_code == 2
        assert "LANGSMITH_API_KEY is not set" in result.stderr


def test_a_hub_that_cannot_answer_exits_2(client: FakeHubClient) -> None:
    client.broken = LangSmithConnectionError("Connection error: no route to host")

    result = agent("status")

    assert result.exit_code == 2
    assert "no route to host" in result.stderr
