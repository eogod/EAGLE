"""Custom (tuple-returning) steps are analyzed by chain_exec, not proc."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import chain_exec_service
from app.services.chain_exec_service import ChainExecutionService


class _Scalars:
    def first(self):
        return None


class _Result:
    def scalars(self):
        return _Scalars()


class _Session:
    async def execute(self, *args, **kwargs):
        return _Result()


@pytest.mark.asyncio
async def test_custom_step_output_is_analyzed_in_chain_exec(monkeypatch):
    @asynccontextmanager
    async def fake_session():
        yield _Session()

    analyzed: list[tuple[str, str]] = []

    async def fake_process(**_kwargs):
        # proc returns (custom result tuple, llm, agent, task)
        return ("first", "second"), "", "", None

    async def fake_analyze(output: str, command: str) -> str:
        analyzed.append((output, command))
        return "analysis"

    monkeypatch.setattr(
        chain_exec_service.database_session, "get_async_session", fake_session
    )
    monkeypatch.setattr(chain_exec_service, "process_approved_cmd", fake_process)
    monkeypatch.setattr(
        chain_exec_service, "analyze_command_output_with_llm", fake_analyze
    )

    step = SimpleNamespace(
        id=1,
        chain_id=2,
        phase="Execution",
        step_type="custom",
        tool_name="custom_tool",
        command="do a thing",
        delay_before_secs=0,
        delay_enabled=True,
    )
    service = ChainExecutionService(session=None, mythic_client=None)  # type: ignore[arg-type]

    chunks = [
        chunk async for chunk in service.perform_chain_step([step], 42, asyncio.Event())
    ]

    payload = json.loads(chunks[0])
    assert analyzed == [("first second", "do a thing")]
    assert payload["llm_analysis"] == "analysis"
    assert payload["AttackStep"]["raw_log"] == "first second"
    assert payload["AttackStep"]["step_id"] is None
