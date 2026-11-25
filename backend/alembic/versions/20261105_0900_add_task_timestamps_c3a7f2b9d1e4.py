"""add create_time/update_time to mythic_task and bash_task

Revision ID: c3a7f2b9d1e4
Revises: b7f3a1c92d40
Create Date: 2026-11-05 09:00:00.000000

Both models inherit Base timestamps, but these two tables were created without
them, so every INSERT/SELECT on a task row failed with
"column ... create_time does not exist".
"""

import sqlalchemy as sa

from alembic import op

revision = "c3a7f2b9d1e4"
down_revision = "b7f3a1c92d40"
branch_labels = None
depends_on = None

_TABLES = ("mythic_task", "bash_task")


def upgrade():
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "create_time",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "update_time",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )


def downgrade():
    for table in _TABLES:
        op.drop_column(table, "update_time")
        op.drop_column(table, "create_time")
