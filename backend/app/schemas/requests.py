"""
Module to defining requests types and format
"""

from pydantic import BaseModel, EmailStr


class BaseRequest(BaseModel):
    # may define additional fields or config shared across requests
    pass


class RefreshTokenRequest(BaseRequest):
    refresh_token: str


class UserUpdatePasswordRequest(BaseRequest):
    password: str


class UserCreateRequest(BaseRequest):
    email: EmailStr
    password: str
    role: str
    username: str


class NewChainRequest(BaseRequest):
    chain_name: str


class NewAgentRequest(BaseRequest):
    chain_name: str
    os_type: str
    payload_type: str | None


class LocalCommandRequest(BaseRequest):
    chain_name: str
    callback_display_id: int
    command: str
    is_analyze_llm: bool = False
    # execute this existing (unexecuted) row instead of inserting a new one
    step_id: int | None = None


class BashCommandRequest(BaseRequest):
    """direct bash on EAGLE host, no C2, ticket + redis events"""

    # TODO: add env, cwd, shell and run_as fields so each step carries its own environment.
    chain_name: str
    command: str
    timeout: int = 300
    step_id: int | None = None


class AgentCommandRequest(BaseRequest):
    chain_name: str
    callback_display_id: int
    command_params: str | None = "uname -a;lscpu;free;df;id;pwd;lsblk"
    tool: str | None = "shell"
    is_analyze_llm: bool = False
    step_id: int | None = None


class ActionApprovalRequest(BaseModel):
    """Модель запроса для одобрения действий"""

    command: str
    agent_id: int
    chain_id: int
    phase: str
    type_cmd: str
    type_tool: str
    target_os_type: str
    approved_by: str
    reason: str | None = ""


class ActionExecutionRequest(BaseModel):
    """Модель запроса для выполнения одобренных действий"""

    command: str
    agent_display_id: int
    chain_id: int
    phase: str
    approved_by: str
    context: str | None = ""


class QueryRequest(BaseModel):
    prompt: str
    provider: str | None = None


class CodeAnalysisRequest(BaseModel):
    code: str


class PayloadRequest(BaseModel):
    description: str
    language: str | None = "python"


class SuggestActionRequest(BaseRequest):
    p_command: str | None = "just analyze last steps and tell me what to do next"
    chain_id: int
    display_id: int


class ChatRequest(BaseModel):
    messages: list[dict[str, str]]
    provider: str | None = None
