"""The prompt templates of the tests: the repository copy, `prompts/<role>.yaml`, read the
way the runtime reads it without LangSmith. No test calls the Hub (see `conftest.py`)."""

from pathlib import Path

from freight_agent.adapters.prompts.local import LocalPromptRegistry
from freight_agent.interfaces.prompts import Prompts

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
PROMPTS = Prompts.resolve(LocalPromptRegistry(PROMPTS_DIR))
