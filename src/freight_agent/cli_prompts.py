"""`agent prompts ...`: the prompt templates in the LangSmith Prompt Hub, registered by
`cli.py` under `prompts`.

Owner: card W4-C. A template lives in two places kept in step: the Hub, where each role
is the prompt `freight-agent-<role>`, one commit per version and tags naming the
versions that run (`prod`, `candidate`), and its repository copy, `prompts/<role>.yaml`,
which a reviewer reads in git. `config/prompts.yaml` pins the reference each role runs.

- `status`: per role, its pin, the commit the pin names, the Hub's latest commit, and
  whether the repository copy is that pinned commit's template. Exit code 1 when one is
  not (or is not in the Hub).
- `push [--tag T] [--public]`: a new commit of each template whose copy differs from the
  Hub's latest, tagged T; a template already at the latest gets T moved to it.
- `pull [--tag T]`: writes into `prompts/` the version each pin names, or every role at
  T (`--tag latest` after an edit in the LangSmith playground).

Promotion: edit a template (here or in the playground, then `pull --tag latest`),
`push --tag candidate`, measure it with `agent eval run --prompt-tag candidate`, and
`push --tag prod` to move `prod` to the measured commits (nothing new is committed).

Every command needs LANGSMITH_API_KEY and exits 2 on an error (a missing key, a Hub that
cannot answer, a broken template). `push` and `pull` refuse a template the code cannot
render (a variable it does not compute), so a version that would stop the runtime at
startup is neither published nor copied. Like `cli.py`, only typer and the settings load
at import time.
"""

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

import typer

from freight_agent.settings import MissingSetting, Settings

if TYPE_CHECKING:
    from freight_agent.adapters.prompts.hub import PromptHub
    from freight_agent.interfaces.prompts import ChatPrompt, PromptRole

prompts_app = typer.Typer(
    no_args_is_help=True, help="Version the prompt templates in the LangSmith Prompt Hub."
)

_TAG_HELP = "A commit tag: prod, candidate, or latest (the newest commit)."


def hub_for(settings: Settings) -> "PromptHub":
    """The Hub of LANGSMITH_API_KEY's workspace; the seam where tests put a fake one."""
    from freight_agent.adapters.prompts.hub import langsmith_hub

    key = settings.require_langsmith_key()
    return langsmith_hub(key.get_secret_value(), os.environ.get("LANGSMITH_ENDPOINT"))


def _checks() -> "dict[PromptRole, Callable[[ChatPrompt], ChatPrompt]]":
    """What each role's code needs of its template (the services check it the same way)."""
    from freight_agent.application.agent import check_agent_prompt
    from freight_agent.application.gate import check_gate_prompt
    from freight_agent.application.gate_decision import check_gate_decision_prompt
    from freight_agent.application.memory import check_memory_prompt
    from freight_agent.application.prompts_documents import check_extract_prompt
    from freight_agent.evalkit.judge import check_judge_prompt
    from freight_agent.interfaces.prompts import PromptRole

    return {
        PromptRole.GATE: check_gate_prompt,
        PromptRole.AGENT: check_agent_prompt,
        PromptRole.EXTRACT: check_extract_prompt,
        PromptRole.JUDGE: check_judge_prompt,
        PromptRole.GATE_DECISION: check_gate_decision_prompt,
        PromptRole.MEMORY: check_memory_prompt,
    }


def _fail(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


@prompts_app.command("status")
def status() -> None:
    """Per role: its pin, the commit it names, the Hub's latest, and whether the
    repository copy matches the pinned commit. Exit code 1 when one does not."""
    from langsmith.utils import LangSmithError

    from freight_agent.adapters.prompts.hub import short_commit
    from freight_agent.adapters.prompts.local import LocalPromptRegistry, local_version
    from freight_agent.adapters.prompts.pins import load_pins, prompt_name
    from freight_agent.interfaces.prompts import PromptError, PromptRole

    settings = Settings()
    try:
        hub = hub_for(settings)
        pins = load_pins(settings.prompts_config, tag=settings.prompts_tag)
        local = LocalPromptRegistry(settings.prompts_dir)
        rows: list[tuple[str, ...]] = []
        behind = 0
        for role in PromptRole:
            ref = pins[role]
            copy = local.file(role)
            pinned, head = hub.pull(ref), hub.pull(prompt_name(ref))
            same = pinned is not None and pinned.file.same_content(copy)
            behind += not same
            rows.append(
                (
                    role.value,
                    ref,
                    short_commit(pinned.commit) if pinned else "not in the Hub",
                    short_commit(head.commit) if head else "-",
                    _local_state(same, pinned is not None, local_version(copy)),
                )
            )
    except (MissingSetting, PromptError, LangSmithError) as exc:
        raise _fail(str(exc)) from None
    header = ("role", "pin", "commit", "latest", "prompts/<role>.yaml")
    widths = [max(len(r[i]) for r in (header, *rows)) for i in range(len(header))]
    for row in (header, *rows):
        typer.echo("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    raise typer.Exit(1 if behind else 0)


def _local_state(same: bool, pushed: bool, version: str) -> str:
    if same:
        return "matches"
    return f"differs ({version})" if pushed else f"only here ({version})"


@prompts_app.command("push")
def push(
    tag: Annotated[str | None, typer.Option(help=f"Tag the pushed versions. {_TAG_HELP}")] = None,
    public: Annotated[
        bool, typer.Option(help="Make the prompts public (the workspace needs a Hub handle).")
    ] = False,
) -> None:
    """Publish each template of prompts/ that differs from the Hub's latest commit, and
    point --tag at every role's latest commit."""
    from langsmith.utils import LangSmithError

    from freight_agent.adapters.prompts.hub import short_commit
    from freight_agent.adapters.prompts.local import LocalPromptRegistry
    from freight_agent.interfaces.prompts import PromptError, PromptRole

    settings = Settings()
    checks = _checks()
    try:
        hub = hub_for(settings)
        local = LocalPromptRegistry(settings.prompts_dir)
        for role in PromptRole:
            checks[role](local.get(role))  # a template the code cannot render stays here
            file = local.file(role)
            head = hub.pull(file.name)
            if head is None or not head.file.same_content(file):
                pushed = hub.push(file, tag=tag, public=public)
                line = f"pushed {short_commit(pushed.commit)}" + (f", tagged {tag}" if tag else "")
                if public and not pushed.public:
                    line += " (kept private: public prompts need a LangSmith Hub handle)"
            else:
                line = f"unchanged at {short_commit(head.commit)}"
                if tag and hub.tags(file.name).get(tag) != head.commit:
                    hub.set_tag(file.name, tag, head.commit)
                    line += f", tagged {tag}"
                elif tag:
                    line += f", already {tag}"
            typer.echo(f"{role.value}: {file.name} {line}")
    except (MissingSetting, PromptError, LangSmithError) as exc:
        raise _fail(str(exc)) from None


@prompts_app.command("pull")
def pull(
    tag: Annotated[
        str | None, typer.Option(help=f"Pull every role at this tag. {_TAG_HELP}")
    ] = None,
) -> None:
    """Write into prompts/ the Hub version each role's pin names (or --tag's)."""
    from langsmith.utils import LangSmithError

    from freight_agent.adapters.prompts.hub import short_commit
    from freight_agent.adapters.prompts.local import LocalPromptRegistry
    from freight_agent.adapters.prompts.pins import load_pins, prompt_name
    from freight_agent.adapters.prompts.templates import chat_prompt, write_prompt_file
    from freight_agent.interfaces.prompts import PromptError, PromptRole

    settings = Settings()
    checks = _checks()
    try:
        hub = hub_for(settings)
        pins = load_pins(settings.prompts_config, tag=tag or settings.prompts_tag)
        local = LocalPromptRegistry(settings.prompts_dir)
        for role in PromptRole:
            ref = pins[role]
            found = hub.pull(ref)
            if found is None:
                raise PromptError(f"{ref} is not in the LangSmith Hub")
            version = short_commit(found.commit)
            checks[role](chat_prompt(found.file, role=role, ref=ref, version=version))
            path = local.path(role)
            kept = local.file(role).description if path.is_file() else ""
            description = hub.description(prompt_name(ref)) or kept
            file = found.file.model_copy(update={"description": description})
            changed = write_prompt_file(path, file, role)
            state = "wrote" if changed else "unchanged"
            typer.echo(f"{role.value}: {state} {path} from {ref} ({version})")
    except (MissingSetting, PromptError, LangSmithError) as exc:
        raise _fail(str(exc)) from None
