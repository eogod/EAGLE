"""deterministic validation and fingerprints for agent proposals"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent.timing import runtime_delay
from app.agent.validation import (
    ALLOWED_STEP_TYPES,
    MAX_COMMAND_CHARS,
    PLACEHOLDER_RE,
)

EDIT_OPS = ("add_step", "remove_step", "set_phase")
RUN_OPS = ("run_step",)
MAX_OPS = 10
MAX_CHAIN_STEPS = 60
EDITABLE_STATUSES = ("planned", "pending")


@dataclass(frozen=True)
class StepSnapshot:
    id: int
    phase: str
    step_type: str
    tool_name: str
    command: str
    status: str
    mythic_task_id: int
    mythic_payload_id: int
    mythic_payload_uuid: str
    delay_before_secs: int


@dataclass(frozen=True)
class PlanContext:
    kind: Literal["edit", "run"]
    phases: tuple[str, ...]
    steps: dict[int, StepSnapshot]
    risky_patterns: tuple[str, ...] = ()
    current_step_count: int = 0


@dataclass
class ValidatedPlan:
    ops: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dangerous_steps: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def chain_fingerprint(chain_id: int, phase: str, steps: list[StepSnapshot]) -> str:
    payload = {
        "chain_id": chain_id,
        "phase": phase,
        "steps": [
            [
                step.id,
                step.phase,
                step.step_type,
                step.tool_name,
                step.command,
                step.status,
                step.mythic_task_id,
                step.mythic_payload_id,
                step.mythic_payload_uuid,
                step.delay_before_secs,
            ]
            for step in sorted(steps, key=lambda item: item.id)
        ],
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def step_fingerprint(step: StepSnapshot) -> str:
    payload = [
        step.id,
        step.phase,
        step.step_type,
        step.tool_name,
        step.command,
        step.status,
        step.mythic_task_id,
        step.mythic_payload_id,
        step.mythic_payload_uuid,
        step.delay_before_secs,
    ]
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def command_sha256(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _is_dangerous(command: str, patterns: tuple[str, ...]) -> bool:
    for pattern in patterns:
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def validate_plan(raw_ops: Any, context: PlanContext) -> ValidatedPlan:
    plan = ValidatedPlan()
    if not isinstance(raw_ops, list):
        plan.errors.append("operations must be a list")
        return plan
    if not raw_ops:
        plan.errors.append("the proposal contains no operations")
        return plan
    if len(raw_ops) > MAX_OPS:
        plan.errors.append(f"at most {MAX_OPS} operations are allowed")
        return plan

    allowed = EDIT_OPS if context.kind == "edit" else RUN_OPS
    targets: set[int] = set()
    added = 0

    for index, raw in enumerate(raw_ops, start=1):
        if not isinstance(raw, dict):
            plan.errors.append(f"operation {index} is not an object")
            continue
        op = _text(raw.get("op"), 32)
        if op not in allowed:
            plan.errors.append(
                f"operation '{op}' is not allowed in {context.kind} mode"
            )
            continue
        if op == "add_step":
            added += _validate_add(plan, raw, context)
        elif op == "remove_step":
            _validate_target(plan, raw, context, targets)
        elif op == "run_step":
            _validate_run(plan, raw, context, targets)
        elif op == "set_phase":
            _validate_phase(plan, raw, context)

    if context.kind == "edit" and context.current_step_count + added > MAX_CHAIN_STEPS:
        plan.errors.append(f"chain would exceed {MAX_CHAIN_STEPS} steps")

    return plan


def _validate_add(
    plan: ValidatedPlan, raw: dict[str, Any], context: PlanContext
) -> int:
    phase = _text(raw.get("phase"), 64)
    if phase not in context.phases:
        plan.errors.append(f"unknown phase '{phase}'")
        return 0
    step_type = _text(raw.get("step_type"), 16)
    if step_type not in ALLOWED_STEP_TYPES:
        plan.errors.append(f"unsupported step_type '{step_type}'")
        return 0
    command = str(raw.get("command") or "").strip()
    if not command:
        plan.errors.append("add_step needs a command")
        return 0
    if len(command) > MAX_COMMAND_CHARS:
        plan.errors.append(f"command exceeds {MAX_COMMAND_CHARS} characters")
        return 0
    if PLACEHOLDER_RE.search(command):
        plan.errors.append("add_step contains an unfilled placeholder")
        return 0
    if _is_dangerous(command, context.risky_patterns):
        plan.dangerous_steps.append(len(plan.ops) + 1)
        plan.warnings.append(
            f"add_step {len(plan.ops) + 1} matches the risky-command list"
        )

    try:
        delay = runtime_delay(int(raw.get("delay_seconds") or 0))
    except TypeError, ValueError:
        delay = 0

    plan.ops.append(
        {
            "op": "add_step",
            "phase": phase,
            "step_type": step_type,
            "tool_name": _text(raw.get("tool_name"), 64) or "shell",
            "command": command,
            "delay_seconds": delay,
            "delay_reason": _text(raw.get("delay_reason"), 200),
            "rationale": _text(raw.get("rationale"), 1000),
        }
    )
    return 1


def _target_step(
    plan: ValidatedPlan, raw: dict[str, Any], context: PlanContext
) -> StepSnapshot | None:
    try:
        step_id = int(raw.get("step_id") or 0)
    except TypeError, ValueError:
        plan.errors.append("invalid step_id")
        return None
    step = context.steps.get(step_id)
    if step is None:
        plan.errors.append(f"step {step_id} is not in this chain")
        return None
    if step.status not in EDITABLE_STATUSES:
        plan.errors.append(
            f"step {step_id} is {step.status}; only unexecuted steps are allowed"
        )
        return None
    if step.mythic_task_id or step.mythic_payload_id:
        plan.errors.append(f"step {step_id} already carries an execution ticket")
        return None
    return step


def _validate_target(
    plan: ValidatedPlan,
    raw: dict[str, Any],
    context: PlanContext,
    targets: set[int],
) -> None:
    step = _target_step(plan, raw, context)
    if step is None:
        return
    if step.id in targets:
        plan.errors.append(f"step {step.id} is targeted twice")
        return
    targets.add(step.id)
    plan.ops.append(
        {
            "op": "remove_step",
            "step_id": step.id,
            "reason": _text(raw.get("reason"), 200),
            "expected_step_fingerprint": step_fingerprint(step),
        }
    )


def _validate_run(
    plan: ValidatedPlan,
    raw: dict[str, Any],
    context: PlanContext,
    targets: set[int],
) -> None:
    step = _target_step(plan, raw, context)
    if step is None:
        return
    if step.id in targets:
        plan.errors.append(f"step {step.id} is targeted twice")
        return
    if step.step_type not in ("local", "agent", "bash"):
        plan.errors.append(f"step {step.id} has unsupported type '{step.step_type}'")
        return
    if not step.command.strip():
        plan.errors.append(f"step {step.id} has no command")
        return
    if _is_dangerous(step.command, context.risky_patterns):
        plan.dangerous_steps.append(step.id)
        plan.warnings.append(f"run_step {step.id} matches the risky-command list")
    targets.add(step.id)
    plan.ops.append(
        {
            "op": "run_step",
            "step_id": step.id,
            "expected_step_fingerprint": step_fingerprint(step),
            "command_sha256": command_sha256(step.command),
        }
    )


def _validate_phase(
    plan: ValidatedPlan, raw: dict[str, Any], context: PlanContext
) -> None:
    phase = _text(raw.get("phase"), 64)
    if phase not in context.phases:
        plan.errors.append(f"unknown phase '{phase}'")
        return
    plan.ops.append({"op": "set_phase", "phase": phase})
