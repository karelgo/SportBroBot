"""Shared web-layer plumbing: templates, DB sessions, session cookies, MCP URL."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from fastapi import Depends, HTTPException, Request, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..config import get_settings
from ..db import get_sessionmaker
from ..models import McpToken, User

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=TEMPLATES_DIR)

SESSION_COOKIE = "sbb_session"
SESSION_MAX_AGE = 30 * 24 * 60 * 60  # 30 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="sbb-session")


def get_db() -> Iterator[Session]:
    """Request-scoped SQLAlchemy session; commits on success, rolls back on error."""
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_session_cookie(response: Response, user_id: int) -> None:
    value = _serializer().dumps({"uid": user_id})
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().base_url.startswith("https"),
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    """Resolve the logged-in user from the signed session cookie, if any."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    try:
        payload = _serializer().loads(raw, max_age=SESSION_MAX_AGE)
    except BadSignature:  # covers SignatureExpired too
        return None
    uid = payload.get("uid") if isinstance(payload, dict) else None
    if not isinstance(uid, int):
        return None
    return db.get(User, uid)


def require_user(user: User | None = Depends(get_current_user)) -> User:
    """Like :func:`get_current_user` but bounces anonymous visitors to /login.

    FastAPI dependencies can't return responses, so the redirect is raised as
    an HTTPException whose status/headers render as a 303 See Other.
    """
    if user is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


def get_mcp_url(db: Session, user: User) -> str:
    """Return the user's personal MCP URL, minting a token on first use."""
    row = db.execute(
        select(McpToken).where(McpToken.user_id == user.id)
    ).scalar_one_or_none()
    if row is not None:
        try:
            token = security.decrypt_text(row.token_encrypted)
        except security.InvalidToken:
            db.delete(row)
            db.flush()
            row = None
    if row is None:
        token = security.generate_mcp_token()
        db.add(
            McpToken(
                user_id=user.id,
                token_hash=security.hash_mcp_token(token),
                token_encrypted=security.encrypt_text(token),
            )
        )
        db.commit()
    return f"{get_settings().base_url}/mcp?apiKey={token}"
