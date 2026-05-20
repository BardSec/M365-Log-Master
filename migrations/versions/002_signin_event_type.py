"""Add signin_event_type column to sign_in_events

Revision ID: 002
Revises: 001
Create Date: 2026-05-20 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sign_in_events",
        sa.Column("signin_event_type", sa.String(32), nullable=True),
    )
    # Historical rows came from /v1.0/auditLogs/signIns without a type filter,
    # which only returns interactive user sign-ins.
    op.execute(
        "UPDATE sign_in_events SET signin_event_type = 'interactiveUser' "
        "WHERE signin_event_type IS NULL"
    )
    op.create_index(
        "ix_signin_event_type",
        "sign_in_events",
        ["signin_event_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_signin_event_type", table_name="sign_in_events")
    op.drop_column("sign_in_events", "signin_event_type")
