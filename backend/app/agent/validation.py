"""Deterministic draft validation.

The model proposes; this module decides. Nothing becomes an importable step unless
its stage, step type and evidence references survive these checks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.timing import REPORT_SOURCE, apply_dwell

ALLOWED_STEP_TYPES = ("local", "agent", "bash")
MAX_COMMAND_CHARS = 4000
MAX_STEPS = 40
# unfilled values the model sometimes leaves behind; the pipeline may retry once
PLACEHOLDER_RE = re.compile(
    r"<[A-Za-z_][\w -]{0,24}>|\bTODO\b|\bTBD\b|\bCHANGEME\b|\bYOUR_[A-Z_]+\b",
    re.IGNORECASE,
)


def _as_float(value: Any, fallback: float = 0.0) -> float:
    try:
        number = float(value)
    except TypeError, ValueError:
        return fallback

    return max(0.0, min(1.0, number))


def _as_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []

    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except TypeError, ValueError:
            continue

    return out


def normalize_findings(
    raw: dict[str, Any],
    *,
    phase_names: tuple[str, ...],
) -> tuple[list[dict[str, Any]], list[str]]:
    """normalize model findings; quotes are verified by the pipeline, not here."""
    warnings: list[str] = []
    findings: list[dict[str, Any]] = []
    items = raw.get("findings")
    if not isinstance(items, list):
        return findings, ["model returned no findings list"]

    for index, item in enumerate(items[:12], start=1):
        if not isinstance(item, dict):
            warnings.append(f"finding {index} is not an object")
            continue
        title = str(item.get("title") or "").strip()[:160]
        if not title:
            warnings.append(f"finding {index} has no title")
            continue
        stage = item.get("stage")
        if stage not in phase_names:
            if stage not in (None, ""):
                warnings.append(
                    f"finding '{title}' used unknown stage '{stage}'; left unmapped"
                )
            stage = None

        evidence = []
        for ev in item.get("evidence") or []:
            if isinstance(ev, dict) and isinstance(ev.get("quote"), str):
                evidence.append({"quote": ev["quote"]})

        findings.append(
            {
                "local_id": index,
                "title": title,
                "description": str(item.get("description") or "").strip(),
                "stage": stage,
                "confidence": _as_float(item.get("confidence"), 0.5),
                "uncertainty": str(item.get("uncertainty") or "").strip(),
                "evidence": evidence,
            }
        )

    return findings, warnings


@dataclass(frozen=True)
class _Rules:
    """what a draft is validated against; built once per call."""

    phase_names: tuple[str, ...]
    known_ids: set[int]
    patterns: list[re.Pattern[str]]
    require_commands: bool


@dataclass
class _Report:
    """collectors shared by the per-step pass; the caller reads them at the end."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dangerous: list[int] = field(default_factory=list)


def _known_ids(known: set[int] | None, findings: Any) -> set[int]:
    if known is not None:
        return known
    return {int(f["id"]) for f in findings if isinstance(f, dict) and "id" in f}


def _parse_step(
    index: int, raw: Any, rules: _Rules, report: _Report
) -> dict[str, Any] | None:
    """validate one model step; None means it was rejected."""
    if not isinstance(raw, dict):
        report.errors.append(f"step {index} is not an object")
        return None
    phase = raw.get("phase")
    if phase not in rules.phase_names:
        report.errors.append(f"step {index}: unknown phase '{phase}'")
        return None
    step_type = raw.get("step_type")
    if step_type not in ALLOWED_STEP_TYPES:
        report.errors.append(f"step {index}: unsupported step_type '{step_type}'")
        return None
    command = str(raw.get("command") or "").strip()
    if not command and rules.require_commands:
        report.errors.append(f"step {index}: empty command")
        return None
    if len(command) > MAX_COMMAND_CHARS:
        report.errors.append(f"step {index}: command longer than {MAX_COMMAND_CHARS}")
        return None

    finding_ids = [
        fid for fid in _as_int_list(raw.get("finding_ids")) if fid in rules.known_ids
    ]
    if not finding_ids:
        report.warnings.append(f"step {index}: no valid evidence reference")

    placeholder = PLACEHOLDER_RE.search(command)
    if placeholder is not None:
        report.warnings.append(
            f"step {index}: command uses placeholder {placeholder.group(0)}"
        )

    try:
        delay_seconds = int(raw.get("delay_seconds") or 0)
    except TypeError, ValueError:
        delay_seconds = 0
    delay_source = str(raw.get("delay_source") or "").strip().lower()

    is_dangerous = any(p.search(command) for p in rules.patterns)
    if is_dangerous:
        report.dangerous.append(index)

    return {
        "order": index,
        "phase": phase,
        "step_type": step_type,
        "tool_name": str(raw.get("tool_name") or "shell").strip()[:64],
        "command": command,
        "rationale": str(raw.get("rationale") or "").strip(),
        "finding_ids": finding_ids,
        "confidence": _as_float(raw.get("confidence"), 0.5),
        "uncertainty": str(raw.get("uncertainty") or "").strip(),
        "dangerous": is_dangerous,
        "client_id": f"draft-{index}",
        "delay_seconds": max(0, delay_seconds),
        "delay_reason": str(raw.get("delay_reason") or "").strip()[:200],
        # keeps the total fitting stable when the draft is re-validated on import
        "delay_source": delay_source if delay_source == REPORT_SOURCE else "estimate",
    }


def _warn_duplicates(steps: list[dict[str, Any]], warnings: list[str]) -> None:
    seen_commands: set[str] = set()
    for step in steps:
        key = step["command"].strip().lower()
        if key in seen_commands:
            warnings.append(f"duplicate command: {step['command'][:80]}")
        seen_commands.add(key)


def _renumber(
    steps: list[dict[str, Any]], phase_order: dict[str, int], total_phases: int
) -> None:
    steps.sort(key=lambda s: (phase_order.get(s["phase"], total_phases), s["order"]))
    for position, step in enumerate(steps, start=1):
        step["order"] = position
        # stable id follows the final order so rehydration is deterministic
        step["client_id"] = f"draft-{position}"


def validate_draft(
    payload: dict[str, Any],
    *,
    phase_names: tuple[str, ...],
    risky_patterns: list[str] | None = None,
    known_finding_ids: set[int] | None = None,
    require_commands: bool = True,
) -> dict[str, Any]:
    """return a normalized copy of the draft with a `validation` block."""
    draft: dict[str, Any] = json.loads(json.dumps(payload))
    report = _Report()

    findings = draft.get("findings") or []
    if not isinstance(findings, list):
        findings = []
        report.errors.append("findings must be a list")
    rules = _Rules(
        phase_names=phase_names,
        known_ids=_known_ids(known_finding_ids, findings),
        patterns=[re.compile(p) for p in (risky_patterns or [])],
        require_commands=require_commands,
    )

    steps_in = draft.get("steps")
    if not isinstance(steps_in, list) or not steps_in:
        report.errors.append("draft has no steps")
        steps_in = []

    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(steps_in[:MAX_STEPS], start=1):
        step = _parse_step(index, raw_step, rules, report)
        if step is not None:
            steps.append(step)

    _warn_duplicates(steps, report.warnings)
    _renumber(steps, {name: i for i, name in enumerate(phase_names)}, len(phase_names))
    report.warnings.extend(apply_dwell(steps))

    if report.dangerous:
        report.warnings.append(
            "steps flagged dangerous by the lab deny-list: "
            + ", ".join(str(i) for i in report.dangerous)
        )

    draft["steps"] = steps
    draft["findings"] = findings
    draft["validation"] = {
        "errors": report.errors,
        "warnings": report.warnings,
        "dangerous_steps": report.dangerous,
    }
    draft["status"] = "invalid" if report.errors else "needs_review"

    return draft
