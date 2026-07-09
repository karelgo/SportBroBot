"""Public pages: landing and the MCP setup guides."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..models import User
from .deps import get_current_user, get_db, get_mcp_url, templates

router = APIRouter()

CLIENTS: list[tuple[str, str]] = [
    ("claude-desktop", "Claude Desktop"),
    ("claude-code", "Claude Code"),
    ("cursor", "Cursor"),
    ("chatgpt", "ChatGPT"),
]


@router.get("/")
def landing(request: Request, user: User | None = Depends(get_current_user)):
    return templates.TemplateResponse(request, "landing.html", {"user": user})


@router.get("/mcp/setup")
def mcp_setup(
    request: Request,
    user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    mcp_url = get_mcp_url(db, user) if user is not None else None
    return templates.TemplateResponse(
        request,
        "mcp_setup.html",
        {"user": user, "mcp_url": mcp_url, "clients": CLIENTS},
    )


@router.get("/mcp/setup/{client}")
def mcp_setup_client(
    request: Request,
    client: str,
    user: User | None = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    names = dict(CLIENTS)
    if client not in names:
        raise HTTPException(status_code=404, detail="Unknown MCP client")
    mcp_url = get_mcp_url(db, user) if user is not None else None
    return templates.TemplateResponse(
        request,
        "mcp_setup_client.html",
        {
            "user": user,
            "client": client,
            "client_name": names[client],
            "mcp_url": mcp_url,
        },
    )
