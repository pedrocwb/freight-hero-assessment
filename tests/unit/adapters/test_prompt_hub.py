"""The prompts in the LangSmith Prompt Hub, on a fake client that keeps commits and tags
the way the Hub answered them when card W4-C was written (a tag created twice is a
conflict, a tag is moved with PATCH, a public prompt needs a workspace handle)."""

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.load import dumpd, load
from langsmith.utils import LangSmithConflictError, LangSmithNotFoundError, LangSmithUserError

from freight_agent.adapters.prompts.hub import (
    LangSmithPromptRegistry,
    PromptHub,
    short_commit,
)
from freight_agent.adapters.prompts.templates import PromptFile, to_langchain
from freight_agent.ports.prompts import ChatPrompt, PromptError, PromptRole

pytestmark = [
    pytest.mark.unit,
    pytest.mark.filterwarnings("ignore::langchain_core._api.beta_decorator.LangChainBetaWarning"),
]

GATE = PromptFile(
    name="freight-agent-gate",
    description="The triage step.",
    system="Classify.\n{{{intents}}}\n",
    user="text: {{{text}}}",
)


@dataclass
class _Commit:
    id: str
    commit_hash: str
    manifest: dict[str, Any]


@dataclass
class _Response:
    body: Any

    def json(self) -> Any:
        return self.body


@dataclass
class FakeHubClient:
    """Implements `HubClient` in memory."""

    handle: str | None = None
    commits: dict[str, list[_Commit]] = field(default_factory=dict[str, list[_Commit]])
    tags: dict[str, dict[str, str]] = field(default_factory=dict[str, dict[str, str]])
    public: dict[str, bool] = field(default_factory=dict[str, bool])
    descriptions: dict[str, str] = field(default_factory=dict[str, str])
    pushes: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    broken: Exception | None = None

    def _find(self, ref: str) -> _Commit:
        name, _, at = ref.partition(":")
        commits = self.commits.get(name)
        if not commits:
            raise LangSmithNotFoundError(f"prompt {name} not found")
        if at in ("", "latest"):
            return commits[-1]
        wanted = self.tags.get(name, {}).get(at, at)
        for commit in commits:
            if commit.commit_hash.startswith(wanted):
                return commit
        raise LangSmithNotFoundError(f"{ref} not found")

    def pull_prompt(self, prompt_identifier: str, *, skip_cache: bool = False) -> Any:
        assert skip_cache, "the SDK cache would refresh a moved tag behind the process"
        if self.broken is not None:
            raise self.broken
        commit = self._find(prompt_identifier)
        template = load(commit.manifest, allowed_objects="core")
        template.metadata = {**(template.metadata or {}), "lc_hub_commit_hash": commit.commit_hash}
        return template

    def get_prompt(self, prompt_identifier: str) -> Any:
        if prompt_identifier not in self.commits:
            return None
        return SimpleNamespace(description=self.descriptions.get(prompt_identifier, ""))

    def push_prompt(
        self,
        prompt_identifier: str,
        *,
        object: Any = None,
        is_public: bool | None = None,
        description: str | None = None,
        commit_tags: str | list[str] | None = None,
    ) -> str:
        name = prompt_identifier
        if is_public and not self.handle:
            raise LangSmithUserError(
                "Cannot create a public prompt without first\ncreating a handle"
            )
        self.pushes.append({"name": name, "is_public": is_public, "commit_tags": commit_tags})
        commits = self.commits.setdefault(name, [])
        self.public[name] = (
            bool(is_public) if is_public is not None else self.public.get(name, False)
        )
        if description is not None:
            self.descriptions[name] = description
        manifest = json.loads(json.dumps(dumpd(object)))
        digest = hashlib.sha256(f"{len(commits)}{json.dumps(manifest)}".encode()).hexdigest()
        commits.append(_Commit(id=f"id-{digest[:6]}", commit_hash=digest, manifest=manifest))
        for tag in [commit_tags] if isinstance(commit_tags, str) else commit_tags or []:
            if tag in self.tags.get(name, {}):
                raise LangSmithConflictError(f"Tag {tag} already exists on commit")
            self.tags.setdefault(name, {})[tag] = digest
        return f"https://smith/prompts/{name}/{digest[:8]}"

    def list_prompt_commits(self, prompt_identifier: str) -> Iterator[Any]:
        return iter(reversed(self.commits.get(prompt_identifier, [])))

    def request_with_retries(self, method: str, pathname: str, **kwargs: Any) -> Any:
        self.requests.append((method, pathname))
        parts = pathname.strip("/").split("/")  # repos, -, <name>, tags[, <tag>]
        name = parts[2]
        by_id = {c.id: c.commit_hash for c in self.commits.get(name, [])}
        tags = self.tags.setdefault(name, {})
        if method == "GET":
            return _Response([{"tag_name": t, "commit_hash": h} for t, h in tags.items()])
        body = kwargs["json"]
        if method == "POST":
            if body["tag_name"] in tags:
                raise LangSmithConflictError("Tag already exists on commit")
            tags[body["tag_name"]] = by_id[body["commit_id"]]
        else:
            tags[parts[4]] = by_id[body["commit_id"]]
        return _Response({})


# --- the operations of `agent prompts` ----------------------------------------------------


def test_a_first_push_creates_the_prompt_with_its_tag() -> None:
    client = FakeHubClient()
    hub = PromptHub(client)

    pushed = hub.push(GATE, tag="prod")

    assert client.pushes == [{"name": GATE.name, "is_public": None, "commit_tags": ["prod"]}]
    assert hub.tags(GATE.name) == {"prod": pushed.commit}
    assert pushed.public is False
    head = hub.pull(GATE.name)
    assert head is not None and head.commit == pushed.commit
    assert head.file.same_content(GATE)
    assert hub.description(GATE.name) == "The triage step."


def test_a_push_moves_a_tag_that_named_another_commit() -> None:
    """The Hub refuses a tag created twice: the new commit goes untagged, then the tag
    moves to it."""
    client = FakeHubClient()
    hub = PromptHub(client)
    first = hub.push(GATE, tag="prod")
    edited = GATE.model_copy(update={"user": "message: {{{text}}}"})

    second = hub.push(edited, tag="prod")

    assert client.pushes[-1]["commit_tags"] is None
    assert hub.tags(GATE.name) == {"prod": second.commit}
    assert second.commit != first.commit
    assert ("PATCH", f"/repos/-/{GATE.name}/tags/prod") in client.requests


def test_set_tag_creates_or_moves_a_tag() -> None:
    client = FakeHubClient()
    hub = PromptHub(client)
    first = hub.push(GATE)
    second = hub.push(GATE.model_copy(update={"user": "x {{{text}}}"}))

    hub.set_tag(GATE.name, "candidate", short_commit(second.commit))
    hub.set_tag(GATE.name, "candidate", first.commit)

    assert hub.tags(GATE.name) == {"candidate": first.commit}
    with pytest.raises(PromptError, match="has no commit 000000000000"):
        hub.set_tag(GATE.name, "prod", "0" * 64)


def test_public_needs_a_handle_and_without_one_the_prompt_stays_private() -> None:
    private = FakeHubClient()
    public = FakeHubClient(handle="freight")

    assert PromptHub(private).push(GATE, public=True).public is False
    assert private.public[GATE.name] is False
    assert PromptHub(public).push(GATE, public=True).public is True
    assert public.public[GATE.name] is True


def test_another_user_error_on_push_is_not_taken_for_the_handle() -> None:
    class Refusing(FakeHubClient):
        def push_prompt(self, prompt_identifier: str, **kwargs: Any) -> str:
            raise LangSmithUserError("Cannot create a prompt for another tenant.")

    with pytest.raises(LangSmithUserError, match="another tenant"):
        PromptHub(Refusing()).push(GATE, public=True)


def test_pulling_what_does_not_exist_is_none() -> None:
    hub = PromptHub(FakeHubClient())

    assert hub.pull("freight-agent-gate:prod") is None
    assert hub.exists(GATE.name) is False
    assert hub.description(GATE.name) == ""


def test_a_hub_answer_without_a_commit_is_refused() -> None:
    class NoCommit(FakeHubClient):
        def pull_prompt(self, prompt_identifier: str, *, skip_cache: bool = False) -> Any:
            return to_langchain(GATE)

    with pytest.raises(PromptError, match="without a commit hash"):
        PromptHub(NoCommit()).pull(GATE.name)


# --- the registry the runtime resolves its pins through ------------------------------------


def registry(
    client: FakeHubClient, cache: dict[tuple[PromptRole, str], ChatPrompt] | None = None
) -> LangSmithPromptRegistry:
    pins = {role: f"freight-agent-{role.value}:prod" for role in PromptRole}
    return LangSmithPromptRegistry(PromptHub(client), pins, cache={} if cache is None else cache)


def test_a_pin_resolves_to_its_commit_and_is_pulled_once() -> None:
    client = FakeHubClient()
    pushed = PromptHub(client).push(GATE, tag="prod")
    cache: dict[tuple[PromptRole, str], ChatPrompt] = {}
    hub = registry(client, cache)

    found = hub.get(PromptRole.GATE)
    PromptHub(client).push(GATE.model_copy(update={"user": "x {{{text}}}"}), tag="prod")

    assert (found.role, found.ref, found.version) == (
        PromptRole.GATE,
        "freight-agent-gate:prod",
        short_commit(pushed.commit),
    )
    assert found.user.format({"text": "ok"}) == "text: ok"
    # The tag moved, and this process keeps what it resolved; a new one reads the move.
    assert hub.get(PromptRole.GATE) is found
    assert registry(client).get(PromptRole.GATE).version != found.version


def test_a_pin_the_hub_does_not_have_says_how_to_publish_it() -> None:
    with pytest.raises(PromptError, match=r"agent prompts push --tag prod.*PROMPTS_SOURCE=local"):
        registry(FakeHubClient()).get(PromptRole.GATE)


def test_a_hub_that_cannot_answer_is_a_prompt_error() -> None:
    client = FakeHubClient(broken=ConnectionError("no route to host"))

    with pytest.raises(PromptError, match=r"could not be pulled.*ConnectionError: no route"):
        registry(client).get(PromptRole.GATE)
