"""Broker isolation, layered editing and real-clock scenario ingress."""

from pathlib import Path
from typing import cast

import pytest
from temporalio.client import Client
from tests.unit.application.test_runtime_harness import World
from tests.unit.orchestration.test_ingress import ClientStub

from freight_agent.domain.errors import DomainError
from freight_agent.domain.load import Load
from freight_agent.domain.sop import MergeMode, SopLayer
from freight_agent.orchestration.contracts import LoadKey
from freight_agent.orchestration.policy import pinned_index
from freight_agent.orchestration.profiles import BROKERS, default_profile, freeze_profile
from freight_agent.orchestration.service import TemporalIngress
from freight_agent.orchestration.workspace import inject_step, scenarios, start_scenario

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize("broker_id", list(BROKERS))
async def test_seeded_brokers_have_valid_layered_profiles_and_scoped_scenarios(broker_id):
    profile = await default_profile(ROOT / "sops", broker_id)
    frozen = freeze_profile(profile)
    assert frozen == freeze_profile(profile)
    assert profile.checked(broker_id)
    assert scenarios(ROOT / "evals", broker_id)
    index = pinned_index(frozen)
    assert (await index.broker(broker_id)).broker_id == broker_id
    if broker_id == "keystone":
        eta = profile.broker.obligation("eta_request")
        assert eta and eta.max_attempts == 1 and eta.on_exhausted == ("broker_slack",)
        reefer = await index.read(broker_id, ("reefer",), "driver_departed")
        assert reefer and "temperature" in reefer.body and "seal" in reefer.body
        dry = await index.read(broker_id, (), "driver_departed")
        assert dry and "temperature" not in dry.body


async def test_profile_edit_changes_catalog_and_text_without_changing_old_execution():
    original = await default_profile(ROOT / "sops", "northline")
    frozen = freeze_profile(original)
    eta = original.broker.obligation("eta_request")
    assert eta
    edited = original.model_copy(
        update={
            "broker": original.broker.revised(
                obligations=(eta.model_copy(update={"max_attempts": 3, "follow_up_minutes": 1}),)
            )
        }
    )
    revised = freeze_profile(edited)
    assert revised["version"] != frozen["version"]
    section = await pinned_index(revised).read("northline", (), "delay_risk_high")
    assert section and "below\n3" in section.body and "1 minutes" in section.body
    original_eta = (await pinned_index(frozen).broker("northline")).obligation("eta_request")
    assert original_eta and original_eta.max_attempts == 2
    other = await default_profile(ROOT / "sops", "keystone")
    other_eta = other.broker.obligation("eta_request")
    assert other_eta and other_eta.max_attempts == 1


async def test_extensions_are_merged_and_cross_broker_or_standard_edits_are_rejected():
    profile = await default_profile(ROOT / "sops", "northline")
    base = next(s for s in profile.sections if s.section_id == "acknowledgements")
    extension = base.model_copy(
        update={
            "layer": SopLayer.CLIENT,
            "client_id": "northline",
            "mode": MergeMode.EXTEND,
            "body": "Additional client rule.",
        }
    )
    edited = profile.model_copy(update={"sections": (*profile.sections, extension)})
    edited.checked("northline", profile)
    resolved = await pinned_index(freeze_profile(edited)).read("northline", (), base.section_id)
    assert resolved and base.body in resolved.body and "Additional client rule." in resolved.body
    with pytest.raises(DomainError, match="outro broker"):
        profile.model_copy(
            update={
                "sections": (
                    *profile.sections,
                    extension.model_copy(update={"client_id": "keystone"}),
                )
            }
        ).checked("northline", profile)
    with pytest.raises(DomainError, match="padrão"):
        profile.model_copy(
            update={"sections": tuple(s for s in profile.sections if s != base)}
        ).checked("northline", profile)


async def test_start_retry_keeps_pinned_sop_and_initial_ping_even_after_profile_changes(load: Load):
    world = await World(load).start()
    stub = ClientStub()
    service = TemporalIngress(world.deps, cast(Client, stub), "test-workspace")
    profile = await default_profile(ROOT / "sops", "northline")
    args = (service, ROOT / "evals", profile, "a_client_a_high_risk", "northline-live", 1)
    result = await start_scenario(*args)
    before = await service.runtime.snapshot("northline-live")
    assert before.load.delay_risk == "high"
    assert len(before.events) == 1
    assert result["workflow_id"] == "load/northline/northline-live"
    changed = profile.model_copy(update={"revision": 2})
    await start_scenario(
        service, ROOT / "evals", changed, "a_client_a_high_risk", "northline-live", 1
    )
    after = await service.runtime.snapshot("northline-live")
    assert after.load.sop_snapshot == before.load.sop_snapshot
    assert len(after.events) == 1
    with pytest.raises(DomainError, match="mudou"):
        await start_scenario(service, ROOT / "evals", changed, "a_client_a_high_risk", "another", 1)
    key = LoadKey("northline", "northline-live")
    assert (await inject_step(service, ROOT / "evals", key, 1)).accepted
    assert (await inject_step(service, ROOT / "evals", key, 1)).duplicate
    with pytest.raises(DomainError, match="timer real"):
        await inject_step(service, ROOT / "evals", key, 2)
    with pytest.raises(DomainError, match="mismatch"):
        await inject_step(service, ROOT / "evals", LoadKey("keystone", key.load_id), 1)
    await service.stop(key)
    with pytest.raises(DomainError, match="parada"):
        await inject_step(service, ROOT / "evals", key, 1)


def test_live_inputs_shift_eta_and_pdf_dates_without_mutating_the_original_fixture():
    from datetime import UTC, datetime

    from freight_agent.adapters.documents.pdfium import PdfiumPageReader
    from freight_agent.orchestration.live_inputs import attachment_bytes, live_message
    from freight_agent.orchestration.workspace import rebase_load

    scenario = next(
        s for s in scenarios(ROOT / "evals", "northline") if s.id == "c1_eta_from_driver"
    )
    step = next(s for s in scenario.steps if s.message)
    assert step.message is not None
    body = live_message(step.message.body, scenario, step, datetime(2026, 10, 22, 0, 0, tzinfo=UTC))
    assert body != step.message.body and ":" in body
    docs = next(s for s in scenarios(ROOT / "evals", "acme") if s.id == "p1_pod_clean_delivers")
    load = rebase_load(
        ROOT / "evals", docs, "acme-live-pod", datetime(2026, 10, 25, 20, 0, tzinfo=UTC)
    )
    fixture = ROOT / "evals/fixtures/docs/pod_clean.pdf"
    original = fixture.read_bytes()
    generated = attachment_bytes(ROOT / "evals", "fixtures/docs/pod_clean.pdf", load)
    text = PdfiumPageReader().pages(generated, "application/pdf")[0].text
    assert "acme-live-pod" in text and "10/25/2026" in text and "55821" in text
    assert fixture.read_bytes() == original
