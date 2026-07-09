"""Strava integration: real OAuth login, token refresh, trimmed data fetchers.

Unlike Garmin, Strava has a self-serve public API: the operator creates an API
application at https://www.strava.com/settings/api and sets
SPORTBRO_STRAVA_CLIENT_ID / SPORTBRO_STRAVA_CLIENT_SECRET. Users then sign in
at Strava's own authorization page — SportBroBot never sees their password.
Access/refresh tokens are Fernet-encrypted at rest; access tokens are
refreshed automatically when within a minute of expiry and rotated refresh
tokens are persisted back to the ``strava_links`` row.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..config import get_settings
from ..db import db_session
from ..models import StravaLink

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
DEAUTHORIZE_URL = "https://www.strava.com/oauth/deauthorize"
API_BASE = "https://www.strava.com/api/v3"

SCOPES = "read,activity:read_all,profile:read_all"
_TIMEOUT = 15
_MAX_SERIES_POINTS = 50


class StravaNotConfigured(Exception):
    """The server has no Strava API application configured."""


class StravaNotLinked(Exception):
    """The user has no Strava account linked."""


class StravaAuthRequired(Exception):
    """The stored Strava tokens are dead; the user must reconnect."""


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.strava_client_id and settings.strava_client_secret)


def _require_config() -> tuple[str, str]:
    settings = get_settings()
    if not (settings.strava_client_id and settings.strava_client_secret):
        raise StravaNotConfigured(
            "Set SPORTBRO_STRAVA_CLIENT_ID and SPORTBRO_STRAVA_CLIENT_SECRET "
            "(create an API application at https://www.strava.com/settings/api)."
        )
    return settings.strava_client_id, settings.strava_client_secret


def build_authorize_url(redirect_uri: str, state: str) -> str:
    client_id, _ = _require_config()
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "approval_prompt": "auto",
            "scope": SCOPES,
            "state": state,
        }
    )


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange an authorization code for tokens + the athlete summary."""
    client_id, client_secret = _require_config()
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=_TIMEOUT,
    )
    if response.status_code >= 400:
        raise StravaAuthRequired(
            f"Strava rejected the authorization code (HTTP {response.status_code})."
        )
    return response.json()


def _refresh_tokens(refresh_token: str) -> dict[str, Any]:
    client_id, client_secret = _require_config()
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=_TIMEOUT,
    )
    if response.status_code >= 400:
        raise StravaAuthRequired(
            "Strava refused to refresh the session; please reconnect Strava."
        )
    return response.json()


def deauthorize(access_token: str) -> None:
    """Best-effort revocation when the user disconnects."""
    try:
        requests.post(
            DEAUTHORIZE_URL, data={"access_token": access_token}, timeout=_TIMEOUT
        )
    except requests.RequestException:
        pass


def get_client_for_user(db: Session, user_id: int) -> "StravaData":
    """Return an authenticated :class:`StravaData` for the user's link.

    Refreshes the access token when it expires within 60 seconds; rotated
    tokens are re-encrypted and persisted via a fresh session (safe from MCP
    threadpool threads).
    """
    link = db.execute(
        select(StravaLink).where(StravaLink.user_id == user_id)
    ).scalar_one_or_none()
    if link is None:
        raise StravaNotLinked("No Strava account is linked to this user.")
    if link.status == "reauth_required":
        raise StravaAuthRequired("The Strava connection needs to be re-linked.")
    try:
        access_token = security.decrypt_text(link.access_token)
        refresh_token = security.decrypt_text(link.refresh_token)
    except security.InvalidToken as exc:
        raise StravaAuthRequired(
            "Stored Strava tokens could not be decrypted; please reconnect."
        ) from exc

    if link.expires_at <= int(time.time()) + 60:
        link_id = link.id
        try:
            payload = _refresh_tokens(refresh_token)
        except StravaAuthRequired:
            with db_session() as session:
                row = session.get(StravaLink, link_id)
                if row is not None:
                    row.status = "reauth_required"
            raise
        access_token = payload["access_token"]
        with db_session() as session:
            row = session.get(StravaLink, link_id)
            if row is not None:
                row.access_token = security.encrypt_text(access_token)
                row.refresh_token = security.encrypt_text(payload["refresh_token"])
                row.expires_at = int(payload.get("expires_at", 0))

    return StravaData(access_token=access_token, athlete_id=link.athlete_id)


def _downsample(series: list[Any], max_points: int = _MAX_SERIES_POINTS) -> list[Any]:
    if len(series) <= max_points:
        return series
    step = (len(series) - 1) / (max_points - 1)
    return [series[round(i * step)] for i in range(max_points)]


def _pick(data: Any, *keys: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in keys if data.get(key) is not None}


_ACTIVITY_FIELDS = (
    "id",
    "name",
    "sport_type",
    "type",
    "start_date_local",
    "distance",
    "moving_time",
    "elapsed_time",
    "total_elevation_gain",
    "average_speed",
    "max_speed",
    "average_heartrate",
    "max_heartrate",
    "average_watts",
    "weighted_average_watts",
    "max_watts",
    "average_cadence",
    "kilojoules",
    "suffer_score",
    "elev_high",
    "elev_low",
    "pr_count",
    "achievement_count",
)

_TOTALS_FIELDS = ("count", "distance", "moving_time", "elevation_gain")


class StravaData:
    """Authenticated Strava API client returning trimmed, JSON-safe data."""

    def __init__(self, access_token: str, athlete_id: int) -> None:
        self._headers = {"Authorization": f"Bearer {access_token}"}
        self.athlete_id = athlete_id

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = requests.get(
            f"{API_BASE}{path}", headers=self._headers, params=params, timeout=_TIMEOUT
        )
        if response.status_code == 401:
            raise StravaAuthRequired("The Strava session has expired; please reconnect.")
        if response.status_code == 429:
            raise RuntimeError(
                "Strava rate limit reached (200 requests / 15 min). Try again shortly."
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Strava API error {response.status_code} on {path}.")
        return response.json()

    def get_athlete(self) -> dict[str, Any]:
        raw = self._get("/athlete")
        return _pick(
            raw,
            "id",
            "username",
            "firstname",
            "lastname",
            "city",
            "country",
            "sex",
            "weight",
            "ftp",
            "summit",
            "created_at",
        )

    def get_athlete_stats(self) -> dict[str, Any]:
        raw = self._get(f"/athletes/{self.athlete_id}/stats")
        result: dict[str, Any] = _pick(
            raw, "biggest_ride_distance", "biggest_climb_elevation_gain"
        )
        for key in (
            "recent_ride_totals",
            "recent_run_totals",
            "recent_swim_totals",
            "ytd_ride_totals",
            "ytd_run_totals",
            "ytd_swim_totals",
            "all_ride_totals",
            "all_run_totals",
            "all_swim_totals",
        ):
            totals = _pick(raw.get(key) if isinstance(raw, dict) else None, *_TOTALS_FIELDS)
            if totals:
                result[key] = totals
        return result

    def list_activities(
        self,
        limit: int = 10,
        after_epoch: int | None = None,
        before_epoch: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        params: dict[str, Any] = {"per_page": limit, "page": 1}
        if after_epoch is not None:
            params["after"] = after_epoch
        if before_epoch is not None:
            params["before"] = before_epoch
        raw = self._get("/athlete/activities", params=params)
        if not isinstance(raw, list):
            return []
        return [_pick(activity, *_ACTIVITY_FIELDS) for activity in raw[:limit]]

    def get_activity(self, activity_id: int | str) -> dict[str, Any]:
        raw = self._get(f"/activities/{activity_id}")
        detail = _pick(raw, *_ACTIVITY_FIELDS)
        detail.update(_pick(raw, "description", "calories", "device_name", "gear_id"))
        splits = raw.get("splits_metric") if isinstance(raw, dict) else None
        if isinstance(splits, list) and splits:
            detail["splits_metric"] = [
                _pick(
                    split,
                    "split",
                    "distance",
                    "elapsed_time",
                    "moving_time",
                    "average_speed",
                    "average_heartrate",
                    "elevation_difference",
                )
                for split in _downsample(splits)
            ]
        efforts = raw.get("best_efforts") if isinstance(raw, dict) else None
        if isinstance(efforts, list) and efforts:
            detail["best_efforts"] = [
                _pick(effort, "name", "distance", "elapsed_time", "pr_rank")
                for effort in _downsample(efforts, 20)
            ]
        return detail
