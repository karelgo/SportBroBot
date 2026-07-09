"""Strava OAuth routes: real login at Strava, callback, disconnect."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..config import get_settings
from ..models import StravaLink, User
from .deps import get_db, require_user

router = APIRouter()

_STATE_MAX_AGE = 600  # seconds between starting and finishing the OAuth dance


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="sbb-strava-state")


def _dashboard(msg: str) -> RedirectResponse:
    from urllib.parse import urlencode

    return RedirectResponse("/dashboard?" + urlencode({"msg": msg}), status_code=303)


@router.get("/strava/connect")
def strava_connect(user: User = Depends(require_user)):
    from ..strava import service as strava

    if not strava.is_configured():
        return _dashboard(
            "Strava isn't configured on this server yet — set "
            "SPORTBRO_STRAVA_CLIENT_ID and SPORTBRO_STRAVA_CLIENT_SECRET."
        )
    state = _state_serializer().dumps({"uid": user.id})
    redirect_uri = f"{get_settings().base_url}/strava/callback"
    return RedirectResponse(
        strava.build_authorize_url(redirect_uri, state), status_code=303
    )


@router.get("/strava/callback")
def strava_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    scope: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..strava import service as strava

    if error:
        return _dashboard("Strava access was declined — nothing was connected.")
    if not code or not state:
        return _dashboard("Strava did not return an authorization code. Try again.")
    try:
        payload = _state_serializer().loads(state, max_age=_STATE_MAX_AGE)
    except BadSignature:
        return _dashboard("The Strava sign-in link expired — try connecting again.")
    if not isinstance(payload, dict) or payload.get("uid") != user.id:
        return _dashboard("This Strava sign-in belongs to a different session.")

    try:
        tokens = strava.exchange_code(code)
    except strava.StravaAuthRequired:
        return _dashboard("Strava rejected the authorization — try connecting again.")
    except strava.StravaNotConfigured as exc:
        return _dashboard(str(exc))

    athlete = tokens.get("athlete") or {}
    name = " ".join(
        part for part in (athlete.get("firstname"), athlete.get("lastname")) if part
    ) or None

    link = db.execute(
        select(StravaLink).where(StravaLink.user_id == user.id)
    ).scalar_one_or_none()
    if link is None:
        link = StravaLink(
            user_id=user.id, athlete_id=0, access_token="", refresh_token=""
        )
        db.add(link)
    link.athlete_id = int(athlete.get("id") or link.athlete_id or 0)
    link.athlete_name = name
    link.scope = scope
    link.access_token = security.encrypt_text(tokens["access_token"])
    link.refresh_token = security.encrypt_text(tokens["refresh_token"])
    link.expires_at = int(tokens.get("expires_at", 0))
    link.status = "active"
    db.commit()
    return _dashboard("Strava connected.")


@router.post("/strava/disconnect")
def strava_disconnect(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..garmin.service import invalidate_user_cache
    from ..strava import service as strava

    link = db.execute(
        select(StravaLink).where(StravaLink.user_id == user.id)
    ).scalar_one_or_none()
    if link is not None:
        try:
            strava.deauthorize(security.decrypt_text(link.access_token))
        except security.InvalidToken:
            pass
        db.delete(link)
        db.commit()
    invalidate_user_cache(user.id)
    return _dashboard("Strava disconnected.")
