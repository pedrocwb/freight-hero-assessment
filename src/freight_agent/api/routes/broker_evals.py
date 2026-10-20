"""A broker's eval runs: `POST /api/brokers/{broker_id}/evals/run` runs a suite on the
broker's current version (`api.schemas.BrokerEvalRunRequest`).

Owner: card W3-EVK (empty router from C0).
"""

from fastapi import APIRouter

router = APIRouter(prefix="/brokers/{broker_id}/evals", tags=["evals"])
