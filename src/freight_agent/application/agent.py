"""The agent node: one `agent` model call with tools, inside the broker's budgets.

Owner: card W3-AGT (stub from C0; nothing imports it yet).

To build here (spec section 2, AGT):
- `agent_node`, replacing `graph.nodes.decide_node` in the graph
  `prefilter -> triage -> knowledge -> agent -> validate`;
- `Staging`, the live staged plan the action tools answer from;
- `stage_from_transcript(transcript, ctx, rules) -> ...`, the pure fold over
  `LlmResponse.transcript` that recomputes accepted and refused actions in every mode
  (live, replay, scripted), and is the authority over the live `Staging` (F1);
- the budgets of `Broker.limits`: `max_steps` (tool rounds; 0 means one plan in the
  answer and no action tools), `max_cost_usd` (`LlmRequest.max_cost_usd`) and
  `max_rejections` (over it: `escalation.blocked_actions` with the accepted actions).
`finish(no_procedure=True)` with nothing accepted escalates with `escalation.sop_gap`
(same `SOP_GAP_TITLE`, F13). This module then replaces `decide.py` and `tools.py`.
"""
