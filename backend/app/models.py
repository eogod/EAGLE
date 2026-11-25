"""Module for SQL Alchemy models declaration"""
# https://docs.sqlalchemy.org/en/20/orm/quickstart.html#declare-models
# mapped_column syntax from SQLAlchemy 2.0.

# https://alembic.sqlalchemy.org/en/latest/tutorial.html
# Note, it is used by alembic migrations logic, see `alembic/env.py`

# Alembic shortcuts:
# # create migration
# alembic revision --autogenerate -m "migration_name"

# # apply all migrations
# alembic upgrade head

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Uuid,
    false,
    func,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# the tests run on aiosqlite (see conftest), production stays on postgres;
# sqlite needs INTEGER for autoincrement ids and has no JSONB
BigIntPk = BigInteger().with_variant(Integer, "sqlite")
JSONValue = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    create_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "user_account"

    user_id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda _: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(
        String(256), nullable=True, unique=True, index=True
    )
    role: Mapped[str] = mapped_column(String(64), nullable=True, unique=False)
    email: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True, index=True
    )
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", passive_deletes=True
    )
    auth_sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", passive_deletes=True
    )
    attack_chain: Mapped[list[AttackChain]] = relationship(back_populates="user")


class AuthSession(Base):
    """one login; access tokens carry its id, so revoke is immediate"""

    __tablename__ = "auth_session"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # epoch seconds keep sqlite and postgres comparisons identical
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revoked_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="auth_sessions")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="session", passive_deletes=True
    )


class RefreshToken(Base):
    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True)
    # only the hash is stored; a database read must not hand over a session
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    used_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    exp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id", ondelete="CASCADE"),
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("auth_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user: Mapped[User] = relationship(back_populates="refresh_tokens")
    session: Mapped[AuthSession] = relationship(back_populates="refresh_tokens")


class AttackChain(Base):
    __tablename__ = "attack_chain"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id", ondelete="CASCADE"),
    )
    chain_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    final_status: Mapped[str] = mapped_column(String(64), nullable=False, unique=False)
    user: Mapped[User] = relationship(back_populates="attack_chain")
    # DB has ON DELETE CASCADE; without passive_deletes SQLAlchemy tries to
    # NULL chain_id first and violates the not-null constraint
    attack_step: Mapped[list[AttackStep]] = relationship(
        back_populates="attack_chain", passive_deletes=True
    )
    # audit may take several days, so storing it in the database is easier
    current_phase: Mapped[CurrentAttackPhase] = relationship(
        back_populates="attack_chain",
        uselist=False,  # each chain has only one current phase.
        cascade="all, delete-orphan",  # del if c_phase is null
    )


class AttackStep(Base):
    __tablename__ = "attack_step"
    __table_args__ = (
        # a draft must never carry an execution ticket
        CheckConstraint(
            "NOT (is_draft AND (mythic_task_id <> 0 OR mythic_payload_id <> 0))",
            name="ck_attack_step_draft_execution",
        ),
    )

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # TODO: add an explicit order column; imported steps cannot rely on insertion id.
    chain_id: Mapped[int] = mapped_column(
        ForeignKey("attack_chain.id", ondelete="CASCADE"),
    )
    # each step has a specific phase, which can differs from the current
    phase: Mapped[str] = mapped_column(String(64), nullable=False, unique=False)
    # TODO: add network_path so a step says which agent or tunnel carries its traffic.
    # real router: local | agent | custom | payload | getcallback | bash,
    # tool_name below is just a label like nmap, shell, impacket-wmiexec
    step_type: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="local"
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, unique=False)
    command: Mapped[str] = mapped_column(Text, nullable=False, unique=False)
    mythic_task_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=False)
    # only for downloading payload link
    mythic_payload_uuid: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), nullable=False, unique=False
    )
    mythic_payload_id: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=False
    )
    # raw command output from task, without llm
    raw_log: Mapped[str] = mapped_column(Text, nullable=True, unique=False)
    # llm analysis of raw_log, run by explicit endpoint by agent/user
    llm_analysis: Mapped[str] = mapped_column(Text, nullable=True, unique=False)
    # how long the attacker worked before this step, honoured only in timed runs
    delay_before_secs: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    delay_reason: Mapped[str] = mapped_column(Text, nullable=True, unique=False)
    # operator opt-out per step; a phase toggle flips it for the whole column
    delay_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    status: Mapped[str] = mapped_column(String(64), nullable=False, unique=False)
    # operator-authored step; draft routes can only touch rows with this flag
    is_draft: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    attack_chain: Mapped[AttackChain] = relationship(back_populates="attack_step")
    # DB cascades these; letting the ORM load them first breaks deletes when a
    # child table drifts and costs two extra queries otherwise
    agent: Mapped[list[Agent]] = relationship(
        back_populates="attack_step", passive_deletes=True
    )
    mythic_tasks: Mapped[list[MythicTask]] = relationship(
        back_populates="attack_step", passive_deletes=True
    )
    bash_tasks: Mapped[list[BashTask]] = relationship(
        back_populates="attack_step", passive_deletes=True
    )


class MythicTask(Base):
    __tablename__ = "mythic_task"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # ticket from mythic issue_task, live status for it in redis by this id
    mythic_task_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    step_id: Mapped[int] = mapped_column(
        ForeignKey("attack_step.id", ondelete="CASCADE"),
    )
    # submitted -> dispatched -> completed | errored, like mythic statuses
    status: Mapped[str] = mapped_column(String(32), nullable=False, unique=False)
    output: Mapped[str] = mapped_column(Text, nullable=True, unique=False)
    # llm analysis after completed by events service, if asked at run
    auto_analyze: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, unique=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True, unique=False
    )

    attack_step: Mapped[AttackStep] = relationship(back_populates="mythic_tasks")


class BashTask(Base):
    __tablename__ = "bash_task"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    step_id: Mapped[int] = mapped_column(
        ForeignKey("attack_step.id", ondelete="CASCADE"),
    )
    # submitted -> running -> completed | failed, one per run
    status: Mapped[str] = mapped_column(String(32), nullable=False, unique=False)
    # cut output like raw_log, full log only on disk
    output: Mapped[str] = mapped_column(Text, nullable=True, unique=False)
    # negative/None when killed by timeout or cancel
    exit_code: Mapped[int] = mapped_column(Integer, nullable=True, unique=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=True, unique=False)
    # bash_{step_id}_{task_id}.log in workspace, full uncut output
    full_log_path: Mapped[str] = mapped_column(String(512), nullable=True, unique=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=True, unique=False
    )

    attack_step: Mapped[AttackStep] = relationship(back_populates="bash_tasks")


class CurrentAttackPhase(Base):
    __tablename__ = "current_attack_phase"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    # chain always start with Reconnaissance
    phase: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Reconnaissance"
    )
    # user_id from attack_chain.user_id if necessary
    chain_id: Mapped[int] = mapped_column(
        ForeignKey("attack_chain.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # and update_time from base
    attack_chain: Mapped[AttackChain] = relationship(back_populates="current_phase")


class Agent(Base):
    __tablename__ = "agent"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    step_id: Mapped[int] = mapped_column(
        ForeignKey("attack_step.id", ondelete="CASCADE"),
    )
    agent_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=False)
    os_type: Mapped[str] = mapped_column(String(64), nullable=False, unique=False)
    status: Mapped[str] = mapped_column(String(64), nullable=False, unique=False)
    # id from mythic for performing tasks
    callback_display_id: Mapped[int] = mapped_column(
        Integer, nullable=False, unique=False
    )
    attack_step: Mapped[AttackStep] = relationship(back_populates="agent")


class AgentSession(Base):
    """one chat thread, optionally bound to an existing attack chain"""

    __tablename__ = "agent_session"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id", ondelete="CASCADE"),
        index=True,
    )
    # binding is optional so the panel can live before any chain exists
    chain_id: Mapped[int | None] = mapped_column(
        ForeignKey("attack_chain.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(256), nullable=False, default="New analysis"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    user: Mapped[User] = relationship()
    chain: Mapped[AttackChain | None] = relationship()
    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )
    documents: Mapped[list[AgentDocument]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list[AgentRun]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )
    drafts: Mapped[list[ChainDraft]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True
    )


class AgentMessage(Base):
    """assistant-ui compatible content parts: [{type, ...}]"""

    __tablename__ = "agent_message"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[list] = mapped_column(JSONValue, nullable=False, default=list)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )

    session: Mapped[AgentSession] = relationship(back_populates="messages")


class AgentDocument(Base):
    """untrusted upload, parsed once at upload time; raw file stays on disk"""

    __tablename__ = "agent_document"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mime: Mapped[str] = mapped_column(String(128), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # parsed text is kept next to the upload for reproducible runs
    parsed_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    parse_status: Mapped[str] = mapped_column(String(32), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    session: Mapped[AgentSession] = relationship(back_populates="documents")


class AgentRun(Base):
    __tablename__ = "agent_run"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_document.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    session: Mapped[AgentSession] = relationship(back_populates="runs")


class AgentFinding(Base):
    """a claim that must carry at least one verbatim document span"""

    __tablename__ = "agent_finding"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("agent_run.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    uncertainty: Mapped[str | None] = mapped_column(Text, nullable=True)
    # [{segment_id, page, char_start, char_end, quote}]
    evidence: Mapped[list] = mapped_column(JSONValue, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="accepted")

    run: Mapped[AgentRun] = relationship()


class ChainDraft(Base):
    """staging area: never an attack_chain/attack_step row until the operator runs it"""

    __tablename__ = "chain_draft"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="needs_review"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    payload: Mapped[dict] = mapped_column(JSONValue, nullable=False, default=dict)

    session: Mapped[AgentSession] = relationship(back_populates="drafts")


class ChainProposal(Base):
    """server-side review gate: model output becomes a row, never an applied edit"""

    __tablename__ = "chain_proposal"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="proposed", index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[int] = mapped_column(
        ForeignKey("agent_session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chain_id: Mapped[int] = mapped_column(
        ForeignKey("attack_chain.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_run.id", ondelete="SET NULL"), nullable=True
    )
    base_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # immutable after creation; apply never accepts operations from a client
    ops: Mapped[list] = mapped_column(JSONValue, nullable=False, default=list)
    validation: Mapped[dict] = mapped_column(JSONValue, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approved_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    applied_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    rejected_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    executed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    danger_ack_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    result: Mapped[list | None] = mapped_column(JSONValue, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ChainAuditEvent(Base):
    """append-only trail of who proposed, approved, applied or ran what"""

    __tablename__ = "chain_audit_event"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    chain_id: Mapped[int | None] = mapped_column(
        ForeignKey("attack_chain.id", ondelete="SET NULL"), nullable=True, index=True
    )
    session_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_session.id", ondelete="SET NULL"), nullable=True
    )
    proposal_id: Mapped[int | None] = mapped_column(
        ForeignKey("chain_proposal.id", ondelete="SET NULL"), nullable=True, index=True
    )
    step_id: Mapped[int | None] = mapped_column(
        ForeignKey("attack_step.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("user_account.user_id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, default="ok")
    detail: Mapped[dict] = mapped_column(JSONValue, nullable=False, default=dict)
