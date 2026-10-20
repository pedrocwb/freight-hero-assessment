# Broker exports

A broker export is one broker's configuration as the evals play it: its catalog (intents,
obligations, document types and requirements, limits, rollout) and its published SOP
sections. With it, a suite runs on that broker without a database:

```
uv run agent eval run --suite core --broker-file evals/brokers/acme.json
uv run agent eval lint --broker-file evals/brokers/acme.json
uv run agent eval draft --broker-file evals/brokers/acme.json --mode live
```

A scenario can also name its broker (`broker_file: brokers/acme.json`, relative to the
evals folder): the `docs` suite runs on Acme this way.

## Format

One JSON object: the fields of `Broker.model_dump(mode="json")`
(`src/freight_agent/domain/broker.py`) plus `sections`, a list of `SopSection`
(`src/freight_agent/domain/sop.py`):

```json
{
  "broker_id": "acme",
  "name": "Acme Freight",
  "triage": true,
  "intents": [{"intent_id": "detention_request", "title": "...", "description": "...", "...": "..."}],
  "obligations": [{"kind": "pod", "follow_up_minutes": 30, "max_attempts": 1, "...": "..."}],
  "document_types": ["..."],
  "documents": [{"type_id": "pod", "at_stage": "at_delivery", "within_minutes": 120, "opens_obligation": "pod"}],
  "limits": {"max_steps": 8, "max_cost_usd": 0.05, "max_rejections": 3, "validation": {"...": "..."}},
  "sop_version": "a1b2c3d4e5f6",
  "config_version": 2,
  "rollout": {"percent": 100, "previous_version": 1},
  "sections": [
    {
      "section_id": "proof_of_delivery",
      "layer": "client",
      "client_id": "acme",
      "title": "Proof of delivery (POD)",
      "description": "...",
      "intents": ["status_update", "pod_follow_up_due", "document_processed"],
      "stages": ["to_delivery", "at_delivery"],
      "body": "...",
      "source": "acme/CO-07 In-Transit Communication, revision 3"
    }
  ]
}
```

- `sections` holds only the broker's own layers: `layer` is `client` or `load_type`, and
  `client_id` is the broker's id. The standard layer comes from `sops/standard/`, which
  every eval reads; a standard section in an export is refused.
- In a trial, the export's sections replace whatever `sops/clients/<broker_id>/` holds for
  that broker; other brokers and the standard layer read through unchanged.
- The load runs on the broker: the export's `broker_id` and `name` replace the load
  fixture's `broker`, and the trial's store holds the broker (so `load_run_context` reads
  its catalog, limits and rollout, as in production).
- The code reads it with `evalkit.brokers.read_broker_export` and writes it with
  `BrokerExport.to_json`. `POST /api/brokers/{broker_id}/evals/run` builds the same object
  from the session's stored broker and its published SOP version.

## Files

| File | Broker |
|---|---|
| `acme.json` | Acme Freight after onboarding `evals/fixtures/sops/acme_sop.md` (CO-07, revision 3): the standard catalog plus the `detention_request` intent and the `pod` obligation (one reminder 120 minutes after the arrival at the delivery stop, then a Hero task), the POD requirement at `at_delivery`, ETA requests every 20 minutes up to 3, and the five sections of the SOP. Config version 2 (version 1 is the copy of the standard catalog). |

`acme.json` follows `evals/fixtures/sops/README.md` ("What a compiler should find" and
"Broker settings a reviewer sets"). Its `detention_request` intent has no scenario yet,
on purpose: `agent eval lint --broker-file evals/brokers/acme.json` reports it, and
`agent eval draft` drafts one.
