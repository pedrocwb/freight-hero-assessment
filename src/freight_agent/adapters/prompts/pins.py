"""`config/prompts.yaml`: the Hub reference each role runs.

A reference is `<name>:<tag>` (follows the tag: `freight-agent-gate:prod`) or
`<name>:<commit>` (one commit, for good). Changing what production runs is moving a tag
in the Hub, or changing this file; a tag override (`PROMPTS_TAG`, `agent eval run
--prompt-tag`) runs every role at another tag, as an experiment does on a candidate.
"""

from pathlib import Path

import yaml

from freight_agent.ports.prompts import PromptError, PromptRole


def prompt_name(ref: str) -> str:
    """`freight-agent-gate` of `freight-agent-gate:prod`."""
    return ref.rsplit("/", 1)[-1].split(":", 1)[0]


def at_tag(ref: str, tag: str) -> str:
    return f"{prompt_name(ref)}:{tag}"


def load_pins(path: Path, *, tag: str | None = None) -> dict[PromptRole, str]:
    """Role -> Hub reference; with `tag`, every role at that tag instead of its pin."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PromptError(
            f"the prompt pins {path} do not exist: set PROMPTS_CONFIG, or run from the "
            "repository root"
        ) from None
    except (OSError, yaml.YAMLError) as exc:
        raise PromptError(f"the prompt pins {path} cannot be read: {exc}") from None
    if not isinstance(data, dict):
        raise PromptError(f"{path} must map each role to a Hub reference")
    roles = {r.value for r in PromptRole}
    unknown = sorted(str(k) for k in data if k not in roles)  # pyright: ignore[reportUnknownVariableType]
    missing = sorted(roles - set(data))  # pyright: ignore[reportUnknownArgumentType]
    if unknown or missing:
        raise PromptError(
            f"{path} must pin exactly the roles {', '.join(sorted(roles))}"
            + (f"; unknown: {', '.join(unknown)}" if unknown else "")
            + (f"; missing: {', '.join(missing)}" if missing else "")
        )
    pins: dict[PromptRole, str] = {}
    for role in PromptRole:
        ref = data[role.value]
        if not isinstance(ref, str) or not prompt_name(ref):
            raise PromptError(f"{path}: {role.value} must be a reference like <name>:<tag>")
        pins[role] = at_tag(ref, tag) if tag else ref.strip()
    return pins
