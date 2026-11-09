"""Back-and-forth between the model and execute_tool until budgets are spent."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.agent.errors import RunCancelled
from app.agent.tools import TOOL_ARG_KEY, TOOL_SCHEMAS
from app.services.llm_service import AgentLLMError, ToolCallingProvider, ToolTurn

TOOL_RESULT_MAX = 12_000
SUMMARY_MAX = 180
BUDGET_EXHAUSTED = (
    "ERROR: tool budget exhausted. Stop calling tools and write the final answer now."
)
_BUDGET_NUDGE = (
    "Tool budget exhausted. Write your final operational notes now, no more tool calls."
)

Execute = Callable[[str, dict[str, Any]], Awaitable[str]]
Emit = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    ok: bool
    summary: str


@dataclass
class ToolLoopResult:
    notes: str
    calls: list[ToolCallRecord]
    warnings: list[str]


def _condensed_args(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    key = TOOL_ARG_KEY.get(tool)
    if key and key in arguments:
        return {key: arguments[key]}
    return dict(arguments)


def _summary(result: str) -> str:
    return result[:SUMMARY_MAX]


def _check(cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RunCancelled


async def _noop_emit(_name: str, _data: dict[str, Any]) -> None:
    pass


def _assistant_tool_message(turn: ToolTurn) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": turn.content or "",
        "tool_calls": [
            {
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
                "id": call.id,
            }
            for call in turn.tool_calls
        ],
    }


async def run_tool_loop(  # noqa: PLR0913  loop driver needs provider + budgets + events
    provider: ToolCallingProvider,
    messages: list[dict[str, Any]],
    *,
    execute: Execute,
    max_steps: int = 4,
    max_calls: int = 5,
    emit: Emit | None = None,
    cancel_event: asyncio.Event | None = None,
) -> ToolLoopResult:
    emit = emit or _noop_emit
    transcript = list(messages)
    calls: list[ToolCallRecord] = []
    warnings: list[str] = []
    executed = 0
    turn: ToolTurn | None = None
    fallback_warned = False

    for _ in range(max_steps):
        _check(cancel_event)
        try:
            turn = await provider.complete_tools(transcript, TOOL_SCHEMAS)
        except AgentLLMError as exc:
            warnings.append(f"tool loop stopped: {exc}")
            turn = None
            break
        if getattr(provider, "tools_fallback", False) and not fallback_warned:
            fallback_warned = True
            warnings.append(
                "provider rejected native tool calls; using prompt-emulated tools"
            )
        if not turn.tool_calls:
            return ToolLoopResult(notes=turn.content, calls=calls, warnings=warnings)
        transcript.append(_assistant_tool_message(turn))

        for call in turn.tool_calls:
            if executed < max_calls:
                _check(cancel_event)
                await emit(
                    "tool.started",
                    {
                        "toolCallId": call.id,
                        "toolName": call.name,
                        "args": _condensed_args(call.name, call.arguments),
                    },
                )
                result = await execute(call.name, call.arguments)
                executed += 1
                ok = not result.startswith("ERROR")
                summary = _summary(result)
            else:
                result = BUDGET_EXHAUSTED
                ok = False
                summary = "tool budget exhausted"
            calls.append(
                ToolCallRecord(
                    tool=call.name, args=call.arguments, ok=ok, summary=summary
                )
            )
            await emit(
                "tool.completed",
                {
                    "toolCallId": call.id,
                    "toolName": call.name,
                    "status": "complete" if ok else "error",
                    "result": {"summary": summary},
                },
            )
            transcript.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": result[:TOOL_RESULT_MAX],
                }
            )

    if turn is not None and turn.tool_calls:
        # loop ended with unanswered tool calls; force one final tool-free answer
        try:
            notes = await provider.complete(
                transcript + [{"role": "user", "content": _BUDGET_NUDGE}],
                json_mode=False,
            )
        except AgentLLMError as exc:
            warnings.append(f"final summary failed: {exc}")
            notes = turn.content
        return ToolLoopResult(notes=notes, calls=calls, warnings=warnings)

    return ToolLoopResult(
        notes=turn.content if turn is not None else "",
        calls=calls,
        warnings=warnings,
    )
