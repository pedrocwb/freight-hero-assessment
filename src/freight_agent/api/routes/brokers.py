"""Brokers as data: create, read, change, list versions, roll out (`/api/brokers`).

Owner: card W3-BRK (empty router from C0). Bodies: `api.schemas.BrokerIn`, `BrokerView`,
`BrokerVersionView`, `RolloutIn`. Routes: `POST`, `GET` (list), `GET/PUT /{broker_id}`,
`GET /{broker_id}/versions`, `POST /{broker_id}/rollout`; `GET /api/brokers/standard`
returns the standard catalog.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/brokers", tags=["brokers"])
