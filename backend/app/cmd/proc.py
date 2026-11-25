"""
module for processing commands in the context of a chain,
based on doc https://www.unifiedkillchain.com/assets/The-Unified-Kill-Chain.pdf
"""

import hashlib
import logging
import time
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.cmd.c2_tool import MythicClient
from app.core.config import PHASE_COMMANDS, UNSAFE_CMD
from app.models import Agent, AttackStep, MythicTask

logger = logging.getLogger(__name__)


@dataclass
class PayloadParams:
    lport: int = -1
    os_type: str = "Windows"


@dataclass
class ChainContext:
    chain_id: int
    phase_name: str


@dataclass
class CallbackInfo:
    """display id + step_type + mythic tool name without the old prefix lore"""

    display_id: int
    step_type: str = "local"
    tool_name: str = ""


async def init_agent():
    """generate payload ->
    download by mythic_payload_uuid ->
    run via subprocess.run, save mythic agent info to db
    save Resource Development info to AttackStep"""
    pass


# TODO: task output arrives only via the events service after submit, so
# nothing feeds live results back and the chain is pre-assembled, idk
async def process_approved_cmd(  # noqa: PLR0911  # one return per step_type branch
    cmd: str,
    ctx: ChainContext,
    cb: CallbackInfo,
    payload_params: PayloadParams | None = None,
    mythic_client: MythicClient | None = None,
):
    """route by step_type (from db) to the right executor,
    no db changes to AttackStep by default cuz it depends on tasks,
    returns (AttackStep|result, llm, agent, MythicTask|None),
    tracker of MythicTask saved by events service via redis"""
    # one return per step_type branch, the switch is the point here
    # noqa: PLR0911
    # TODO: add a manual step type that pauses the chain until an operator confirms.
    supported_type_n = ["local", "agent", "custom", "payload", "getcallback", "bash"]
    if cb.step_type not in supported_type_n:
        raise HTTPException(
            400,
            f"{cb.step_type} not supported step type, we have only {str(supported_type_n)}",
        )

    if payload_params is None:
        payload_params = PayloadParams()

    if cb.step_type == "local":
        result, llm_a, m_task = await check_and_process_local_cmd(
            cmd=cmd,
            display_id=cb.display_id,
            ctx=ctx,
            mythic_client=mythic_client,
        )
        return result, llm_a, "", m_task
    if cb.step_type == "payload":
        result, llm_a = await check_and_create_mpayload(
            ctx,
            cb.tool_name,
            cb.tool_name,
            payload_params,
            mythic_client,
        )
        return result, llm_a, "", None
    if cb.step_type == "getcallback":
        result, agent = await process_new_callback(
            ctx,
            cb.tool_name,
            cmd,
            1,
            mythic_client,
        )
        return result, "", agent, None
    if cb.step_type == "agent":
        result, llm_a, agent, m_task = await check_and_process_agent_cmd(
            cb,
            ctx,
            cmd,
            mythic_client,
        )
        return result, llm_a, agent, m_task
    if cb.step_type == "bash":
        result, llm_a = await check_and_process_bash_cmd(cmd, ctx)
        return result, llm_a, "", None
    # custom: any other scenarios based on custom C2 functions in c2_tool
    if mythic_client is None or not hasattr(mythic_client, cb.tool_name):
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED,
            f"custom tool '{cb.tool_name}' not supported, check c2_tool. Attack step not executed: {cmd}",
        )
    def_func = getattr(mythic_client, cb.tool_name)
    args_e = cmd.split(":")  # enforce LLM to return params as b:c
    result = await def_func(cb.display_id, *args_e)
    # one return shape for every step type; callers unpack all four, and
    # chain_exec fills llm_analysis from the raw custom output
    return result, "", "", None


async def process_new_callback(
    ctx: ChainContext,
    tool_name: str,
    cmd: str,
    parent_step_id: int,
    mythic_client: MythicClient,
) -> tuple[AttackStep, Agent]:
    """for save to Agent table by callback"""
    res = await mythic_client.get_callback_after(cmd)  # cmd is rhost
    os_type, status, display_id = res
    p_id, p_uuid = await mythic_client.get_payload_ids(display_id)
    return AttackStep(
        phase=ctx.phase_name,
        chain_id=ctx.chain_id,
        step_type="getcallback",
        status=status,
        tool_name=tool_name,
        command=cmd,
        mythic_task_id=0,  # just created
        mythic_payload_id=p_id,
        mythic_payload_uuid=p_uuid,
        raw_log=str(res),
    ), Agent(
        step_id=parent_step_id,  # not connected to reproduce as step
        agent_name=f"{os_type}#{str(display_id)}",
        os_type=os_type,
        status=status,
        callback_display_id=display_id,
    )


async def check_and_process_agent_cmd(
    cb: CallbackInfo,
    ctx: ChainContext,
    cmd: str,
    mythic_client: MythicClient,
    is_analyze_llm: bool = False,
) -> tuple[AttackStep, str, Agent | str, MythicTask]:
    """run commands on agent and return ticket for tracking,
    output updates by events service, analysis by explicit endpoint"""
    tool_n = cb.tool_name  # already the plain mythic tool, prefix lives in db
    if cmd in UNSAFE_CMD:
        raise HTTPException(400, "bro, this is unsafe command for sure")
    try:
        # submit task to C2, ticket tracks live status
        ticket = await mythic_client.issue_task_async(
            command_name=tool_n,
            parameters=cmd,  # basically in local_cmd is also params
            callback_display_id=cb.display_id,
        )  # by llm cmd based on get_cmd_list...
    except Exception as e:  # general exception cuz mythic lib
        logger.error(
            "agent command failed for display_id %s tool %s: %s",
            cb.display_id,
            tool_n,
            e,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The command was aborted by C2, the step/agent not saved,"
            " but you can bypass this issue using agent commands"
            " and shell scripts for long time tasks.",
        ) from e
    # for reproducibility
    rhost, agent_os, agent_status = await mythic_client.get_os_by_display_id(
        cb.display_id
    )
    p_id, p_uuid = await mythic_client.get_payload_ids(cb.display_id)
    step = AttackStep(
        phase=ctx.phase_name,
        chain_id=ctx.chain_id,
        step_type="agent",
        status="submitted",
        tool_name=cb.tool_name,
        command=cmd,
        mythic_task_id=ticket.mythic_task_id,
        mythic_payload_id=p_id,
        mythic_payload_uuid=str(p_uuid),
        raw_log="",
    )
    mythic_task = MythicTask(
        mythic_task_id=ticket.mythic_task_id,
        step_id=0,  # set after AttackStep saved, by service layer
        status="submitted",
        auto_analyze=is_analyze_llm,
    )
    return (
        step,
        "",
        Agent(
            step_id=1,  # need update in tasks
            os_type=agent_os,
            status=agent_status,
            agent_name=str(cb.display_id) + "#" + rhost,
            callback_display_id=cb.display_id,
        ),
        mythic_task,
    )


async def check_and_create_mpayload(
    ctx: ChainContext,
    tool_name: str,
    tool_n: str,
    payload_params: PayloadParams,
    mythic_client: MythicClient,
) -> tuple[AttackStep, str]:
    """check payload parameters and create payload, save in mythic,
    return uuid/id to get information or send to rhost"""
    if payload_params.os_type not in ["Windows", "macOS", "Linux"]:  # from mythic api
        raise HTTPException(
            400, detail="OS type not supported, try closest one like Linux"
        )
    # TODO set port/os by C2, get information about agents profile
    file_name = tool_n + hashlib.md5(
        str(time.time()).encode("utf-8"), usedforsecurity=False
    )
    cmd = "create_payload"
    result = await mythic_client.create_payload(
        payload_type=tool_n,
        file_name=file_name,
        lport=payload_params.lport,
        os_type=payload_params.os_type,  # host from default
    )
    # build goes async, watcher in events service updates step like all others
    return AttackStep(
        chain_id=ctx.chain_id,
        tool_name=tool_name,
        phase="Resource Development",
        mythic_task_id=0,
        command=cmd,
        mythic_payload_id=result.payload_id,
        mythic_payload_uuid=str(result.payload_uuid),
        status=result.status,
        raw_log="",
    ), ""


async def get_agent_status(
    callback_display_id: int, mythic_client: MythicClient
) -> str:
    """maybe process status, like if fail -> restart agent in chain"""
    agent_status = await mythic_client.check_status(callback_display_id)
    return agent_status


async def check_and_process_local_cmd(
    cmd: str,
    display_id: int,
    ctx: ChainContext,
    mythic_client: MythicClient,
    is_analyze_llm: bool = False,
) -> tuple[AttackStep, str, MythicTask]:
    """async function for check is safe command ->
    submit on zero agent, formatting to AttackStep with ticket,
    output updates by events service, analysis by explicit endpoint"""
    if cmd in UNSAFE_CMD:
        raise HTTPException(400, "bro, this is unsafe command for sure")
    try:
        # submit command to C2, ticket tracks live status
        ticket = await mythic_client.issue_task_async("shell", cmd, display_id)
    except Exception as e:
        logger.error(
            "local command failed for display %s: %s", display_id, e, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="The command was aborted by C2, the step was not saved,"
            " but you can bypass this issue using bash scripts"
            " for long time tasks.",
        ) from e
    p_id, p_uuid = await mythic_client.get_payload_ids(display_id)

    attack_step = AttackStep(
        chain_id=ctx.chain_id,
        phase=ctx.phase_name,
        step_type="local",
        tool_name=cmd.split(maxsplit=1)[0] or "local",
        command=cmd,
        mythic_task_id=ticket.mythic_task_id,
        mythic_payload_uuid=str(p_uuid),
        mythic_payload_id=p_id,
        raw_log="",
        status="submitted",
    )
    mythic_task = MythicTask(
        mythic_task_id=ticket.mythic_task_id,
        step_id=0,  # set after AttackStep saved, by service layer
        status="submitted",
        auto_analyze=is_analyze_llm,
    )
    return attack_step, "", mythic_task


async def check_and_process_bash_cmd(
    cmd: str,
    ctx: ChainContext,
) -> tuple[AttackStep, str]:
    """direct bash on EAGLE host instead of zero agent, runs inline to
    completion for the chain flow, returns finished AttackStep,
    single-run ticket flow uses services/bash_service + events in redis"""
    from app.cmd.local_bash import (  # noqa: PLC0415  # avoid an import cycle
        BashDisabled,
        BashError,
        run_bash,
    )

    try:
        result = await run_bash(cmd, chain_id=ctx.chain_id)
    except BashDisabled as e:
        raise HTTPException(403, str(e)) from e
    except BashError as e:
        raise HTTPException(400, str(e)) from e
    status = "success" if result.exit_code == 0 else "fail"
    step = AttackStep(
        chain_id=ctx.chain_id,
        phase=ctx.phase_name,
        step_type="bash",
        tool_name=cmd.split(maxsplit=1)[0] or "bash",
        command=cmd,
        mythic_task_id=0,  # no C2 ticket here, tracked by bash_task.id
        mythic_payload_id=0,
        mythic_payload_uuid=str(
            uuid.uuid4()
        ),  # schema needs a value, nothing was built
        raw_log=result.output,
        status=status,
        llm_analysis="",  # manual analyze endpoint only, see tasks_mcp
    )
    return step, ""


async def is_command_allowed_in_phase(
    cmd: str,
    phase_name: str,
    payload_type: str,
    os_type: str,
    mythic_client: MythicClient,
) -> bool:
    """check command for allowed, we don't want to ransomware"""
    allowed_commands = get_commands_for_phase(phase_name)

    # Get additional allowed commands based on payload type and OS
    payload_commands = await mythic_client.get_commands_for_payload(
        payload_type, os_type
    )
    allowed_commands.extend(payload_commands)

    # Normalize command for comparison
    cmd_normalized = cmd.strip().lower()

    # Check for exact matches and partial matches
    for allowed in allowed_commands:
        allowed_normalized = allowed.strip().lower()

        # Exact match
        if cmd_normalized == allowed_normalized:
            return True

        # Partial match - command starts with allowed command
        if cmd_normalized.startswith(allowed_normalized.split()[0]):
            return True

        # Check if allowed command is a substring of the input command
        if allowed_normalized in cmd_normalized:
            return True

    # Additional security checks
    # Block dangerous commands
    dangerous_patterns = [
        "rm -rf",
        "format",
        "del /f",
        "rmdir /s",
        "shutdown",
        "reboot",
        "poweroff",
        "dd if=/dev/zero",
        "chmod 777",
        "chown root",
        "sudo rm",
    ]

    for pattern in dangerous_patterns:
        if pattern in cmd_normalized:
            return False

    return False


def get_commands_for_phase(phase_name: str):
    """get specific commands for given phase, format"""
    return PHASE_COMMANDS.get(phase_name, [])
