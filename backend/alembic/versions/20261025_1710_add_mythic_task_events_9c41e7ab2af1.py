"""add mythic task table with events tracking and llm analysis column

Revision ID: 9c41e7ab2af1
Revises: b4d2c51dc55a
Create Date: 2026-10-25 17:10:54.257310

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "9c41e7ab2af1"
down_revision = "b4d2c51dc55a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "attack_step",
        sa.Column("llm_analysis", sa.Text(), nullable=True),
    )
    op.create_table(
        "mythic_task",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("mythic_task_id", sa.Integer(), nullable=False, unique=True),
        sa.Column(
            "step_id",
            sa.Integer(),
            sa.ForeignKey("attack_step.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("auto_analyze", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table("mythic_task")
    op.drop_column("attack_step", "llm_analysis")
