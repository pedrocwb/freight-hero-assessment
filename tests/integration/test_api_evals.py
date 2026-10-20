"""The eval page: list the scenarios and run a suite, or one scenario, k times; and a
broker's eval runs (`POST /api/brokers/{broker_id}/evals/run`)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freight_agent.api import evals as api_evals
from freight_agent.domain.actions import Plan
from freight_agent.domain.intents import GateDecision, Intent
from freight_agent.domain.runs import Usage
from freight_agent.domain.sop import SopVersion
from freight_agent.evalkit.brokers import BrokerExport, read_broker_export
from freight_agent.ports.llm import LlmRequest, LlmResponse, SchemaT
from tests.evalkit.test_langsmith_experiment import FakeClient
from tests.integration.test_api_harness import ROOT, Api, api_settings, open_api

pytestmark = pytest.mark.unit

SCENARIO = "a_client_a_high_risk"


@pytest.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    async with open_api(tmp_path) as api:
        yield api


async def test_scenarios_are_listed_with_their_suite_and_tags(api: Api) -> None:
    scenarios = {s["id"]: s for s in await api.get("/api/evals/scenarios")}

    scenario = scenarios[SCENARIO]
    assert set(scenario) == {"id", "title", "suite", "tags"}
    assert scenario["suite"] == "core"
    assert "sop:delay_risk_high" in scenario["tags"]
    assert len(scenarios) >= 20


async def test_one_scenario_runs_in_scripted_mode_and_passes(api: Api) -> None:
    response = await api.client.post(
        "/api/evals/run", json={"suite": "core", "mode": "scripted", "k": 2, "scenario": SCENARIO}
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert (summary["suite"], summary["mode"], summary["k"]) == ("core", "scripted", 2)
    assert (summary["total"], summary["passed"], summary["failed"]) == (1, 1, 0)
    assert summary["pass_rate"] == 1.0
    assert len(summary["sop_version"]) == 12
    [scenario] = summary["scenarios"]
    assert scenario["id"] == SCENARIO
    assert (scenario["trials"], scenario["passes"]) == (2, 2)
    assert (scenario["passed"], scenario["pass_at_k"], scenario["pass_hat_k"]) == (True, True, True)
    assert scenario["failures"] == []
    assert scenario["cost_usd"] == 0.0


async def test_replay_without_cassettes_fails_with_the_reason(api: Api) -> None:
    response = await api.client.post(
        "/api/evals/run", json={"mode": "replay", "scenario": SCENARIO}
    )

    assert response.status_code == 200, response.text
    [scenario] = response.json()["scenarios"]
    assert scenario["passed"] is False
    assert any(f["check"] == "error" for f in scenario["failures"])


async def test_an_unknown_scenario_is_a_404(api: Api) -> None:
    response = await api.client.post("/api/evals/run", json={"scenario": "nope"})

    assert response.status_code == 404
    assert response.json() == {"detail": "no scenario 'nope' in suite 'core'"}


async def test_an_unknown_suite_is_a_404(api: Api) -> None:
    response = await api.client.post("/api/evals/run", json={"suite": "nope"})

    assert response.status_code == 404
    assert response.json() == {"detail": "no scenarios in suite 'nope'"}


async def test_live_mode_is_not_offered(api: Api) -> None:
    response = await api.client.post("/api/evals/run", json={"mode": "live"})

    assert response.status_code == 422


# --- a broker's eval runs -------------------------------------------------------------------

ACME = ROOT / "evals" / "brokers" / "acme.json"


async def save_acme(api: Api) -> BrokerExport:
    """The Acme broker and its published SOP version, in the session's store."""
    acme = read_broker_export(ACME)
    service = api.app.state.service
    version = SopVersion(
        broker_id="acme",
        version=acme.broker.sop_version or "v",
        sections=acme.sections,
        published_at=datetime(2026, 10, 20, tzinfo=UTC),
        published_by="test",
    )
    async with service.uow_factory() as uow:
        await uow.brokers.save(acme.broker)
        await uow.sops.save_version(version)
        await uow.commit()
    return acme


async def test_a_suite_runs_on_a_stored_broker_and_its_published_sections(api: Api) -> None:
    await save_acme(api)

    response = await api.client.post(
        "/api/brokers/acme/evals/run", json={"suite": "agent", "mode": "scripted"}
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert (summary["suite"], summary["mode"], summary["broker"]) == (
        "agent",
        "scripted",
        "acme@v2",
    )
    assert "+acme:" in summary["sop_version"]
    assert summary["total"] == 3
    assert summary["langsmith_url"] is None and summary["stopped"] is None


async def test_the_standard_broker_runs_before_any_broker_is_saved(api: Api) -> None:
    response = await api.client.post(
        "/api/brokers/standard/evals/run", json={"suite": "core", "k": 1, "cost_cap_usd": 0.1}
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["broker"] == "standard@v1"
    assert (summary["total"], summary["passed"]) == (22, 22)


async def test_an_unknown_broker_is_a_404(api: Api) -> None:
    response = await api.client.post("/api/brokers/nope/evals/run", json={})

    assert response.status_code == 404
    assert response.json() == {"detail": "no broker 'nope'"}


async def test_record_mode_belongs_to_the_cli(api: Api) -> None:
    response = await api.client.post("/api/brokers/standard/evals/run", json={"mode": "record"})

    assert response.status_code == 400
    assert "run it from the CLI" in response.json()["detail"]


async def test_live_mode_without_a_key_is_a_400_before_any_trial(api: Api) -> None:
    response = await api.client.post("/api/brokers/standard/evals/run", json={"mode": "live"})

    assert response.status_code == 400
    assert "cannot build the live model" in response.json()["detail"]


class PricedModel:
    """A stand-in live model: every call costs $0.30; the gate never acts."""

    async def generate(self, request: LlmRequest, schema: type[SchemaT]) -> LlmResponse[SchemaT]:
        output = (
            GateDecision(intent=Intent.ACKNOWLEDGEMENT, act=False, reason="ok")
            if schema is GateDecision
            else Plan()
        )
        usage = Usage(role=request.role.value, model="fake", cost_usd=0.3)
        return LlmResponse(parsed=schema.model_validate(output.model_dump()), usage=usage)


async def test_a_live_run_stops_at_its_cost_cap_and_links_its_langsmith_experiment(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeClient()
    monkeypatch.setattr(api_evals, "build_llm", lambda settings, **kwargs: PricedModel())
    monkeypatch.setattr(api_evals, "langsmith_client", lambda settings: client)

    response = await api.client.post(
        "/api/brokers/standard/evals/run",
        json={"suite": "agent", "mode": "live", "cost_cap_usd": 0.5},
    )

    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["mode"] == "live"
    assert summary["stopped"].startswith("stopped by the cost cap")
    assert summary["not_run"] == ["t1_gap_search_then_hero"]
    assert summary["langsmith_url"].startswith("https://ls/datasets/d1/compare?selectedSessions=")
    assert client.projects and client.feedback


async def test_a_live_run_goes_on_without_langsmith_when_it_is_down(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_evals, "build_llm", lambda settings, **kwargs: PricedModel())
    monkeypatch.setattr(
        api_evals, "langsmith_client", lambda settings: FakeClient(fail={"create_dataset"})
    )

    response = await api.client.post(
        "/api/brokers/standard/evals/run", json={"suite": "agent", "mode": "live", "k": 1}
    )

    assert response.status_code == 200, response.text
    assert response.json()["langsmith_url"] is None


def test_the_langsmith_client_needs_a_key(tmp_path: Path) -> None:
    assert api_evals.langsmith_client(api_settings(tmp_path)) is None
    keyed = api_settings(tmp_path, langsmith_api_key="ls-test")
    assert api_evals.langsmith_client(keyed) is not None


async def test_session_broker_reads_only_the_brokers_own_published_sections(api: Api) -> None:
    acme = await save_acme(api)

    export = await api_evals.session_broker(api.app.state.service.uow_factory, "acme")

    assert export == acme
