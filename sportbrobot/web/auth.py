"""App-account routes: /signup, /login, /logout."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..models import User
from .deps import (
    clear_session_cookie,
    get_current_user,
    get_db,
    set_session_cookie,
    templates,
)

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_MIN_PASSWORD_LEN = 8


def _normalize_email(email: str) -> str:
    return email.strip().lower()


@router.get("/signup")
def signup_form(request: Request, user: User | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "signup.html", {"user": None, "error": None})


@router.post("/signup")
def signup(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    db: Session = Depends(get_db),
):
    def rerender(error: str):
        return templates.TemplateResponse(
            request, "signup.html", {"user": None, "error": error}, status_code=400
        )

    email = _normalize_email(email)
    if not _EMAIL_RE.match(email):
        return rerender("Please enter a valid email address.")
    if len(password) < _MIN_PASSWORD_LEN:
        return rerender(f"Password must be at least {_MIN_PASSWORD_LEN} characters.")
    if password != password_confirm:
        return rerender("Passwords do not match.")
    existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        return rerender("An account with that email already exists — try logging in.")

    user = User(email=email, password_hash=security.hash_password(password))
    db.add(user)
    db.commit()

    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, user.id)
    return response


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"user": None, "error": None})


@router.post("/login")
def login(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.execute(
        select(User).where(User.email == _normalize_email(email))
    ).scalar_one_or_none()
    if user is None or not security.verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"user": None, "error": "Invalid email or password"},
            status_code=400,
        )

    response = RedirectResponse("/dashboard", status_code=303)
    set_session_cookie(response, user.id)
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/", status_code=303)
    clear_session_cookie(response)
    return response
