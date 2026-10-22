"""Adapt the repository's sample inputs to a load running today, without changing evals."""

import importlib.util
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from freight_agent.domain.load import Load
from freight_agent.evalkit.spec import Scenario, Step


def live_message(body: str, scenario: Scenario, step: Step, now: datetime) -> str:
    """Keep a sample ETA's distance from its input time, expressed in live local time."""
    start = datetime.fromisoformat(f"{scenario.date}T{step.at}").replace(
        tzinfo=ZoneInfo(scenario.timezone)
    )
    local_now = now.astimezone(ZoneInfo(scenario.timezone))

    def shift(match: re.Match) -> str:
        hour, minute = map(int, match.group().split(":"))
        if hour > 23 or minute > 59:
            return match.group()
        promised = start.replace(hour=hour, minute=minute)
        return (local_now + (promised - start)).strftime("%H:%M")

    return re.sub(r"\b\d{1,2}:\d{2}\b", shift, body).replace("this morning", "today")


def attachment_bytes(
    root: Path, fixture: str, load: Load, received_at: datetime | None = None
) -> bytes:
    path = (root / fixture).resolve()
    if not path.is_relative_to(root.resolve() / "fixtures"):
        raise ValueError("Anexo fora da pasta de fixtures.")
    if fixture not in {"fixtures/docs/pod_clean.pdf", "fixtures/docs/pod_exceptions.pdf"}:
        return path.read_bytes()
    # Reuse the project's PDF fixture generator, in a fresh module per request.
    # Its original files and globals are never edited or shared between executions.
    spec = importlib.util.spec_from_file_location(
        "live_fixture", root / "fixtures/docs/make_fixtures.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    pickup, delivery = load.stops[0], load.stops[-1]
    picked = pickup.appointment.start or pickup.appointment.at
    delivered = delivery.appointment.at or delivery.appointment.start
    assert picked and delivered
    picked = picked.astimezone(ZoneInfo(pickup.timezone))
    delivered = delivered.astimezone(ZoneInfo(delivery.timezone))
    vars(module).update(
        LOAD_ID=load.load_id,
        PICKUP_DATE=picked.date().isoformat(),
        DELIVERY_APPOINTMENT=delivered.isoformat(),
        DELIVERED_AT=(
            received_at.astimezone(ZoneInfo(delivery.timezone))
            if received_at
            else delivered + timedelta(minutes=12)
        ).isoformat(),
    )
    bad = fixture.endswith("pod_exceptions.pdf")
    page = module.draw_pod(
        po=module.PO_TYPO if bad else module.PO, exceptions=module.EXCEPTIONS if bad else ""
    )
    return module.pdf.write_pdf(
        [page], title="Proof of delivery", author=load.carrier_name, created=""
    )
