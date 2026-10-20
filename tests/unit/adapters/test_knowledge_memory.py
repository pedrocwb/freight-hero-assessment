"""The in-memory SOP indexes (fixed sections, a `sops/` tree, a SOP store) and the fake
embedder.

Owner: card W3-KNW.
"""

import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.adapters.knowledge.memory import (
    MemorySopIndex,
    StoreSopIndex,
    compose_version,
)
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.adapters.sop.files import FileSopRepository, SopFormatError
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    SopLayer,
    SopSection,
    SopVersion,
    effective_sections,
    resolve,
    version_hash,
)

ROOT = Path(__file__).resolve().parents[3]
SOPS = ROOT / "sops"
CLIENTS = (("northline", ()), ("keystone", ("reefer",)), ("a_new_client", ()))


def _section(section_id: str, body: str, *, client_id: str | None = None) -> SopSection:
    layer = SopLayer.CLIENT if client_id else SopLayer.STANDARD
    return SopSection(
        section_id=section_id,
        layer=layer,
        client_id=client_id,
        title=section_id.replace("_", " "),
        description=f"About {section_id}.",
        body=body,
        source=f"{client_id or 'standard'}/{section_id}.md",
    )


@pytest.mark.unit
@pytest.mark.parametrize(("broker_id", "load_types"), CLIENTS)
async def test_from_files_gives_the_sections_the_file_repository_gave(
    broker_id: str, load_types: tuple[str, ...]
) -> None:
    index = MemorySopIndex.from_files(SOPS)
    sections = await FileSopRepository(SOPS).sections_for_client(broker_id)

    for intent in Intent:
        for stage in Stage:
            assert await index.by_applies(broker_id, load_types, intent, stage) == resolve(
                sections, client_id=broker_id, load_types=load_types, intent=intent, stage=stage
            )
    assert await index.visible(broker_id, load_types) == effective_sections(
        sections, client_id=broker_id, load_types=load_types
    )
    assert await index.version(broker_id) == FileSopRepository(SOPS).version()


@pytest.mark.unit
async def test_from_files_reads_the_tree_on_every_call(tmp_path: Path) -> None:
    index = MemorySopIndex.from_files(tmp_path / "sops")  # building it reads nothing

    with pytest.raises(SopFormatError):
        await index.visible("acme", ())
    (tmp_path / "sops" / "standard").mkdir(parents=True)
    (tmp_path / "sops" / "standard" / "rules.md").write_text(
        "---\nid: rules\ntitle: Rules\ndescription: d\n---\nBe brief.\n", encoding="utf-8"
    )

    assert [s.section_id for s in await index.visible("acme", ())] == ["rules"]
    assert index.content_version == FileSopRepository(tmp_path / "sops").version()


@pytest.mark.unit
async def test_another_broker_s_sections_never_show() -> None:
    index = MemorySopIndex(
        [_section("rules", "Be brief."), _section("detention", "Pay after 2 h.", client_id="acme")]
    )

    assert [s.section_id for s in await index.visible("other", ())] == ["rules"]
    assert await index.read("other", (), "detention") is None
    found = await index.read("acme", (), "detention")
    assert found is not None and found.body == "Pay after 2 h."


@pytest.mark.unit
async def test_search_ranks_by_the_words_a_section_shares_with_the_query() -> None:
    index = MemorySopIndex(
        [
            _section("reefer", "Ask for the trailer temperature and the seal number."),
            _section("seals", "Check the seal at pickup."),
            _section("eta", "Ask the driver for the ETA."),
        ]
    )

    hits = await index.search("acme", (), "trailer temperature seal", k=2)

    assert [h.section_id for h in hits] == ["reefer", "seals"]
    assert hits[0].score > hits[1].score
    assert await index.search("acme", (), "lumper", k=5) == ()
    assert await index.search("acme", (), "", k=5) == ()


@pytest.mark.unit
async def test_the_version_is_a_content_hash_unless_one_is_given() -> None:
    one = MemorySopIndex([_section("rules", "Be brief.")])
    same = MemorySopIndex([_section("rules", "Be brief.")])
    edited = MemorySopIndex([_section("rules", "Be very brief.")])

    assert await one.version("acme") == await same.version("acme")
    assert await one.version("acme") != await edited.version("acme")
    assert await MemorySopIndex([], version="v1").version("acme") == "v1"


@pytest.mark.unit
async def test_the_fake_embedder_is_deterministic_and_normalized() -> None:
    embedder = FakeEmbedder(dim=16)

    first, again, other, empty = await embedder.embed(
        ["trailer temperature", "trailer temperature", "lumper receipt", ""]
    )

    assert first == again
    assert len(first) == embedder.dim == 16
    assert math.isclose(sum(v * v for v in first), 1.0)
    assert first != other
    assert empty == [0.0] * 16
    assert embedder.model_id == "fake/token-hash"


# --- the index over a SOP store ----------------------------------------------------------

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


async def publish(store: MemoryStore, broker_id: str | None, *sections: SopSection) -> str:
    version = version_hash(sections)
    async with store.unit_of_work() as uow:
        await uow.sops.save_version(
            SopVersion(
                broker_id=broker_id,
                version=version,
                sections=sections,
                published_at=T0,
                published_by="test",
            )
        )
        await uow.commit()
    return version


@pytest.mark.unit
@pytest.mark.parametrize(("broker_id", "load_types"), CLIENTS)
async def test_a_store_with_nothing_published_reads_the_files(
    broker_id: str, load_types: tuple[str, ...]
) -> None:
    index = StoreSopIndex(MemoryStore().unit_of_work, files=SOPS)
    files = MemorySopIndex.from_files(SOPS)
    high, pickup = Intent.ETA_FOLLOW_UP_DUE, Stage.TO_PICKUP

    assert await index.visible(broker_id, load_types) == await files.visible(broker_id, load_types)
    assert await index.by_applies(broker_id, load_types, high, pickup) == await files.by_applies(
        broker_id, load_types, high, pickup
    )
    assert await index.version(broker_id) == await files.version(broker_id)


@pytest.mark.unit
async def test_a_published_broker_layer_replaces_its_files_for_that_broker_only() -> None:
    store = MemoryStore()
    index = StoreSopIndex(store.unit_of_work, files=SOPS)
    detention = _section("detention", "Pay after 2 h.", client_id="acme")
    northline = _section("delay_risk_high", "Only call.", client_id="northline")

    acme_version = await publish(store, "acme", detention)
    northline_version = await publish(store, "northline", northline)

    acme = {s.section_id: s for s in await index.visible("acme", ())}
    assert acme["detention"].body == "Pay after 2 h."
    assert acme["escalation"].sources == ("standard/escalation.md",)  # from the files
    merged = {s.section_id: s for s in await index.visible("northline", ())}
    assert merged["delay_risk_high"].body == "Only call."
    assert "detention" not in merged
    tree = FileSopRepository(SOPS).version()
    assert await index.version("acme") == f"{tree}+{acme_version}"
    assert await index.version("northline") == f"{tree}+{northline_version}"
    assert await index.version("keystone") == tree


@pytest.mark.unit
async def test_a_published_standard_layer_replaces_the_standard_files() -> None:
    store = MemoryStore()
    index = StoreSopIndex(store.unit_of_work, files=SOPS)

    standard = await publish(
        store, None, _section("rules", "Be brief."), _section("driver_departed", "Note it.")
    )

    keystone = {s.section_id: s for s in await index.visible("keystone", ("reefer",))}
    assert set(keystone) == {"rules", "driver_departed", "delay_risk_high"}
    assert keystone["driver_departed"].body.startswith("Note it.")  # the files' extend on top
    tree = FileSopRepository(SOPS).version()
    assert await index.version("keystone") == f"{standard}+{tree}"


@pytest.mark.unit
async def test_without_files_only_what_was_published_is_read() -> None:
    store = MemoryStore()
    index = StoreSopIndex(store.unit_of_work)

    assert await index.visible("acme", ()) == ()
    assert await index.version("acme") is None
    assert await index.search("acme", (), "brief") == ()
    standard = await publish(store, None, _section("rules", "Be brief."))
    acme = await publish(store, "acme", _section("detention", "Pay.", client_id="acme"))

    assert [s.section_id for s in await index.visible("acme", ())] == ["detention", "rules"]
    assert [s.section_id for s in await index.visible("standard", ())] == ["rules"]
    assert await index.version("acme") == f"{standard}+{acme}"
    assert await index.version("standard") == standard
    hits = await index.search("acme", (), "brief")
    assert [h.section_id for h in hits] == ["rules"]
    found = await index.read("acme", (), "detention")
    assert found is not None and found.body == "Pay."


@pytest.mark.unit
def test_a_version_joins_the_layers_that_have_one() -> None:
    assert compose_version("s1", "b1") == "s1+b1"
    assert compose_version("s1", None) == "s1"
    assert compose_version(None, "b1") == "b1"
    assert compose_version("t", "t") == "t"
    assert compose_version(None, None) is None
