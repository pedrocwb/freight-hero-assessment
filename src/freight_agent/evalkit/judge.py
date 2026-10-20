"""The LLM judge: grades a run's wording against a scenario's rubric.

Owner: card W3-EVK (stub from C0; nothing imports it yet).

To build here: `JudgeVerdict(score, reasons)`, a grader on the `judge` role called by
the runner on the uncounted inner model (F10: judge calls never count in `llm_calls`),
its usage reported apart; skipped with a warning in scripted mode without `script.judge`.
"""
