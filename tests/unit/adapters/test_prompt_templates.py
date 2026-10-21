"""The prompt templates as LangChain chat templates, their repository copy, and the pins.

What the runtime renders is LangChain's mustache over the repository copy or a Hub
commit, so these tests pin the parts a bad edit would break: unescaped variables only,
two messages, the tool descriptions kept in the metadata, a YAML copy that `pull` writes
back byte for byte, and the pins of `config/prompts.yaml`.
"""

import json
from pathlib import Path
from typing import get_args

import pytest
from langchain_core.load import dumpd, load
from langchain_core.prompts import ChatPromptTemplate

from freight_agent.adapters.prompts.local import LocalPromptRegistry, local_version
from freight_agent.adapters.prompts.pins import at_tag, load_pins, prompt_name
from freight_agent.adapters.prompts.templates import (
    PromptFile,
    chat_prompt,
    dump_prompt_file,
    from_langchain,
    read_prompt_file,
    to_langchain,
    write_prompt_file,
)
from freight_agent.domain.broker import FieldKind
from freight_agent.interfaces.prompts import PromptError, PromptRole
from tests.prompts import PROMPTS_DIR

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
FILE = PromptFile(
    name="freight-agent-test",
    description="A test prompt.",
    system='Classify. {"json": true}\n{{{intents}}}\n',
    user="text: {{{text}}}",
    tools={"finish": "Call it once."},
)


# --- rendering ----------------------------------------------------------------------------


def test_variables_are_inserted_unescaped_and_braces_in_the_text_need_no_escaping() -> None:
    rendered = chat_prompt(FILE, role=PromptRole.GATE, ref="r", version="v1")

    assert rendered.system.variables == frozenset({"intents"})
    assert rendered.user.variables == frozenset({"text"})
    assert rendered.system.format({"intents": '- a: "x" <y> & z'}) == (
        'Classify. {"json": true}\n- a: "x" <y> & z\n'
    )
    assert rendered.user.format({"text": '"{{not a tag}}"', "extra": "ignored"}) == (
        'text: "{{not a tag}}"'
    )
    assert (rendered.name, rendered.ref, rendered.version) == ("freight-agent-test", "r", "v1")
    assert rendered.tools == {"finish": "Call it once."}
    assert rendered.describe() == "r (v1)"


def test_a_variable_without_a_value_is_an_error_not_an_empty_block() -> None:
    rendered = chat_prompt(FILE, role=PromptRole.GATE, ref="freight-agent-gate:prod", version="v")

    with pytest.raises(PromptError, match=r"freight-agent-gate:prod \(user\) names text"):
        rendered.user.format({})


def test_an_escaped_variable_is_refused_since_it_would_html_escape_the_quotes() -> None:
    escaped = FILE.model_copy(update={"user": "text: {{text}}"})

    with pytest.raises(PromptError, match=r"write \{\{\{text\}\}\} for the variable text"):
        to_langchain(escaped)


@pytest.mark.parametrize("template", ["{{#open}}x{{/open}}", "{{^open}}x{{/open}}", "{{>part}}"])
def test_sections_and_partials_are_refused(template: str) -> None:
    with pytest.raises(PromptError, match="is not supported"):
        to_langchain(FILE.model_copy(update={"system": template}))


def test_broken_mustache_is_refused() -> None:
    with pytest.raises(PromptError, match="is not valid mustache"):
        to_langchain(FILE.model_copy(update={"system": "{{#open}} never closed"}))


# --- LangChain and the Hub's manifest -----------------------------------------------------


def test_the_hub_template_is_a_mustache_chat_template_with_the_tools_in_its_metadata() -> None:
    template = to_langchain(FILE)

    assert template.metadata == {"tools": {"finish": "Call it once."}}
    assert [type(m).__name__ for m in template.messages] == [
        "SystemMessagePromptTemplate",
        "HumanMessagePromptTemplate",
    ]
    assert to_langchain(FILE.model_copy(update={"tools": {}})).metadata is None


@pytest.mark.filterwarnings("ignore::langchain_core._api.beta_decorator.LangChainBetaWarning")
def test_a_template_survives_the_manifest_the_hub_stores() -> None:
    """`push_prompt` sends `dumpd(template)`; `pull_prompt` loads it back."""
    manifest = json.loads(json.dumps(dumpd(to_langchain(FILE))))

    loaded = load(manifest, allowed_objects="core")
    back = from_langchain(loaded, name=FILE.name, description=FILE.description)

    assert back == FILE
    assert back.content_hash() == FILE.content_hash()


def test_from_langchain_refuses_anything_but_system_then_human() -> None:
    wrong_order = ChatPromptTemplate.from_messages(
        [("human", "x"), ("system", "y")], template_format="mustache"
    )
    three = ChatPromptTemplate.from_messages(
        [("system", "x"), ("human", "y"), ("ai", "z")], template_format="mustache"
    )
    fstring = ChatPromptTemplate.from_messages([("system", "x {a}"), ("human", "y")])

    for template in (wrong_order, three):
        with pytest.raises(PromptError, match="two messages, system then human"):
            from_langchain(template, name="p")
    with pytest.raises(PromptError, match="must be a mustache template"):
        from_langchain(fstring, name="p")
    with pytest.raises(PromptError, match="not a chat prompt template"):
        from_langchain("a string", name="p")


def test_from_langchain_refuses_tools_that_are_not_text() -> None:
    template = to_langchain(FILE)
    template.metadata = {"tools": {"finish": 3}}

    with pytest.raises(PromptError, match="must map tool names to text"):
        from_langchain(template, name="p")


def test_the_content_hash_is_the_messages_and_the_tools_not_the_name() -> None:
    renamed = FILE.model_copy(update={"name": "other", "description": "Other."})
    edited = FILE.model_copy(update={"user": "text: {{{text}}}!"})

    assert renamed.content_hash() == FILE.content_hash() and renamed.same_content(FILE)
    assert edited.content_hash() != FILE.content_hash() and not edited.same_content(FILE)
    assert len(FILE.content_hash()) == 12


# --- the repository copy ------------------------------------------------------------------


def test_the_yaml_copy_reads_back_as_written(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"

    assert write_prompt_file(path, FILE, PromptRole.AGENT) is True
    assert write_prompt_file(path, FILE, PromptRole.AGENT) is False  # same text: untouched
    text = path.read_text(encoding="utf-8")

    assert read_prompt_file(path) == FILE
    assert text.startswith("# The agent prompt: the repository copy of the LangSmith Hub prompt")
    assert "system: |\n  Classify." in text  # a literal block, as the prompt reads
    assert "user: 'text: {{{text}}}'" in text


@pytest.mark.parametrize("role", list(PromptRole))
def test_each_shipped_copy_is_exactly_what_pull_would_write(role: PromptRole) -> None:
    """So `agent prompts pull` of the pushed version leaves git clean."""
    path = PROMPTS_DIR / f"{role.value}.yaml"
    file = read_prompt_file(path)

    assert dump_prompt_file(file, role) == path.read_text(encoding="utf-8")
    assert file.name == f"freight-agent-{role.value}"
    assert file.description


def test_a_missing_or_broken_copy_is_a_prompt_error(tmp_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text("name: x\nsystem: [not, text]\n", encoding="utf-8")
    unreadable = tmp_path / "unreadable.yaml"
    unreadable.write_text("name: [x\n", encoding="utf-8")

    with pytest.raises(PromptError, match="does not exist"):
        read_prompt_file(tmp_path / "none.yaml")
    with pytest.raises(PromptError, match=r"is not valid: system: .*; user: Field required"):
        read_prompt_file(broken)
    with pytest.raises(PromptError, match="cannot be read"):
        read_prompt_file(unreadable)


def test_the_local_registry_versions_each_copy_by_its_content(tmp_path: Path) -> None:
    write_prompt_file(tmp_path / "agent.yaml", FILE, PromptRole.AGENT)
    registry = LocalPromptRegistry(tmp_path)

    found = registry.get(PromptRole.AGENT)

    assert found.version == local_version(FILE) == f"local:{FILE.content_hash()}"
    assert (found.role, found.name, found.ref) == (
        PromptRole.AGENT,
        "freight-agent-test",
        str(tmp_path / "agent.yaml"),
    )
    with pytest.raises(PromptError, match=r"gate\.yaml does not exist"):
        registry.get(PromptRole.GATE)


def test_the_extract_template_says_how_to_write_every_field_kind() -> None:
    system = read_prompt_file(PROMPTS_DIR / "extract.yaml").system

    for kind in get_args(FieldKind):
        assert f"\n- {kind}: " in system


# --- pins ---------------------------------------------------------------------------------


def test_the_shipped_pins_follow_prod() -> None:
    pins = load_pins(ROOT / "config" / "prompts.yaml")

    assert pins == {role: f"freight-agent-{role.value}:prod" for role in PromptRole}


def test_a_tag_runs_every_role_at_it(tmp_path: Path) -> None:
    path = tmp_path / "prompts.yaml"
    path.write_text(
        "gate: freight-agent-gate:3f2a9c41d0be\nagent: freight-agent-agent:prod\n"
        "extract: freight-agent-extract\njudge: freight-agent-judge:prod\n"
        "gate_decision: freight-agent-gate_decision:prod\n",
        encoding="utf-8",
    )

    assert load_pins(path)[PromptRole.GATE] == "freight-agent-gate:3f2a9c41d0be"
    assert load_pins(path)[PromptRole.EXTRACT] == "freight-agent-extract"
    assert set(load_pins(path, tag="candidate").values()) == {
        f"freight-agent-{role.value}:candidate" for role in PromptRole
    }
    assert prompt_name("owner/freight-agent-gate:prod") == "freight-agent-gate"
    assert at_tag("freight-agent-gate:3f2a9c41d0be", "latest") == "freight-agent-gate:latest"


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (
            "gate: a:prod\n",
            "must pin exactly the roles .*; missing: agent, extract, gate_decision, judge",
        ),
        (
            "gate: a\nagent: b\nextract: c\njudge: d\ngate_decision: f\ndriver: e\n",
            "must pin exactly the roles .*; unknown: driver",
        ),
        (
            "gate: a\nagent: 3\nextract: c\njudge: d\ngate_decision: f\n",
            "agent must be a reference",
        ),
        ("- gate\n", "must map each role to a Hub reference"),
        ("gate: [a\n", "cannot be read"),
    ],
)
def test_bad_pins_are_refused(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "prompts.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(PromptError, match=error):
        load_pins(path)


def test_missing_pins_say_which_setting_to_change(tmp_path: Path) -> None:
    with pytest.raises(PromptError, match="PROMPTS_CONFIG"):
        load_pins(tmp_path / "none.yaml")
