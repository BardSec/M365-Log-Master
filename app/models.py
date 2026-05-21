"""SQLAlchemy ORM models."""
from __future__ import annotations

import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .extensions import Base


class SignInEvent(Base):
    """One row per Microsoft Graph sign-in event."""

    __tablename__ = "sign_in_events"

    pk: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Graph event id – must be unique for idempotent upserts
    id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=False), nullable=False
    )

    # User fields
    user_id: Mapped[str | None] = mapped_column(String(128))
    user_display_name: Mapped[str | None] = mapped_column(String(256))
    user_principal_name: Mapped[str | None] = mapped_column(String(256))

    # App fields
    app_id: Mapped[str | None] = mapped_column(String(128))
    app_display_name: Mapped[str | None] = mapped_column(String(256))

    # Network
    ip_address: Mapped[str | None] = mapped_column(String(64))
    client_app_used: Mapped[str | None] = mapped_column(String(128))

    # Status – JSONB (errorCode, failureReason, additionalDetails)
    status: Mapped[dict | None] = mapped_column(JSONB)

    # Location – JSONB (city, state, countryOrRegion, geoCoordinates)
    location: Mapped[dict | None] = mapped_column(JSONB)

    # Device detail – JSONB
    device_detail: Mapped[dict | None] = mapped_column(JSONB)

    # Conditional access / risk
    conditional_access_status: Mapped[str | None] = mapped_column(String(64))
    risk_detail: Mapped[str | None] = mapped_column(String(128))
    risk_level_aggregated: Mapped[str | None] = mapped_column(String(64))

    # Forward-compatibility: full raw event
    raw_event: Mapped[dict | None] = mapped_column(JSONB)

    # Derived/denormalised fields for fast filtering
    error_code: Mapped[int | None] = mapped_column(Integer)
    country: Mapped[str | None] = mapped_column(String(128))

    # Sign-in event type: interactiveUser | nonInteractiveUser | servicePrincipal | managedIdentity
    signin_event_type: Mapped[str | None] = mapped_column(String(32))

    # Full-text search vector (populated by DB trigger / manual update)
    search_vector: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # We store tsvector as text for simplicity

    ingested_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("id", name="uq_signin_event_id"),
        # Btree indexes for common filter columns
        Index("ix_signin_created_at", "created_at"),
        Index("ix_signin_upn", "user_principal_name"),
        Index("ix_signin_ip", "ip_address"),
        Index("ix_signin_app", "app_display_name"),
        Index("ix_signin_error_code", "error_code"),
        Index("ix_signin_country", "country"),
        Index("ix_signin_event_type", "signin_event_type"),
        # Composite for anomaly heuristics' LAG window function
        Index(
            "ix_signin_type_upn_created",
            "signin_event_type", "user_principal_name", "created_at",
        ),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "user_id": self.user_id,
            "user_display_name": self.user_display_name,
            "user_principal_name": self.user_principal_name,
            "app_id": self.app_id,
            "app_display_name": self.app_display_name,
            "ip_address": self.ip_address,
            "client_app_used": self.client_app_used,
            "status": self.status,
            "location": self.location,
            "device_detail": self.device_detail,
            "conditional_access_status": self.conditional_access_status,
            "risk_detail": self.risk_detail,
            "risk_level_aggregated": self.risk_level_aggregated,
            "error_code": self.error_code,
            "country": self.country,
            "signin_event_type": self.signin_event_type,
        }


class SyncCursor(Base):
    """Persists the incremental sync state (singleton row)."""

    __tablename__ = "sync_cursor"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_sync_time: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    last_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class ArchiveLog(Base):
    """One row per day that has been (or attempted to be) archived to R2."""

    __tablename__ = "archive_log"

    day: Mapped[datetime.date] = mapped_column(
        sa.Date, primary_key=True
    )
    status: Mapped[str] = mapped_column(String(32))  # success | error | in_progress
    rows_archived: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bytes_uploaded: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    r2_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )


class SyncLog(Base):
    """One row per completed (or failed) sync run."""

    __tablename__ = "sync_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=False))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    status: Mapped[str] = mapped_column(String(32))  # success | error
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
