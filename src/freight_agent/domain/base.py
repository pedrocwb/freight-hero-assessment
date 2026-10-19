"""Shared base for domain value objects."""

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Immutable, strictly shaped data. Changes produce new instances."""

    model_config = ConfigDict(frozen=True, extra="forbid")
