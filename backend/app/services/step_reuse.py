"""Execute an existing unexecuted step in place.

Executed steps used to be inserted as new rows, so ids and canvas order
changed after every run. These helpers copy the execution result onto the
original row instead.
"""

from datetime import UTC, datetime

from app.models import AttackStep

# a finished step can be replayed in place: same row, same id, new output
REUSABLE_STATUSES = ("planned", "pending", "success", "fail", "failed", "error")

# TODO: append a run record instead of overwriting the planned row (see README TODO).


def reusable(target: AttackStep | None, chain_id: int) -> AttackStep | None:
    if target is None or target.chain_id != chain_id:
        return None
    # never touch an in-flight step or a rejected one
    if target.status not in REUSABLE_STATUSES:
        return None
    return target


def apply_execution(target: AttackStep, produced: AttackStep) -> AttackStep:
    """keep id/order, take everything the execution produced."""
    target.phase = produced.phase
    target.step_type = produced.step_type
    target.tool_name = produced.tool_name
    target.command = produced.command
    target.mythic_task_id = produced.mythic_task_id
    target.mythic_payload_uuid = produced.mythic_payload_uuid
    target.mythic_payload_id = produced.mythic_payload_id
    target.raw_log = produced.raw_log
    target.llm_analysis = produced.llm_analysis
    target.status = produced.status
    target.is_draft = False
    target.executed_at = datetime.now(UTC)

    return target
