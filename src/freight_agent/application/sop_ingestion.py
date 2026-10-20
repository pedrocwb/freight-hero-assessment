"""SOP onboarding: upload a broker's documents, compile a draft, lint it, publish it.

Owner: card W3-KNW (stub from C0; nothing imports it yet).

To build here: `ingest_document` (blob, pages through a `PageReader`, a `SopDocument`),
`compile_draft` (the `compile` role, structured output `CompiledDraft`: sections with
title, description, verbatim body and proposed `applies`, proposed new intents and
proposed document requirements), `lint_draft`, and `publish_draft` (embeddings, content
hash, lint, then in one unit of work: save the `SopVersion` and save the broker with
`sop_version`, `config_version + 1` and `rollout = {percent: 100, previous_version}`).
"""
