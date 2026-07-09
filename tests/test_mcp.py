"""End-to-end MCP tests: real streamable-HTTP handshake against uvicorn.

No live Garmin: ``sportbrobot.garmin.service`` is replaced by a stub — via
attribute patching when the real module imports, or by injecting a fake module
into ``sys.modules`` otherwise (the server imports it lazily inside tools).
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
import types

import httpx
import pytest

EXPECTED_TOOLS = {
    "get_athlete_profile",
    "get_daily_summary",
    "list_activities",
    "get_activity_details",
    "get_sleep",
    "get_hrv",
    "get_training_status",
    "get_training_readiness",
    "get_body_battery",
    "get_stress",
    "get_steps",
    "get_heart_rate",
    "get_race_predictions",
    "get_body_composition",
    "strava_get_athlete",
    "strava_get_athlete_stats",
    "strava_list_activities",
    "strava_get_activity",
}

PROFILE = {"display_name": "davidk", "full_name": "David K", "unit_system": "metric"}
SLEEP_METRICS = {"sleep_hours": 7.5, "sleep_score": 82, "deep_minutes": 95}
ACTIVITIES = [
    {"activityId": 101, "name": "Morning Run", "type": "running", "distance_km": 10.2},
    {"activityId": 100, "name": "Evening Ride", "type": "cycling", "distance_km": 32.5},
]


class StubGarminData:
    """Canned stand-in for garmin.service.GarminData (contract surface)."""

    def get_profile(self):
        return dict(PROFILE)

    def get_daily_summary(self, date):
        return {"date": date, "steps": 8500}

    def list_activities(self, limit, start_date, end_date, activity_type):
        return [dict(a) for a in ACTIVITIES[:limit]]

    def get_activity(self, activity_id):
        return {"activityId": activity_id, "name": "Morning Run"}

    def get_sleep(self, date):
        return {"date": date, **SLEEP_METRICS}

    def get_hrv(self, date):
        return {"date": date, "last_night_avg": 55}

    def get_training_status(self, date):
        return {"date": date, "status": "productive"}

    def get_training_readiness(self, date):
        return {"date": date, "score": 78}

    def get_body_battery(self, start_date, end_date):
        return {"start": start_date, "end": end_date, "charged": 70}

    def get_stress(self, date):
        return {"date": date, "avg_stress": 31}

    def get_steps(self, start_date, end_date):
        return {"start": start_date, "end": end_date, "total_steps": 12000}

    def get_heart_rate(self, date):
        return {"date": date, "resting_hr": 48}

    def get_race_predictions(self):
        return {"time_5k": "20:31", "marathon": "3:29:00"}

    def get_body_composition(self, start_date, end_date):
        return {"start": start_date, "end": end_date, "weight_kg": 72.4}


def _install_garmin_stub():
    """Route garmin.service.get_client_for_user to the stub; return an undo.

    Prefers patching the real module; falls back to injecting a fake module
    into sys.modules when the real one is absent or fails to import.
    """

    def get_client_for_user(db, user_id):
        return StubGarminData()

    try:
        from sportbrobot.garmin import service as real
    except Exception:
        real = None

    if real is not None and hasattr(real, "get_client_for_user"):
        original = real.get_client_for_user

        def undo():
            real.get_client_for_user = original

        real.get_client_for_user = get_client_for_user
        return undo

    import sportbrobot.garmin as garmin_pkg

    fake = types.ModuleType("sportbrobot.garmin.service")
    fake.GarminNotLinked = type("GarminNotLinked", (Exception,), {})
    fake.GarminAuthRequired = type("GarminAuthRequired", (Exception,), {})
    fake.get_client_for_user = get_client_for_user
    fake.cached = lambda user_id, key, ttl, producer: producer()
    fake.invalidate_user_cache = lambda user_id: None
    sys.modules["sportbrobot.garmin.service"] = fake
    garmin_pkg.service = fake

    def undo():
        if sys.modules.get("sportbrobot.garmin.service") is fake:
            del sys.modules["sportbrobot.garmin.service"]
        if getattr(garmin_pkg, "service", None) is fake:
            del garmin_pkg.service

    return undo


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def mcp_server():
    """Boot a real uvicorn server with the MCP app mounted at /mcp.

    Yields ``(base_url, token)`` for a seeded user whose Garmin client is the
    stub above. Mirrors main.py: mcp_lifespan wired into the app lifespan and
    build_mcp_asgi_app() mounted at /mcp.
    """
    from contextlib import asynccontextmanager

    import uvicorn
    from fastapi import FastAPI

    from sportbrobot import models, security
    from sportbrobot.db import db_session, init_db
    from sportbrobot.mcp_server.server import build_mcp_asgi_app, mcp_lifespan

    undo_stub = _install_garmin_stub()

    init_db()
    token = security.generate_mcp_token()
    with db_session() as db:
        user = models.User(
            email="mcp-e2e@example.com", password_hash=security.hash_password("test-password")
        )
        db.add(user)
        db.flush()
        db.add(
            models.McpToken(
                user_id=user.id,
                token_hash=security.hash_mcp_token(token),
                token_encrypted=security.encrypt_text(token),
            )
        )

    @asynccontextmanager
    async def lifespan(app):
        init_db()
        async with mcp_lifespan():
            yield

    app = FastAPI(lifespan=lifespan)
    app.mount("/mcp", build_mcp_asgi_app(), name="mcp")

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn server thread exited during startup")
        if time.time() > deadline:
            raise RuntimeError("uvicorn server did not start in time")
        time.sleep(0.05)

    try:
        yield f"http://127.0.0.1:{port}", token
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        undo_stub()


async def _list_tool_names(url: str, headers: dict[str, str] | None = None) -> set[str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return {tool.name for tool in result.tools}


async def _call_tool(url: str, name: str, arguments: dict):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)


def test_list_tools_exposes_all_tools(mcp_server):
    base, token = mcp_server
    names = asyncio.run(_list_tool_names(f"{base}/mcp?apiKey={token}"))
    assert names == EXPECTED_TOOLS


def test_get_sleep_returns_canned_data(mcp_server):
    base, token = mcp_server
    result = asyncio.run(
        _call_tool(f"{base}/mcp?apiKey={token}", "get_sleep", {"date": "2026-07-07"})
    )
    assert result.isError is False
    payload = json.loads(result.content[0].text)
    assert payload == {"date": "2026-07-07", **SLEEP_METRICS}
    assert result.structuredContent == {"date": "2026-07-07", **SLEEP_METRICS}


def test_list_activities_returns_canned_data(mcp_server):
    base, token = mcp_server
    result = asyncio.run(
        _call_tool(f"{base}/mcp?apiKey={token}", "list_activities", {"limit": 2})
    )
    assert result.isError is False
    items = [json.loads(block.text) for block in result.content]
    assert items == ACTIVITIES
    assert result.structuredContent == {"result": ACTIVITIES}


def test_bearer_header_auth_works(mcp_server):
    base, token = mcp_server
    names = asyncio.run(
        _list_tool_names(f"{base}/mcp", headers={"Authorization": f"Bearer {token}"})
    )
    assert names == EXPECTED_TOOLS


def test_path_token_auth_works(mcp_server):
    base, token = mcp_server
    names = asyncio.run(_list_tool_names(f"{base}/mcp/{token}"))
    assert names == EXPECTED_TOOLS


def test_bad_token_is_rejected_with_401(mcp_server):
    base, _ = mcp_server
    resp = httpx.post(f"{base}/mcp/?apiKey=sbb_definitely-wrong", json={"ping": 1})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert "error" in resp.json()


def test_missing_token_is_rejected_with_401(mcp_server):
    base, _ = mcp_server
    resp = httpx.post(f"{base}/mcp/", json={"ping": 1})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"
    assert "error" in resp.json()
