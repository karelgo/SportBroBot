"""Web-layer tests: auth flows, dashboard, Garmin connect (mocked), MCP setup pages."""

from __future__ import annotations

import uuid
from urllib.parse import unquote_plus

import pytest
from sqlalchemy import select

PASSWORD = "password123"


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex[:10]}@example.com"


def _signup(client, email: str | None = None, password: str = PASSWORD) -> str:
    email = email or _unique_email()
    response = client.post(
        "/signup",
        data={"email": email, "password": password, "password_confirm": password},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    return email


def _get_user(db, email: str):
    from sportbrobot.models import User

    return db.execute(select(User).where(User.email == email)).scalar_one()


def _add_link(db, user_id: int) -> None:
    from sportbrobot import security
    from sportbrobot.models import GarminLink

    db.add(
        GarminLink(
            user_id=user_id,
            garmin_email="garmin@example.com",
            token_blob=security.encrypt_text('{"oauth": "blob"}'),
            status="active",
        )
    )
    db.commit()


@pytest.fixture()
def login_success():
    from sportbrobot.garmin.service import LoginSuccess

    return LoginSuccess(
        token_blob='{"oauth": "fresh-blob"}',
        display_name="display-name",
        full_name="Test Athlete",
        unit_system="metric",
        garmin_email="garmin@example.com",
    )


def test_landing_renders(client):
    response = client.get("/")
    assert response.status_code == 200


def test_signup_password_mismatch_rerenders(client):
    response = client.post(
        "/signup",
        data={
            "email": _unique_email(),
            "password": PASSWORD,
            "password_confirm": "different-1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "match" in response.text.lower()
    assert "sbb_session" not in client.cookies


def test_signup_short_password_rejected(client):
    response = client.post(
        "/signup",
        data={"email": _unique_email(), "password": "short", "password_confirm": "short"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "sbb_session" not in client.cookies


def test_signup_bad_email_rejected(client):
    response = client.post(
        "/signup",
        data={"email": "not-an-email", "password": PASSWORD, "password_confirm": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_signup_success_sets_cookie_and_creates_user(client, db):
    email = _signup(client)
    assert "sbb_session" in client.cookies
    user = _get_user(db, email)
    assert user.password_hash.startswith("scrypt$")


def test_signup_duplicate_email_rejected(client):
    email = _signup(client)
    client.cookies.clear()
    response = client.post(
        "/signup",
        data={"email": email, "password": PASSWORD, "password_confirm": PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "already" in response.text.lower()


def test_login_success_and_wrong_password(client):
    email = _signup(client)
    client.cookies.clear()

    bad = client.post(
        "/login",
        data={"email": email, "password": "wrong-password"},
        follow_redirects=False,
    )
    assert bad.status_code == 400
    assert "Invalid email or password" in bad.text
    assert "sbb_session" not in client.cookies

    good = client.post(
        "/login", data={"email": email, "password": PASSWORD}, follow_redirects=False
    )
    assert good.status_code == 303
    assert good.headers["location"] == "/dashboard"
    assert "sbb_session" in client.cookies


def test_logout_clears_session(client):
    _signup(client)
    response = client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    after = client.get("/dashboard", follow_redirects=False)
    assert after.status_code == 303


def test_dashboard_anonymous_redirects_to_login(client):
    response = client.get("/dashboard", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_dashboard_renders_without_link_and_mints_token(client, db):
    from sportbrobot.models import McpToken

    email = _signup(client)
    response = client.get("/dashboard")
    assert response.status_code == 200
    user = _get_user(db, email)
    token = db.execute(
        select(McpToken).where(McpToken.user_id == user.id)
    ).scalar_one()
    assert token.token_hash


def test_garmin_connect_success(client, db, monkeypatch, login_success):
    from sportbrobot import security
    from sportbrobot.garmin import service as garmin_service
    from sportbrobot.models import GarminLink

    email = _signup(client)
    seen: dict[str, str] = {}

    def fake_start(garmin_email, password):
        seen["email"] = garmin_email
        return login_success

    monkeypatch.setattr(garmin_service, "start_garmin_login", fake_start)
    response = client.post(
        "/garmin/connect",
        data={"garmin_email": "garmin@example.com", "garmin_password": "secret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/dashboard")
    assert seen["email"] == "garmin@example.com"

    user = _get_user(db, email)
    link = db.execute(
        select(GarminLink).where(GarminLink.user_id == user.id)
    ).scalar_one()
    assert link.status == "active"
    assert link.garmin_email == "garmin@example.com"
    assert security.decrypt_text(link.token_blob) == '{"oauth": "fresh-blob"}'


def test_garmin_connect_mfa_renders_pending_form(client, monkeypatch):
    from sportbrobot.garmin import service as garmin_service

    _signup(client)
    monkeypatch.setattr(
        garmin_service,
        "start_garmin_login",
        lambda email, password: garmin_service.MfaPending(pending_id="pend-abc-123"),
    )
    response = client.post(
        "/garmin/connect",
        data={"garmin_email": "garmin@example.com", "garmin_password": "secret"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert "pend-abc-123" in response.text


def test_garmin_connect_auth_error_flashes(client, monkeypatch):
    from garminconnect import GarminConnectAuthenticationError

    from sportbrobot.garmin import service as garmin_service

    _signup(client)

    def fake_start(email, password):
        raise GarminConnectAuthenticationError("Garmin rejected the credentials.")

    monkeypatch.setattr(garmin_service, "start_garmin_login", fake_start)
    response = client.post(
        "/garmin/connect",
        data={"garmin_email": "garmin@example.com", "garmin_password": "bad"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = unquote_plus(response.headers["location"])
    assert location.startswith("/dashboard?msg=")
    assert "rejected" in location


def test_garmin_mfa_complete_success(client, db, monkeypatch, login_success):
    from sportbrobot.garmin import service as garmin_service
    from sportbrobot.models import GarminLink

    email = _signup(client)
    monkeypatch.setattr(
        garmin_service, "complete_garmin_mfa", lambda pending_id, code: login_success
    )
    response = client.post(
        "/garmin/mfa",
        data={"pending_id": "pend-abc-123", "mfa_code": "123456"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/dashboard")
    user = _get_user(db, email)
    link = db.execute(
        select(GarminLink).where(GarminLink.user_id == user.id)
    ).scalar_one()
    assert link.status == "active"


def test_garmin_mfa_expired_redirects_with_message(client, monkeypatch):
    from sportbrobot.garmin import service as garmin_service

    _signup(client)

    def fake_complete(pending_id, code):
        raise KeyError(pending_id)

    monkeypatch.setattr(garmin_service, "complete_garmin_mfa", fake_complete)
    response = client.post(
        "/garmin/mfa",
        data={"pending_id": "gone", "mfa_code": "123456"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "expired" in unquote_plus(response.headers["location"]).lower()


def test_garmin_disconnect_removes_link(client, db, monkeypatch):
    from sportbrobot.garmin import service as garmin_service
    from sportbrobot.models import GarminLink

    email = _signup(client)
    user = _get_user(db, email)
    _add_link(db, user.id)

    invalidated: list[int] = []
    monkeypatch.setattr(garmin_service, "invalidate_user_cache", invalidated.append)

    response = client.post("/garmin/disconnect", follow_redirects=False)
    assert response.status_code == 303
    assert invalidated == [user.id]
    db.expire_all()
    link = db.execute(
        select(GarminLink).where(GarminLink.user_id == user.id)
    ).scalar_one_or_none()
    assert link is None


def test_mcp_rotate_replaces_token(client, db):
    from sportbrobot.models import McpToken

    email = _signup(client)
    first = client.post("/mcp/rotate", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/dashboard?msg=Token+rotated"

    user = _get_user(db, email)
    old_hash = db.execute(
        select(McpToken.token_hash).where(McpToken.user_id == user.id)
    ).scalar_one()

    second = client.post("/mcp/rotate", follow_redirects=False)
    assert second.status_code == 303
    db.expire_all()
    new_hash = db.execute(
        select(McpToken.token_hash).where(McpToken.user_id == user.id)
    ).scalar_one()
    assert new_hash != old_hash


def test_dashboard_flags_reauth_on_auth_error(client, db, monkeypatch):
    from sportbrobot.garmin import service as garmin_service
    from sportbrobot.models import GarminLink

    email = _signup(client)
    user = _get_user(db, email)
    _add_link(db, user.id)

    def fake_get_client(session, user_id):
        raise garmin_service.GarminAuthRequired("session dead")

    monkeypatch.setattr(garmin_service, "get_client_for_user", fake_get_client)
    response = client.get("/dashboard")
    assert response.status_code == 200
    db.expire_all()
    link = db.execute(
        select(GarminLink).where(GarminLink.user_id == user.id)
    ).scalar_one()
    assert link.status == "reauth_required"


def test_fetch_stats_shapes_dashboard_dict(monkeypatch):
    from sportbrobot.garmin import service as garmin_service
    from sportbrobot.web import dashboard as dashboard_module

    class FakeClient:
        def get_daily_summary(self, date):
            return {"totalSteps": 12345, "restingHeartRate": 48}

        def get_sleep(self, date):
            return {"sleepTimeSeconds": 27000}

        def get_body_battery(self, start, end):
            return [{"endOfDayLevel": 62, "highestLevel": 90}]

        def list_activities(self, limit=1):
            return [
                {
                    "activityName": "Morning Run",
                    "activityType": "running",
                    "startTimeLocal": "2026-07-08 07:01:00",
                    "distance": 10000.0,
                    "duration": 3000.0,
                }
            ]

    monkeypatch.setattr(
        garmin_service, "get_client_for_user", lambda db, user_id: FakeClient()
    )
    stats = dashboard_module._fetch_stats(None, user_id=1)
    assert stats["steps_today"] == 12345
    assert stats["resting_hr"] == 48
    assert stats["sleep_hours"] == 7.5
    assert stats["body_battery"] == 62
    assert stats["last_activity"] == {
        "name": "Morning Run",
        "type": "running",
        "date": "2026-07-08",
        "distance_km": 10.0,
        "duration_min": 50,
    }


def test_mcp_setup_pages(client):
    hub = client.get("/mcp/setup")
    assert hub.status_code == 200
    for slug in ("claude-desktop", "claude-code", "cursor", "chatgpt"):
        assert f'href="/mcp/setup/{slug}"' in hub.text  # cards link by slug
        response = client.get(f"/mcp/setup/{slug}")
        assert response.status_code == 200, slug
    assert "Claude Desktop" in hub.text
    assert client.get("/mcp/setup/not-a-client").status_code == 404


def test_mcp_setup_shows_personal_url_when_logged_in(client):
    _signup(client)
    response = client.get("/mcp/setup")
    assert response.status_code == 200
    assert "apiKey=sbb_" in response.text
