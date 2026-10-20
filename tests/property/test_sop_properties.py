"""Properties of the SOP merge over generated, well-formed layer trees.

The trees are built so that every client and load-type section has a standard base,
which keeps them free of lint errors (no orphan extends, no duplicate ids in a layer).
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from freight_agent.domain.intents import Intent
from freight_agent.domain.load import Stage
from freight_agent.domain.sop import (
    MergeMode,
    SopLayer,
    SopSection,
    effective_sections,
    resolve,
)

SECTION_IDS = ("delay_risk_high", "driver_departed", "documents", "escalation", "eta_received")
CLIENTS = ("northline", "keystone")
LOAD_TYPES = ("reefer", "hazmat", "dry_van")

texts = st.text(alphabet="abc .\n", max_size=12)
intents = st.lists(st.sampled_from(Intent), max_size=3, unique=True).map(tuple)
stages = st.lists(st.sampled_from(Stage), max_size=3, unique=True).map(tuple)


@st.composite
def sections_of(
    draw: st.DrawFn, layer: SopLayer, client_id: str | None, load_type: str | None
) -> list[SopSection]:
    ids = draw(st.lists(st.sampled_from(SECTION_IDS), unique=True))
    return [
        SopSection(
            section_id=section_id,
            layer=layer,
            client_id=client_id,
            load_type=load_type,
            title=draw(texts),
            description=draw(texts),
            intents=draw(intents),
            stages=draw(stages),
            mode=draw(st.sampled_from(MergeMode)),
            body=draw(texts),
            source=f"{layer}/{client_id}/{load_type}/{section_id}.md",
        )
        for section_id in ids
    ]


@st.composite
def client_layers(draw: st.DrawFn, client_id: str) -> list[SopSection]:
    sections = draw(sections_of(SopLayer.CLIENT, client_id, None))
    for type_ in LOAD_TYPES:
        sections += draw(sections_of(SopLayer.LOAD_TYPE, client_id, type_))
    return sections


standard_tree = st.just(
    [
        SopSection(
            section_id=section_id,
            layer=SopLayer.STANDARD,
            title=section_id,
            description=section_id,
            body=f"standard {section_id}",
            source=f"standard/{section_id}.md",
        )
        for section_id in SECTION_IDS
    ]
)
load_type_lists = st.lists(st.sampled_from(LOAD_TYPES), max_size=4).map(tuple)
LAYER_ORDER = (SopLayer.STANDARD, SopLayer.CLIENT, SopLayer.LOAD_TYPE)


def _specificity(section: SopSection, load_types: tuple[str, ...]) -> tuple[int, int]:
    """Layer rank, then the position of the load type's first mention."""
    type_rank = load_types.index(section.load_type) if section.load_type in load_types else -1
    return LAYER_ORDER.index(section.layer), type_rank


@pytest.mark.property
@given(standard_tree, client_layers("northline"), client_layers("keystone"), load_type_lists)
def test_another_clients_sections_never_change_the_result(
    standard: list[SopSection],
    northline: list[SopSection],
    keystone: list[SopSection],
    load_types: tuple[str, ...],
) -> None:
    alone = effective_sections(
        [*standard, *northline], client_id="northline", load_types=load_types
    )
    shared = effective_sections(
        [*keystone, *standard, *northline], client_id="northline", load_types=load_types
    )

    assert shared == alone
    assert all("keystone" not in source for s in shared for source in s.sources)


@pytest.mark.property
@given(standard_tree, client_layers("northline"), load_type_lists)
def test_output_is_one_section_per_id_sorted(
    standard: list[SopSection], northline: list[SopSection], load_types: tuple[str, ...]
) -> None:
    result = effective_sections(
        [*standard, *northline], client_id="northline", load_types=load_types
    )

    ids = [s.section_id for s in result]
    assert ids == sorted(set(ids))
    assert set(ids) == set(SECTION_IDS)


@pytest.mark.property
@given(standard_tree, client_layers("northline"), load_type_lists)
def test_the_last_source_is_the_most_specific_contributor(
    standard: list[SopSection], northline: list[SopSection], load_types: tuple[str, ...]
) -> None:
    sections = [*standard, *northline]
    visible_types = set(load_types)

    for resolved in effective_sections(sections, client_id="northline", load_types=load_types):
        contributors = [
            s
            for s in sections
            if s.section_id == resolved.section_id
            and (s.load_type is None or s.load_type in visible_types)
        ]
        most_specific = max(contributors, key=lambda s: _specificity(s, load_types))
        assert resolved.sources[-1] == most_specific.source
        assert resolved.body.endswith(most_specific.body.lstrip("\r\n"))


@pytest.mark.property
@given(
    standard_tree,
    client_layers("northline"),
    load_type_lists,
    st.sampled_from(Intent),
    st.sampled_from(Stage),
)
def test_resolve_picks_a_subset_and_catalogs_everything(
    standard: list[SopSection],
    northline: list[SopSection],
    load_types: tuple[str, ...],
    intent: Intent,
    stage: Stage,
) -> None:
    sections = [*standard, *northline]
    visible = effective_sections(sections, client_id="northline", load_types=load_types)

    sop = resolve(
        sections, client_id="northline", load_types=load_types, intent=intent, stage=stage
    )

    assert [entry.section_id for entry in sop.catalog] == [s.section_id for s in visible]
    assert all(s in visible for s in sop.sections)
    assert list(sop.section_ids) == sorted(sop.section_ids)
    unrestricted = [s for s in visible if not s.intents and not s.stages]
    assert all(s in sop.sections for s in unrestricted)
