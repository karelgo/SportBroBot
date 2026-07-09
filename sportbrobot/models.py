"""SQLAlchemy models: app users, Garmin links, MCP access tokens."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    garmin_link: Mapped["GarminLink | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    strava_link: Mapped["StravaLink | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    mcp_token: Mapped["McpToken | None"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )


class GarminLink(Base):
    __tablename__ = "garmin_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    garmin_email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    unit_system: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Fernet-encrypted garminconnect token bundle (client.dumps() JSON).
    token_blob: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | reauth_required
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="garmin_link")


class StravaLink(Base):
    __tablename__ = "strava_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    athlete_id: Mapped[int] = mapped_column(index=True)
    athlete_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Fernet-encrypted OAuth tokens.
    access_token: Mapped[str] = mapped_column(Text)
    refresh_token: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[int] = mapped_column(default=0)  # epoch seconds
    status: Mapped[str] = mapped_column(String(32), default="active")  # active | reauth_required
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="strava_link")


class McpToken(Base):
    __tablename__ = "mcp_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Fernet-encrypted copy so the dashboard can re-display the MCP URL.
    token_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(back_populates="mcp_token")
