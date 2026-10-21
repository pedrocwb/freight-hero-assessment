"""The prompts in the LangSmith Prompt Hub: the registry the runtime resolves its pins
through, and the Hub operations of `agent prompts` (status, push, pull).

Each role's prompt is a Hub prompt of the workspace of LANGSMITH_API_KEY,
`freight-agent-<role>`, whose commits are the versions of its template. A commit tag
(`prod`, `candidate`) names the version a pin follows (`config/prompts.yaml`); a version
is recorded as its commit's first 12 hex digits, which the Hub accepts as a reference.

The registry pulls each pin once per process (`Client.pull_prompt`, past the SDK's own
cache, whose background refresh would switch a long-running process to a moved tag
behind its back) and keeps it: every run of a process reads the same versions, and
moving a tag applies when a process starts. A pin the Hub cannot serve (missing, no
network, a bad key) is a `PromptError`: the runtime does not silently run another
version than the one pinned.

Tags have no SDK method beyond creating one with a commit, so they go through the
client's REST helper: `GET`, `POST` and `PATCH` on `/repos/-/<name>/tags`.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from langsmith import Client
from langsmith.utils import LangSmithNotFoundError, LangSmithUserError

from freight_agent.adapters.prompts.pins import prompt_name
from freight_agent.adapters.prompts.templates import (
    PromptFile,
    chat_prompt,
    from_langchain,
    to_langchain,
)
from freight_agent.interfaces.prompts import ChatPrompt, PromptError, PromptRole

COMMIT_LENGTH = 12
_COMMIT_KEY = "lc_hub_commit_hash"  # what `pull_prompt` adds to the template's metadata


def short_commit(commit: str) -> str:
    return commit[:COMMIT_LENGTH]


class HubClient(Protocol):
    """The slice of `langsmith.Client` used here; a fake implements it in tests."""

    def pull_prompt(self, prompt_identifier: str, *, skip_cache: bool = False) -> Any: ...

    def get_prompt(self, prompt_identifier: str) -> Any: ...

    def push_prompt(
        self,
        prompt_identifier: str,
        *,
        object: Any = None,
        is_public: bool | None = None,
        description: str | None = None,
        commit_tags: str | list[str] | None = None,
    ) -> str: ...

    def list_prompt_commits(self, prompt_identifier: str) -> Iterator[Any]: ...

    def request_with_retries(self, method: str, pathname: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class HubCommit:
    file: PromptFile
    commit: str  # the full hash


@dataclass(frozen=True)
class Pushed:
    commit: str
    public: bool  # False also when public was asked and the workspace cannot have it


class PromptHub:
    """The Hub operations on one workspace."""

    def __init__(self, client: HubClient) -> None:
        self._client = client

    def pull(self, ref: str) -> HubCommit | None:
        """The commit `ref` names (`<name>`: the latest), or None when the prompt or its
        tag does not exist."""
        try:
            template = self._client.pull_prompt(ref, skip_cache=True)
        except LangSmithNotFoundError:
            return None
        metadata = cast(Mapping[str, Any], getattr(template, "metadata", None) or {})
        commit = metadata.get(_COMMIT_KEY)
        if not isinstance(commit, str) or not commit:
            raise PromptError(f"the Hub answered {ref} without a commit hash")
        return HubCommit(file=from_langchain(template, name=prompt_name(ref)), commit=commit)

    def exists(self, name: str) -> bool:
        return self._client.get_prompt(name) is not None

    def description(self, name: str) -> str:
        prompt = self._client.get_prompt(name)
        return str(getattr(prompt, "description", "") or "") if prompt is not None else ""

    def push(self, file: PromptFile, *, tag: str | None = None, public: bool = False) -> Pushed:
        """A new commit of `file` (the prompt is created on its first push), with `tag`:
        created with the commit (`commit_tags`), or moved to it when it named another one.
        Public needs a LangSmith Hub handle on the workspace; without one the prompt stays
        private, and `Pushed.public` says so."""
        moved = tag is not None and self.exists(file.name) and tag in self.tags(file.name)
        kwargs: dict[str, Any] = {
            "object": to_langchain(file),
            "description": file.description or None,
            "commit_tags": [tag] if tag and not moved else None,
        }
        try:
            self._client.push_prompt(file.name, is_public=True if public else None, **kwargs)
        except LangSmithUserError as exc:
            if not public or "handle" not in str(exc):
                raise
            public = False
            self._client.push_prompt(file.name, is_public=None, **kwargs)
        head = self.pull(file.name)
        if head is None:
            raise PromptError(f"{file.name} was pushed, and the Hub does not show it")
        if moved and tag is not None:
            self.set_tag(file.name, tag, head.commit)
        return Pushed(commit=head.commit, public=public)

    def tags(self, name: str) -> dict[str, str]:
        """Tag -> the full hash of the commit it names."""
        response = self._client.request_with_retries("GET", f"/repos/-/{name}/tags")
        return {str(t["tag_name"]): str(t["commit_hash"]) for t in response.json()}

    def set_tag(self, name: str, tag: str, commit: str) -> None:
        """Point `tag` at `commit`, creating the tag or moving it."""
        commit_id = self._commit_id(name, commit)
        if tag in self.tags(name):
            self._client.request_with_retries(
                "PATCH", f"/repos/-/{name}/tags/{tag}", json={"commit_id": commit_id}
            )
        else:
            self._client.request_with_retries(
                "POST", f"/repos/-/{name}/tags", json={"tag_name": tag, "commit_id": commit_id}
            )

    def _commit_id(self, name: str, commit: str) -> str:
        for listed in self._client.list_prompt_commits(name):
            if str(listed.commit_hash).startswith(commit):
                return str(listed.id)
        raise PromptError(f"{name} has no commit {short_commit(commit)}")


# Pulled pins, for the process: (role, reference) -> template.
_PROCESS_CACHE: dict[tuple[PromptRole, str], ChatPrompt] = {}


class LangSmithPromptRegistry:
    """Implements `ports.prompts.PromptRegistry`: each role's pin, pulled from the Hub
    once (`cache`: the process's by default) and kept."""

    def __init__(
        self,
        hub: PromptHub,
        pins: Mapping[PromptRole, str],
        *,
        cache: dict[tuple[PromptRole, str], ChatPrompt] | None = None,
    ) -> None:
        self._hub = hub
        self._pins = dict(pins)
        self._cache = _PROCESS_CACHE if cache is None else cache

    def get(self, role: PromptRole) -> ChatPrompt:
        ref = self._pins[role]
        cached = self._cache.get((role, ref))
        if cached is not None:
            return cached
        try:
            found = self._hub.pull(ref)
        except PromptError:
            raise
        except Exception as exc:  # the SDK, requests and LangChain's loader raise many types
            raise PromptError(
                f"the {role.value} prompt {ref} could not be pulled from the LangSmith Hub "
                f"({type(exc).__name__}: {exc}); set PROMPTS_SOURCE=local to run the "
                "repository copy"
            ) from exc
        if found is None:
            raise PromptError(
                f"the {role.value} prompt {ref} is not in the LangSmith workspace of "
                "LANGSMITH_API_KEY: publish the repository copy with `agent prompts push "
                "--tag prod`, or set PROMPTS_SOURCE=local to run it"
            )
        prompt = chat_prompt(found.file, role=role, ref=ref, version=short_commit(found.commit))
        self._cache[(role, ref)] = prompt
        return prompt


def langsmith_hub(api_key: str, api_url: str | None = None) -> PromptHub:
    """The Hub of the key's workspace, on the real SDK client (it sends nothing yet)."""
    return PromptHub(cast(HubClient, Client(api_key=api_key, api_url=api_url)))
