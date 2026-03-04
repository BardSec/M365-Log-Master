"""Initial schema: sign_in_events, sync_cursor, sync_log

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pg_trgm for trigram/keyword search
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── sign_in_events ─────────────────────────────────────────────────────────
    op.create_table(
        "sign_in_events",
        sa.Column("pk", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("user_display_name", sa.String(256), nullable=True),
        sa.Column("user_principal_name", sa.String(256), nullable=True),
        sa.Column("app_id", sa.String(128), nullable=True),
        sa.Column("app_display_name", sa.String(256), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("client_app_used", sa.String(128), nullable=True),
        sa.Column("status", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("location", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("device_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("conditional_access_status", sa.String(64), nullable=True),
        sa.Column("risk_detail", sa.String(128), nullable=True),
        sa.Column("risk_level_aggregated", sa.String(64), nullable=True),
        sa.Column("raw_event", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_code", sa.Integer(), nullable=True),
        sa.Column("country", sa.String(128), nullable=True),
        sa.Column("search_vector", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("pk"),
        sa.UniqueConstraint("id", name="uq_signin_event_id"),
    )

    # Btree indexes
    op.create_index("ix_signin_created_at", "sign_in_events", ["created_at"])
    op.create_index("ix_signin_upn", "sign_in_events", ["user_principal_name"])
    op.create_index("ix_signin_ip", "sign_in_events", ["ip_address"])
    op.create_index("ix_signin_app", "sign_in_events", ["app_display_name"])
    op.create_index("ix_signin_error_code", "sign_in_events", ["error_code"])
    op.create_index("ix_signin_country", "sign_in_events", ["country"])

    # Trigram GIN indexes for fast ILIKE / keyword search
    op.execute(
        "CREATE INDEX ix_signin_upn_trgm ON sign_in_events "
        "USING GIN (user_principal_name gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_signin_ip_trgm ON sign_in_events "
        "USING GIN (ip_address gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_signin_app_trgm ON sign_in_events "
        "USING GIN (app_display_name gin_trgm_ops)"
    )

    # GIN index on raw_event JSONB for forward compatibility
    op.execute(
        "CREATE INDEX ix_signin_raw_gin ON sign_in_events "
        "USING GIN (raw_event)"
    )

    # ── sync_cursor ────────────────────────────────────────────────────────────
    op.create_table(
        "sync_cursor",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_sync_time", sa.DateTime(), nullable=True),
        sa.Column("last_event_id", sa.String(128), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ── sync_log ───────────────────────────────────────────────────────────────
    op.create_table(
        "sync_log",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=True, default=0),
        sa.Column("inserted_count", sa.Integer(), nullable=True, default=0),
        sa.Column("updated_count", sa.Integer(), nullable=True, default=0),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("sync_log")
    op.drop_table("sync_cursor")
    op.drop_table("sign_in_events")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
