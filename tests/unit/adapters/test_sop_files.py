import shutil
from pathlib import Path

import pytest

from freight_agent.adapters.sop.files import FileSopRepository, SopFormatError, read_catalog
from freight_agent.domain.broker import standard_broker
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import MergeMode, SopLayer, SopSection

FIXTURES = Path(__file__).parent / "sop_fixtures"


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "sops"
    shutil.copytree(FIXTURES, root)
    return root


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _by_source(sections: list[SopSection]) -> dict[str, SopSection]:
    return {s.source: s for s in sections}


@pytest.mark.unit
async def test_round_trip_gives_each_file_its_layer_client_and_load_type() -> None:
    repo = FileSopRepository(FIXTURES)

    sections = await repo.sections_for_client("keystone")

    shape = [(s.source, s.section_id, s.layer, s.client_id, s.load_type) for s in sections]
    assert shape == [
        (
            "clients/keystone/delay_risk_high.md",
            "delay_risk_high",
            SopLayer.CLIENT,
            "keystone",
            None,
        ),
        (
            "clients/keystone/types/reefer/driver_departed.md",
            "driver_departed",
            SopLayer.LOAD_TYPE,
            "keystone",
            "reefer",
        ),
        ("standard/communication_rules.md", "communication_rules", SopLayer.STANDARD, None, None),
        ("standard/delay_risk_high.md", "delay_risk_high", SopLayer.STANDARD, None, None),
        ("standard/driver_departed.md", "driver_departed", SopLayer.STANDARD, None, None),
    ]


@pytest.mark.unit
async def test_front_matter_and_body_become_the_section() -> None:
    sections = _by_source(await FileSopRepository(FIXTURES).sections_for_client("northline"))

    standard = sections["standard/delay_risk_high.md"]
    assert standard.title == "Delay risk changes to high"
    assert standard.description == "Ask the driver for an ETA and follow up until one arrives."
    assert standard.intents == (Intent.DELAY_RISK_HIGH, Intent.ETA_FOLLOW_UP_DUE)
    assert standard.stages == (Stage.TO_PICKUP, Stage.TO_DELIVERY)
    assert standard.mode is MergeMode.REPLACE
    assert standard.body == "SMS the driver for the ETA. No ETA after 30 minutes: SMS again."

    rules = sections["standard/communication_rules.md"]
    assert rules.intents == ()
    assert rules.stages == ()
    assert rules.body.startswith("Write short")
    assert rules.body.endswith("Never share rates or prices.")


@pytest.mark.unit
async def test_an_extend_without_title_or_description_still_loads() -> None:
    sections = _by_source(await FileSopRepository(FIXTURES).sections_for_client("keystone"))

    reefer = sections["clients/keystone/types/reefer/driver_departed.md"]
    assert reefer.mode is MergeMode.EXTEND
    assert reefer.title == "driver_departed"
    assert reefer.description == ""
    assert reefer.intents == ()


@pytest.mark.unit
async def test_a_client_sees_standard_and_only_its_own_sections() -> None:
    repo = FileSopRepository(FIXTURES)

    northline = {s.source for s in await repo.sections_for_client("northline")}
    unknown = {s.source for s in await repo.sections_for_client("acme")}

    assert northline == {
        "clients/northline/delay_risk_high.md",
        "standard/communication_rules.md",
        "standard/delay_risk_high.md",
        "standard/driver_departed.md",
    }
    assert unknown == {
        "standard/communication_rules.md",
        "standard/delay_risk_high.md",
        "standard/driver_departed.md",
    }


@pytest.mark.unit
async def test_mode_is_ignored_in_standard(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "standard/escalation.md", "---\nid: escalation\nmode: extend\n---\nCall a Hero.")

    sections = _by_source(await FileSopRepository(root).sections_for_client("acme"))

    assert sections["standard/escalation.md"].mode is MergeMode.REPLACE


@pytest.mark.unit
async def test_a_single_intent_or_stage_does_not_need_brackets(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(
        root,
        "standard/documents.md",
        "---\nid: documents\nintents: document\nstages: at_pickup\n---\nReview it.",
    )

    sections = _by_source(await FileSopRepository(root).sections_for_client("acme"))

    assert sections["standard/documents.md"].intents == (Intent.DOCUMENT,)
    assert sections["standard/documents.md"].stages == (Stage.AT_PICKUP,)


@pytest.mark.unit
async def test_readme_files_are_not_sections(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/README.md", "Notes about Acme, no front matter.")

    sources = {s.source for s in await FileSopRepository(root).sections_for_client("acme")}

    assert not any(source.endswith("README.md") for source in sources)


@pytest.mark.unit
def test_version_is_a_short_stable_hash() -> None:
    first = FileSopRepository(FIXTURES).version()
    second = FileSopRepository(FIXTURES).version()

    assert first == second
    assert len(first) == 12
    int(first, 16)


@pytest.mark.unit
def test_version_changes_when_one_file_changes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    repo = FileSopRepository(root)
    before = repo.version()

    path = root / "clients/keystone/delay_risk_high.md"
    path.write_text(path.read_text() + "\nAdded a line.\n")

    assert repo.version() != before


@pytest.mark.unit
def test_version_changes_when_a_file_moves(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    repo = FileSopRepository(root)
    before = repo.version()

    (root / "clients/keystone/delay_risk_high.md").rename(root / "clients/keystone/delay.md")

    assert repo.version() != before


_BROKEN = [
    pytest.param("no header here", "no header", id="missing-front-matter"),
    pytest.param("---\nid: x\nbody without a closing line", "not closed", id="unclosed"),
    pytest.param("---\nid: [x\n---\nbody", "could not be read", id="invalid-yaml"),
    pytest.param("---\n- id\n---\nbody", "name: value", id="not-a-mapping"),
    pytest.param("---\ntitle: X\n---\nbody", "no `id`", id="missing-id"),
    pytest.param("---\nid: Delay-Risk\n---\nbody", "is not valid", id="bad-id"),
    pytest.param("---\nid: x\nintents: [late]\n---\nbody", "'late'", id="unknown-intent"),
    pytest.param("---\nid: x\nstages: [parked]\n---\nbody", "'parked'", id="unknown-stage"),
    pytest.param("---\nid: x\nmode: merge\n---\nbody", "`mode: merge`", id="bad-mode"),
    pytest.param("---\nid: x\nintent: [question]\n---\nbody", "`intent`", id="unknown-key"),
    pytest.param("---\nid: x\n---\n\n", "no text", id="empty-body"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("text", "problem"), _BROKEN)
async def test_a_broken_file_raises_an_error_that_names_it(
    tmp_path: Path, text: str, problem: str
) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/broken.md", text)

    with pytest.raises(SopFormatError) as caught:
        await FileSopRepository(root).sections_for_client("acme")

    assert caught.value.source == "clients/acme/broken.md"
    assert str(caught.value).startswith("clients/acme/broken.md: ")
    assert problem in caught.value.problem


@pytest.mark.unit
async def test_a_file_that_is_not_utf8_raises(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    (root / "standard/latin1.md").write_bytes("---\nid: x\n---\ncaf\xe9".encode("latin-1"))

    with pytest.raises(SopFormatError, match=r"standard/latin1\.md: .*UTF-8"):
        await FileSopRepository(root).sections_for_client("acme")


@pytest.mark.unit
async def test_a_misplaced_file_of_this_client_raises(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/keystone/reefer/seal.md", "---\nid: seal\n---\nCheck the seal.")

    with pytest.raises(SopFormatError, match=r"clients/keystone/reefer/seal\.md: .*Move it"):
        await FileSopRepository(root).sections_for_client("keystone")


@pytest.mark.unit
async def test_a_duplicate_id_in_the_same_folder_raises_naming_both_files(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "standard/delay_risk_high_copy.md", "---\nid: delay_risk_high\n---\nCopy.")

    with pytest.raises(SopFormatError) as caught:
        await FileSopRepository(root).sections_for_client("acme")

    assert caught.value.source == "standard/delay_risk_high_copy.md"
    assert "standard/delay_risk_high.md" in caught.value.problem


@pytest.mark.unit
async def test_another_clients_broken_file_does_not_block_this_client(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/broken.md", "no header here")
    _write(root, "stray.md", "no header here")

    sections = await FileSopRepository(root).sections_for_client("northline")

    assert len(sections) == 4


@pytest.mark.unit
async def test_a_missing_folder_raises(tmp_path: Path) -> None:
    repo = FileSopRepository(tmp_path / "nowhere")

    with pytest.raises(SopFormatError, match="does not exist"):
        await repo.sections_for_client("acme")
    with pytest.raises(SopFormatError, match="does not exist"):
        repo.version()


# --- a client's catalog (`clients/<id>/broker.yaml`) ----------------------------------------

DETENTION_FILE = (
    "---\nid: detention\ntitle: Detention\ndescription: Truck held.\n"
    "intents: [detention_request]\n---\n1. Write a TMS note.\n"
)
ACME_CATALOG = """\
name: Acme Freight
intents:
  - intent_id: detention_request
    title: Detention request
    description: The truck waited more than 2 hours at a facility.
    priority: 55
obligations:
  - kind: eta_request
    title: ETA request
    description: Every 20 minutes, three times.
    follow_up_minutes: 20
    max_attempts: 3
    follow_up_intent: eta_follow_up_due
    on_exhausted: [dispatcher_email, hero_task]
"""


@pytest.mark.unit
def test_a_client_without_a_catalog_runs_on_the_standard_one() -> None:
    assert FileSopRepository(FIXTURES).broker("northline") == standard_broker()
    assert read_catalog(FIXTURES, "a_new_client") == standard_broker()


@pytest.mark.unit
def test_a_client_s_catalog_is_the_standard_one_with_its_overrides(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/broker.yaml", ACME_CATALOG)

    broker = FileSopRepository(root).broker("acme")

    assert (broker.broker_id, broker.name) == ("acme", "Acme Freight")
    assert broker.intent("detention_request") is not None
    eta = broker.obligation("eta_request")
    assert eta is not None and (eta.follow_up_minutes, eta.max_attempts) == (20, 3)
    assert broker.document_types == standard_broker().document_types


@pytest.mark.unit
async def test_a_client_s_files_may_name_the_intents_of_its_catalog(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/broker.yaml", ACME_CATALOG)
    _write(root, "clients/acme/detention.md", DETENTION_FILE)
    _write(root, "clients/beta/detention.md", DETENTION_FILE)
    repo = FileSopRepository(root)

    acme = _by_source(await repo.sections_for_client("acme"))

    assert acme["clients/acme/detention.md"].intents == ("detention_request",)
    with pytest.raises(SopFormatError, match="'detention_request' in `intents` is not a known"):
        await repo.sections_for_client("beta")  # beta's catalog is the standard one


@pytest.mark.unit
async def test_a_standard_file_names_only_standard_intents(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/broker.yaml", ACME_CATALOG)
    _write(root, "standard/detention.md", DETENTION_FILE)

    with pytest.raises(SopFormatError, match=r"standard/detention\.md"):
        await FileSopRepository(root).sections_for_client("acme")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("name: [unclosed\n", "the catalog could not be read near line"),
        ("- just a list\n", "the catalog must be a list of settings"),
        ("nmae: Acme\n", "nmae: Extra inputs are not permitted"),
        (
            "obligations:\n  - kind: pod\n    title: POD\n    description: The signed POD.\n",
            "pod_follow_up_due, which is not in the catalog",
        ),
    ],
)
def test_a_broken_catalog_raises_an_error_that_names_it(
    tmp_path: Path, text: str, problem: str
) -> None:
    root = _tree(tmp_path)
    _write(root, "clients/acme/broker.yaml", text)

    with pytest.raises(SopFormatError, match=r"clients/acme/broker\.yaml") as raised:
        read_catalog(root, "acme")

    assert problem in raised.value.problem


@pytest.mark.unit
def test_the_version_changes_when_a_catalog_changes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    before = FileSopRepository(root).version()
    _write(root, "clients/acme/broker.yaml", ACME_CATALOG)
    with_catalog = FileSopRepository(root).version()
    _write(root, "clients/acme/broker.yaml", ACME_CATALOG.replace("55", "56"))

    assert len({before, with_catalog, FileSopRepository(root).version()}) == 3


@pytest.mark.unit
@pytest.mark.parametrize("client_id", ["../..", "..", "../acme", "clients/acme", ""])
def test_an_id_that_is_no_folder_name_has_no_catalog(tmp_path: Path, client_id: str) -> None:
    """The id comes from the load: it never reaches a file outside its own folder."""
    root = _tree(tmp_path)
    _write(root, "clients/acme/broker.yaml", ACME_CATALOG)
    _write(tmp_path, "broker.yaml", ACME_CATALOG)  # sops/.. holds a catalog too

    assert read_catalog(root, client_id) == standard_broker()
