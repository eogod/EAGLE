"""plan validation: every rejection branch and fingerprint stability"""

import pytest

from app.agent.edit_plan import (
    PlanContext,
    StepSnapshot,
    chain_fingerprint,
    step_fingerprint,
    validate_plan,
)

PHASES = ("Reconnaissance", "Execution")


def _step(
    step_id: int = 1,
    *,
    status: str = "planned",
    step_type: str = "bash",
    command: str = "echo hi",
    mythic_task_id: int = 0,
) -> StepSnapshot:
    return StepSnapshot(
        id=step_id,
        phase="Execution",
        step_type=step_type,
        tool_name="shell",
        command=command,
        status=status,
        mythic_task_id=mythic_task_id,
        mythic_payload_id=0,
        mythic_payload_uuid="00000000-0000-0000-0000-000000000000",
        delay_before_secs=0,
    )


def _context(*steps: StepSnapshot, kind: str = "edit", count: int = 0) -> PlanContext:
    return PlanContext(
        kind=kind,
        phases=PHASES,
        steps={step.id: step for step in steps},
        current_step_count=count,
    )


ADD = {
    "op": "add_step",
    "phase": "Execution",
    "step_type": "bash",
    "tool_name": "shell",
    "command": "echo hi",
}


def test_plan_shape_errors() -> None:
    assert not validate_plan("nope", _context()).ok
    assert not validate_plan([], _context()).ok
    assert not validate_plan([ADD] * 11, _context()).ok
    assert not validate_plan([["not-a-dict"]], _context()).ok
    assert not validate_plan([{"op": "run_step", "step_id": 1}], _context()).ok


def test_add_step_rejections() -> None:
    cases = [
        (dict(ADD, phase="Unknown"), "unknown phase"),
        (dict(ADD, step_type="custom"), "unsupported step_type"),
        (dict(ADD, command=""), "needs a command"),
        (dict(ADD, command="x" * 4001), "exceeds"),
        (dict(ADD, command="nmap <target>"), "placeholder"),
    ]
    for op, expected in cases:
        plan = validate_plan([op], _context())
        assert not plan.ok
        assert any(expected in error for error in plan.errors), plan.errors


def test_add_step_normalizes_and_survives_bad_delay() -> None:
    plan = validate_plan([dict(ADD, delay_seconds="not-a-number")], _context())
    assert plan.ok
    assert plan.ops[0]["delay_seconds"] == 0
    assert plan.ops[0]["tool_name"] == "shell"


def test_chain_step_limit_is_enforced() -> None:
    plan = validate_plan([ADD], _context(count=60))
    assert not plan.ok
    assert any("exceed" in error for error in plan.errors)


def test_remove_step_rejections() -> None:
    cases = [
        ([{"op": "remove_step", "step_id": "x"}], [_step()], "invalid step_id"),
        ([{"op": "remove_step", "step_id": 9}], [_step()], "not in this chain"),
        (
            [{"op": "remove_step", "step_id": 1}],
            [_step(status="success")],
            "is success",
        ),
        (
            [{"op": "remove_step", "step_id": 1}],
            [_step(mythic_task_id=7)],
            "execution ticket",
        ),
        (
            [
                {"op": "remove_step", "step_id": 1},
                {"op": "remove_step", "step_id": 1},
            ],
            [_step()],
            "targeted twice",
        ),
    ]
    for ops, steps, expected in cases:
        plan = validate_plan(ops, _context(*steps))
        assert not plan.ok
        assert any(expected in error for error in plan.errors), plan.errors


def test_run_step_rejections() -> None:
    cases = [
        ([{"op": "run_step", "step_id": 9}], [_step()], "not in this chain"),
        (
            [
                {"op": "run_step", "step_id": 1},
                {"op": "run_step", "step_id": 1},
            ],
            [_step()],
            "targeted twice",
        ),
        (
            [{"op": "run_step", "step_id": 1}],
            [_step(step_type="custom")],
            "unsupported type",
        ),
        ([{"op": "run_step", "step_id": 1}], [_step(command=" ")], "no command"),
    ]
    for ops, steps, expected in cases:
        plan = validate_plan(ops, _context(*steps, kind="run"))
        assert not plan.ok
        assert any(expected in error for error in plan.errors), plan.errors


def test_set_phase_rejects_unknown_phase() -> None:
    plan = validate_plan([{"op": "set_phase", "phase": "Nowhere"}], _context())
    assert not plan.ok
    assert any("unknown phase" in error for error in plan.errors)


def test_risky_commands_need_acknowledgement() -> None:
    step = _step(command="shutdown -h now")
    context = PlanContext(
        kind="edit",
        phases=PHASES,
        steps={step.id: step},
        risky_patterns=(r"\bshutdown\b",),
    )

    add = validate_plan([dict(ADD, command="shutdown -h now")], context)
    assert add.ok
    assert add.dangerous_steps

    run = validate_plan(
        [{"op": "run_step", "step_id": 1}],
        PlanContext(
            kind="run",
            phases=PHASES,
            steps={step.id: step},
            risky_patterns=(r"\bshutdown\b",),
        ),
    )
    assert run.ok
    assert run.dangerous_steps == [1]


def test_valid_plans_carry_fingerprints() -> None:
    step = _step()
    run = validate_plan([{"op": "run_step", "step_id": 1}], _context(step, kind="run"))
    assert run.ok
    assert run.ops[0]["expected_step_fingerprint"] == step_fingerprint(step)
    assert len(run.ops[0]["command_sha256"]) == 64

    first = chain_fingerprint(1, "Execution", [step])
    assert first == chain_fingerprint(1, "Execution", [step])
    changed = _step(command="echo changed")
    assert first != chain_fingerprint(1, "Execution", [changed])


@pytest.mark.parametrize("kind", ["edit", "run"])
def test_mixed_ops_are_rejected(kind: str) -> None:
    other = "run_step" if kind == "edit" else "add_step"
    plan = validate_plan(
        [ADD, {"op": other, "step_id": 1}], _context(_step(), kind=kind)
    )
    assert not plan.ok
    assert any("not allowed" in error for error in plan.errors)
