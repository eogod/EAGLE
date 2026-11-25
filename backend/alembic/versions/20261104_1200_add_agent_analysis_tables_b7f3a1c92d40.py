"""document-analysis agent: sessions, uploads, runs, findings, chain drafts

Revision ID: b7f3a1c92d40
Revises: 88c1f02a6d47
Create Date: 2026-11-04 12:00:00.000000

"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7f3a1c92d40"
down_revision = "88c1f02a6d47"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_session",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("user_account.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "chain_id",
            sa.BigInteger(),
            sa.ForeignKey("attack_chain.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "create_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "update_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_agent_session_user_id", "agent_session", ["user_id"])
    op.create_index("ix_agent_session_chain_id", "agent_session", ["chain_id"])

    op.create_table(
        "agent_document",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("mime", sa.String(128), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("parsed_path", sa.String(1024), nullable=True),
        sa.Column("parse_status", sa.String(32), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("char_count", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "create_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "update_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_agent_document_session_id", "agent_document", ["session_id"])
    op.create_index("ix_agent_document_sha256", "agent_document", ["sha256"])

    op.create_table(
        "agent_run",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_document.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "create_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "update_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_agent_run_session_id", "agent_run", ["session_id"])

    op.create_table(
        "agent_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "create_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "update_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_agent_message_session_id", "agent_message", ["session_id"])

    op.create_table(
        "agent_finding",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(64), nullable=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("uncertainty", sa.Text(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "create_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "update_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_agent_finding_run_id", "agent_finding", ["run_id"])
    op.create_index("ix_agent_finding_session_id", "agent_finding", ["session_id"])

    op.create_table(
        "chain_draft",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "session_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.BigInteger(),
            sa.ForeignKey("agent_run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("user_account.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "create_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "update_time", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )
    op.create_index("ix_chain_draft_session_id", "chain_draft", ["session_id"])


def downgrade():
    op.drop_table("chain_draft")
    op.drop_table("agent_finding")
    op.drop_table("agent_message")
    op.drop_table("agent_run")
    op.drop_table("agent_document")
    op.drop_table("agent_session")
