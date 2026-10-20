"""Transcripts in the OpenRouter adapter's format, for scripted agent replies (no tests here).

An assistant entry holds a round's tool calls (`{id, name, args}`) and its `usage`; a tool
entry answers one call by id. Scripted and replayed models never run tools, so the agent
folds these entries into its staged plan (`application.agent.stage_from_transcript`).
"""

from collections.abc import Iterator
from itertools import count
from typing import Any

Call = tuple[str, dict[str, Any]]


class Transcript:
    """Builds a transcript round by round; call ids are c1, c2, ... in call order."""

    def __init__(self) -> None:
        self._ids: Iterator[int] = count(1)
        self.entries: list[dict[str, Any]] = [
            {"role": "system", "content": "the agent prompt"},
            {"role": "user", "content": "the event"},
        ]

    def round(
        self, *calls: Call, results: tuple[str, ...] | None = None, cost: float = 0.0
    ) -> "Transcript":
        """One model round. `results` adds a tool entry per call (as a live run would)."""
        ids = [f"c{next(self._ids)}" for _ in calls]
        self.entries.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": i, "name": name, "args": args}
                    for i, (name, args) in zip(ids, calls, strict=True)
                ],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 20,
                    "cached_tokens": 0,
                    "cost_usd": cost,
                },
            }
        )
        if results is not None:
            for call_id, (name, _), content in zip(ids, calls, results, strict=True):
                self.entries.append(
                    {"role": "tool", "tool_call_id": call_id, "name": name, "content": content}
                )
        return self

    def build(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.entries)


def calls(*items: Call) -> tuple[dict[str, Any], ...]:
    """A transcript with one round holding every call, and no tool results."""
    return Transcript().round(*items).build()
