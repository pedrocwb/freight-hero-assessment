"""The seed SOPs in `sops/` follow the canonical table of docs/decisions.md.

The eval scenarios and the decide prompt depend on these files: which sections exist
in which layer, which intents they answer, how the client layers merge, and the
high-risk procedure of each client. A change that breaks one of these is a change to
the shared contract, so it fails here before it reaches a scenario.
"""

import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from freight_agent.adapters.sop.files import (
    FileSopRepository,
    SopFile,
    locate,
    read_sop_file,
    sop_paths,
)
from freight_agent.adapters.sop.lint import Severity, lint
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    MergeMode,
    ResolvedSection,
    effective_sections,
    resolve,
)

SOPS = Path(__file__).resolve().parents[2] / "sops"
NO_OVERRIDES = "a_new_client"  # a client with no folder: the standard procedures apply

# The canonical table: section id -> intents (empty: every intent).
CANONICAL: dict[str, frozenset[Intent]] = {
    "communication_rules": frozenset(),
    "escalation": frozenset(),
    "delay_risk_high": frozenset({Intent.DELAY_RISK_HIGH, Intent.ETA_FOLLOW_UP_DUE}),
    "delay_risk_cleared": frozenset({Intent.DELAY_RISK_LOW}),
    "eta_received": frozenset({Intent.ETA_PROVIDED}),
    "eta_deferred": frozenset({Intent.ETA_DEFERRED}),
    "driver_questions": frozenset({Intent.QUESTION}),
    "acknowledgements": frozenset({Intent.ACKNOWLEDGEMENT}),
    "status_updates": frozenset({Intent.STATUS_UPDATE}),
    "driver_departed": frozenset({Intent.DRIVER_DEPARTED}),
    "documents": frozenset({Intent.DOCUMENT}),
}

# Every file outside standard/, with the merge mode the table gives it.
OVERRIDES: dict[str, MergeMode] = {
    "clients/northline/delay_risk_high.md": MergeMode.REPLACE,
    "clients/keystone/delay_risk_high.md": MergeMode.REPLACE,
    "clients/keystone/types/reefer/driver_departed.md": MergeMode.EXTEND,
}

# The validator escalates outbound text with these, so no example in a SOP may hold one.
_PHONE = re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")

MIN_LINES, MAX_LINES = 10, 40


async def _effective(client_id: str, load_types: Sequence[str] = ()) -> dict[str, ResolvedSection]:
    sections = await FileSopRepository(SOPS).sections_for_client(client_id)
    merged = effective_sections(sections, client_id=client_id, load_types=load_types)
    return {s.section_id: s for s in merged}


def _standard(section_id: str) -> str:
    return f"standard/{section_id}.md"


def _sources() -> list[str]:
    """Collected before any test runs, so a missing tree fails the tests, not the collection."""
    return sorted(s for s, _ in sop_paths(SOPS)) if SOPS.is_dir() else []


def _files() -> dict[str, SopFile]:
    files: dict[str, SopFile] = {}
    for source, path in sop_paths(SOPS):
        location = locate(source)
        assert location is not None, f"{source} is outside the layout"
        files[source] = read_sop_file(path, location)
    return files


@pytest.mark.unit
def test_the_seed_tree_has_no_lint_errors() -> None:
    errors = [issue for issue in lint(SOPS) if issue.severity is Severity.ERROR]

    assert errors == []


@pytest.mark.unit
def test_every_canonical_section_is_in_the_layers_the_table_lists() -> None:
    files = _files()

    expected = {_standard(section_id) for section_id in CANONICAL} | set(OVERRIDES)
    assert set(files) == expected
    for source, file in files.items():
        assert file.section is not None, file.problems
        assert source.endswith(f"/{file.section_id}.md"), "name each file after its id"
    for source, mode in OVERRIDES.items():
        assert files[source].mode is mode, source


@pytest.mark.unit
async def test_each_section_answers_the_intents_of_the_table() -> None:
    for client_id, load_types in ((NO_OVERRIDES, ()), ("northline", ()), ("keystone", ("reefer",))):
        sections = await _effective(client_id, load_types)

        assert set(sections) == set(CANONICAL)
        for section_id, intents in CANONICAL.items():
            assert set(sections[section_id].intents) == intents, (client_id, section_id)


@pytest.mark.unit
def test_every_intent_but_other_has_a_section_that_names_it() -> None:
    named = set().union(*CANONICAL.values())

    assert named == set(Intent) - {Intent.OTHER}


@pytest.mark.unit
async def test_a_client_with_no_overrides_gets_the_standard_sections() -> None:
    sections = await _effective(NO_OVERRIDES, ("reefer",))

    for section_id, section in sections.items():
        assert section.sources == (_standard(section_id),)


@pytest.mark.unit
async def test_the_standard_high_risk_procedure_ends_with_a_hero_call() -> None:
    body = (await _effective(NO_OVERRIDES))["delay_risk_high"].body

    assert "30 minutes" in body
    assert "call the driver" in body
    assert "dispatcher" not in body


@pytest.mark.unit
async def test_northline_uses_its_own_high_risk_procedure() -> None:
    sections = await _effective("northline")
    section = sections["delay_risk_high"]

    assert section.sources == ("clients/northline/delay_risk_high.md",)
    assert "30 minutes" in section.body
    assert "email the dispatcher" in section.body.lower()
    assert "main thread" in section.body
    assert "Hero task" in section.body
    others = {i: s.sources for i, s in sections.items() if i != "delay_risk_high"}
    assert others == {i: (_standard(i),) for i in others}


@pytest.mark.unit
async def test_keystone_alerts_the_broker_slack_and_never_contacts_the_dispatcher() -> None:
    sections = await _effective("keystone")
    section = sections["delay_risk_high"]

    assert section.sources == ("clients/keystone/delay_risk_high.md",)
    assert "30 minutes" in section.body
    assert "Slack" in section.body
    assert "never contact the dispatcher" in section.body.lower()
    assert "second SMS" in section.body
    # Without the reefer load type, the departure procedure is the standard one.
    assert sections["driver_departed"].sources == (_standard("driver_departed"),)


@pytest.mark.unit
async def test_a_keystone_reefer_departure_adds_temperature_and_seal_to_the_standard() -> None:
    standard = (await _effective(NO_OVERRIDES))["driver_departed"]
    section = (await _effective("keystone", ("reefer",)))["driver_departed"]

    assert section.sources == (
        _standard("driver_departed"),
        "clients/keystone/types/reefer/driver_departed.md",
    )
    assert section.body.startswith(standard.body)
    addition = section.body.removeprefix(standard.body).lower()
    assert "temperature" in addition
    assert "seal number" in addition
    assert "one sms" in addition
    assert (section.title, section.intents) == (standard.title, standard.intents)


@pytest.mark.unit
async def test_the_reefer_extension_does_not_leak_to_another_client() -> None:
    section = (await _effective("northline", ("reefer",)))["driver_departed"]

    assert section.sources == (_standard("driver_departed"),)


@pytest.mark.unit
@pytest.mark.parametrize("client_id", [NO_OVERRIDES, "northline", "keystone"])
async def test_a_due_eta_follow_up_at_to_pickup_resolves_the_high_risk_procedure(
    client_id: str,
) -> None:
    sections = await FileSopRepository(SOPS).sections_for_client(client_id)

    sop = resolve(
        sections,
        client_id=client_id,
        load_types=(),
        intent=Intent.ETA_FOLLOW_UP_DUE,
        stage=Stage.TO_PICKUP,
    )

    assert set(sop.section_ids) == {"communication_rules", "delay_risk_high", "escalation"}
    assert {c.section_id for c in sop.catalog} == set(CANONICAL)


@pytest.mark.unit
@pytest.mark.parametrize("source", _sources())
def test_each_file_is_one_short_topic_with_no_contact_details(source: str) -> None:
    text = (SOPS / source).read_text(encoding="utf-8")

    assert MIN_LINES <= len(text.splitlines()) <= MAX_LINES, "one topic, 10 to 40 lines"
    assert not _PHONE.search(text), "a number in an example reads as a phone number"
    assert not _EMAIL.search(text), "name people by role, never by email address"


@pytest.mark.unit
def test_the_editor_guide_exists_and_is_not_a_section() -> None:
    guide = (SOPS / "README.md").read_text(encoding="utf-8")

    assert "README.md" not in {Path(s).name for s, _ in sop_paths(SOPS)}
    for words in ("replace", "extend", "uv run agent sop lint", "version"):
        assert words in guide
