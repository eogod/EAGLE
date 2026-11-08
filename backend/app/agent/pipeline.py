"""Deterministic analysis pipeline: ingest -> extract -> map -> draft -> validate."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from string import Template
from typing import Any

from app.agent import prompts as prompt_text
from app.agent.errors import RunCancelled
from app.agent.parsing import (
    Segment,
    parse_json_object,
    quote_in_text,
    segment_pages,
)
from app.agent.validation import normalize_findings, validate_draft
from app.core.config import CHUNK_CHARS, MAX_CHUNKS
from app.services.llm_service import ChatStreamProvider

Emit = Callable[[str, dict[str, Any]], Awaitable[None]]
SaveFinding = Callable[[dict[str, Any]], Awaitable[int]]


@dataclass
class AnalysisRequest:
    filename: str
    pages: list[tuple[int | None, str]]
    provider: ChatStreamProvider
    phase_names: tuple[str, ...]
    phase_hints: str = ""
    risky_patterns: list[str] = field(default_factory=list)
    chunk_chars: int = CHUNK_CHARS
    max_chunks: int = MAX_CHUNKS
    draft_name: str = ""
    max_findings_per_segment: int = 12
    # ask | plan | build; plan allows command-less steps
    mode: str = "build"
    # operator text used when no document is attached
    brief: str = ""
    # compact snapshot of the chain being worked on, for ask mode
    chain_context: str = ""


@dataclass
class AnalysisResult:
    findings: list[dict[str, Any]]
    draft: dict[str, Any]
    text: str
    warnings: list[str]
    mode: str = "build"


def _check(cancel_event: asyncio.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RunCancelled


async def analyze(
    request: AnalysisRequest,
    *,
    emit: Emit,
    save_finding: SaveFinding,
    cancel_event: asyncio.Event | None = None,
) -> AnalysisResult:
    """run the whole analysis, emitting assistant-ui friendly events as it goes."""
    segments = segment_pages(request.pages, max_chars=request.chunk_chars)
    selected = segments[: request.max_chunks]
    warnings: list[str] = []

    extract_id = f"extract-{uuid.uuid4().hex[:12]}"
    await emit(
        "tool.started",
        {
            "toolCallId": extract_id,
            "toolName": "extract_document",
            "args": {"filename": request.filename},
        },
    )
    await emit(
        "tool.completed",
        {
            "toolCallId": extract_id,
            "toolName": "extract_document",
            "status": "complete",
            "result": {
                "segments": len(segments),
                "analyzed_segments": len(selected),
                "pages": len(request.pages),
                "characters": sum(len(text) for _, text in request.pages),
            },
        },
    )

    findings: list[dict[str, Any]] = []
    await emit("stage.started", {"stage": "extract_findings"})
    if not request.pages:
        warnings.append("no document attached: drafting from the operator brief")
        await emit(
            "stage.completed",
            {"stage": "extract_findings", "summary": "no document, using brief"},
        )

    for index, segment in enumerate(selected, start=1):
        _check(cancel_event)
        chunk_findings, chunk_warnings = await _extract_segment(request, segment, index)
        warnings.extend(chunk_warnings)
        for finding in chunk_findings:
            _check(cancel_event)
            finding_id = await save_finding(finding)
            finding["id"] = finding_id
            findings.append(finding)
            await emit("finding.created", {"finding": finding})
        await emit(
            "stage.progress",
            {"stage": "extract_findings", "completed": index, "total": len(selected)},
        )

    if len(segments) > len(selected):
        warnings.append(
            f"document truncated for analysis: {len(selected)} of {len(segments)} "
            "segments processed"
        )
    if request.pages:
        await emit(
            "stage.completed",
            {
                "stage": "extract_findings",
                "summary": f"{len(findings)} evidence-backed finding(s)",
            },
        )

    _check(cancel_event)
    await emit("stage.started", {"stage": "draft_chain"})
    draft_id = f"draft-{uuid.uuid4().hex[:12]}"
    await emit(
        "tool.started",
        {
            "toolCallId": draft_id,
            "toolName": "propose_chain_draft",
            "args": {"name": request.draft_name, "findings": len(findings)},
        },
    )

    draft = await _draft_chain(request, findings, warnings)
    await emit(
        "tool.completed",
        {
            "toolCallId": draft_id,
            "toolName": "propose_chain_draft",
            "status": "complete" if not draft["validation"]["errors"] else "error",
            "result": {
                "steps": len(draft["steps"]),
                "status": draft["status"],
                "errors": draft["validation"]["errors"],
            },
        },
    )
    await emit(
        "stage.completed",
        {
            "stage": "draft_chain",
            "summary": f"{len(draft['steps'])} draft step(s), status {draft['status']}",
        },
    )

    stages = {f.get("stage") for f in findings if f.get("stage")}
    text = prompt_text.SUMMARY_TEMPLATE.format(
        findings=len(findings), stages=len(stages), steps=len(draft["steps"])
    )
    if warnings:
        text += "\nWarnings:\n" + "\n".join(f"- {w}" for w in warnings)
    if draft["validation"]["errors"]:
        text += "\nBlocking errors:\n" + "\n".join(
            f"- {e}" for e in draft["validation"]["errors"]
        )

    await emit("assistant.delta", {"text": text})

    # draft.ready is emitted by the caller after the draft row is persisted,
    # so the event carries the real draft id instead of a placeholder.
    return AnalysisResult(
        findings=findings,
        draft=draft,
        text=text,
        warnings=warnings,
        mode=request.mode,
    )


async def _extract_segment(
    request: AnalysisRequest, segment: Segment, index: int
) -> tuple[list[dict[str, Any]], list[str]]:
    system = Template(prompt_text.EXTRACT_SYSTEM).substitute(
        phases=request.phase_hints or "\n".join(request.phase_names)
    )
    user = Template(prompt_text.EXTRACT_USER).substitute(
        filename=request.filename,
        segment_id=segment.segment_id,
        page=segment.page if segment.page is not None else "n/a",
        char_start=segment.char_start,
        char_end=segment.char_end,
        text=segment.text,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    warnings: list[str] = []
    try:
        raw = await request.provider.complete(messages, json_mode=True)
        parsed = parse_json_object(raw)
    except Exception as exc:
        return [], [f"segment {index}: extraction failed: {exc}"]

    normalized, finding_warnings = normalize_findings(
        parsed, phase_names=request.phase_names
    )
    warnings.extend(f"segment {index}: {w}" for w in finding_warnings)

    accepted: list[dict[str, Any]] = []
    for finding in normalized:
        evidence = [
            {
                "quote": item["quote"],
                "segment_id": segment.segment_id,
                "page": segment.page,
                "char_start": segment.char_start,
                "char_end": segment.char_end,
            }
            for item in finding["evidence"]
            if quote_in_text(item["quote"], segment.text)
        ]
        if not evidence:
            warnings.append(
                f"segment {index}: dropped '{finding['title']}' (no verbatim evidence)"
            )
            continue
        finding["evidence"] = evidence
        accepted.append(finding)
        if len(accepted) >= request.max_findings_per_segment:
            break

    return accepted, warnings


async def answer_question(
    request: AnalysisRequest,
    *,
    question: str,
    emit: Emit,
    cancel_event: asyncio.Event | None = None,
) -> str:
    """read-only Q&A over the report (or the operator's own question)."""
    document = ""
    if request.pages:
        document = "\n".join(text for _, text in request.pages)[:60_000]
        document_block = (
            f"Document: {request.filename}\n"
            f"<untrusted_document>\n{document}\n</untrusted_document>"
        )
    else:
        document_block = "No document is attached. Answer from the question alone."

    chain_block = (
        f"Current chain state:\n{request.chain_context}"
        if request.chain_context
        else "No chain is bound to this session."
    )
    messages = [
        {
            "role": "system",
            "content": Template(prompt_text.ASK_SYSTEM).substitute(),
        },
        {
            "role": "user",
            "content": Template(prompt_text.ASK_USER).substitute(
                document_block=document_block,
                chain_block=chain_block,
                question=question,
            ),
        },
    ]
    chunks: list[str] = []
    async for delta in request.provider.stream(messages):
        _check(cancel_event)
        chunks.append(delta)
        await emit("assistant.delta", {"text": delta})

    return "".join(chunks)


async def edit_chain(
    request: AnalysisRequest,
    *,
    instruction: str,
    emit: Emit,
    apply_ops: Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]],
    cancel_event: asyncio.Event | None = None,
) -> str:
    """ask the model for chain operations, then apply them through the service."""
    messages = [
        {
            "role": "system",
            "content": Template(prompt_text.EDIT_SYSTEM).substitute(
                phases=request.phase_hints or "\n".join(request.phase_names)
            ),
        },
        {
            "role": "user",
            "content": Template(prompt_text.EDIT_USER).substitute(
                chain_block=request.chain_context or "empty chain",
                instruction=instruction,
            ),
        },
    ]

    tool_id = f"edit-{uuid.uuid4().hex[:12]}"
    await emit(
        "tool.started",
        {
            "toolCallId": tool_id,
            "toolName": "edit_chain",
            "args": {"instruction": instruction[:200]},
        },
    )

    operations: list[dict[str, Any]] = []
    summary = ""
    try:
        parsed = parse_json_object(
            await request.provider.complete(messages, json_mode=True)
        )
        raw_ops = parsed.get("operations")
        if isinstance(raw_ops, list):
            operations = [op for op in raw_ops if isinstance(op, dict)][:10]
        summary = str(parsed.get("summary") or "").strip()
    except Exception as exc:
        summary = f"Could not read the edit plan: {exc}"

    _check(cancel_event)
    results = await apply_ops(operations)

    applied = [r for r in results if r.get("status") == "ok"]
    errors = [r for r in results if r.get("status") != "ok"]
    await emit(
        "tool.completed",
        {
            "toolCallId": tool_id,
            "toolName": "edit_chain",
            "status": "error" if errors else "complete",
            "result": {
                "applied": len(applied),
                "errors": len(errors),
                "operations": results,
            },
        },
    )

    lines = [f"Applied {len(applied)} of {len(results)} operation(s)."]
    if summary:
        lines.insert(0, summary)
    for result in results:
        label = result.get("op", "op")
        if result.get("status") == "ok":
            extra = result.get("step_id") or result.get("new_step_id") or ""
            lines.append(f"- {label} {extra}".rstrip())
        else:
            lines.append(f"- {label} failed: {result.get('detail', 'unknown error')}")

    text = "\n".join(lines)
    await emit("edit.applied", {"results": results})
    await emit("assistant.delta", {"text": text})
    return text


async def _draft_chain(
    request: AnalysisRequest, findings: list[dict[str, Any]], warnings: list[str]
) -> dict[str, Any]:
    payload_findings = [
        {
            "id": f["id"],
            "title": f["title"],
            "description": f["description"],
            "stage": f["stage"],
            "confidence": f["confidence"],
            "uncertainty": f["uncertainty"],
        }
        for f in findings
    ]
    rendered = json.dumps(payload_findings, indent=2, ensure_ascii=False)
    system_template = (
        prompt_text.PLAN_SYSTEM if request.mode == "plan" else prompt_text.DRAFT_SYSTEM
    )
    messages = [
        {
            "role": "system",
            "content": Template(system_template).substitute(
                phases=request.phase_hints or "\n".join(request.phase_names)
            ),
        },
        {
            "role": "user",
            "content": Template(prompt_text.DRAFT_USER).substitute(
                findings=rendered,
                brief=request.brief or "(none)",
                chain_block=(
                    f"Current chain (revise the unexecuted steps, keep executed ones):\n"
                    f"{request.chain_context}"
                    if request.chain_context
                    else "Current chain: empty"
                ),
            ),
        },
    ]

    raw_draft: dict[str, Any] = {"steps": []}
    try:
        raw_draft = parse_json_object(
            await request.provider.complete(messages, json_mode=True)
        )
    except Exception as exc:
        warnings.append(f"draft generation failed: {exc}")

    raw_draft["name"] = request.draft_name or request.filename
    raw_draft["findings"] = payload_findings
    validated = validate_draft(
        raw_draft,
        phase_names=request.phase_names,
        risky_patterns=request.risky_patterns,
        known_finding_ids={f["id"] for f in findings},
        require_commands=request.mode != "plan",
    )
    validated["name"] = raw_draft["name"]
    validated["mode"] = request.mode
    if request.mode == "plan":
        validated["status"] = "plan"

    return validated
