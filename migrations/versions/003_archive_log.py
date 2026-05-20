"""Add archive_log table for R2 archival tracking

Revision ID: 003
Revises: 002
Create Date: 2026-05-20 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "archive_log",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),  # success | error | in_progress
        sa.Column("rows_archived", sa.Integer(), nullable=True),
        sa.Column("bytes_uploaded", sa.BigInteger(), nullable=True),
        sa.Column("r2_key", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("day"),
    )
    op.create_index("ix_archive_log_status", "archive_log", ["status"])


def downgrade() -> None:
    op.drop_index("ix_archive_log_status", table_name="archive_log")
    op.drop_table("archive_log")
