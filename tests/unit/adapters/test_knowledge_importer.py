"""`agent sop import`: a `sops/` tree published into the SOP store, idempotently.

Runs on the memory store.
"""

import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.adapters.clock import VirtualClock
from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.adapters.sop.files import load_tree
from freight_agent.adapters.sop.importer import ImportedLayer, SopImporter, SopImportError, layers
from freight_agent.domain.broker import IntentDef, Rollout, new_broker
from freight_agent.domain.sop import SopLayer, version_hash
from freight_agent.ports.knowledge import Embedder
from freight_agent.ports.repositories import UnitOfWorkFactory
from tests.contract.uow_factories import MAX_QUEUE_ATTEMPTS, open_memory

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[3]
SOPS = ROOT / "sops"
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


@pytest.fixture
async def uow_factory() -> AsyncIterator[UnitOfWorkFactory]:
    async with open_memory(max_queue_attempts=MAX_QUEUE_ATTEMPTS) as new_uow:
        yield new_uow


def importer(uow_factory: UnitOfWorkFactory, embedder: Embedder | None = None) -> SopImporter:
    return SopImporter(uow_factory, clock=VirtualClock(T0), embedder=embedder)


def tree(tmp_path: Path) -> Path:
    root = tmp_path / "sops"
    shutil.copytree(SOPS, root)
    return root


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


DETENTION = (
    "---\nid: detention\ntitle: Detention\ndescription: Truck held.\n"
    "intents: [detention_request]\n---\n1. Write a TMS note.\n"
)


async def test_the_seed_tree_publishes_one_version_per_layer(
    uow_factory: UnitOfWorkFactory,
) -> None:
    imported = await importer(uow_factory).import_tree(SOPS)

    assert [(i.broker_id, i.changed) for i in imported] == [
        (None, True),
        ("keystone", True),
        ("northline", True),
    ]
    grouped = layers(load_tree(SOPS))
    async with uow_factory() as uow:
        for layer in imported:
            published = await uow.sops.published(layer.broker_id)
            assert published is not None
            assert published.sections == grouped[layer.broker_id]
            assert published.version == layer.version == version_hash(grouped[layer.broker_id])
            assert published.published_by == "agent sop import"
        keystone = await uow.sops.published("keystone")
    assert keystone is not None
    assert {(s.layer, s.load_type) for s in keystone.sections} == {
        (SopLayer.CLIENT, None),
        (SopLayer.LOAD_TYPE, "reefer"),
    }


async def test_a_new_broker_is_created_from_the_standard_catalog(
    uow_factory: UnitOfWorkFactory,
) -> None:
    imported = await importer(uow_factory).import_tree(SOPS)

    async with uow_factory() as uow:
        northline = await uow.brokers.get("northline")
    assert northline is not None
    assert northline.name == "Northline"
    assert northline.sop_version == imported[2].version
    assert (northline.config_version, northline.rollout) == (1, Rollout())
    assert northline.intents == new_broker("northline", "x").intents


async def test_importing_the_same_tree_again_changes_nothing(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first = await importer(uow_factory).import_tree(SOPS)

    again = await importer(uow_factory).import_tree(SOPS)

    assert [i.version for i in again] == [i.version for i in first]
    assert not any(i.changed for i in again)
    async with uow_factory() as uow:
        assert len(await uow.sops.versions(None)) == 1
        assert len(await uow.brokers.versions("keystone")) == 1


async def test_an_edited_layer_publishes_a_new_version_and_bumps_its_broker(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    root = tree(tmp_path)
    first = await importer(uow_factory).import_tree(root)
    path = root / "clients" / "keystone" / "delay_risk_high.md"
    path.write_text(path.read_text(encoding="utf-8") + "\nOne more line.\n", encoding="utf-8")

    again = await importer(uow_factory).import_tree(root)

    assert [i.changed for i in again] == [False, True, False]
    assert again[1].version != first[1].version
    async with uow_factory() as uow:
        keystone = await uow.brokers.get("keystone")
        assert [v.version for v in await uow.sops.versions("keystone")] == [
            first[1].version,
            again[1].version,
        ]
    assert keystone is not None
    assert (keystone.sop_version, keystone.config_version) == (again[1].version, 2)
    assert keystone.rollout == Rollout(percent=100, previous_version=1)


async def test_one_broker_can_be_imported_alone(uow_factory: UnitOfWorkFactory) -> None:
    imported = await importer(uow_factory).import_tree(SOPS, broker="northline")
    standard = await importer(uow_factory).import_tree(SOPS, broker="standard")

    assert [i.broker_id for i in imported] == ["northline"]
    assert [i.broker_id for i in standard] == [None]
    async with uow_factory() as uow:
        assert await uow.sops.published("keystone") is None


async def test_a_broker_with_no_files_is_an_error(uow_factory: UnitOfWorkFactory) -> None:
    with pytest.raises(SopImportError, match="no SOP files for acme"):
        await importer(uow_factory).import_tree(SOPS, broker="acme")


async def test_a_broker_s_files_may_name_its_own_intents(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    root = tree(tmp_path)
    write(root, "clients/acme/detention.md", DETENTION)

    with pytest.raises(SopImportError, match="'detention_request' in `intents`"):
        await importer(uow_factory).import_tree(root, broker="acme")

    acme = new_broker("acme", "Acme")
    detention = IntentDef(intent_id="detention_request", title="Detention", description="d")
    async with uow_factory() as uow:
        await uow.brokers.save(acme.model_copy(update={"intents": (*acme.intents, detention)}))
        await uow.commit()
    imported = await importer(uow_factory).import_tree(root, broker="acme")

    assert [(i.broker_id, i.sections) for i in imported] == [("acme", 1)]
    async with uow_factory() as uow:
        saved = await uow.brokers.get("acme")
    assert saved is not None and saved.config_version == 2


async def test_every_problem_is_reported_and_nothing_is_written(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    root = tree(tmp_path)
    write(root, "standard/broken.md", "no header\n")
    write(root, "clients/acme/orphan.md", "---\nid: orphan\nmode: extend\n---\nMore.\n")
    write(root, "elsewhere/stray.md", "---\nid: stray\n---\nText.\n")
    write(root, "clients/acme/escalation.md", "---\nid: escalation\nintents: [late]\n---\nx\n")

    with pytest.raises(SopImportError) as raised:
        await importer(uow_factory).import_tree(root)

    files = [issue.file for issue in raised.value.issues]
    assert files == sorted(files)
    assert set(files) == {
        "clients/acme/escalation.md",
        "clients/acme/orphan.md",
        "elsewhere/stray.md",
        "standard/broken.md",
    }
    assert "standard/broken.md" in str(raised.value)
    async with uow_factory() as uow:
        assert await uow.sops.published(None) is None


async def test_a_missing_folder_is_an_import_error(
    uow_factory: UnitOfWorkFactory, tmp_path: Path
) -> None:
    with pytest.raises(SopImportError, match="does not exist"):
        await importer(uow_factory).import_tree(tmp_path / "nowhere")


async def test_with_an_embedder_each_version_carries_its_vectors(
    uow_factory: UnitOfWorkFactory,
) -> None:
    imported = await importer(uow_factory, embedder=FakeEmbedder(dim=1536)).import_tree(SOPS)

    assert all(i.changed for i in imported)


@pytest.mark.unit
def test_an_imported_layer_reads_as_one_line() -> None:
    assert str(ImportedLayer(None, "abc", 11, changed=True)) == (
        "standard: published version abc (11 sections)"
    )
    assert str(ImportedLayer("acme", "def", 1, changed=False)) == (
        "acme: unchanged version def (1 sections)"
    )
