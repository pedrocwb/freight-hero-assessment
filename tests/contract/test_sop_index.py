"""Contract suite for `SopIndex` (`interfaces/knowledge.py`) on the index the runs read: the
`sops/` tree (`MemorySopIndex.from_files`).

The selection equals `domain.sop.resolve` on the merged layers of the whole tree, search
finds the section that holds the words, the version is the tree's, a broker never sees
another broker's sections, and each broker gets its own catalog.
"""

from pathlib import Path

import pytest

from freight_agent.adapters.knowledge.memory import MemorySopIndex
from freight_agent.adapters.sop.files import FileSopRepository
from freight_agent.domain.broker import standard_broker
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import effective_sections, resolve
from freight_agent.interfaces.knowledge import SopIndex

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SOPS = ROOT / "sops"
REPOSITORY = FileSopRepository(SOPS)
CLIENTS = (("northline", ()), ("keystone", ("reefer",)), ("keystone", ()), ("acme", ()))


@pytest.fixture
def index() -> SopIndex:
    return MemorySopIndex.from_files(SOPS)


@pytest.mark.parametrize(("broker_id", "load_types"), CLIENTS)
async def test_by_applies_is_the_domain_resolve_on_the_merged_layers(
    index: SopIndex, broker_id: str, load_types: tuple[str, ...]
) -> None:
    tree = await REPOSITORY.sections_for_client(broker_id)
    for intent in Intent:
        for stage in Stage:
            assert await index.by_applies(broker_id, load_types, intent, stage) == resolve(
                tree, client_id=broker_id, load_types=load_types, intent=intent, stage=stage
            ), (intent, stage)


@pytest.mark.parametrize(("broker_id", "load_types"), CLIENTS)
async def test_visible_is_every_merged_section(
    index: SopIndex, broker_id: str, load_types: tuple[str, ...]
) -> None:
    assert await index.visible(broker_id, load_types) == effective_sections(
        await REPOSITORY.sections_for_client(broker_id), client_id=broker_id, load_types=load_types
    )


async def test_search_ranks_the_reefer_section_first(index: SopIndex) -> None:
    hits = await index.search("keystone", ("reefer",), "trailer temperature seal")

    assert hits[0].section_id == "driver_departed"
    assert hits[0].title == "Driver departed a stop"
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    assert len({h.section_id for h in hits}) == len(hits)


async def test_search_gives_at_most_k_hits_and_nothing_for_no_words(index: SopIndex) -> None:
    assert len(await index.search("northline", (), "driver SMS ETA Hero task", k=2)) == 2
    assert await index.search("northline", (), "driver", k=0) == ()
    assert await index.search("northline", (), " ?! ", k=5) == ()


async def test_read_gives_one_merged_section_the_broker_can_see(index: SopIndex) -> None:
    visible = {s.section_id: s for s in await index.visible("keystone", ("reefer",))}

    found = await index.read("keystone", ("reefer",), "driver_departed")

    assert found == visible["driver_departed"]
    assert await index.read("keystone", (), "no_such_section") is None


async def test_the_version_is_the_tree_s_for_every_broker(index: SopIndex) -> None:
    version = FileSopRepository(SOPS).version()

    assert await index.version("northline") == version
    assert await index.version("keystone") == version


async def test_a_broker_never_sees_another_broker_s_sections(index: SopIndex) -> None:
    """Acme's `detention` (sops/clients/acme/) is Acme's alone."""
    assert "detention" in {s.section_id for s in await index.visible("acme", ())}
    assert "detention" not in {s.section_id for s in await index.visible("northline", ())}
    assert await index.read("northline", (), "detention") is None
    hits = await index.search("northline", (), "detention facility wait hours")
    assert "detention" not in {h.section_id for h in hits}
    acme_hits = await index.search("acme", (), "detention facility wait hours")
    assert acme_hits[0].section_id == "detention"
    sop = await index.by_applies("northline", (), "detention_request", Stage.AT_DELIVERY)
    assert "detention" not in {c.section_id for c in sop.catalog}


async def test_each_broker_gets_its_own_catalog(index: SopIndex) -> None:
    acme = await index.broker("acme")

    assert (acme.broker_id, acme.name) == ("acme", "Acme Freight")
    assert acme.intent("detention_request") is not None
    assert await index.broker("northline") == standard_broker()
