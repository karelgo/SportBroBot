"""Logged-in area: dashboard, Garmin connect/MFA/disconnect, MCP token rotation."""

from __future__ import annotations

import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..models import GarminLink, McpToken, User
from .deps import get_db, get_mcp_url, require_user, templates

router = APIRouter()

_STATS_TTL_SECONDS = 600.0


def _dashboard_redirect(msg: str | None = None) -> RedirectResponse:
    url = "/dashboard"
    if msg:
        url += "?" + urlencode({"msg": msg})
    return RedirectResponse(url, status_code=303)


def _get_link(db: Session, user_id: int) -> GarminLink | None:
    return db.execute(
        select(GarminLink).where(GarminLink.user_id == user_id)
    ).scalar_one_or_none()


def _fetch_stats(db: Session, user_id: int) -> dict[str, Any]:
    """Build the dashboard quick-stats dict from live Garmin data.

    Each fetch is wrapped defensively so one flaky endpoint doesn't blank the
    whole panel; only auth failures propagate (the caller flags the link).
    """
    from ..garmin import service as garmin_service

    client = garmin_service.get_client_for_user(db, user_id)
    today = datetime.date.today().isoformat()
    stats: dict[str, Any] = {
        "steps_today": None,
        "resting_hr": None,
        "sleep_hours": None,
        "body_battery": None,
        "last_activity": None,
    }

    try:
        summary = client.get_daily_summary(today)
        stats["steps_today"] = summary.get("totalSteps")
        stats["resting_hr"] = summary.get("restingHeartRate")
    except garmin_service.GarminAuthRequired:
        raise
    except Exception:
        pass

    try:
        sleep = client.get_sleep(today)
        seconds = sleep.get("sleepTimeSeconds")
        if isinstance(seconds, (int, float)):
            stats["sleep_hours"] = round(seconds / 3600, 1)
    except garmin_service.GarminAuthRequired:
        raise
    except Exception:
        pass

    try:
        days = client.get_body_battery(today, today)
        if days:
            day = days[0]
            stats["body_battery"] = day.get("endOfDayLevel", day.get("highestLevel"))
    except garmin_service.GarminAuthRequired:
        raise
    except Exception:
        pass

    try:
        activities = client.list_activities(limit=1)
        if activities:
            activity = activities[0]
            distance = activity.get("distance")
            duration = activity.get("duration")
            stats["last_activity"] = {
                "name": activity.get("activityName"),
                "type": activity.get("activityType"),
                "date": (activity.get("startTimeLocal") or "")[:10] or None,
                "distance_km": round(distance / 1000, 2)
                if isinstance(distance, (int, float))
                else None,
                "duration_min": round(duration / 60)
                if isinstance(duration, (int, float))
                else None,
            }
    except garmin_service.GarminAuthRequired:
        raise
    except Exception:
        pass

    return stats


_RECONNECT_MESSAGE = (
    "Your Garmin session has expired — please reconnect your account below."
)


def _build_stats(
    db: Session, user: User, link: GarminLink | None
) -> tuple[dict[str, Any] | None, str | None]:
    if link is None:
        return None, None
    if link.status != "active":
        return None, _RECONNECT_MESSAGE
    from ..garmin import service as garmin_service

    try:
        stats = garmin_service.cached(
            user.id,
            "dashboard-stats",
            _STATS_TTL_SECONDS,
            lambda: _fetch_stats(db, user.id),
        )
    except garmin_service.GarminAuthRequired:
        link.status = "reauth_required"
        db.commit()
        return None, _RECONNECT_MESSAGE
    except Exception:
        return None, "Could not fetch data from Garmin right now."
    return stats, None


@router.get("/dashboard")
def dashboard(
    request: Request,
    msg: str | None = None,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    link = _get_link(db, user.id)
    mcp_url = get_mcp_url(db, user)
    stats, stats_error = _build_stats(db, user, link)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "link": link,
            "mcp_url": mcp_url,
            "stats": stats,
            "stats_error": stats_error,
            "flash": msg,
        },
    )


def _save_link(db: Session, user: User, result: Any) -> None:
    """Upsert the user's GarminLink from a garmin.service LoginSuccess."""
    link = _get_link(db, user.id)
    if link is None:
        link = GarminLink(user_id=user.id, garmin_email=result.garmin_email, token_blob="")
        db.add(link)
    link.garmin_email = result.garmin_email
    link.display_name = result.display_name
    link.full_name = result.full_name
    link.unit_system = result.unit_system
    link.token_blob = security.encrypt_text(result.token_blob)
    link.status = "active"
    db.commit()


@router.post("/garmin/connect")
def garmin_connect(
    request: Request,
    garmin_email: str = Form(...),
    garmin_password: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from garminconnect import (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    from ..garmin import service as garmin_service

    try:
        result = garmin_service.start_garmin_login(garmin_email.strip(), garmin_password)
    except (
        GarminConnectAuthenticationError,
        GarminConnectTooManyRequestsError,
        GarminConnectConnectionError,
    ) as exc:
        return _dashboard_redirect(str(exc))

    if isinstance(result, garmin_service.MfaPending):
        return templates.TemplateResponse(
            request,
            "garmin_mfa.html",
            {"user": user, "pending_id": result.pending_id, "error": None},
        )

    _save_link(db, user, result)
    garmin_service.invalidate_user_cache(user.id)
    return _dashboard_redirect("Garmin account connected.")


@router.post("/garmin/mfa")
def garmin_mfa(
    request: Request,
    pending_id: str = Form(...),
    mfa_code: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from garminconnect import (
        GarminConnectAuthenticationError,
        GarminConnectConnectionError,
        GarminConnectTooManyRequestsError,
    )

    from ..garmin import service as garmin_service

    try:
        result = garmin_service.complete_garmin_mfa(pending_id, mfa_code.strip())
    except KeyError:
        return _dashboard_redirect("MFA session expired, try again.")
    except GarminConnectAuthenticationError:
        # The pending login is consumed on failure, so the code can't be retried.
        return _dashboard_redirect(
            "The MFA code was not accepted — start the Garmin connection again."
        )
    except (GarminConnectTooManyRequestsError, GarminConnectConnectionError) as exc:
        return _dashboard_redirect(str(exc))

    _save_link(db, user, result)
    garmin_service.invalidate_user_cache(user.id)
    return _dashboard_redirect("Garmin account connected.")


@router.post("/garmin/disconnect")
def garmin_disconnect(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    from ..garmin import service as garmin_service

    link = _get_link(db, user.id)
    if link is not None:
        db.delete(link)
        db.commit()
    garmin_service.invalidate_user_cache(user.id)
    return _dashboard_redirect("Garmin account disconnected.")


@router.post("/mcp/rotate")
def mcp_rotate(
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    row = db.execute(
        select(McpToken).where(McpToken.user_id == user.id)
    ).scalar_one_or_none()
    if row is not None:
        db.delete(row)
        db.flush()
    token = security.generate_mcp_token()
    db.add(
        McpToken(
            user_id=user.id,
            token_hash=security.hash_mcp_token(token),
            token_encrypted=security.encrypt_text(token),
        )
    )
    db.commit()
    return _dashboard_redirect("Token rotated")
