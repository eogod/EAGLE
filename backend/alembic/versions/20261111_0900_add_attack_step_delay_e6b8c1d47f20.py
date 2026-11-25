"""add dwell columns to attack_step

Revision ID: e6b8c1d47f20
Revises: c3a7f2b9d1e4
Create Date: 2026-11-11 09:00:00.000000

Timing is opt-in at run time, but the numbers live on the step so the canvas,
the phase run and the chain run all read the same value.
"""

import sqlalchemy as sa

from alembic import op

revision = "e6b8c1d47f20"
down_revision = "c3a7f2b9d1e4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "attack_step",
        sa.Column(
            "delay_before_secs", sa.Integer(), nullable=False, server_default="0"
        ),
    )
    op.add_column("attack_step", sa.Column("delay_reason", sa.Text(), nullable=True))
    op.add_column(
        "attack_step",
        sa.Column("delay_enabled", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade():
    op.drop_column("attack_step", "delay_enabled")
    op.drop_column("attack_step", "delay_reason")
    op.drop_column("attack_step", "delay_before_secs")
