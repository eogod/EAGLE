"""auth sessions, draft steps and chain proposals

Revision ID: f1a2b3c4d5e6
Revises: e6b8c1d47f20
Create Date: 2026-11-23 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e6b8c1d47f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_account",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
    )

    op.create_table(
        "auth_session",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("last_used_at", sa.BigInteger(), nullable=True),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_account.user_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_session_user_id", "auth_session", ["user_id"])

    op.add_column("refresh_token", sa.Column("session_id", sa.BigInteger()))
    op.add_column("refresh_token", sa.Column("token_hash", sa.String(length=64)))
    op.add_column("refresh_token", sa.Column("used_at", sa.BigInteger()))
    # old plaintext tokens cannot be tied to a session; force one re-login
    op.execute(sa.text("DELETE FROM refresh_token"))
    op.drop_column("refresh_token", "used")
    op.alter_column("refresh_token", "session_id", nullable=False)
    op.alter_column("refresh_token", "token_hash", nullable=False)
    op.create_foreign_key(
        "fk_refresh_token_session_id",
        "refresh_token",
        "auth_session",
        ["session_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_refresh_token_session_id", "refresh_token", ["session_id"])
    op.create_index("ix_refresh_token_user_id", "refresh_token", ["user_id"])
    op.create_unique_constraint(
        "uq_refresh_token_token_hash", "refresh_token", ["token_hash"]
    )
    op.drop_index("ix_refresh_token_refresh_token", table_name="refresh_token")
    op.drop_column("refresh_token", "refresh_token")

    op.add_column(
        "attack_step",
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_check_constraint(
        "ck_attack_step_draft_execution",
        "attack_step",
        "NOT (is_draft AND (mythic_task_id <> 0 OR mythic_payload_id <> 0))",
    )

    op.create_table(
        "chain_proposal",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=False),
        sa.Column("session_id", sa.BigInteger(), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=True),
        sa.Column("base_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("ops", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "validation", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("summary", sa.String(length=1000), nullable=False),
        sa.Column("provider", sa.String(length=128), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("approved_at", sa.BigInteger(), nullable=True),
        sa.Column("approved_by", sa.String(length=36), nullable=True),
        sa.Column("applied_at", sa.BigInteger(), nullable=True),
        sa.Column("rejected_at", sa.BigInteger(), nullable=True),
        sa.Column("executed_at", sa.BigInteger(), nullable=True),
        sa.Column("finished_at", sa.BigInteger(), nullable=True),
        sa.Column("danger_ack_at", sa.BigInteger(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_account.user_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_session.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["chain_id"], ["attack_chain.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_run.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_chain_proposal_public_id", "chain_proposal", ["public_id"])
    op.create_index("ix_chain_proposal_status", "chain_proposal", ["status"])
    op.create_index("ix_chain_proposal_user_id", "chain_proposal", ["user_id"])
    op.create_index("ix_chain_proposal_session_id", "chain_proposal", ["session_id"])
    op.create_index("ix_chain_proposal_chain_id", "chain_proposal", ["chain_id"])

    op.create_table(
        "chain_audit_event",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=True),
        sa.Column("session_id", sa.BigInteger(), nullable=True),
        sa.Column("proposal_id", sa.BigInteger(), nullable=True),
        sa.Column("step_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.Uuid(as_uuid=False), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["chain_id"], ["attack_chain.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["agent_session.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["chain_proposal.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["step_id"], ["attack_step.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user_account.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chain_audit_event_chain_id", "chain_audit_event", ["chain_id"])
    op.create_index(
        "ix_chain_audit_event_proposal_id", "chain_audit_event", ["proposal_id"]
    )


def downgrade() -> None:
    op.drop_table("chain_audit_event")
    op.drop_table("chain_proposal")
    op.drop_constraint("ck_attack_step_draft_execution", "attack_step", type_="check")
    op.drop_column("attack_step", "is_draft")

    op.add_column("refresh_token", sa.Column("refresh_token", sa.String(length=512)))
    op.add_column(
        "refresh_token",
        sa.Column("used", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute(sa.text("DELETE FROM refresh_token"))
    op.drop_constraint("uq_refresh_token_token_hash", "refresh_token", type_="unique")
    op.drop_index("ix_refresh_token_user_id", table_name="refresh_token")
    op.drop_index("ix_refresh_token_session_id", table_name="refresh_token")
    op.drop_constraint(
        "fk_refresh_token_session_id", "refresh_token", type_="foreignkey"
    )
    op.alter_column("refresh_token", "refresh_token", nullable=False)
    op.create_index(
        "ix_refresh_token_refresh_token",
        "refresh_token",
        ["refresh_token"],
        unique=True,
    )
    op.drop_column("refresh_token", "used_at")
    op.drop_column("refresh_token", "token_hash")
    op.drop_column("refresh_token", "session_id")
    op.drop_table("auth_session")
    op.drop_column("user_account", "is_active")
