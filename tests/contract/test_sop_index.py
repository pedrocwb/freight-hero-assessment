"""Contract suite for `SopIndex` (`ports/knowledge.py`) over published versions: the
memory index over a SOP store.

It serves the `sops/` tree published layer by layer (what `agent sop import` does):
the selection equals `domain.sop.resolve` on the merged layers, search finds the
section that holds the words, the version follows each publish, and a broker never
sees another broker's sections.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.adapters.knowledge.memory import StoreSopIndex
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.adapters.sop.files import load_tree
from freight_agent.adapters.sop.importer import layers
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    SopLayer,
    SopSection,
    SopVersion,
    effective_sections,
    resolve,
    search_text,
    version_hash,
)
from freight_agent.ports.knowledge import SectionEmbeddings, SopIndex
from freight_agent.ports.repositories import UnitOfWorkFactory

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[2]
SOPS = ROOT / "sops"
TREE = load_tree(SOPS)
CLIENTS = (("northline", ()), ("keystone", ("reefer",)), ("keystone", ()), ("acme", ()))
EMBEDDER = FakeEmbedder(dim=1536)
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)

Publish = Callable[[str | None, Sequence[SopSection]], Awaitable[str]]


@dataclass
class Indexed:
    index: SopIndex
    publish: Publish  # a layer's sections -> the published version


def publisher(uow_factory: UnitOfWorkFactory) -> Publish:
    clock = iter(range(1, 1000))

    async def publish(broker_id: str | None, sections: Sequence[SopSection]) -> str:
        version = version_hash(sections)
        vectors = await EMBEDDER.embed([search_text(s) for s in sections])
        async with uow_factory() as uow:
            await uow.sops.save_version(
                SopVersion(
                    broker_id=broker_id,
                    version=version,
                    sections=tuple(sections),
                    published_at=T0.replace(second=next(clock) % 60, minute=next(clock) % 60),
                    published_by="test",
                ),
                SectionEmbeddings(EMBEDDER.model_id, tuple(tuple(v) for v in vectors)),
            )
            await uow.commit()
        return version

    return publish


@pytest.fixture
async def indexed() -> AsyncIterator[Indexed]:
    """An empty index over a memory store, with the seed tree published."""
    store = MemoryStore()
    found = Indexed(StoreSopIndex(store.unit_of_work), publisher(store.unit_of_work))
    for owner, sections in layers(TREE).items():
        await found.publish(owner, sections)
    yield found


@pytest.mark.parametrize(("broker_id", "load_types"), CLIENTS)
async def test_by_applies_is_the_domain_resolve_on_the_merged_layers(
    indexed: Indexed, broker_id: str, load_types: tuple[str, ...]
) -> None:
    for intent in Intent:
        for stage in Stage:
            assert await indexed.index.by_applies(broker_id, load_types, intent, stage) == resolve(
                TREE, client_id=broker_id, load_types=load_types, intent=intent, stage=stage
            ), (intent, stage)


@pytest.mark.parametrize(("broker_id", "load_types"), CLIENTS)
async def test_visible_is_every_merged_section(
    indexed: Indexed, broker_id: str, load_types: tuple[str, ...]
) -> None:
    assert await indexed.index.visible(broker_id, load_types) == effective_sections(
        TREE, client_id=broker_id, load_types=load_types
    )


async def test_search_ranks_the_reefer_section_first(indexed: Indexed) -> None:
    hits = await indexed.index.search("keystone", ("reefer",), "trailer temperature seal")

    assert hits[0].section_id == "driver_departed"
    assert hits[0].title == "Driver departed a stop"
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    assert len({h.section_id for h in hits}) == len(hits)


async def test_search_gives_at_most_k_hits_and_nothing_for_no_words(indexed: Indexed) -> None:
    assert len(await indexed.index.search("northline", (), "driver SMS ETA Hero task", k=2)) == 2
    assert await indexed.index.search("northline", (), "driver", k=0) == ()
    assert await indexed.index.search("northline", (), " ?! ", k=5) == ()


async def test_read_gives_one_merged_section_the_broker_can_see(indexed: Indexed) -> None:
    visible = {s.section_id: s for s in await indexed.index.visible("keystone", ("reefer",))}

    found = await indexed.index.read("keystone", ("reefer",), "driver_departed")

    assert found == visible["driver_departed"]
    assert await indexed.index.read("keystone", (), "no_such_section") is None


async def test_the_version_follows_each_publish(indexed: Indexed) -> None:
    index = indexed.index
    standard = await index.version("acme")
    keystone = await index.version("keystone")

    assert standard is not None and keystone is not None
    assert keystone.startswith(f"{standard}+")
    assert await index.version("keystone") == keystone  # stable while nothing is published
    acme = await indexed.publish("acme", [_detention()])

    assert await index.version("acme") == f"{standard}+{acme}"
    assert await index.version("keystone") == keystone


async def test_a_broker_never_sees_another_broker_s_sections(indexed: Indexed) -> None:
    index = indexed.index
    await indexed.publish("acme", [_detention()])

    assert "detention" in {s.section_id for s in await index.visible("acme", ())}
    assert "detention" not in {s.section_id for s in await index.visible("northline", ())}
    assert await index.read("northline", (), "detention") is None
    hits = await index.search("northline", (), "detention facility wait hours")
    assert "detention" not in {h.section_id for h in hits}
    acme_hits = await index.search("acme", (), "detention facility wait hours")
    assert acme_hits[0].section_id == "detention"
    sop = await index.by_applies("northline", (), "detention_request", Stage.AT_DELIVERY)
    assert "detention" not in {c.section_id for c in sop.catalog}


async def test_a_new_publish_replaces_the_broker_s_layer(indexed: Indexed) -> None:
    index = indexed.index
    await indexed.publish("northline", [_detention("northline")])

    sections = {s.section_id: s for s in await index.visible("northline", ())}

    assert "detention" in sections
    assert sections["delay_risk_high"].sources == ("standard/delay_risk_high.md",)


def _detention(broker_id: str = "acme") -> SopSection:
    return SopSection(
        section_id="detention",
        layer=SopLayer.CLIENT,
        client_id=broker_id,
        title="Detention",
        description="The truck waited more than 2 hours at a facility.",
        intents=("detention_request",),
        stages=(Stage.AT_PICKUP, Stage.AT_DELIVERY),
        body="1. Write a TMS note with the facility and the wait.",
        source="acme_sop.pdf p. 2",
    )
