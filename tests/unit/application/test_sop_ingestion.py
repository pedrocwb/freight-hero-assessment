"""SOP onboarding: upload, compile (a scripted `compile` model), lint and publish.

The Acme Freight fixture (`evals/fixtures/sops/`) is the broker's document; the scripted
compile answer is what its README says a compiler should find, with each section's text
cut from the document. `acme_compiled()` is shared with the API tests.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freight_agent.adapters.clock import SequentialIdGenerator, VirtualClock
from freight_agent.adapters.documents.fake_pages import FakePageReader
from freight_agent.adapters.documents.memory_blobs import MemoryBlobStore
from freight_agent.adapters.knowledge.fake_embedder import FakeEmbedder
from freight_agent.adapters.knowledge.memory import MemorySopIndex, StoreSopIndex
from freight_agent.adapters.llm.scripted import ScriptedModel
from freight_agent.adapters.persistence.memory.store import MemoryStore
from freight_agent.application.sop_ingestion import (
    CompiledDraft,
    CompiledIntent,
    CompiledObligation,
    CompiledRequirement,
    CompiledSection,
    DraftChanged,
    DraftInvalid,
    NoDraft,
    PageText,
    SopIngestion,
    SopIngestionError,
    UnknownDocument,
    catalog_problems,
    check_draft,
    draft_from_compiled,
    merge_drafts,
)
from freight_agent.domain.broker import (
    DocumentRequirement,
    ObligationDef,
    Rollout,
    new_broker,
    standard_broker,
)
from freight_agent.domain.documents import Page
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    DraftSection,
    MergeMode,
    SopDocument,
    SopDraft,
    SopLayer,
    SourceRef,
)
from freight_agent.ports.documents import PageReader
from freight_agent.ports.llm import LlmError, ModelRole

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SOPS = ROOT / "sops"
ACME_MD = ROOT / "evals" / "fixtures" / "sops" / "acme_sop.md"
T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
COMPILE, EXTRACT = ModelRole.COMPILE, ModelRole.EXTRACT


def acme_topics() -> dict[int, str]:
    """The text under each numbered `##` heading of the Acme document."""
    topics: dict[int, str] = {}
    number = 0
    for line in ACME_MD.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            number = int(line[3:].split(".", 1)[0])
            topics[number] = ""
        elif number:
            topics[number] += line + "\n"
    return {n: text.strip() for n, text in topics.items()}


def acme_compiled() -> CompiledDraft:
    """What a compiler should find in the Acme document (its README)."""
    text = acme_topics()
    late, delivery, pickup = Stage.TO_PICKUP, Stage.AT_DELIVERY, Stage.AT_PICKUP
    return CompiledDraft(
        sections=(
            CompiledSection(
                section_id="delay_risk_high",
                title="Late truck: asking the driver for an ETA",
                description="Ask the driver for the ETA every 20 minutes, three times, then "
                "email the dispatcher and open an urgent Hero task.",
                body=text[1],
                intents=(Intent.DELAY_RISK_HIGH, Intent.ETA_FOLLOW_UP_DUE),
                stages=(late, Stage.TO_DELIVERY),
                pages=(1,),
            ),
            CompiledSection(
                section_id="detention",
                title="Detention: truck held at a facility",
                description="The truck waited more than 2 hours at a facility.",
                body=text[2],
                intents=("detention_request",),
                stages=(pickup, delivery),
                pages=(1, 2),
            ),
            CompiledSection(
                section_id="proof_of_delivery",
                title="Proof of delivery (POD)",
                description="The POD is due 120 minutes after arrival; remind once, then Hero.",
                body=text[3],
                intents=("pod_follow_up_due", Intent.DOCUMENT_PROCESSED),
                stages=(delivery,),
                pages=(2,),
            ),
            CompiledSection(
                section_id="driver_questions",
                title="Questions from the driver",
                description="Answer from the load details; never share rates.",
                body=text[4],
                intents=(Intent.QUESTION,),
                pages=(2, 3),
            ),
            CompiledSection(
                section_id="communication_rules",
                title="Communication rules",
                description="Who we contact and how, on every Acme load.",
                body=text[5],
                pages=(3,),
            ),
        ),
        proposed_intents=(
            CompiledIntent(
                intent_id="detention_request",
                title="Detention request",
                description="The driver reports that the truck has waited more than 2 hours "
                'at a shipper or a receiver, or asks us to start detention: "Been at the '
                'dock since 7, still not unloaded".',
                examples=(
                    "Been at the dock since 7, still not unloaded",
                    "We've been here 3 hours, can you start detention?",
                ),
                evidence="the truck has waited more than 2 hours at a shipper or a receiver",
            ),
            CompiledIntent(  # already in the catalog: not proposed again
                intent_id=Intent.QUESTION,
                title="Question",
                description="asks us something",
                evidence="asks us something about the load",
            ),
        ),
        proposed_requirements=(
            CompiledRequirement(
                type_id="pod", at_stage=delivery, within_minutes=120, opens_obligation="pod"
            ),
        ),
        proposed_obligations=(
            CompiledObligation(
                kind="eta_request",
                title="ETA request",
                description="Every 20 minutes, up to three times.",
                follow_up_minutes=20,
                max_attempts=3,
                on_exhausted=("dispatcher_email", "hero_task"),
            ),
            CompiledObligation(
                kind="pod",
                title="POD request",
                description="The signed POD, due 120 minutes after arrival.",
                follow_up_minutes=30,
                max_attempts=1,
                on_exhausted=("hero_task",),
            ),
        ),
    )


def accept_all(draft: SopDraft) -> SopDraft:
    """What an editor does after reviewing: accept every proposal and section."""
    return draft.model_copy(
        update={
            "proposed_intents": tuple(
                i.model_copy(update={"accepted": True}) for i in draft.proposed_intents
            ),
            "sections": tuple(s.model_copy(update={"proposed": False}) for s in draft.sections),
        }
    )


@dataclass
class World:
    store: MemoryStore
    model: ScriptedModel
    clock: VirtualClock
    blobs: MemoryBlobStore
    service: SopIngestion

    async def upload_acme(self) -> SopDraft:
        self.model.push(COMPILE, acme_compiled())
        upload = await self.service.upload(
            "acme", "acme_sop.md", "text/markdown", ACME_MD.read_bytes()
        )
        assert upload.status == "compiled", upload.error
        assert upload.draft is not None
        return upload.draft

    async def accept(self) -> SopDraft:
        draft = accept_all(await self.service.get_draft("acme"))
        async with self.store.unit_of_work() as uow:
            await uow.sops.save_draft(draft)
            await uow.commit()
        return draft

    async def save_broker(self, *, config_version: int = 1) -> None:
        async with self.store.unit_of_work() as uow:
            broker = new_broker("acme", "Acme Freight")
            await uow.brokers.save(broker.model_copy(update={"config_version": config_version}))
            await uow.commit()


def world(pages: PageReader | None = None) -> World:
    store = MemoryStore()
    model = ScriptedModel()
    clock = VirtualClock(T0)
    blobs = MemoryBlobStore()
    service = SopIngestion(
        uow_factory=store.unit_of_work,
        clock=clock,
        ids=SequentialIdGenerator(),
        llm=model,
        blobs=blobs,
        pages=pages or FakePageReader(),
        embedder=FakeEmbedder(),
        knowledge=StoreSopIndex(store.unit_of_work, files=SOPS),
    )
    return World(store, model, clock, blobs, service)


# --- upload and compile ------------------------------------------------------------------


async def test_the_acme_document_compiles_into_a_draft_of_its_five_topics() -> None:
    w = world()

    draft = await w.upload_acme()

    assert [s.section_id for s in draft.sections] == [
        "delay_risk_high",
        "detention",
        "proof_of_delivery",
        "driver_questions",
        "communication_rules",
    ]
    assert all(s.proposed and s.layer is SopLayer.CLIENT for s in draft.sections)
    assert draft.sections[1].source == SourceRef(document_id="sopdoc_1", pages=(1, 2))
    assert draft.sections[1].body.startswith("When this applies: the driver tells us")
    assert [i.intent_id for i in draft.proposed_intents] == ["detention_request"]
    assert draft.proposed_intents[0].accepted is None
    assert draft.proposed_requirements == (
        DocumentRequirement(
            type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120, opens_obligation="pod"
        ),
    )
    eta, pod = draft.proposed_obligations
    assert (eta.kind, eta.follow_up_minutes, eta.max_attempts) == ("eta_request", 20, 3)
    assert eta.follow_up_intent == Intent.ETA_FOLLOW_UP_DUE  # the catalog's name is kept
    assert (pod.kind, pod.follow_up_intent, pod.on_exhausted) == ("pod", "", ("hero_task",))
    assert draft.document_ids == ("sopdoc_1",)
    async with w.store.unit_of_work() as uow:
        assert await uow.sops.get_draft("acme") == draft


async def test_the_compile_prompt_holds_the_catalog_the_standard_sections_and_the_text() -> None:
    w = world()

    await w.upload_acme()

    (call,) = w.model.calls
    assert (call.role, call.schema) == (COMPILE, "CompiledDraft")
    assert call.request.max_tool_rounds == 0
    user = call.request.user
    assert "- eta_provided: gives an arrival time" in user
    assert "delay_risk_high (system: set by code" in user
    assert "- eta_request: ETA request. Wait 30 minutes after each request, at most 2" in user
    assert "pod (Proof of delivery)" in user
    assert "- driver_questions: " in user  # a standard section the broker may replace
    assert "<<<BEGIN DOCUMENT>>>\n--- page 1 ---\n# Carrier Operations Procedure CO-07" in user
    assert user.index("<<<END DOCUMENT>>>") > user.index("## 5. Communication rules")
    assert "word for word" in call.request.system


async def test_the_same_bytes_uploaded_again_are_the_same_document() -> None:
    w = world()
    first = await w.upload_acme()

    second = await w.upload_acme()

    assert second.document_ids == first.document_ids == ("sopdoc_1",)
    async with w.store.unit_of_work() as uow:
        assert len(await uow.sops.list_documents("acme")) == 1


async def test_a_scanned_page_is_transcribed_from_its_image_first() -> None:
    @dataclass(frozen=True)
    class Scanned:
        def pages(self, data: bytes, content_type: str) -> list[Page]:
            return [Page(number=1, text="CO-07"), Page(number=2, image_png=b"\x89PNG-2")]

    w = world(pages=Scanned())
    w.model.push(EXTRACT, PageText(text="## 2. Detention\nWait 2 hours."))
    w.model.push(COMPILE, acme_compiled())

    upload = await w.service.upload("acme", "scan.pdf", "application/pdf", b"%PDF-scan")

    assert upload.status == "compiled"
    extract, compile_ = w.model.calls
    assert (extract.role, extract.schema) == (EXTRACT, "PageText")
    (image,) = extract.request.images
    assert (image.data, image.media_type) == (b"\x89PNG-2", "image/png")
    assert "Page 2 of scan.pdf" in extract.request.user
    assert "--- page 2 ---\n## 2. Detention\nWait 2 hours." in compile_.request.user
    assert upload.document.pages == 2


async def test_a_failed_compile_keeps_the_upload_stored() -> None:
    w = world()
    w.model.push(COMPILE, LlmError("compile model timed out"))

    upload = await w.service.upload("acme", "acme_sop.md", "text/markdown", b"# Acme\n1. Ask.")

    assert upload.status == "stored"
    assert upload.error is not None and "timed out" in upload.error
    assert upload.draft is None
    async with w.store.unit_of_work() as uow:
        assert await uow.sops.get_document(upload.document.document_id) == upload.document
        assert await uow.sops.get_draft("acme") is None


async def test_a_file_the_reader_cannot_open_is_stored_as_failed() -> None:
    @dataclass(frozen=True)
    class Refusing:
        def pages(self, data: bytes, content_type: str) -> list[Page]:
            raise ValueError("not a PDF")

    w = world(pages=Refusing())

    upload = await w.service.upload("acme", "notes.docx", "application/msword", b"PK")

    assert (upload.status, upload.document.pages) == ("failed", 0)
    assert w.model.calls == []
    assert await w.blobs.get(upload.document.blob_ref) == b"PK"


async def test_a_second_document_merges_into_the_draft() -> None:
    w = world()
    first = await w.upload_acme()
    await w.accept()
    w.model.push(
        COMPILE,
        CompiledDraft(
            sections=(
                CompiledSection(
                    section_id="detention",
                    title="Detention",
                    description="Changed.",
                    body="1. A new detention text.",
                    intents=("detention_request",),
                ),
                CompiledSection(
                    section_id="reefer_checks",
                    title="Reefer checks",
                    description="Temperature at pickup.",
                    body="1. Ask the temperature.",
                    load_type="reefer",
                ),
            ),
            proposed_intents=(acme_compiled().proposed_intents[0],),
        ),
    )

    upload = await w.service.upload("acme", "addendum.md", "text/markdown", b"# Addendum")

    assert upload.draft is not None
    sections = {s.section_id: s for s in upload.draft.sections}
    assert len(sections) == len(first.sections) + 1
    assert sections["detention"].body == "1. A new detention text."
    assert sections["reefer_checks"].layer is SopLayer.LOAD_TYPE
    assert upload.draft.proposed_intents[0].accepted is True  # the editor's decision stays
    assert upload.draft.document_ids == ("sopdoc_1", "sopdoc_2")


async def test_only_a_broker_s_own_documents_compile_into_its_draft() -> None:
    w = world()
    await w.upload_acme()

    with pytest.raises(UnknownDocument):
        await w.service.compile_draft("keystone", "sopdoc_1")
    with pytest.raises(UnknownDocument):
        await w.service.compile_draft("acme", "sopdoc_9")


async def test_the_standard_layer_is_not_uploaded() -> None:
    w = world()

    with pytest.raises(SopIngestionError, match="agent sop import"):
        await w.service.upload("standard", "x.md", "text/markdown", b"x")


# --- the editor's draft ------------------------------------------------------------------


async def test_an_edited_draft_replaces_the_broker_s_and_keeps_its_uploads() -> None:
    w = world()
    await w.upload_acme()
    w.clock.advance(timedelta(minutes=5))

    saved = await w.service.save_draft(
        "acme", sections=(DraftSection(section_id="rules", title="R", description="D", body="B"),)
    )

    assert saved.document_ids == ("sopdoc_1",)
    assert saved.updated_at == T0 + timedelta(minutes=5)
    assert await w.service.get_draft("acme") == saved
    fresh = await w.service.save_draft("keystone", sections=())
    assert fresh.document_ids == ()


async def test_a_broker_without_a_draft_has_none_to_get_or_publish() -> None:
    w = world()

    with pytest.raises(NoDraft):
        await w.service.get_draft("acme")
    with pytest.raises(NoDraft):
        await w.service.publish_draft("acme")


# --- lint and publish --------------------------------------------------------------------


async def test_an_unaccepted_intent_in_a_section_blocks_the_publish() -> None:
    w = world()
    draft = await w.upload_acme()

    lint = await w.service.lint_draft(draft)

    assert "section detention: intent detention_request is proposed and not accepted" in (
        " ".join(lint.errors)
    )
    with pytest.raises(DraftInvalid, match="detention_request is proposed and not accepted"):
        await w.service.publish_draft("acme")


async def test_the_reviewed_acme_draft_lints_clean_but_for_its_notes() -> None:
    w = world()
    await w.upload_acme()
    reviewed = await w.accept()

    lint = await w.service.lint_draft(reviewed)

    assert lint.errors == ()
    assert lint.warnings == ()
    assert lint.problems() == ()


async def test_publishing_the_acme_draft_bumps_the_broker_and_serves_its_sections() -> None:
    w = world()
    await w.save_broker()
    await w.upload_acme()
    await w.accept()

    published = await w.service.publish_draft("acme", published_by="pedro")

    assert published.config_version == 2
    async with w.store.unit_of_work() as uow:
        broker = await uow.brokers.get("acme")
        version = await uow.sops.published("acme")
        history = await uow.brokers.versions("acme")
    assert broker is not None and version is not None
    assert (broker.sop_version, broker.config_version) == (published.version, 2)
    assert broker.rollout == Rollout(percent=100, previous_version=1)
    assert [b.config_version for b in history] == [1, 2]
    detention = broker.intent("detention_request")
    assert detention is not None and detention.must_act and not detention.system
    pod = broker.obligation("pod")
    assert pod is not None and (pod.follow_up_minutes, pod.max_attempts) == (30, 1)
    follow_up = broker.intent("pod_follow_up_due")
    assert follow_up is not None and follow_up.system
    eta = broker.obligation("eta_request")
    assert eta is not None and (eta.follow_up_minutes, eta.max_attempts) == (20, 3)
    assert broker.documents[0].opens_obligation == "pod"
    assert (version.version, version.published_by) == (published.version, "pedro")
    assert [s.client_id for s in version.sections] == ["acme"] * 5
    assert version.sections[1].source == "acme_sop.md pp. 1, 2"
    index = StoreSopIndex(w.store.unit_of_work, files=SOPS)
    visible = {s.section_id: s for s in await index.visible("acme", ())}
    assert visible["delay_risk_high"].sources == ("acme_sop.md p. 1",)
    assert visible["escalation"].sources == ("standard/escalation.md",)
    applies = await index.by_applies("acme", (), "detention_request", Stage.AT_DELIVERY)
    assert "detention" in applies.section_ids


async def test_a_canary_publish_sends_a_share_of_the_loads_to_the_new_version() -> None:
    w = world()
    await w.save_broker(config_version=3)
    await w.upload_acme()
    await w.accept()

    published = await w.service.publish_draft("acme", percent=10)

    async with w.store.unit_of_work() as uow:
        broker = await uow.brokers.get("acme")
    assert broker is not None
    assert published.config_version == 4
    assert broker.rollout == Rollout(percent=10, previous_version=3)


async def test_a_broker_never_saved_is_created_by_its_first_publish() -> None:
    w = world()
    await w.upload_acme()
    await w.accept()

    published = await w.service.publish_draft("acme")

    async with w.store.unit_of_work() as uow:
        broker = await uow.brokers.get("acme")
    assert broker is not None
    assert (published.config_version, broker.name, broker.rollout) == (1, "Acme", Rollout())


async def test_an_intent_with_no_section_blocks_the_publish() -> None:
    w = world()
    await w.upload_acme()
    draft = await w.accept()
    without = [s for s in draft.sections if s.section_id != "detention"]
    await w.service.save_draft(
        "acme",
        sections=without,
        proposed_intents=draft.proposed_intents,
        proposed_requirements=draft.proposed_requirements,
        proposed_obligations=draft.proposed_obligations,
    )

    with pytest.raises(DraftInvalid, match="intent detention_request has no section for a load"):
        await w.service.publish_draft("acme")


async def test_a_section_with_an_unknown_intent_blocks_the_publish() -> None:
    w = world()
    await w.upload_acme()
    draft = await w.accept()
    lumper = DraftSection(
        section_id="lumper",
        title="Lumper",
        description="Lumper receipts.",
        intents=("lumper_receipt",),
        body="1. Hero task.",
    )
    await w.service.save_draft(
        "acme",
        sections=(*draft.sections, lumper),
        proposed_intents=draft.proposed_intents,
        proposed_requirements=draft.proposed_requirements,
        proposed_obligations=draft.proposed_obligations,
    )

    with pytest.raises(DraftInvalid) as raised:
        await w.service.publish_draft("acme")

    assert raised.value.errors == (
        "section lumper: intent lumper_receipt is not in the broker's catalog",
    )


async def test_unreviewed_sections_and_intents_publish_with_a_warning() -> None:
    w = world()
    await w.upload_acme()
    draft = await w.accept()
    sections = (*draft.sections[:-1], draft.sections[-1].model_copy(update={"proposed": True}))
    unreviewed = draft.proposed_intents[0].model_copy(
        update={"intent_id": "lumper_request", "accepted": None}
    )
    await w.service.save_draft(
        "acme",
        sections=sections,
        proposed_intents=(*draft.proposed_intents, unreviewed),
        proposed_requirements=draft.proposed_requirements,
        proposed_obligations=draft.proposed_obligations,
    )

    published = await w.service.publish_draft("acme")

    assert published.warnings == (
        "warning: intent lumper_request was proposed and not reviewed: it is not added",
        "warning: section communication_rules was compiled and not reviewed",
    )
    async with w.store.unit_of_work() as uow:
        version = await uow.sops.published("acme")
        broker = await uow.brokers.get("acme")
    assert version is not None and version.lint == published.warnings
    assert broker is not None and broker.intent("lumper_request") is None


async def test_a_draft_changed_while_publishing_is_not_published() -> None:
    w = world()

    @dataclass(frozen=True)
    class Meddling:
        """Embeds, and meanwhile an editor saves the draft again."""

        model_id: str = "fake/meddling"
        dim: int = 8

        async def embed(self, texts: Sequence[str]) -> list[list[float]]:
            async with w.store.unit_of_work() as uow:
                draft = await uow.sops.get_draft("acme")
                assert draft is not None
                await uow.sops.save_draft(
                    draft.model_copy(update={"updated_at": T0 + timedelta(1)})
                )
                await uow.commit()
            return [[0.0] * 8 for _ in range(5)]

    w.service = replace(w.service, embedder=Meddling())
    await w.upload_acme()
    await w.accept()

    with pytest.raises(DraftChanged):
        await w.service.publish_draft("acme")
    async with w.store.unit_of_work() as uow:
        assert await uow.sops.published("acme") is None


async def test_a_config_the_broker_refuses_is_a_draft_error() -> None:
    w = world()
    await w.save_broker()
    await w.upload_acme()
    await w.accept()

    with pytest.raises(DraftInvalid, match="percent"):
        await w.service.publish_draft("acme", percent=150)


# --- the lint, case by case --------------------------------------------------------------


def section(section_id: str = "detention", **fields: object) -> DraftSection:
    values: dict[str, object] = {
        "section_id": section_id,
        "title": "Detention",
        "description": "Held at a facility.",
        "intents": (Intent.STATUS_UPDATE,),
        "body": "1. Note it.",
        "proposed": False,
        **fields,
    }
    return DraftSection.model_validate(values)


async def lint_of(*sections: DraftSection, **draft: object) -> tuple[str, ...]:
    standard = await MemorySopIndex.from_files(SOPS).visible("standard", ())
    values: dict[str, object] = {"broker_id": "acme", "sections": sections, "updated_at": T0}
    built = SopDraft.model_validate({**values, **draft})
    return check_draft(built, new_broker("acme", "Acme"), standard).errors


@pytest.mark.parametrize(
    ("bad", "problem"),
    [
        (section("Detention Request"), "the id must be snake_case"),
        (section(layer=SopLayer.STANDARD), "in the client or the load type layer"),
        (section(layer=SopLayer.LOAD_TYPE), "a load type section names its load type"),
        (section(load_type="reefer"), "a load type section names its load type"),
        (section(title=" "), "give it a title and a description"),
        (section(body="\n"), "the section has no text"),
        (section(mode=MergeMode.EXTEND), "it extends a section that no less specific layer has"),
    ],
)
async def test_a_section_that_cannot_publish_is_named(bad: DraftSection, problem: str) -> None:
    errors = await lint_of(bad)

    assert any(problem in e and e.startswith(f"section {bad.section_id}:") for e in errors), errors


async def test_an_extend_may_leave_its_header_to_the_section_it_extends() -> None:
    extend = section("driver_departed", title="", description="", mode=MergeMode.EXTEND)
    reefer = section(
        "detention",
        layer=SopLayer.LOAD_TYPE,
        load_type="reefer",
        mode=MergeMode.EXTEND,
        title="",
        description="",
    )

    assert await lint_of(extend, section(), reefer) == ()


async def test_a_section_twice_in_one_layer_is_an_error() -> None:
    errors = await lint_of(section(), section())

    assert errors == ("section detention appears twice in the same layer",)


async def test_coverage_is_checked_for_each_load_type_of_the_draft() -> None:
    narrowed = section(
        "driver_departed",
        layer=SopLayer.LOAD_TYPE,
        load_type="reefer",
        intents=(Intent.STATUS_UPDATE,),
    )

    errors = await lint_of(narrowed)

    assert errors == (
        "intent driver_departed has no section for a reefer load: add one that lists it",
    )


async def test_a_requirement_must_name_a_known_type_and_obligation() -> None:
    errors = await lint_of(
        section(),
        proposed_requirements=(
            DocumentRequirement(type_id="lumper_receipt", at_stage=Stage.AT_PICKUP),
            DocumentRequirement(type_id="pod", at_stage=Stage.AT_DELIVERY, opens_obligation="pod"),
            DocumentRequirement(type_id="bol", at_stage=Stage.AT_PICKUP, within_minutes=30),
        ),
    )

    assert errors == (
        "requirement lumper_receipt at at_pickup: unknown document type lumper_receipt",
        "requirement pod at at_delivery: unknown obligation kind pod",
        "requirement bol at at_pickup: within_minutes needs opens_obligation",
    )


def test_the_catalog_checks_what_a_draft_can_change() -> None:
    broker = standard_broker()
    bad = broker.model_copy(
        update={
            "intents": (
                *broker.intents,
                broker.intents[0].model_copy(update={"intent_id": "Bad Id", "title": " "}),
            ),
            "obligations": (
                ObligationDef(
                    kind="Pod",
                    title="POD",
                    description="d",
                    follow_up_minutes=0,
                    on_exhausted=(),
                ),
            ),
        }
    )

    assert catalog_problems(bad) == (
        "intent id 'Bad Id' must be snake_case",
        "intent Bad Id needs a title and a description",
        "obligation kind 'Pod' must be snake_case",
        "obligation Pod: follow_up_minutes and max_attempts must be at least 1",
        "obligation Pod: on_exhausted names nobody",
    )
    assert catalog_problems(broker) == ()


# --- compiled output and merging ---------------------------------------------------------

DOCUMENT = SopDocument(
    document_id="sopdoc_1",
    broker_id="acme",
    filename="acme_sop.pdf",
    content_type="application/pdf",
    blob_ref="sha256:1",
    sha256="1",
    uploaded_at=T0,
)


def test_an_obligation_the_catalog_has_as_proposed_is_not_proposed() -> None:
    same = CompiledObligation(
        kind="eta_request",
        title="ETA request",
        description=standard_broker().obligations[0].description,
        follow_up_minutes=30,
        max_attempts=2,
        on_exhausted=("dispatcher_email", "hero_task"),
    )

    draft = draft_from_compiled(
        CompiledDraft(sections=(), proposed_obligations=(same,)),
        "acme",
        standard_broker(),
        DOCUMENT,
        updated_at=T0,
    )

    assert draft.proposed_obligations == ()


def test_merging_keeps_the_existing_parts_a_new_compile_does_not_replace() -> None:
    old = draft_from_compiled(acme_compiled(), "acme", standard_broker(), DOCUMENT, updated_at=T0)
    new = draft_from_compiled(
        CompiledDraft(
            sections=(acme_compiled().sections[0].model_copy(update={"body": "New."}),),
            proposed_requirements=(
                CompiledRequirement(type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=60),
            ),
        ),
        "acme",
        standard_broker(),
        DOCUMENT.model_copy(update={"document_id": "sopdoc_2"}),
        updated_at=T0 + timedelta(hours=1),
    )

    merged = merge_drafts(old, new)

    assert [s.section_id for s in merged.sections] == [s.section_id for s in old.sections]
    assert merged.sections[0].body == "New."
    assert merged.proposed_requirements[0].within_minutes == 60
    assert merged.proposed_intents == old.proposed_intents
    assert merged.proposed_obligations == old.proposed_obligations
    assert merged.document_ids == ("sopdoc_1", "sopdoc_2")
    assert merged.updated_at == T0 + timedelta(hours=1)


async def test_a_section_s_source_names_its_upload_and_pages() -> None:
    w = world()
    await w.upload_acme()
    draft = await w.accept()
    edited = (
        draft.sections[0].model_copy(update={"source": None}),
        draft.sections[1].model_copy(update={"source": SourceRef(document_id="gone")}),
        *draft.sections[2:],
    )
    await w.service.save_draft(
        "acme",
        sections=edited,
        proposed_intents=draft.proposed_intents,
        proposed_requirements=draft.proposed_requirements,
        proposed_obligations=draft.proposed_obligations,
    )

    await w.service.publish_draft("acme")

    async with w.store.unit_of_work() as uow:
        version = await uow.sops.published("acme")
    assert version is not None
    assert [s.source for s in version.sections[:3]] == [
        "editor",
        "upload gone",
        "acme_sop.md p. 2",
    ]
