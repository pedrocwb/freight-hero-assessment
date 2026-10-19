import pytest

from freight_agent.domain.errors import DomainError
from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    CatalogEntry,
    MergeMode,
    ResolvedSection,
    SopLayer,
    SopSection,
    effective_sections,
    resolve,
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
