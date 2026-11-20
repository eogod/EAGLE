"""
Module for tasks endpoints, also might repeat tasks
based on chain id or commands and payloads from exported chain
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, StreamingResponse

from app.api import deps
from app.api.deps import ChainController, get_chain_controller
from app.cmd.mythic_events import MythicEventsService
from app.models import User
from app.schemas.requests import (
    ActionApprovalRequest,
    ActionExecutionRequest,
    AgentCommandRequest,
    BashCommandRequest,
    LocalCommandRequest,
    NewAgentRequest,
    NewChainRequest,
)
from app.schemas.responses import (
    AttackStepResponse,
    BashCommandResponse,
    ChainOverviewResponse,
    ChainWithStepsResponse,
    GetChainPhaseResponse,
    LocalCommandResponse,
    NewAgentResponse,
    NewChainResponse,
    NewPayloadResponse,
    NewPhaseResponse,
)
from app.services.bash_service import BashCommandService
from app.services.c2_service import CommandsC2ExecService
from app.services.chain_exec_service import ChainExecutionService
from app.services.chain_service import ChainService
from app.services.payload_service import AgentPayloadService
from app.services.s_deps import (
    get_agent_payload_service,
    get_bash_service,
    get_chain_execution_service,
    get_chain_service,
    get_command_service,
    get_events_service,
)

router = APIRouter()


@router.get(
    "/chains",
    description="List all chains for current user",
    response_model=list[ChainOverviewResponse],
)
async def list_chains(
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
) -> list[ChainOverviewResponse]:
    chains = await chain_service.list_chains(current_user)
    return chains


@router.delete(
    "/chains/{chain_id}",
    description="Delete chain",
)
async def delete_chain(
    chain_id: int,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
):
    await chain_service.delete_chain(chain_id, current_user)
    return {"status": "deleted", "chain_id": chain_id}


@router.post(
    "/new-chain", description="Create new chain", response_model=NewChainResponse
)
async def create_new_chain(
    data: NewChainRequest,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
) -> NewChainResponse:
    chain_id, chain_name, phase = await chain_service.create_chain(
        data.chain_name, current_user
    )
    return NewChainResponse(
        chain_id=chain_id, chain_name=chain_name, current_phase_name=phase
    )


@router.post(
    "/run-command",
    description="Run shell command on zero agent",
    response_model=LocalCommandResponse,
)
async def run_local_command(
    data: LocalCommandRequest,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
    command_service: CommandsC2ExecService = Depends(get_command_service),
) -> LocalCommandResponse:
    chain_name, chain_id, phase_name = await chain_service.get_chain_n_phase(
        data.chain_name, current_user
    )
    result = await command_service.run_local_command(
        data.command,
        data.callback_display_id,
        chain_id,
        phase_name,
        current_user,
        data.is_analyze_llm,
        step_id=data.step_id,
    )
    return LocalCommandResponse(
        chain_name=chain_name,
        **result,
    )


@router.post(
    "/run-bash",
    description="Run bash command directly on EAGLE host (no C2), "
    "ticket now, output & status live in redis events of bash_task",
    response_model=BashCommandResponse,
)
async def run_bash_command(
    data: BashCommandRequest,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
    bash_service: BashCommandService = Depends(get_bash_service),
) -> BashCommandResponse:
    chain_name, _chain_id, _phase = await chain_service.get_chain_n_phase(
        data.chain_name, current_user
    )
    result = await bash_service.start(
        data.command, chain_name, current_user, data.timeout, step_id=data.step_id
    )
    return BashCommandResponse(step_type="bash", **result)


@router.post(
    "/run-agent-command",
    description="Run agent command on remote agent like libinject",
    response_model=LocalCommandResponse,
)
async def run_agent_command(
    data: AgentCommandRequest,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
    command_service: CommandsC2ExecService = Depends(get_command_service),
) -> LocalCommandResponse:
    chain_name, chain_id, phase_name = await chain_service.get_chain_n_phase(
        data.chain_name, current_user
    )
    result = await command_service.run_agent_command(
        data.command_params or "",
        data.callback_display_id,
        data.tool or "shell",
        chain_id,
        phase_name,
        current_user,
        data.is_analyze_llm,
        step_id=data.step_id,
    )
    return LocalCommandResponse(
        chain_name=chain_name,
        **result,
    )


@router.post(
    "/new-agent",
    description="Create new mythic agent payload, return download url",
    response_model=NewPayloadResponse,
)
async def new_agent(
    data: NewAgentRequest,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
    payload_service: AgentPayloadService = Depends(get_agent_payload_service),
) -> NewPayloadResponse:
    chain_name, chain_id, phase_name = await chain_service.get_chain_n_phase(
        data.chain_name, current_user
    )
    result = await payload_service.create_payload(
        chain_id, phase_name, data.payload_type, data.os_type
    )
    return NewPayloadResponse(**result)


@router.post(
    "/update-agents",
    description="Add agent to chain with save as AttackStep, Agent",
    response_model=NewAgentResponse,
)
async def update_agent(
    rhost: str,
    chain_name: str,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
    payload_service: AgentPayloadService = Depends(get_agent_payload_service),
) -> NewAgentResponse:
    _, chain_id, phase_name = await chain_service.get_chain_n_phase(
        chain_name, current_user
    )
    result = await payload_service.register_agent(rhost, chain_id, phase_name)
    return NewAgentResponse(**result)


@router.get(
    "/status/{display_id}", description="Get status of agent by callback_display_id"
)
async def read_agent_status(
    display_id: int,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    command_service: CommandsC2ExecService = Depends(get_command_service),
) -> str:
    """current_user is only the auth gate"""
    return await command_service.get_agent_status(display_id)


@router.get(
    "/step/{step_id}",
    description="Snapshot of step & mythic task statuses from db",
)
async def read_step_snapshot(
    step_id: int,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    command_service: CommandsC2ExecService = Depends(get_command_service),
) -> dict[str, Any]:
    """current_user is only the auth gate"""
    return await command_service.get_step_with_tasks(step_id)


@router.get(
    "/step/{step_id}/events",
    description="Live events of step & mythic tasks via redis stream,"
    " ndjson, ends after output & analysis got calm",
)
async def step_events(  # noqa: PLR0913, PLR0917
    step_id: int,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    command_service: CommandsC2ExecService = Depends(get_command_service),
    events_service: MythicEventsService = Depends(get_events_service),
) -> StreamingResponse:
    """current_user is only the auth gate"""
    snapshot = await command_service.get_step_with_tasks(step_id)
    # stream protocol (terminal/grace/idle) lives in the events service
    return StreamingResponse(
        events_service.stream_step_events(step_id, snapshot),
        media_type="application/json",
    )


@router.post(
    "/step/{step_id}/analyze",
    description="Explicit llm analysis of the step output,"
    " result saved to db & mirrored to the events stream",
)
async def analyze_step(
    step_id: int,
    current_user: User = Depends(deps.get_current_user),
    command_service: CommandsC2ExecService = Depends(get_command_service),
    events_service: MythicEventsService = Depends(get_events_service),
) -> dict[str, Any]:
    result = await command_service.analyze_step(step_id, current_user)
    if events_service.available:
        await events_service.emit_analysis(step_id, result["llm_analysis"])
    return result


@router.get(
    "/chain-phase/{chain_id}",
    description="Get chain info and UCKC phase, last attack step",
)
async def read_chain_info(
    chain_id: int,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
) -> GetChainPhaseResponse:
    info = await chain_service.read_chain_info(chain_id, current_user)
    last_step = info.pop("last_attack_step")
    return GetChainPhaseResponse(
        **info,
        last_attack_step=AttackStepResponse.from_orm(last_step) if last_step else None,
    )


@router.get(
    "/chain/{chain_id}",
    description="Get full chain with all steps",
    response_model=ChainWithStepsResponse,
)
async def read_chain_with_steps(
    chain_id: int,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
) -> ChainWithStepsResponse:
    info = await chain_service.get_chain_with_steps(chain_id, current_user)
    steps = info.pop("steps", [])

    return ChainWithStepsResponse(
        chain_id=info["chain_id"],
        chain_name=info["chain_name"],
        current_phase_name=info["current_phase_name"],
        steps=[AttackStepResponse.from_orm(s) for s in steps],
    )


@router.delete(
    "/step/{step_id}",
    description="Delete a specific step from the chain",
)
async def delete_step(
    step_id: int,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
):
    await chain_service.delete_step(step_id, current_user)
    return {"status": "deleted", "step_id": step_id}


@router.patch(
    "/step/{step_id}",
    description="Edit a specific step fields, "
    "then move step to the end of current phase",
)
async def edit_step(
    step_id: int,
    patch: dict[str, Any],
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
):
    step = await chain_service.edit_step(step_id, current_user, patch)
    return {
        "id": step.id,
        "chain_id": step.chain_id,
        "phase": step.phase,
        "step_type": step.step_type,
        "tool_name": step.tool_name,
        "command": step.command,
        "status": step.status,
        "raw_log": step.raw_log,
        "delay_before_secs": step.delay_before_secs,
        "delay_reason": step.delay_reason,
        "delay_enabled": step.delay_enabled,
    }


@router.patch(
    "/chain/{chain_id}/delay",
    description="Enable or disable dwell for every step of one phase",
)
async def set_phase_timing(
    chain_id: int,
    phase: str,
    enabled: bool,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
) -> dict[str, Any]:
    updated = await chain_service.set_phase_timing(
        chain_id, phase, enabled, current_user
    )
    return {
        "chain_id": chain_id,
        "phase": phase,
        "enabled": enabled,
        "updated": updated,
    }


@router.post(
    "/next-phase/{chain_id}",
    description="Switch to the next chain phase",
    response_model=NewPhaseResponse,
)
async def next_phase(
    chain_id: int,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    chain_service: ChainService = Depends(get_chain_service),
) -> NewPhaseResponse:
    """current_user is only the auth gate, ownership checked in service layer"""
    c_id, phase = await chain_service.next_phase(chain_id)
    return NewPhaseResponse(chain_id=c_id, current_phase_name=phase)


@router.post(
    "/set-phase/{chain_id}",
    description="Switch to the specific chain phase",
    response_model=NewPhaseResponse,
)
async def set_phase(
    chain_id: int,
    phase_name: str,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    chain_service: ChainService = Depends(get_chain_service),
) -> NewPhaseResponse:
    """current_user is only the auth gate, ownership checked in service layer"""
    c_id, phase = await chain_service.set_phase(chain_id, phase_name)
    return NewPhaseResponse(chain_id=c_id, current_phase_name=phase)


@router.post(
    "/reject-s/{chain_name}",
    description="Reject the last suggested step in the current chain",
    response_model=AttackStepResponse,
)
async def reject_last_step_or_cmd(
    chain_name: str,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
) -> AttackStepResponse:
    step = await chain_service.reject_last_step(chain_name, current_user)
    return AttackStepResponse.from_orm(step)


@router.post(
    "/run-chain/{chain_id}",
    description="Run attack chain from db by id",
)
async def run_chain(  # noqa: PLR0913, PLR0917
    chain_id: int,
    zero_display_id: int,
    timing: bool = False,
    current_user: User = Depends(deps.get_current_user),
    chain_service: ChainService = Depends(get_chain_service),
    chain_execution_service: ChainExecutionService = Depends(
        get_chain_execution_service
    ),
    chain_controller: ChainController = Depends(get_chain_controller),
) -> StreamingResponse:
    steps = await chain_service.get_chain_steps(chain_id)
    sorted_steps = chain_execution_service.get_sorted_steps(steps)
    cancel_event = asyncio.Event()
    chain_controller.active_chains[chain_id] = cancel_event
    return StreamingResponse(
        chain_execution_service.perform_chain_step(
            sorted_steps, zero_display_id, cancel_event, timing
        ),
        media_type="application/json",
    )


@router.websocket("/ws/cancel-chain/{chain_id}")
async def cancel_chain_ws(
    websocket: WebSocket,
    chain_id: int,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    chain_service: ChainService = Depends(get_chain_service),
    chain_controller: ChainController = Depends(get_chain_controller),
) -> None:
    """current_user is only the auth gate, cookies ride the ws upgrade"""
    await websocket.accept()
    try:
        chain_name = await websocket.receive_text()
        verified = await chain_service.verify_chain_name(chain_id, chain_name)
        if verified:
            chain_controller.cancel_chain(chain_id)
    except WebSocketDisconnect:
        pass


@router.post("/cancel-chain/{chain_id}")
async def cancel_chain_a_http(
    chain_id: int,
    chain_name: str,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    chain_service: ChainService = Depends(get_chain_service),
    chain_controller: ChainController = Depends(get_chain_controller),
) -> JSONResponse:
    """current_user is only the auth gate, chain name itself is the secret"""
    verified = await chain_service.verify_chain_name(chain_id, chain_name)
    if verified:
        chain_controller.cancel_chain(chain_id)
    return JSONResponse(content={"status": "canceled", "chain_name": chain_name})


@router.post("/approve-action")
async def approve_action_from_llm(
    action_request: ActionApprovalRequest,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    command_service: CommandsC2ExecService = Depends(get_command_service),
) -> dict[str, Any]:
    """current_user is only the auth gate, actions execute on real agents"""
    try:
        result = await command_service.approve_action(
            command=action_request.command,
            chain_id=action_request.chain_id,
            phase=action_request.phase,
            agent_id=action_request.agent_id,
            type_cmd=action_request.type_cmd,
            type_tool=action_request.type_tool,
            target_os_type=action_request.target_os_type,
        )
        return {
            "success": True,
            "step_id": result.get("attack_step_id"),
            "status": result.get("status"),
            "mythic_task_id": result.get("mythic_task_id"),
            "llm_analysis": result.get("llm_analysis"),
            "message": "Action submitted, track by step events stream",
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error executing approved action: {str(e)}"
        ) from e


@router.post("/api/llm/action/execute")
async def execute_approved_action(
    action_request: ActionExecutionRequest,
    current_user: User = Depends(deps.get_current_user),  # noqa: ARG001
    command_service: CommandsC2ExecService = Depends(get_command_service),
) -> dict[str, Any]:
    """current_user is only the auth gate, actions execute on real agents"""
    try:
        if not action_request.command:
            raise HTTPException(status_code=400, detail="Command is required")
        result = await command_service.execute_approved_action(
            command=action_request.command,
            agent_display_id=action_request.agent_display_id,
            chain_id=action_request.chain_id,
            phase=action_request.phase,
        )
        return {
            "success": True,
            "attack_step": result["attack_step"],
            "message": "Action executed and saved to AttackStep successfully",
            "llm analysis": str(result["llm_analysis"]),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to execute approved action: {str(e)}"
        ) from e
