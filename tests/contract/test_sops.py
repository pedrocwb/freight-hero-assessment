"""Contract suite for `SopStore` (`ports/knowledge.py`), on the memory store and Postgres.

Pins down what the port leaves implicit: the standard layer (None) and a broker's layer
never mix, `"standard"` names the standard layer, documents list oldest first, a broker
has one draft, a version's sections never change, publishing a version again makes it
the published one, and nothing outlives a unit of work that did not commit.
"""

from datetime import UTC, datetime, timedelta

import pytest

from freight_agent.domain.broker import DocumentRequirement
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    DraftSection,
    MergeMode,
    ProposedIntent,
    SopDocument,
    SopDraft,
    SopLayer,
    SopSection,
    SopVersion,
    SourceRef,
)
from freight_agent.ports.knowledge import SectionEmbeddings
from freight_agent.ports.repositories import UnitOfWorkFactory

pytestmark = pytest.mark.contract

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
DIM = 1536  # the Postgres column's width


def document(document_id: str, broker_id: str | None, minute: int = 0) -> SopDocument:
    return SopDocument(
        document_id=document_id,
        broker_id=broker_id,
        filename=f"{document_id}.pdf",
        content_type="application/pdf",
        blob_ref=f"sha256:{document_id}",
        pages=3,
        sha256=document_id * 4,
        uploaded_at=T0 + timedelta(minutes=minute),
    )


def section(section_id: str, broker_id: str | None = "acme", **fields: object) -> SopSection:
    values: dict[str, object] = {
        "section_id": section_id,
        "layer": SopLayer.STANDARD if broker_id is None else SopLayer.CLIENT,
        "client_id": broker_id,
        "title": section_id.replace("_", " ").title(),
        "description": f"When {section_id} applies.",
        "intents": (Intent.DELAY_RISK_HIGH, Intent.ETA_FOLLOW_UP_DUE),
        "stages": (Stage.TO_PICKUP,),
        "body": f"1. Do {section_id}.",
        "source": f"{broker_id or 'standard'}/{section_id}.md",
        **fields,
    }
    return SopSection.model_validate(values)


def version(name: str, broker_id: str | None, *sections: SopSection, minute: int = 0) -> SopVersion:
    return SopVersion(
        broker_id=broker_id,
        version=name,
        sections=sections,
        published_at=T0 + timedelta(minutes=minute),
        published_by="editor",
        lint=("warning: section detention was not reviewed",),
    )


def draft(broker_id: str | None, body: str = "1. Write a TMS note.") -> SopDraft:
    return SopDraft(
        broker_id=broker_id,
        sections=(
            DraftSection(
                section_id="detention",
                title="Detention",
                description="Truck held more than 2 hours.",
                intents=("detention_request",),
                stages=(Stage.AT_PICKUP, Stage.AT_DELIVERY),
                body=body,
                source=SourceRef(document_id="doc_1", pages=(2,)),
            ),
        ),
        proposed_intents=(
            ProposedIntent(
                intent_id="detention_request",
                title="Detention request",
                description="Waited more than 2 hours.",
                evidence="more than 2 hours",
            ),
        ),
        proposed_requirements=(
            DocumentRequirement(type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120),
        ),
        document_ids=("doc_1",),
        updated_at=T0,
    )


def vectors(count: int, value: float = 0.5) -> SectionEmbeddings:
    return SectionEmbeddings(model="fake/token-hash", vectors=((value,) * DIM,) * count)


# --- documents ---------------------------------------------------------------------------


async def test_a_document_reads_back_as_saved(uow_factory: UnitOfWorkFactory) -> None:
    saved = document("doc_1", "acme")
    async with uow_factory() as uow:
        await uow.sops.save_document(saved)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.sops.get_document("doc_1") == saved
        assert await uow.sops.get_document("doc_2") is None


async def test_documents_list_per_layer_oldest_first(uow_factory: UnitOfWorkFactory) -> None:
    async with uow_factory() as uow:
        await uow.sops.save_document(document("doc_b", "acme", minute=5))
        await uow.sops.save_document(document("doc_a", "acme", minute=5))  # same time: by id
        await uow.sops.save_document(document("doc_c", "acme", minute=1))
        await uow.sops.save_document(document("doc_s", None))
        await uow.sops.save_document(document("doc_k", "keystone"))
        await uow.commit()

    async with uow_factory() as uow:
        acme = [d.document_id for d in await uow.sops.list_documents("acme")]
        standard = [d.document_id for d in await uow.sops.list_documents(None)]
        also_standard = [d.document_id for d in await uow.sops.list_documents("standard")]

    assert acme == ["doc_c", "doc_a", "doc_b"]
    assert standard == also_standard == ["doc_s"]


# --- drafts ------------------------------------------------------------------------------


async def test_a_layer_has_one_draft_and_a_save_replaces_it(
    uow_factory: UnitOfWorkFactory,
) -> None:
    first, second = draft("acme"), draft("acme", body="1. Write a longer TMS note.")
    async with uow_factory() as uow:
        await uow.sops.save_draft(first)
        await uow.sops.save_draft(draft(None))
        await uow.commit()
    async with uow_factory() as uow:
        assert await uow.sops.get_draft("acme") == first
        await uow.sops.save_draft(second)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.sops.get_draft("acme") == second
        assert await uow.sops.get_draft("keystone") is None
        assert await uow.sops.get_draft(None) == draft(None)
        assert await uow.sops.get_draft("standard") == draft(None)


# --- versions ----------------------------------------------------------------------------


async def test_nothing_is_published_before_the_first_publish(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        assert await uow.sops.published("acme") is None
        assert await uow.sops.versions("acme") == []
        assert await uow.sops.get_version("acme", "v1") is None


async def test_a_version_reads_back_with_its_sections_in_order(
    uow_factory: UnitOfWorkFactory,
) -> None:
    reefer = section(
        "driver_departed",
        layer=SopLayer.LOAD_TYPE,
        load_type="reefer",
        mode=MergeMode.EXTEND,
        intents=(),
        stages=(),
    )
    v1 = version("v1", "acme", section("delay_risk_high"), reefer, section("alpha"))
    async with uow_factory() as uow:
        await uow.sops.save_version(v1)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.sops.get_version("acme", "v1") == v1
        assert await uow.sops.published("acme") == v1
        assert await uow.sops.versions("acme") == [v1]
        assert await uow.sops.published("keystone") is None
        assert await uow.sops.get_version("keystone", "v1") is None


async def test_the_standard_layer_is_none_or_the_standard_id(
    uow_factory: UnitOfWorkFactory,
) -> None:
    standard = version("s1", "standard", section("escalation", None))
    async with uow_factory() as uow:
        await uow.sops.save_version(standard)
        await uow.commit()

    async with uow_factory() as uow:
        found = await uow.sops.published(None)
        assert found == standard
        assert found is not None and found.broker_id is None
        assert await uow.sops.get_version("standard", "s1") == standard
        assert await uow.sops.published("acme") is None


async def test_the_last_published_version_is_the_published_one(
    uow_factory: UnitOfWorkFactory,
) -> None:
    v1 = version("v1", "acme", section("delay_risk_high"))
    v2 = version("v2", "acme", section("delay_risk_high", body="1. Call."), minute=10)
    async with uow_factory() as uow:
        await uow.sops.save_version(v1)
        await uow.sops.save_version(v2)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.sops.published("acme") == v2
        assert await uow.sops.versions("acme") == [v1, v2]
        assert await uow.sops.get_version("acme", "v1") == v1


async def test_publishing_a_version_again_brings_it_back_and_keeps_its_sections(
    uow_factory: UnitOfWorkFactory,
) -> None:
    v1 = version("v1", "acme", section("delay_risk_high"))
    v2 = version("v2", "acme", section("delay_risk_high", body="1. Call."), minute=10)
    again = version("v1", "acme", section("delay_risk_high", body="changed"), minute=20)
    async with uow_factory() as uow:
        await uow.sops.save_version(v1)
        await uow.sops.save_version(v2)
        await uow.commit()
    async with uow_factory() as uow:
        await uow.sops.save_version(again)
        await uow.commit()

    async with uow_factory() as uow:
        published = await uow.sops.published("acme")
        assert published == again.model_copy(update={"sections": v1.sections})
        assert [v.version for v in await uow.sops.versions("acme")] == ["v1", "v2"]


async def test_a_version_saved_again_unchanged_changes_nothing(
    uow_factory: UnitOfWorkFactory,
) -> None:
    v1 = version("v1", "acme", section("delay_risk_high"))
    v2 = version("v2", "acme", section("delay_risk_high", body="1. Call."))  # same time
    async with uow_factory() as uow:
        await uow.sops.save_version(v1)
        await uow.sops.save_version(v2)
        await uow.sops.save_version(v1)
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.sops.published("acme") == v2
        assert await uow.sops.versions("acme") == [v1, v2]


async def test_a_version_may_carry_embeddings(uow_factory: UnitOfWorkFactory) -> None:
    v1 = version("v1", "acme", section("delay_risk_high"), section("alpha"))
    async with uow_factory() as uow:
        await uow.sops.save_version(v1, vectors(2))
        await uow.sops.save_version(v1, vectors(2, 0.25))  # a new model's vectors
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.sops.published("acme") == v1


async def test_an_empty_version_publishes(uow_factory: UnitOfWorkFactory) -> None:
    empty = version("v0", "acme")
    async with uow_factory() as uow:
        await uow.sops.save_version(empty, vectors(0))
        await uow.commit()

    async with uow_factory() as uow:
        assert await uow.sops.published("acme") == empty


async def test_nothing_outlives_a_unit_of_work_that_did_not_commit(
    uow_factory: UnitOfWorkFactory,
) -> None:
    async with uow_factory() as uow:
        await uow.sops.save_document(document("doc_1", "acme"))
        await uow.sops.save_draft(draft("acme"))
        await uow.sops.save_version(version("v1", "acme", section("alpha")))

    async with uow_factory() as uow:
        assert await uow.sops.get_document("doc_1") is None
        assert await uow.sops.get_draft("acme") is None
        assert await uow.sops.published("acme") is None
