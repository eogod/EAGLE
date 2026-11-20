"""Shared agent-test payloads and the fake provider; finding ids stage against
each caller's fakes."""

from collections.abc import Sequence
from typing import Any

from app.services.llm_service import ToolTurn

FINDINGS = {
    "findings": [
        {
            "title": "C2 beacon",
            "description": "host beaconed out",
            "stage": "Command & Control",
            "confidence": 0.8,
            "uncertainty": "",
            "evidence": [{"quote": "C2 beacon to 26.26"}],
        }
    ]
}


def make_draft(finding_ids: list[int]) -> dict:
    return {
        "steps": [
            {
                "phase": "Command & Control",
                "step_type": "bash",
                "tool_name": "shell",
                "command": "echo beacon",
                "finding_ids": finding_ids,
                "confidence": 0.6,
            }
        ]
    }


class FakeProvider:
    """Queued complete()/complete_tools() replies plus optional stream chunks.

    Calls are recorded as (messages, json_mode) so a test can assert what the
    caller saw. stream() raises unless chunks were queued, which keeps a wrong
    code path loud instead of silently returning nothing.
    """

    name = "fake"
    model = "fake-model"

    def __init__(
        self,
        replies: Sequence[str] = (),
        *,
        tool_replies: Sequence[ToolTurn] = (),
        streamed: Sequence[str] | None = None,
    ) -> None:
        self.replies = list(replies)
        self.tool_replies = list(tool_replies)
        self.streamed = None if streamed is None else list(streamed)
        self.calls: list[tuple[list[dict[str, Any]], bool]] = []
        self.tool_calls: list[list[dict[str, Any]]] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def complete(self, messages, *, json_mode=False):
        self.calls.append((messages, json_mode))
        return self.replies.pop(0)

    async def complete_tools(self, messages, tools):
        self.tool_calls.append(messages)
        return self.tool_replies.pop(0)

    def stream(self, messages, *, json_mode=False):
        chunks = self.streamed
        if chunks is None:
            raise AssertionError("stream() was not expected on this provider")
        self.calls.append((messages, json_mode))

        async def gen():
            for chunk in chunks:
                yield chunk

        return gen()
