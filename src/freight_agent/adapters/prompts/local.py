"""The repository copy of the prompts, `prompts/<role>.yaml`, as a prompt registry.

The unit tests read it, and so does the runtime when LANGSMITH_API_KEY is unset or
PROMPTS_SOURCE is `local`: a reviewer without access to the Hub workspace runs the same
templates, and a prompt edited here can be tried before it is pushed. Its version is
`local:<sha256 prefix>` of the template's content, which `agent prompts status` compares
with the Hub's commit.
"""

from pathlib import Path

from freight_agent.adapters.prompts.templates import PromptFile, chat_prompt, read_prompt_file
from freight_agent.ports.prompts import ChatPrompt, PromptRole

LOCAL_PREFIX = "local:"


def local_version(file: PromptFile) -> str:
    return f"{LOCAL_PREFIX}{file.content_hash()}"


class LocalPromptRegistry:
    """Implements `ports.prompts.PromptRegistry` from `prompts_dir`, read on every `get`."""

    def __init__(self, prompts_dir: Path) -> None:
        self.prompts_dir = prompts_dir

    def path(self, role: PromptRole) -> Path:
        return self.prompts_dir / f"{role.value}.yaml"

    def file(self, role: PromptRole) -> PromptFile:
        return read_prompt_file(self.path(role))

    def get(self, role: PromptRole) -> ChatPrompt:
        file = self.file(role)
        return chat_prompt(file, role=role, ref=str(self.path(role)), version=local_version(file))
