"""
Module to defining requests types and format
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


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


class StepUpdate(BaseRequest):
    """client-editable step fields; status and output belong to the server"""

    model_config = ConfigDict(extra="forbid")

    phase: str | None = None
    step_type: Literal["local", "agent", "bash"] | None = None
    tool_name: str | None = Field(default=None, max_length=64)
    command: str | None = Field(default=None, max_length=4000)
    delay_before_secs: int | None = None
    delay_reason: str | None = Field(default=None, max_length=200)
    delay_enabled: bool | None = None


class DraftStepCreate(BaseRequest):
    phase: str
    step_type: Literal["local", "agent", "bash"] = "local"
    tool_name: str = Field(default="shell", max_length=64)
    command: str = Field(min_length=1, max_length=4000)
    delay_before_secs: int = 0


class RunStepRequest(BaseRequest):
    expected_step_fingerprint: str | None = Field(default=None, max_length=64)


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
    """commands are read from the stored step, never from the request body"""

    step_id: int
    approved_by: str = ""
    reason: str | None = ""


class ActionExecutionRequest(BaseModel):
    step_id: int
    approved_by: str = ""
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
