"""Composite index to speed up anomaly heuristics

Anomaly queries (impossible_travel in particular) sort by
(user_principal_name, created_at) inside a WindowAgg. Without a
matching composite index Postgres external-merge-sorts ~180k rows on
every dashboard render. Add the composite and include
signin_event_type as the leading column so anomaly queries that
filter to interactiveUser get further pruning.

Uses CREATE INDEX CONCURRENTLY so the deploy doesn't lock the table.

Revision ID: 004
Revises: 003
Create Date: 2026-05-21 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS "
            "ix_signin_type_upn_created "
            "ON sign_in_events (signin_event_type, user_principal_name, created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_signin_type_upn_created")
