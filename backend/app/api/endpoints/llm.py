"""
Module only for llm endpoints
to simplify the creation of a chain relative to the user
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.llm_templ import LLMTemplates
from app.models import (
    AttackChain,
    AttackStep,
    CurrentAttackPhase,
    User,
)
from app.schemas.requests import (
    ChatRequest,
    CodeAnalysisRequest,
    PayloadRequest,
    QueryRequest,
    SuggestActionRequest,
)
from app.schemas.responses import SuggestActionResponse
from app.services.agent_chat_service import AgentChatService

router = APIRouter()

AgentServiceDep = Annotated[AgentChatService, Depends(deps.get_agent_chat_service)]


@router.post("/query", description="General questions to LLM")
async def llm_query(request: QueryRequest, service: AgentServiceDep):
    """
    Основной эндпоинт для запросов к LLM
    """
    try:
        result = await service.query_llm(request.prompt, request.provider)
        return {"success": True, "response": result, "prompt": request.prompt}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/generate/payload")
async def generate_payload(request: PayloadRequest, service: AgentServiceDep):
    """
    Генерирует пейлоады для penetration testing
    """
    try:
        # Используем шаблон из конфигурации
        prompt = LLMTemplates.PAYLOAD_GENERATION.format(
            language=request.language, description=request.description
        )
        result = await service.query_llm(prompt)

        # Используем шаблон для генерации команд
        commands_prompt = LLMTemplates.COMMANDS_GENERATION.format(
            language=request.language, script=result
        )

        commands_result = await service.query_llm(commands_prompt)

        # Пытаемся распарсить команды
        try:
            commands_data = json.loads(commands_result)
        except json.JSONDecodeError:
            commands_data = {
                "setup_commands": [],
                "execution_commands": [],
                "verification_commands": [],
                "cleanup_commands": [],
            }

        return {
            "success": True,
            "payload": result,
            "commands": commands_data,
            "language": request.language,
            "description": request.description,
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"generation error: {str(e)}"
        ) from e


@router.post("/suggest-action", response_model=SuggestActionResponse)
async def suggest_action_from_llm(
    req: SuggestActionRequest,
    service: AgentServiceDep,
    session: AsyncSession = Depends(deps.get_session),
    current_user: User = Depends(deps.get_current_user),
):
    """suggest action for approve based on process_approved_cmd
    then user fix and send to cmd approve by hand"""
    chain_ca_list: list[AttackChain] = await session.execute(
        select(AttackChain).where(
            AttackChain.id == req.chain_id,
        )
    )
    chain_ca: AttackChain = chain_ca_list.scalars().first()
    await session.commit()
    chain_c_phase_list: list[CurrentAttackPhase] = await session.execute(
        select(CurrentAttackPhase).where(CurrentAttackPhase.chain_id == chain_ca.id)
    )
    chain_c_phase = chain_c_phase_list.scalars().first()
    current_phase_n = chain_c_phase.phase or "Reconnaissance"
    res_l_step = await session.execute(
        select(AttackStep)
        .where(AttackStep.chain_id == req.chain_id)
        .order_by(desc(AttackStep.update_time))
        .limit(3)
    )
    last_attack_steps = [
        {
            "id": step.id,
            "phase": step.phase,
            "tool_name": step.tool_name,
            "command": step.command,
            "raw_log": step.raw_log,
            "status": step.status,
        }
        for step in res_l_step.scalars().all()
    ]
    pre_dict = {
        "chain_id": chain_ca.id,
        "user_id": chain_ca.user_id,
        "chain_name": chain_ca.chain_name,
        "user_email": current_user.email,
        "final_status": chain_ca.final_status,
        "current_phase_name": current_phase_n,
        "last_attack_step": last_attack_steps or "no last step",
    }
    st: str = json.dumps(pre_dict, indent=2)
    prompt = LLMTemplates.SUGGEST_ACTION_CMD.format(p_command=req.p_command, step=st)
    llm_raw: str = await service.query_llm(prompt)

    try:
        llm_json = json.loads(llm_raw)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"LLM returned invalid JSON: {str(exc)} — raw: {llm_raw}",
        ) from exc

    return SuggestActionResponse(
        chain_id=req.chain_id,
        agent_id=req.display_id,
        command=llm_json.get("command"),
        phase=llm_json.get("phase"),
        target_os_type=llm_json.get("target_os_type"),
        type_cmd=llm_json.get("type_cmd"),
        type_tool=llm_json.get("type_tool"),
    )


@router.post("/analyze/code", description="Analyze and explain code via LLM")
async def analyze_code(request: CodeAnalysisRequest, service: AgentServiceDep):
    """
    Анализирует код с помощью LLM
    """
    try:
        prompt = f"analyze this code and explain it to me\n\n{request.code}"
        result = await service.query_llm(prompt)
        return {"success": True, "analysis": result, "code": request.code}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"error code analysis {str(e)}"
        ) from e


@router.get("/providers", description="Return list of LLM providers")
async def get_providers(service: AgentServiceDep):
    """
    Возвращает список доступных провайдеров
    """
    try:
        providers_list = service.llm_providers()
        return {
            "success": True,
            "providers": [
                {"name": name, "class": class_name}
                for name, class_name in providers_list.items()
            ],
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"error retrieving provider: {str(e)}"
        ) from e


@router.post("/chat", description="Chat with llm in context of chain")
async def chat_conversation(req: ChatRequest, service: AgentServiceDep):
    """
    Эндпоинт для ведения диалога
    """
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages list is required")

    result = await service.query_llm_chat(req.messages, req.provider)
    return {
        "success": True,
        "response": result,
    }
