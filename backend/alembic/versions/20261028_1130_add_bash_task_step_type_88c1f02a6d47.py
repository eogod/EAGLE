"""bash executor task tracking, step_type column instead of tool_name prefix lore

Revision ID: 88c1f02a6d47
Revises: 9c41e7ab2af1
Create Date: 2026-10-28 11:30:12.487310

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "88c1f02a6d47"
down_revision = "9c41e7ab2af1"
branch_labels = None
depends_on = None


def upgrade():
    # step_type is the real router, tool_name is a label from now on
    op.add_column(
        "attack_step",
        sa.Column(
            "step_type",
            sa.String(64),
            nullable=False,
            server_default="local",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE attack_step SET step_type = CASE "
            "WHEN tool_name LIKE 'agent\\_%' THEN 'agent' "
            "WHEN tool_name LIKE 'local\\_%' THEN 'local' "
            "WHEN tool_name LIKE 'payload\\_%' THEN 'payload' "
            "WHEN tool_name LIKE 'getcallback\\_%' THEN 'getcallback' "
            "ELSE 'custom' END "
        )
    )
    # unglue mythic tool name, cb.tool_name.split("_", 1) will be removed
    op.execute(
        sa.text(
            "UPDATE attack_step SET tool_name = "
            "regexp_replace(tool_name, '^(local|agent|payload|getcallback)_', '') "
            "WHERE tool_name ~ '^(local|agent|payload|getcallback)_'"
        )
    )
    op.create_table(
        "bash_task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "step_id",
            sa.BigInteger(),
            sa.ForeignKey("attack_step.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("full_log_path", sa.String(512), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table("bash_task")
    op.drop_column("attack_step", "step_type")
