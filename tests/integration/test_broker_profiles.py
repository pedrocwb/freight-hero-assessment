"""Broker configuration survives restarts and detects stale browser saves."""

from pathlib import Path

import pytest

from freight_agent.adapters.persistence.postgres.profiles import PostgresProfiles
from freight_agent.adapters.persistence.postgres.store import PostgresUnitOfWorkFactory
from freight_agent.domain.errors import DomainError
from freight_agent.orchestration.profiles import default_profile
from tests.postgres import DATABASE_URL, POSTGRES, scratch_schema

pytestmark = POSTGRES
ROOT = Path(__file__).resolve().parents[2]


async def test_profiles_survive_restart_and_stale_saves_cannot_overwrite():
    profile = (await default_profile(ROOT / "sops", "northline")).model_dump(mode="json")
    other = (await default_profile(ROOT / "sops", "keystone")).model_dump(mode="json")
    async with scratch_schema(DATABASE_URL, prefix="profiles") as schema:
        store = PostgresUnitOfWorkFactory(DATABASE_URL, schema=schema)
        repository = PostgresProfiles(store)
        try:
            assert (await repository.get_or_create("northline", profile))["revision"] == 1
            await repository.get_or_create("keystone", other)
            profile["broker"]["obligations"][0]["follow_up_minutes"] = 1
            saved = await repository.save("northline", 1, profile)
            assert saved["revision"] == 2
            with pytest.raises(DomainError, match="outra janela"):
                await repository.save("northline", 1, profile)
            assert (await repository.get_or_create("keystone", other))["broker"] == other["broker"]
        finally:
            await store.close()
        reopened = PostgresUnitOfWorkFactory(DATABASE_URL, schema=schema)
        try:
            assert await PostgresProfiles(reopened).get_or_create("northline", {}) == saved
        finally:
            await reopened.close()
