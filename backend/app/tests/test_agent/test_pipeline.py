"""Pipeline behaviour: evidence admission, cancellation, degradation."""

import asyncio
import json

import pytest

from app.agent.pipeline import AnalysisRequest, RunCancelled, analyze
from app.tests.test_agent.fixtures import FINDINGS, make_draft

PHASES = ("Command & Control", "Execution")
DOC = [(1, "2026-01-01 C2 beacon to 26.26 observed in powershell logs.")]

# save_finding assigns 7 to the first finding, so the draft must reference 7
DRAFT = make_draft([7])


class FakeProvider:
    """complete()-only on purpose: the stream fake lives in test_agent_api."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, messages, *, json_mode=False):
        self.calls.append((messages, json_mode))
        return self.replies.pop(0)

    def stream(self, messages, *, json_mode=False):
        raise AssertionError("pipeline uses complete()")

    @property
    def call_count(self):
        return len(self.calls)


def _request(provider, **overrides):
    data = {
        "filename": "case.txt",
        "pages": DOC,
        "provider": provider,
        "phase_names": PHASES,
        "phase_hints": "- Command & Control: c2",
        "chunk_chars": 12_000,
        "max_chunks": 10,
    }
    data.update(overrides)
    return AnalysisRequest(**data)


async def _collect(provider, saved=None, cancel_event=None, **overrides):
    events = []
    saved = saved if saved is not None else []

    async def emit(name, data):
        events.append((name, data))

    async def save(finding):
        saved.append(finding)
        return 7 if len(saved) == 1 else 8

    result = await analyze(
        _request(provider, **overrides),
        emit=emit,
        save_finding=save,
        cancel_event=cancel_event,
    )

    return events, saved, result


@pytest.mark.asyncio
async def test_happy_path_emits_progress_and_valid_draft():
    provider = FakeProvider([json.dumps(FINDINGS), json.dumps(DRAFT)])
    events, saved, result = await _collect(provider)
    names = [name for name, _ in events]

    assert names[0] == "tool.started"
    assert names[1] == "tool.completed"
    assert "finding.created" in names
    assert names[-1] == "assistant.delta"
    assert "stage.progress" in names
    assert saved[0]["id"] == 7
    assert result.draft["status"] == "needs_review"
    assert result.draft["steps"][0]["command"] == "echo beacon"
    assert result.draft["steps"][0]["finding_ids"] == [7]
    assert provider.call_count == 2
    assert "1 finding(s)" in result.text


@pytest.mark.asyncio
async def test_finding_without_verbatim_evidence_is_dropped():
    bad = json.loads(json.dumps(FINDINGS))
    bad["findings"][0]["evidence"] = [{"quote": "quote that is not in the report"}]
    provider = FakeProvider([json.dumps(bad), json.dumps(DRAFT)])
    events, saved, result = await _collect(provider)

    assert saved == []
    assert not any(name == "finding.created" for name, _ in events)
    assert any("no verbatim evidence" in w for w in result.warnings)
    # draft still runs, but its finding reference is rejected by the validator
    assert result.draft["steps"][0]["finding_ids"] == []
    assert result.draft["status"] == "needs_review"


@pytest.mark.asyncio
async def test_invalid_json_degrades_without_crashing():
    provider = FakeProvider(["not json at all", "also not json"])
    events, saved, result = await _collect(provider)

    assert saved == []
    assert result.draft["steps"] == []
    assert result.draft["status"] == "invalid"
    assert any("extraction failed" in w for w in result.warnings)
    assert any("draft generation failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_cancellation_before_first_llm_call():
    provider = FakeProvider([json.dumps(FINDINGS)])
    cancel = asyncio.Event()
    cancel.set()
    saved: list = []

    with pytest.raises(RunCancelled):
        await _collect(provider, saved=saved, cancel_event=cancel)
    # empty saved list means save_finding was never reached after cancel
    assert provider.call_count == 0
    assert saved == []


@pytest.mark.asyncio
async def test_chunk_and_finding_caps():
    long_pages = [(1, "x" * 10)]
    # two extract calls (2 chunks), then one draft call
    provider = FakeProvider(
        [json.dumps(FINDINGS), json.dumps(FINDINGS), json.dumps(DRAFT)]
    )
    events, _, result = await _collect(
        provider, pages=long_pages, chunk_chars=4, max_chunks=2
    )
    assert sum(1 for name, _ in events if name == "stage.progress") == 2
    assert any("document truncated" in w for w in result.warnings)
    assert result.draft["validation"]["errors"] == []


@pytest.mark.asyncio
async def test_findings_per_segment_cap():
    many = {
        "findings": [dict(FINDINGS["findings"][0], title=f"f{i}") for i in range(20)]
    }
    provider = FakeProvider([json.dumps(many), json.dumps(DRAFT)])
    _, saved, _ = await _collect(provider, max_findings_per_segment=3)
    assert len(saved) == 3


@pytest.mark.asyncio
async def test_placeholder_retry_fires_once_when_brief_supplies_values():
    sloppy = json.loads(json.dumps(DRAFT))
    sloppy["steps"][0]["command"] = "nmap -sV <IP>"
    fixed = json.loads(json.dumps(DRAFT))
    provider = FakeProvider(
        [json.dumps(FINDINGS), json.dumps(sloppy), json.dumps(fixed)]
    )
    _, _, result = await _collect(provider, brief="target is 10.0.0.5 on 445")

    # extract + first draft + exactly one corrective retry
    assert provider.call_count == 3
    retry_user_message = provider.calls[2][0][-1]["content"]
    assert retry_user_message.startswith("The operator already provided these values")
    # the accepted draft is the corrected one
    assert result.draft["steps"][0]["command"] == "echo beacon"
    assert not any("placeholder" in w for w in result.draft["validation"]["warnings"])


@pytest.mark.asyncio
async def test_placeholder_without_brief_does_not_retry():
    sloppy = json.loads(json.dumps(DRAFT))
    sloppy["steps"][0]["command"] = "nmap -sV TODO"
    provider = FakeProvider([json.dumps(FINDINGS), json.dumps(sloppy)])
    _, _, result = await _collect(provider)

    assert provider.call_count == 2
    assert any("placeholder" in w for w in result.draft["validation"]["warnings"])
