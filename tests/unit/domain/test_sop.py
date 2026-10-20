from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from freight_agent.domain.broker import (
    DocumentRequirement,
    ObligationDef,
    Rollout,
    new_broker,
    standard_broker,
)
from freight_agent.domain.errors import DomainError
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    CatalogEntry,
    DraftSection,
    MergeMode,
    ProposedIntent,
    ResolvedSection,
    SopDocument,
    SopDraft,
    SopLayer,
    SopSection,
    SopVersion,
    SourceRef,
    adopt_sop,
    contributors,
    effective_sections,
    follow_up_intents,
    resolve,
    revised_broker,
    search_text,
    section_from_draft,
    version_hash,
    with_proposals,
)

REPLACE, EXTEND = MergeMode.REPLACE, MergeMode.EXTEND
HIGH, LOW = Intent.DELAY_RISK_HIGH, Intent.DELAY_RISK_LOW
FOLLOW_UP, DEPARTED = Intent.ETA_FOLLOW_UP_DUE, Intent.DRIVER_DEPARTED


def standard(
    section_id: str,
    *,
    body: str = "standard body",
    title: str | None = None,
    intents: tuple[Intent, ...] = (),
    stages: tuple[Stage, ...] = (),
    mode: MergeMode = REPLACE,
) -> SopSection:
    return SopSection(
        section_id=section_id,
        layer=SopLayer.STANDARD,
        title=title or f"Standard {section_id}",
        description=f"standard description of {section_id}",
        intents=intents,
        stages=stages,
        mode=mode,
        body=body,
        source=f"standard/{section_id}.md",
    )


def client(
    client_id: str,
    section_id: str,
    *,
    body: str = "client body",
    mode: MergeMode = REPLACE,
    intents: tuple[Intent, ...] = (),
    stages: tuple[Stage, ...] = (),
    source: str | None = None,
) -> SopSection:
    return SopSection(
        section_id=section_id,
        layer=SopLayer.CLIENT,
        client_id=client_id,
        title=f"{client_id} {section_id}",
        description=f"{client_id} description of {section_id}",
        intents=intents,
        stages=stages,
        mode=mode,
        body=body,
        source=source or f"clients/{client_id}/{section_id}.md",
    )


def load_type(
    client_id: str,
    type_: str,
    section_id: str,
    *,
    body: str = "load type body",
    mode: MergeMode = EXTEND,
    intents: tuple[Intent, ...] = (),
) -> SopSection:
    return SopSection(
        section_id=section_id,
        layer=SopLayer.LOAD_TYPE,
        client_id=client_id,
        load_type=type_,
        title=f"{client_id} {type_} {section_id}",
        description=f"{client_id} {type_} description of {section_id}",
        intents=intents,
        mode=mode,
        body=body,
        source=f"clients/{client_id}/types/{type_}/{section_id}.md",
    )


# The canonical SOP tree in miniature: the high-risk procedure overridden by both
# clients, and the reefer extension of the departure procedure.
TREE = (
    standard("escalation", body="Create a Hero task."),
    standard(
        "delay_risk_high",
        body="SMS the driver. Then a Hero task.",
        intents=(HIGH, FOLLOW_UP),
        stages=(Stage.TO_PICKUP, Stage.TO_DELIVERY),
    ),
    standard("driver_departed", body="Update the stage.", intents=(DEPARTED,)),
    standard("delay_risk_cleared", body="Close the ETA request.", intents=(LOW,)),
    client("northline", "delay_risk_high", body="SMS twice, email the dispatcher."),
    client("keystone", "delay_risk_high", body="One SMS, then broker Slack."),
    load_type("keystone", "reefer", "driver_departed", body="Ask temperature and seal."),
)


def by_id(sections: tuple[ResolvedSection, ...]) -> dict[str, ResolvedSection]:
    return {s.section_id: s for s in sections}


@pytest.mark.unit
def test_standard_sections_apply_unchanged_when_no_client_layer_exists() -> None:
    result = effective_sections(TREE, client_id="acme", load_types=())

    assert [s.section_id for s in result] == [
        "delay_risk_cleared",
        "delay_risk_high",
        "driver_departed",
        "escalation",
    ]
    high = result[1]
    assert high.title == "Standard delay_risk_high"
    assert high.description == "standard description of delay_risk_high"
    assert high.intents == (HIGH, FOLLOW_UP)
    assert high.stages == (Stage.TO_PICKUP, Stage.TO_DELIVERY)
    assert high.body == "SMS the driver. Then a Hero task."
    assert high.sources == ("standard/delay_risk_high.md",)


@pytest.mark.unit
def test_mode_is_ignored_in_the_standard_layer() -> None:
    sections = (standard("escalation", body="Create a Hero task.", mode=EXTEND),)

    (result,) = effective_sections(sections, client_id="northline", load_types=())

    assert result.body == "Create a Hero task."
    assert result.sources == ("standard/escalation.md",)


@pytest.mark.unit
def test_client_replace_swaps_body_and_metadata() -> None:
    sections = (
        standard("delay_risk_high", intents=(HIGH, FOLLOW_UP), stages=(Stage.TO_PICKUP,)),
        client("northline", "delay_risk_high", body="SMS twice.", intents=(HIGH,)),
    )

    (result,) = effective_sections(sections, client_id="northline", load_types=())

    assert result.title == "northline delay_risk_high"
    assert result.description == "northline description of delay_risk_high"
    assert result.intents == (HIGH,)
    assert result.stages == ()
    assert result.body == "SMS twice."
    assert result.sources == ("clients/northline/delay_risk_high.md",)


@pytest.mark.unit
def test_client_extend_appends_body_and_keeps_inherited_metadata() -> None:
    sections = (
        standard("escalation", body="Create a Hero task.", stages=(Stage.TO_PICKUP,)),
        client("northline", "escalation", body="Also tag the account manager.", mode=EXTEND),
    )

    (result,) = effective_sections(sections, client_id="northline", load_types=())

    assert result.body == "Create a Hero task.\n\nAlso tag the account manager."
    assert result.title == "Standard escalation"
    assert result.description == "standard description of escalation"
    assert result.stages == (Stage.TO_PICKUP,)
    assert result.sources == ("standard/escalation.md", "clients/northline/escalation.md")


@pytest.mark.unit
def test_extend_separates_bodies_with_exactly_one_blank_line() -> None:
    sections = (
        standard("escalation", body="Create a Hero task.\n\n"),
        client("northline", "escalation", body="\nAlso tag the manager.\n", mode=EXTEND),
    )

    (result,) = effective_sections(sections, client_id="northline", load_types=())

    assert result.body == "Create a Hero task.\n\nAlso tag the manager.\n"


@pytest.mark.unit
def test_load_type_extend_on_top_of_a_client_replace() -> None:
    sections = (
        standard("driver_departed", body="Update the stage.", intents=(DEPARTED,)),
        client("keystone", "driver_departed", body="Update the stage, note the time."),
        load_type("keystone", "reefer", "driver_departed", body="Ask temperature and seal."),
    )

    (result,) = effective_sections(sections, client_id="keystone", load_types=("reefer",))

    assert result.body == "Update the stage, note the time.\n\nAsk temperature and seal."
    assert result.title == "keystone driver_departed"
    assert result.intents == ()
    assert result.sources == (
        "clients/keystone/driver_departed.md",
        "clients/keystone/types/reefer/driver_departed.md",
    )


@pytest.mark.unit
def test_load_type_extend_directly_on_the_standard_layer() -> None:
    result = by_id(effective_sections(TREE, client_id="keystone", load_types=("reefer",)))

    departed = result["driver_departed"]
    assert departed.body == "Update the stage.\n\nAsk temperature and seal."
    assert departed.intents == (DEPARTED,)
    assert departed.sources == (
        "standard/driver_departed.md",
        "clients/keystone/types/reefer/driver_departed.md",
    )


@pytest.mark.unit
def test_a_replace_drops_earlier_contributors_and_later_extends_add_to_it() -> None:
    sections = (
        standard("escalation", body="A."),
        client("keystone", "escalation", body="B.", mode=EXTEND),
        load_type("keystone", "reefer", "escalation", body="C.", mode=REPLACE),
        load_type("keystone", "hazmat", "escalation", body="D."),
    )

    (result,) = effective_sections(sections, client_id="keystone", load_types=("reefer", "hazmat"))

    assert result.body == "C.\n\nD."
    assert result.title == "keystone reefer escalation"
    assert result.sources == (
        "clients/keystone/types/reefer/escalation.md",
        "clients/keystone/types/hazmat/escalation.md",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("load_types", "expected"),
    [
        (("reefer", "hazmat"), "Base.\n\nReefer.\n\nHazmat."),
        (("hazmat", "reefer"), "Base.\n\nHazmat.\n\nReefer."),
    ],
)
def test_load_types_apply_in_the_order_given(load_types: tuple[str, ...], expected: str) -> None:
    sections = (
        standard("escalation", body="Base."),
        load_type("keystone", "reefer", "escalation", body="Reefer."),
        load_type("keystone", "hazmat", "escalation", body="Hazmat."),
    )

    (result,) = effective_sections(sections, client_id="keystone", load_types=load_types)

    assert result.body == expected


@pytest.mark.unit
def test_a_load_type_listed_twice_applies_once() -> None:
    sections = (
        standard("escalation", body="Base."),
        load_type("keystone", "reefer", "escalation", body="Reefer."),
    )

    (result,) = effective_sections(sections, client_id="keystone", load_types=("reefer", "reefer"))

    assert result.body == "Base.\n\nReefer."


@pytest.mark.unit
def test_load_type_sections_of_other_types_are_not_visible() -> None:
    result = by_id(effective_sections(TREE, client_id="keystone", load_types=("dry_van",)))

    assert result["driver_departed"].body == "Update the stage."


@pytest.mark.unit
def test_a_client_only_section_needs_no_standard_base() -> None:
    sections = (client("northline", "detention", body="Bill detention after 2 h."),)

    (result,) = effective_sections(sections, client_id="northline", load_types=())

    assert result.section_id == "detention"
    assert result.sources == ("clients/northline/detention.md",)


@pytest.mark.unit
def test_a_client_never_sees_another_clients_sections() -> None:
    sections = (
        *TREE,
        client("keystone", "detention", body="Keystone only."),
        client("keystone", "escalation", body="Keystone escalation.", mode=EXTEND),
    )

    northline = by_id(effective_sections(sections, client_id="northline", load_types=("reefer",)))

    assert set(northline) == {
        "delay_risk_cleared",
        "delay_risk_high",
        "driver_departed",
        "escalation",
    }
    high = northline["delay_risk_high"]
    assert high.body == "SMS twice, email the dispatcher."
    assert high.sources == ("clients/northline/delay_risk_high.md",)
    assert northline["escalation"].body == "Create a Hero task."
    # keystone's reefer extension does not apply to a northline reefer load
    assert northline["driver_departed"].body == "Update the stage."


@pytest.mark.unit
def test_extend_without_a_base_raises() -> None:
    sections = (client("northline", "detention", body="More.", mode=EXTEND),)

    with pytest.raises(DomainError, match=r"detention.*clients/northline/detention\.md"):
        effective_sections(sections, client_id="northline", load_types=())


@pytest.mark.unit
def test_load_type_extend_without_a_base_raises() -> None:
    sections = (load_type("keystone", "reefer", "seal_check"),)

    with pytest.raises(DomainError, match="seal_check"):
        effective_sections(sections, client_id="keystone", load_types=("reefer",))


@pytest.mark.unit
def test_an_orphan_extend_of_another_client_is_not_an_error() -> None:
    sections = (standard("escalation"), client("keystone", "detention", mode=EXTEND))

    result = effective_sections(sections, client_id="northline", load_types=())

    assert [s.section_id for s in result] == ["escalation"]


@pytest.mark.unit
def test_two_files_with_the_same_id_in_one_layer_raise() -> None:
    sections = (
        client("northline", "escalation", source="clients/northline/a.md"),
        client("northline", "escalation", source="clients/northline/b.md"),
    )

    with pytest.raises(DomainError, match=r"escalation.*a\.md.*b\.md"):
        effective_sections(sections, client_id="northline", load_types=())


@pytest.mark.unit
def test_resolve_keeps_sections_for_the_intent_and_stage() -> None:
    sop = resolve(TREE, client_id="northline", load_types=(), intent=HIGH, stage=Stage.TO_PICKUP)

    # escalation has no intents and no stages: it applies to every event
    assert sop.section_ids == ("delay_risk_high", "escalation")
    assert sop.sections[0].body == "SMS twice, email the dispatcher."


@pytest.mark.unit
def test_resolve_filters_by_intent() -> None:
    sop = resolve(TREE, client_id="acme", load_types=(), intent=LOW, stage=Stage.TO_PICKUP)

    assert sop.section_ids == ("delay_risk_cleared", "escalation")


@pytest.mark.unit
def test_resolve_filters_by_stage() -> None:
    sop = resolve(TREE, client_id="acme", load_types=(), intent=HIGH, stage=Stage.AT_PICKUP)

    # delay_risk_high lists to_pickup and to_delivery only
    assert sop.section_ids == ("escalation",)


@pytest.mark.unit
def test_resolve_treats_empty_intents_and_stages_as_all() -> None:
    sections = (
        standard("anything"),
        standard("any_stage", intents=(DEPARTED,)),
        standard("any_intent", stages=(Stage.AT_DELIVERY,)),
    )

    sop = resolve(
        sections,
        client_id="acme",
        load_types=(),
        intent=DEPARTED,
        stage=Stage.AT_DELIVERY,
    )
    other = resolve(
        sections, client_id="acme", load_types=(), intent=Intent.OTHER, stage=Stage.DISPATCHED
    )

    assert sop.section_ids == ("any_intent", "any_stage", "anything")
    assert other.section_ids == ("anything",)


@pytest.mark.unit
def test_resolve_uses_the_merged_metadata() -> None:
    sections = (
        standard("delay_risk_high", intents=(HIGH,)),
        client("keystone", "delay_risk_high", intents=(FOLLOW_UP,)),
    )

    on_high = resolve(
        sections, client_id="keystone", load_types=(), intent=HIGH, stage=Stage.TO_PICKUP
    )
    on_follow_up = resolve(
        sections, client_id="keystone", load_types=(), intent=FOLLOW_UP, stage=Stage.TO_PICKUP
    )

    assert on_high.section_ids == ()
    assert on_follow_up.section_ids == ("delay_risk_high",)


@pytest.mark.unit
def test_catalog_lists_every_visible_section_sorted_by_id() -> None:
    sop = resolve(
        TREE, client_id="keystone", load_types=("reefer",), intent=LOW, stage=Stage.TO_PICKUP
    )

    assert sop.catalog == (
        CatalogEntry(
            section_id="delay_risk_cleared",
            title="Standard delay_risk_cleared",
            description="standard description of delay_risk_cleared",
        ),
        CatalogEntry(
            section_id="delay_risk_high",
            title="keystone delay_risk_high",
            description="keystone description of delay_risk_high",
        ),
        CatalogEntry(
            section_id="driver_departed",
            title="Standard driver_departed",
            description="standard description of driver_departed",
        ),
        CatalogEntry(
            section_id="escalation",
            title="Standard escalation",
            description="standard description of escalation",
        ),
    )


@pytest.mark.unit
def test_catalog_excludes_other_clients_sections() -> None:
    sections = (*TREE, client("keystone", "detention"))

    sop = resolve(
        sections, client_id="northline", load_types=(), intent=HIGH, stage=Stage.TO_PICKUP
    )

    assert "detention" not in [entry.section_id for entry in sop.catalog]


@pytest.mark.unit
def test_resolve_with_no_matching_section_still_returns_the_catalog() -> None:
    sections = (standard("delay_risk_cleared", intents=(LOW,)),)

    sop = resolve(sections, client_id="acme", load_types=(), intent=HIGH, stage=Stage.TO_PICKUP)

    assert sop.sections == ()
    assert [entry.section_id for entry in sop.catalog] == ["delay_risk_cleared"]


@pytest.mark.unit
def test_no_sections_resolve_to_nothing() -> None:
    sop = resolve((), client_id="acme", load_types=(), intent=HIGH, stage=Stage.TO_PICKUP)

    assert sop.sections == ()
    assert sop.catalog == ()


# --- contributors ----------------------------------------------------------------------


@pytest.mark.unit
def test_contributors_are_the_sections_whose_text_each_merged_section_holds() -> None:
    found = contributors(TREE, client_id="keystone", load_types=("reefer",))

    assert set(found) == {
        s.section_id for s in effective_sections(TREE, client_id="keystone", load_types=("reefer",))
    }
    assert found["driver_departed"] == (TREE[2], TREE[6])  # standard, then the reefer extend
    assert found["delay_risk_high"] == (TREE[5],)  # the replace shadows the standard text
    assert found["escalation"] == (TREE[0],)
    assert all(a is b for a, b in zip(found["driver_departed"], (TREE[2], TREE[6]), strict=True))


@pytest.mark.unit
def test_a_replace_after_an_extend_starts_the_contributors_over() -> None:
    sections = (
        standard("rules"),
        client("acme", "rules", mode=EXTEND),
        load_type("acme", "reefer", "rules", mode=REPLACE),
    )

    assert contributors(sections, client_id="acme", load_types=("reefer",))["rules"] == (
        sections[2],
    )


# --- versions, search text, drafts -------------------------------------------------------


@pytest.mark.unit
def test_the_version_hash_is_stable_short_and_changes_with_any_field() -> None:
    edited = TREE[0].model_copy(update={"body": "Create an urgent Hero task."})

    assert version_hash(TREE) == version_hash(tuple(TREE))
    assert len(version_hash(TREE)) == 12
    assert version_hash((edited, *TREE[1:])) != version_hash(TREE)
    assert version_hash(reversed(TREE)) != version_hash(TREE)  # order is content too


@pytest.mark.unit
def test_the_search_text_holds_the_heading_and_the_body() -> None:
    text = search_text(TREE[1])

    assert TREE[1].title in text
    assert TREE[1].description in text
    assert text.endswith(TREE[1].body)


def draft_section(section_id: str = "detention", **fields: object) -> DraftSection:
    values: dict[str, object] = {
        "section_id": section_id,
        "title": "Detention",
        "description": "Truck held at a facility for more than 2 hours.",
        "intents": ("detention_request",),
        "stages": (Stage.AT_PICKUP, Stage.AT_DELIVERY),
        "body": "1. Write a TMS note.",
        "source": SourceRef(document_id="doc_1", pages=(2,)),
        **fields,
    }
    return DraftSection.model_validate(values)


@pytest.mark.unit
def test_a_draft_section_publishes_in_its_broker_s_layer() -> None:
    section = section_from_draft(draft_section(), "acme", "acme_sop.pdf p. 2")

    assert (section.layer, section.client_id, section.load_type) == (SopLayer.CLIENT, "acme", None)
    assert section.intents == ("detention_request",)
    assert section.source == "acme_sop.pdf p. 2"
    reefer = section_from_draft(
        draft_section(layer=SopLayer.LOAD_TYPE, load_type="reefer", mode=EXTEND), "acme", "x"
    )
    assert (reefer.layer, reefer.load_type, reefer.mode) == (SopLayer.LOAD_TYPE, "reefer", EXTEND)


@pytest.mark.unit
def test_a_standard_draft_section_publishes_in_the_standard_layer() -> None:
    section = section_from_draft(
        draft_section(layer=SopLayer.LOAD_TYPE, load_type="reefer", mode=EXTEND), None, "x"
    )

    assert (section.layer, section.client_id, section.load_type) == (
        SopLayer.STANDARD,
        None,
        None,
    )
    assert section.mode is REPLACE


@pytest.mark.unit
def test_the_standard_catalog_id_names_the_standard_layer() -> None:
    now = datetime(2026, 10, 20, tzinfo=UTC)

    assert SopDraft(broker_id="standard", updated_at=now).broker_id is None
    assert (
        SopVersion(
            broker_id="standard", version="v", sections=(), published_at=now, published_by="x"
        ).broker_id
        is None
    )
    document = SopDocument(
        document_id="d",
        broker_id="acme",
        filename="a.pdf",
        content_type="application/pdf",
        blob_ref="sha256:1",
        sha256="1",
        uploaded_at=now,
    )
    assert document.broker_id == "acme"


# --- publishing changes the broker -------------------------------------------------------

DETENTION = ProposedIntent(
    intent_id="detention_request",
    title="Detention request",
    description="The truck waited more than 2 hours at a facility.",
    evidence="the truck has waited more than 2 hours",
    accepted=True,
)
POD = ObligationDef(
    kind="pod",
    title="POD request",
    description="We wait for the signed POD.",
    follow_up_minutes=30,
    max_attempts=1,
)
FASTER_ETA = ObligationDef(
    kind="eta_request",
    title="ETA request",
    description="Every 20 minutes, three times.",
    follow_up_minutes=20,
    max_attempts=3,
    follow_up_intent="eta_follow_up_due",
    on_exhausted=("dispatcher_email", "hero_task"),
)
POD_AT_DELIVERY = DocumentRequirement(
    type_id="pod", at_stage=Stage.AT_DELIVERY, within_minutes=120, opens_obligation="pod"
)


def acme_draft(**fields: object) -> SopDraft:
    values: dict[str, object] = {
        "broker_id": "acme",
        "sections": (draft_section(),),
        "proposed_intents": (DETENTION,),
        "proposed_requirements": (POD_AT_DELIVERY,),
        "proposed_obligations": (POD, FASTER_ETA),
        "updated_at": datetime(2026, 10, 20, tzinfo=UTC),
        **fields,
    }
    return SopDraft.model_validate(values)


@pytest.mark.unit
def test_publishing_adds_the_accepted_intents_obligations_and_requirements() -> None:
    broker = with_proposals(new_broker("acme", "Acme Freight"), acme_draft())

    detention = broker.intent("detention_request")
    assert detention is not None and detention.title == "Detention request"
    assert not detention.system
    assert broker.obligation("eta_request") == FASTER_ETA  # replaced, not added
    assert broker.obligation("pod") == POD
    assert [o.kind for o in broker.obligations] == ["eta_request", "pod"]
    follow_up = broker.intent("pod_follow_up_due")
    assert follow_up is not None and follow_up.system
    assert broker.documents == (POD_AT_DELIVERY,)
    assert broker.intents[: len(standard_broker().intents)] == standard_broker().intents


@pytest.mark.unit
def test_an_intent_not_accepted_or_already_known_is_not_added() -> None:
    known = DETENTION.model_copy(update={"intent_id": "question", "title": "Changed"})
    draft = acme_draft(
        proposed_intents=(
            DETENTION.model_copy(update={"accepted": None}),
            DETENTION.model_copy(update={"intent_id": "lumper", "accepted": False}),
            known,
        ),
        proposed_obligations=(),
        proposed_requirements=(),
    )

    broker = with_proposals(new_broker("acme", "Acme"), draft)

    assert broker == new_broker("acme", "Acme")


@pytest.mark.unit
def test_a_requirement_of_the_same_type_and_stage_is_replaced() -> None:
    first = with_proposals(new_broker("acme", "Acme"), acme_draft())
    later = POD_AT_DELIVERY.model_copy(update={"within_minutes": 60})

    broker = with_proposals(first, acme_draft(proposed_requirements=(later,)))

    assert broker.documents == (later,)
    assert len(broker.intents) == len(first.intents)  # nothing added twice


@pytest.mark.unit
def test_a_saved_broker_adopts_a_sop_as_a_new_config_version() -> None:
    broker = new_broker("acme", "Acme").model_copy(update={"config_version": 4})

    adopted = adopt_sop(broker, "abc123", saved=True)
    canary = adopt_sop(broker, "abc123", saved=True, percent=10)

    assert (adopted.sop_version, adopted.config_version) == ("abc123", 5)
    assert adopted.rollout == Rollout(percent=100, previous_version=4)
    assert canary.rollout == Rollout(percent=10, previous_version=4)


@pytest.mark.unit
def test_a_broker_saved_for_the_first_time_keeps_version_one() -> None:
    adopted = adopt_sop(new_broker("acme", "Acme"), "abc123", saved=False, percent=10)

    assert (adopted.sop_version, adopted.config_version) == ("abc123", 1)
    assert adopted.rollout == Rollout()


@pytest.mark.unit
def test_an_obligation_s_named_follow_up_intent_is_the_one_added() -> None:
    named = POD.model_copy(update={"follow_up_intent": "pod_overdue"})

    intents = follow_up_intents(standard_broker().intents, (FASTER_ETA, named, POD))

    added = intents[len(standard_broker().intents) :]
    assert [(i.intent_id, i.system, i.must_act) for i in added] == [
        ("pod_overdue", True, True),
        ("pod_follow_up_due", True, True),
    ]
    assert added[1].title == "POD request follow-up due"


@pytest.mark.unit
def test_a_revised_broker_is_validated() -> None:
    broker = new_broker("acme", "Acme")

    assert revised_broker(broker, name="Acme Freight").name == "Acme Freight"
    with pytest.raises(ValidationError):
        revised_broker(broker, rollout={"percent": 101})
