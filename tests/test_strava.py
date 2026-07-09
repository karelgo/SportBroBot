"""Strava integration tests: OAuth flow, token refresh, trimmers — no network."""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from itsdangerous import URLSafeTimedSerializer

from sportbrobot import security
from sportbrobot.config import get_settings
from sportbrobot.db import db_session, init_db
from sportbrobot.models import StravaLink, User
from sportbrobot.strava import service


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> Any:
        return self._payload


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    init_db()
    monkeypatch.setattr(
        service,
        "_require_config",
        lambda: ("test-client-id", "test-client-secret"),
    )
    monkeypatch.setattr(service, "is_configured", lambda: True)


def make_linked_user(db, expires_in: int = 3600, status: str = "active"):
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.flush()
    link = StravaLink(
        user_id=user.id,
        athlete_id=4242,
        athlete_name="Test Athlete",
        access_token=security.encrypt_text("access-1"),
        refresh_token=security.encrypt_text("refresh-1"),
        expires_at=int(time.time()) + expires_in,
        status=status,
    )
    db.add(link)
    db.commit()
    return user, link


# ------------------------------------------------------------------- oauth


def test_authorize_url_contains_required_params():
    url = service.build_authorize_url("https://host/strava/callback", "signed-state")
    assert url.startswith(service.AUTHORIZE_URL + "?")
    assert "client_id=test-client-id" in url
    assert "redirect_uri=https%3A%2F%2Fhost%2Fstrava%2Fcallback" in url
    assert "response_type=code" in url
    assert "state=signed-state" in url
    assert "activity%3Aread_all" in url


def test_exchange_code_success(monkeypatch):
    seen = {}

    def fake_post(url, data=None, timeout=None):
        seen["url"], seen["data"] = url, data
        return FakeResponse(200, {"access_token": "a", "refresh_token": "r", "expires_at": 1})

    monkeypatch.setattr(service.requests, "post", fake_post)
    payload = service.exchange_code("the-code")
    assert payload["access_token"] == "a"
    assert seen["url"] == service.TOKEN_URL
    assert seen["data"]["code"] == "the-code"
    assert seen["data"]["grant_type"] == "authorization_code"


def test_exchange_code_failure_raises(monkeypatch):
    monkeypatch.setattr(
        service.requests, "post", lambda *a, **k: FakeResponse(400, {"message": "Bad Request"})
    )
    with pytest.raises(service.StravaAuthRequired):
        service.exchange_code("bad-code")


# --------------------------------------------------------- get_client_for_user


def test_get_client_not_linked(db):
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    with pytest.raises(service.StravaNotLinked):
        service.get_client_for_user(db, user.id)


def test_get_client_fresh_token_no_refresh(db, monkeypatch):
    user, _ = make_linked_user(db)

    def boom(*a, **k):
        raise AssertionError("refresh must not run for a fresh token")

    monkeypatch.setattr(service, "_refresh_tokens", boom)
    client = service.get_client_for_user(db, user.id)
    assert client.athlete_id == 4242
    assert client._headers["Authorization"] == "Bearer access-1"


def test_get_client_refreshes_expired_and_persists(db, monkeypatch):
    user, link = make_linked_user(db, expires_in=-10)
    monkeypatch.setattr(
        service,
        "_refresh_tokens",
        lambda refresh: {
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "expires_at": int(time.time()) + 21600,
        },
    )
    client = service.get_client_for_user(db, user.id)
    assert client._headers["Authorization"] == "Bearer access-2"
    with db_session() as check:
        row = check.get(StravaLink, link.id)
        assert security.decrypt_text(row.access_token) == "access-2"
        assert security.decrypt_text(row.refresh_token) == "refresh-2"
        assert row.expires_at > time.time() + 20000


def test_get_client_refresh_failure_flags_reauth(db, monkeypatch):
    user, link = make_linked_user(db, expires_in=-10)

    def refuse(refresh):
        raise service.StravaAuthRequired("refused")

    monkeypatch.setattr(service, "_refresh_tokens", refuse)
    with pytest.raises(service.StravaAuthRequired):
        service.get_client_for_user(db, user.id)
    with db_session() as check:
        assert check.get(StravaLink, link.id).status == "reauth_required"


def test_get_client_reauth_status_short_circuits(db):
    user, _ = make_linked_user(db, status="reauth_required")
    with pytest.raises(service.StravaAuthRequired):
        service.get_client_for_user(db, user.id)


# ---------------------------------------------------------------- fetchers


def _client_with(monkeypatch, payload, status=200):
    client = service.StravaData(access_token="tok", athlete_id=4242)
    seen = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"], seen["params"] = url, params
        return FakeResponse(status, payload)

    monkeypatch.setattr(service.requests, "get", fake_get)
    return client, seen


def test_list_activities_trims_and_bounds(monkeypatch):
    payload = [
        {
            "id": 1,
            "name": "Morning Run",
            "sport_type": "Run",
            "start_date_local": "2026-07-08T07:12:03Z",
            "distance": 12100.0,
            "moving_time": 3690,
            "average_heartrate": 152.4,
            "map": {"polyline": "should-be-dropped"},
            "athlete": {"id": 4242},
        }
    ]
    client, seen = _client_with(monkeypatch, payload)
    result = client.list_activities(limit=500, after_epoch=1000, before_epoch=2000)
    assert seen["url"].endswith("/athlete/activities")
    assert seen["params"]["per_page"] == 200  # clamped
    assert seen["params"]["after"] == 1000 and seen["params"]["before"] == 2000
    assert result[0]["name"] == "Morning Run"
    assert "map" not in result[0] and "athlete" not in result[0]


def test_get_activity_downsamples_splits(monkeypatch):
    payload = {
        "id": 9,
        "name": "Long Ride",
        "splits_metric": [
            {"split": i, "distance": 1000, "elapsed_time": 180 + i} for i in range(120)
        ],
        "best_efforts": [{"name": "1k", "distance": 1000, "elapsed_time": 210}],
        "segment_efforts": [{"huge": "blob"}],
    }
    client, _ = _client_with(monkeypatch, payload)
    detail = client.get_activity(9)
    assert len(detail["splits_metric"]) == 50
    assert detail["best_efforts"][0]["name"] == "1k"
    assert "segment_efforts" not in detail


def test_get_athlete_stats_trims(monkeypatch):
    payload = {
        "biggest_ride_distance": 160934.0,
        "recent_run_totals": {
            "count": 9,
            "distance": 92500.0,
            "moving_time": 30000,
            "elevation_gain": 800,
            "achievement_count": 4,
        },
        "ytd_ride_totals": {"count": 40, "distance": 1500000.0, "moving_time": 9,
                            "elevation_gain": 1},
    }
    client, seen = _client_with(monkeypatch, payload)
    stats = client.get_athlete_stats()
    assert seen["url"].endswith("/athletes/4242/stats")
    assert stats["recent_run_totals"]["count"] == 9
    assert "achievement_count" not in stats["recent_run_totals"]


def test_401_maps_to_auth_required(monkeypatch):
    client, _ = _client_with(monkeypatch, {"message": "Unauthorized"}, status=401)
    with pytest.raises(service.StravaAuthRequired):
        client.get_athlete()


# ----------------------------------------------------------------- web flow


def _signup(client):
    email = f"{uuid.uuid4().hex}@example.com"
    response = client.post(
        "/signup",
        data={"email": email, "password": "trainhard1", "password_confirm": "trainhard1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return email


def test_connect_redirects_to_strava(client, monkeypatch):
    import sportbrobot.strava.service as strava_service

    monkeypatch.setattr(strava_service, "is_configured", lambda: True)
    monkeypatch.setattr(
        strava_service,
        "build_authorize_url",
        lambda redirect_uri, state: f"https://www.strava.com/oauth/authorize?state={state}",
    )
    _signup(client)
    response = client.get("/strava/connect", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://www.strava.com/oauth/authorize")


def test_connect_unconfigured_explains(client, monkeypatch):
    import sportbrobot.strava.service as strava_service

    monkeypatch.setattr(strava_service, "is_configured", lambda: False)
    _signup(client)
    response = client.get("/strava/connect", follow_redirects=False)
    assert response.status_code == 303
    assert "STRAVA_CLIENT_ID" in response.headers["location"]


def _valid_state_for_current_user(client) -> str:
    from sportbrobot.models import User as UserModel

    with db_session() as db:
        uid = db.query(UserModel.id).order_by(UserModel.id.desc()).limit(1).scalar()
    serializer = URLSafeTimedSerializer(get_settings().secret_key, salt="sbb-strava-state")
    return serializer.dumps({"uid": uid})


def test_callback_stores_link(client, monkeypatch):
    import sportbrobot.strava.service as strava_service

    monkeypatch.setattr(
        strava_service,
        "exchange_code",
        lambda code: {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_at": int(time.time()) + 21600,
            "athlete": {"id": 777, "firstname": "Karel", "lastname": "G"},
        },
    )
    _signup(client)
    state = _valid_state_for_current_user(client)
    response = client.get(
        f"/strava/callback?code=abc&state={state}&scope=read,activity:read_all",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Strava+connected" in response.headers["location"]
    with db_session() as db:
        link = db.query(StravaLink).order_by(StravaLink.id.desc()).first()
        assert link.athlete_id == 777
        assert link.athlete_name == "Karel G"
        assert security.decrypt_text(link.access_token) == "at-1"


def test_callback_rejects_bad_state(client):
    _signup(client)
    response = client.get(
        "/strava/callback?code=abc&state=tampered", follow_redirects=False
    )
    assert response.status_code == 303
    assert "expired" in response.headers["location"]
    with db_session() as db:
        before = db.query(StravaLink).count()
    assert before == db_strava_count()


def db_strava_count() -> int:
    with db_session() as db:
        return db.query(StravaLink).count()


def test_callback_declined(client):
    _signup(client)
    response = client.get("/strava/callback?error=access_denied", follow_redirects=False)
    assert response.status_code == 303
    assert "declined" in response.headers["location"]


def test_disconnect_deletes_link(client, monkeypatch):
    import sportbrobot.strava.service as strava_service

    revoked = {}
    monkeypatch.setattr(
        strava_service, "deauthorize", lambda token: revoked.setdefault("token", token)
    )
    monkeypatch.setattr(
        strava_service,
        "exchange_code",
        lambda code: {
            "access_token": "at-2",
            "refresh_token": "rt-2",
            "expires_at": int(time.time()) + 21600,
            "athlete": {"id": 778, "firstname": "K", "lastname": "G"},
        },
    )
    _signup(client)
    state = _valid_state_for_current_user(client)
    client.get(f"/strava/callback?code=abc&state={state}", follow_redirects=False)
    before = db_strava_count()
    response = client.post("/strava/disconnect", follow_redirects=False)
    assert response.status_code == 303
    assert db_strava_count() == before - 1
    assert revoked["token"] == "at-2"
