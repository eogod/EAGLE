from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.deps import get_events_service, get_mythic_client
from app.cmd.c2_tool import MythicClient
from app.cmd.mythic_events import MythicEventsService
from app.services.bash_service import BashCommandService
from app.services.c2_service import CommandsC2ExecService
from app.services.chain_exec_service import ChainExecutionService
from app.services.chain_service import ChainService
from app.services.payload_service import AgentPayloadService


async def get_chain_service(
    session: AsyncSession = Depends(deps.get_session),
) -> ChainService:
    return ChainService(session)


async def get_command_service(
    session: AsyncSession = Depends(deps.get_session),
    mythic_client: MythicClient = Depends(get_mythic_client),
) -> CommandsC2ExecService:
    return CommandsC2ExecService(session, mythic_client)


async def get_bash_service(
    session: AsyncSession = Depends(deps.get_session),
    events_service: MythicEventsService = Depends(deps.get_events_service),
) -> BashCommandService:
    return BashCommandService(session, events_service)


async def get_agent_payload_service(
    session: AsyncSession = Depends(deps.get_session),
    mythic_client: MythicClient = Depends(get_mythic_client),
) -> AgentPayloadService:
    return AgentPayloadService(session, mythic_client)


async def get_chain_execution_service(
    session: AsyncSession = Depends(deps.get_session),
    mythic_client: MythicClient = Depends(get_mythic_client),
) -> ChainExecutionService:
    return ChainExecutionService(session, mythic_client)


__all__ = [
    "get_agent_payload_service",
    "get_bash_service",
    "get_chain_execution_service",
    "get_chain_service",
    "get_command_service",
    "get_events_service",
]
