"""Tool-loop behaviour: budgets, events, degradation; tools executor and research wiring."""

import asyncio

import pytest

from app.agent.research import run_research
from app.agent.tool_loop import (
    BUDGET_EXHAUSTED,
    RunCancelled,
    ToolCallRecord,
    ToolLoopResult,
    run_tool_loop,
)
from app.agent.tools import ToolLimits, ToolServices, execute_tool
from app.agent.web_tools import FetchResult, SearchHit
from app.services.cve import CveInfo
from app.services.llm_service import AgentLLMError, ToolCall, ToolTurn


class FakeProvider:
    """complete_tools pops queued ToolTurns, then complete() returns the final text."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.complete_tools_calls = []
        self.complete_calls = []
        self.tool_error: Exception | None = None

    async def complete_tools(self, messages, tools):
        self.complete_tools_calls.append((messages, tools))
        if self.tool_error is not None:
            raise self.tool_error
        return self.turns.pop(0)

    async def complete(self, messages, *, json_mode=False):
        self.complete_calls.append((messages, json_mode))
        return "final squeeze notes"


async def _not_called(*_args) -> None:
    raise AssertionError("this service must not be called")


def _events() -> tuple[list[tuple[str, dict]], object]:
    events: list[tuple[str, dict]] = []

    async def emit(name, data):
        events.append((name, data))

    return events, emit


def _execute(results: list[str]):
    seen: list[tuple[str, dict]] = []

    async def execute(name, arguments):
        seen.append((name, arguments))
        return results.pop(0)

    return seen, execute


_SERVICES = ToolServices(
    search=_not_called, fetch=_not_called, cve=_not_called, cpe=_not_called
)


async def test_no_tool_calls_returns_notes_without_events():
    provider = FakeProvider([ToolTurn(content="just notes", tool_calls=[])])
    events, emit = _events()
    _, execute = _execute([])

    result = await run_tool_loop(provider, [], execute=execute, emit=emit)

    assert isinstance(result, ToolLoopResult)
    assert result.notes == "just notes"
    assert result.calls == []
    assert result.warnings == []
    assert events == []
    assert not provider.complete_calls


async def test_two_tool_calls_then_final_notes():
    provider = FakeProvider(
        [
            ToolTurn(
                content="",
                tool_calls=[
                    ToolCall(
                        id="t1", name="websearch", arguments={"query": "ssh brute"}
                    ),
                    ToolCall(
                        id="t2", name="get_cve_info", arguments={"cve_id": "CVE-2024-1"}
                    ),
                ],
            ),
            ToolTurn(content="operational notes", tool_calls=[]),
        ]
    )
    events, emit = _events()
    _, execute = _execute(["some results", "cve facts"])

    result = await run_tool_loop(
        provider, [{"role": "user", "content": "go"}], execute=execute, emit=emit
    )

    assert result.notes == "operational notes"
    assert result.calls == [
        ToolCallRecord(
            tool="websearch",
            args={"query": "ssh brute"},
            ok=True,
            summary="some results",
        ),
        ToolCallRecord(
            tool="get_cve_info",
            args={"cve_id": "CVE-2024-1"},
            ok=True,
            summary="cve facts",
        ),
    ]
    assert [(name, data["toolName"]) for name, data in events] == [
        ("tool.started", "websearch"),
        ("tool.completed", "websearch"),
        ("tool.started", "get_cve_info"),
        ("tool.completed", "get_cve_info"),
    ]
    assert events[1][1]["status"] == "complete"
    assert events[1][1]["result"] == {"summary": "some results"}
    assert events[2][1]["args"] == {"cve_id": "CVE-2024-1"}
    # user message + assistant + two tool messages in the second-step transcript
    transcript, tools = provider.complete_tools_calls[1]
    assert tools
    assert transcript[3]["role"] == "tool"
    assert transcript[3]["tool_call_id"] == "t2"
    assert transcript[3]["content"] == "cve facts"


async def test_budget_exhausted_records_error_and_squeezes_final_answer():
    provider = FakeProvider(
        [
            ToolTurn(
                content="",
                tool_calls=[
                    ToolCall(id="t1", name="websearch", arguments={"query": "q"}),
                    ToolCall(id="t2", name="webfetch", arguments={"url": "http://x"}),
                ],
            )
        ]
    )
    events, emit = _events()
    _, execute = _execute(["search ok"])

    result = await run_tool_loop(
        provider, [], execute=execute, emit=emit, max_steps=1, max_calls=1
    )

    assert result.calls[0].ok is True
    assert result.calls[1].ok is False
    assert result.calls[1].summary == "tool budget exhausted"
    assert result.notes == "final squeeze notes"
    assert len(provider.complete_calls) == 1
    nudge = provider.complete_calls[0][0][-1]
    assert nudge["role"] == "user"
    assert "Tool budget exhausted" in nudge["content"]
    # the budget ERROR string landed in the transcript as the last tool result
    tool_messages = [
        m for m in provider.complete_calls[0][0] if m.get("role") == "tool"
    ]
    assert tool_messages[-1]["content"] == BUDGET_EXHAUSTED
    assert events[-1][1]["status"] == "error"


async def test_provider_error_degrades_to_warning():
    provider = FakeProvider([])
    provider.tool_error = AgentLLMError("provider down")
    events, emit = _events()
    _, execute = _execute([])

    result = await run_tool_loop(provider, [], execute=execute, emit=emit)

    assert result.calls == []
    assert not result.notes
    assert any("provider down" in w for w in result.warnings)
    assert events == []


async def test_cancelled_before_loop():
    provider = FakeProvider([ToolTurn(content="x", tool_calls=[])])
    cancel = asyncio.Event()
    cancel.set()
    _, execute = _execute([])

    with pytest.raises(RunCancelled):
        await run_tool_loop(provider, [], execute=execute, cancel_event=cancel)
    assert provider.complete_tools_calls == []


async def test_execute_tool_unknown_name():
    result = await execute_tool("netcat", {}, _SERVICES, ToolLimits())
    assert result == "ERROR: unknown tool netcat."


async def test_execute_tool_websearch_formats_hits():
    seen: list[tuple[str, int]] = []

    async def search(query: str, limit: int) -> list:
        seen.append((query, limit))
        return [
            SearchHit(title="HackTricks SSH", url="https://x/ssh", snippet="s" * 400)
        ]

    services = ToolServices(
        search=search,
        fetch=_SERVICES.fetch,
        cve=_SERVICES.cve,
        cpe=_SERVICES.cpe,
    )
    result = await execute_tool(
        "websearch",
        {"query": " ssh brute force ", "max_results": "7"},
        services,
        ToolLimits(),
    )

    assert seen == [("ssh brute force", 7)]
    assert result.startswith("Found 1 search results:\n1. HackTricks SSH\n")
    assert "   URL: https://x/ssh\n" in result
    summary = [line for line in result.splitlines() if line.startswith("   Summary: ")][
        0
    ]
    assert len(summary) == len("   Summary: ") + 300


async def test_execute_tool_cve_bad_id():
    result = await execute_tool(
        "get_cve_info", {"cve_id": "nonsense"}, _SERVICES, ToolLimits()
    )
    assert result.startswith("ERROR:")
    assert "CVE-2024-12345" in result


async def test_execute_tool_webfetch_pages():
    calls: list[tuple[str, int, int]] = []

    async def fetch(url: str, offset: int, max_length: int) -> FetchResult:
        calls.append((url, offset, max_length))
        truncated = offset == 0
        content = "PAGE1" if truncated else "PAGE2"
        return FetchResult(
            url=url,
            content=content,
            content_type="text/markdown",
            total_length=12,
            truncated=truncated,
        )

    services = ToolServices(
        search=_SERVICES.search,
        fetch=fetch,
        cve=_SERVICES.cve,
        cpe=_SERVICES.cpe,
    )
    limits = ToolLimits(fetch_max_chars=5)
    first = await execute_tool(
        "webfetch", {"url": "http://docs/x", "max_length": "junk"}, services, limits
    )
    assert calls[-1] == ("http://docs/x", 0, 5)
    assert first.startswith("URL: http://docs/x\nLength: 12 chars\n")
    assert "Truncated. Call again with offset=5 for more.\n" in first
    assert first.endswith("---\nPAGE1")

    second = await execute_tool(
        "webfetch", {"url": "http://docs/x", "offset": 5}, services, limits
    )
    assert calls[-1] == ("http://docs/x", 5, 5)
    assert second.endswith("PAGE2")
    assert "Truncated" not in second


async def test_execute_tool_service_exception_becomes_error_text():
    async def boom(url: str, offset: int, max_length: int) -> FetchResult:
        raise RuntimeError("connection refused")

    services = ToolServices(
        search=_SERVICES.search,
        fetch=boom,
        cve=_SERVICES.cve,
        cpe=_SERVICES.cpe,
    )
    result = await execute_tool("webfetch", {"url": "http://x"}, services, ToolLimits())
    assert result.startswith("ERROR: fetch failed: connection refused.")
    assert "Try a different URL or continue." in result


async def test_research_truncates_excerpt_and_returns_notes():
    provider = FakeProvider(
        [
            ToolTurn(
                content="",
                tool_calls=[
                    ToolCall(id="t1", name="websearch", arguments={"query": "smb"})
                ],
            ),
            ToolTurn(content="smb signing disabled -> relay", tool_calls=[]),
        ]
    )
    events, emit = _events()
    _, execute = _execute(["search facts"])

    notes, warnings = await run_research(
        provider,
        findings_text="SMB on 445, CVE-2024-12345",
        brief="",
        excerpt="y" * 20_000,
        chain_context="step 3 succeeded: foothold",
        tools=_SERVICES,
        max_steps=2,
        max_calls=3,
        emit=emit,
    )

    assert notes == "smb signing disabled -> relay"
    assert warnings == []
    messages = provider.complete_tools_calls[0][0]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "budget is 3 tool calls" in messages[0]["content"]
    assert "max 3000 characters" in messages[0]["content"]
    # 20 000-char excerpt cut to webtools.excerpt_chars (6000)
    assert "y" * 6001 not in messages[1]["content"]
    assert "y" * 6000 in messages[1]["content"]
    assert "<untrusted_document>" in messages[1]["content"]
    assert "Operator brief:\n(none)" in messages[1]["content"]
    assert "step 3 succeeded: foothold" in messages[1]["content"]


async def test_research_blank_notes_warns():
    provider = FakeProvider([ToolTurn(content="   ", tool_calls=[])])
    _, execute = _execute([])

    notes, warnings = await run_research(
        provider,
        findings_text="f",
        brief="b",
        excerpt="e",
        tools=_SERVICES,
        max_steps=1,
        max_calls=1,
    )

    assert notes == ""
    assert any("research produced no notes" in w for w in warnings)


async def test_research_cve_error_note_is_surfaced_in_tool_result():
    info = CveInfo(
        cve_id="CVE-2024-12345",
        description="describes the flaw",
        cwes=[],
        cvss_score=None,
        severity="",
        cvss_vector="",
        references=[],
        pocs=[],
        error="NVD unreachable",
    )

    async def cve(cve_id: str) -> CveInfo:
        return info

    services = ToolServices(
        search=_SERVICES.search,
        fetch=_SERVICES.fetch,
        cve=cve,
        cpe=_SERVICES.cpe,
    )
    result = await execute_tool(
        "get_cve_info", {"cve_id": "CVE-2024-12345"}, services, ToolLimits()
    )
    assert result.startswith("Note: NVD unreachable.\n")
