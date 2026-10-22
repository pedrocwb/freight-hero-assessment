# Stop grounding

The operational stop is resolved by `Load.current_stop()`. The agent receives its
sequence, type, facility, city and relationship to the current stage in the load
context, the `get_load` tool and a short reminder beside the current event.

| Stage | Operational stop | Relationship |
|---|---|---|
| dispatched / to_pickup | Pickup | Heading to |
| at_pickup | Pickup | Already at |
| to_delivery | Delivery | Heading to |
| at_delivery | Delivery | Already at |
| delivered | Delivery | Completed; no pending ETA |

An explicit `current_stop_sequence` selects the stop within a leg with multiple
stops. This change reuses the domain resolver; it does not implement automatic
multi-stop sequencing.

For ETA requests and delay escalations, "next stop" or "destination" in an older
SOP means the operational stop, not the next row in the itinerary. A departure
advances the stage before subsequent events use the new stop. Questions explicitly
about a different stop can still use the full itinerary.

The correction lives in the runtime's prompt variables. It therefore applies to
local templates, pinned LangSmith templates and existing per-load SOP snapshots.
Saved customer SOPs are not rewritten. New workspace defaults also use the explicit
term "operational stop".

## Regression eval

`evals/scenarios/grounding/` contains seven scenarios: the six stages and a
pickup-to-delivery transition on the same load. The two in-transit scenarios check
both the initial SMS and the later Keystone Slack escalation. The transition
retains the earlier pickup reply in the transcript to catch stale-stop reuse.

- Deterministic checks require the appropriate facility/city and reject the other
  stop, missing stop information and ambiguous references to both stops.
- An LLM judge checks that the facility is actually the target of the alert or
  answer, and that arrived/completed stages are not described as future arrivals.
- Negative controls run the actual YAML checks against correct and corrupted
  outputs, including the original "next stop (Valley DC, Reno)" pickup regression.
- The original core Keystone scenario now checks its SMS and Slack stop too.

Run the real models with the configured prompts and judge:

```sh
uv run agent eval run --suite grounding --k 3 --cost-cap-usd 0.12
```

`make eval` and the manual live-evals GitHub workflow include this suite by default.
The automated player advances a virtual clock for speed. Operations launched in the
workspace still run through Temporal with real timers. Passing a finite eval is
regression evidence, not a guarantee that a model can never generate an incorrect
facility; this change does not add a deterministic outbound-text blocker.
