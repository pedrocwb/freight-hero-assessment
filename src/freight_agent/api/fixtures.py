"""Load metadata the control room can create a load from.

Every JSON file in `evals/fixtures/`, plus two built-in variants of the assignment's load
for the other clients in `docs/decisions.md`: Keystone (Client B) and Keystone reefer
(Client B, refrigerated, at the pickup so that a departure exercises the reefer SOP).
The variants get their own load ids, so all three loads can run side by side in one
simulation; the eval scenarios keep load 481207 and override only the broker and types.
"""

import json
from pathlib import Path
from typing import Any, cast

from freight_agent.api.schemas import Fixture

BASE_FIXTURE = "load_481207"
KEYSTONE = {"id": "keystone", "name": "Keystone Freight"}


def fixture_catalog(evals_dir: Path) -> list[Fixture]:
    fixtures = [
        Fixture(name=path.stem, metadata=_read(path))
        for path in sorted((evals_dir / "fixtures").glob("*.json"))
    ]
    base = next((f for f in fixtures if f.name == BASE_FIXTURE), None)
    if base is not None:
        fixtures += _variants(base.metadata)
    return fixtures


def _read(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: a load fixture must be a JSON object")
    return cast(dict[str, Any], data)


def _variants(base: dict[str, Any]) -> list[Fixture]:
    keystone = {**base, "load_id": "481208", "broker": dict(KEYSTONE)}
    reefer = {
        **base,
        "load_id": "481209",
        "broker": dict(KEYSTONE),
        "commodity": "Frozen vegetables, 40,000 lb, reefer at -10 F",
        "stage": "at_pickup",
        "load_types": ["reefer"],
    }
    return [
        Fixture(name=f"{BASE_FIXTURE}_keystone", metadata=keystone),
        Fixture(name=f"{BASE_FIXTURE}_keystone_reefer", metadata=reefer),
    ]
