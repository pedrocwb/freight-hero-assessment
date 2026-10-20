"""The agent's tools: read tools and one action tool per `Action` type, plus `finish`.

Owner: card W3-AGT (stub from C0; nothing imports it yet).

Read tools: `search_sop`, `read_sop_section`, `get_history`, `get_ledger`, `get_load`,
`read_document`, `list_document_requirements`. Action tools (`send_sms`, `send_email`,
`post_broker_slack`, `write_tms_note`, `create_hero_task`, `post_internal_slack`,
`update_stage`, `schedule_timer`, `defer_obligation`, `close_obligation`,
`open_obligation`) build the `Action`, run `domain.validation.check_action` against the
actions staged so far and answer `accepted as action #n` or `rejected by <rule>: ...`.
A tool never executes anything (`ports.llm.ToolSpec`). `finish(intent, summary,
no_procedure)` ends the loop. Replaces `application/tools.py`.
"""
