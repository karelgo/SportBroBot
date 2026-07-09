"""Personal MCP server: FastMCP tools over the authenticated user's Garmin data.

The streamable-HTTP FastMCP app is wrapped in :class:`TokenAuthMiddleware`,
which resolves an ``sbb_`` MCP token (query param, Bearer header, or first
path segment) to a user id stashed in a ContextVar. Tools are ``async def``s
that offload the blocking Garmin I/O to a worker thread via
``anyio.to_thread.run_sync`` (FastMCP 1.x calls sync tools directly on the
event loop, which would freeze the server); contextvars propagate into the
thread. Data loads lazily through ``sportbrobot.garmin.service``, so importing
this module never touches settings, the database, or the Garmin client.
"""

from __future__ import annotations

import contextvars
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Callable
from urllib.parse import parse_qs

from anyio import to_thread
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

_INSTRUCTIONS = """\
SportBroBot gives AI assistants read-only access to one athlete's training
data. Garmin Connect tools cover the athlete profile, daily wellness (sleep,
HRV, stress, body battery, steps, heart rate), training state (training
status, readiness, race predictions), activities and body composition. The
strava_* tools cover the athlete's Strava profile, lifetime/year-to-date
stats and activities (with splits and best efforts).

Every tool operates on the account that owns the MCP token used to connect —
no athlete id or credentials are ever passed as arguments. Dates are
'YYYY-MM-DD' strings and default to today in the server's timezone — pass an
explicit date when the athlete's local calendar day may differ. Data is
fetched live from Garmin Connect with short-lived caching, so repeated calls
are cheap.
"""

# The token in the URL is the credential; DNS-rebinding Host checks would
# break deployments behind a real domain or reverse proxy.
mcp = FastMCP(
    "SportBroBot",
    instructions=_INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_user_id_var: contextvars.ContextVar[int] = contextvars.ContextVar("sportbrobot_mcp_user_id")

PROFILE_TTL = 900
DAILY_TTL = 600
ACTIVITY_TTL = 300

_NOT_LINKED_MSG = (
    "No Garmin account is linked yet. Open the SportBroBot dashboard, connect "
    "your Garmin account, then try again."
)
_REAUTH_MSG = (
    "The Garmin connection has expired. Open the SportBroBot dashboard and "
    "reconnect your Garmin account, then try again."
)
_STRAVA_NOT_LINKED_MSG = (
    "No Strava account is linked yet. Open the SportBroBot dashboard and "
    "click 'Connect with Strava', then try again."
)
_STRAVA_REAUTH_MSG = (
    "The Strava connection has expired. Open the SportBroBot dashboard and "
    "reconnect Strava, then try again."
)


def current_user_id() -> int:
    """Return the user id of the authenticated MCP request.

    Raises RuntimeError when called outside an authenticated request context.
    """
    try:
        return _user_id_var.get()
    except LookupError:
        raise RuntimeError("No authenticated MCP user in the current context") from None


# --------------------------------------------------------------------------
# Token authentication (pure ASGI)
# --------------------------------------------------------------------------


def _extract_token(scope: Scope) -> tuple[str | None, str]:
    """Extract the MCP token from an http scope.

    Priority: ``?apiKey=`` query param, ``Authorization: Bearer`` header, then
    the first path segment (which is stripped from the path). Returns
    ``(token, new_path)`` where ``new_path`` is the normalized ``scope["path"]``
    the inner app should see (always ending in ``"/"`` at the mount root).
    """
    root_path = scope.get("root_path", "")
    path: str = scope["path"]
    route_path = path[len(root_path) :] if root_path and path.startswith(root_path) else path

    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    values = query.get("apiKey")
    token = values[0] if values else None

    if not token:
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, credentials = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and credentials.strip():
                    token = credentials.strip()
                break

    if not token and route_path not in ("", "/"):
        first, _, rest = route_path.lstrip("/").partition("/")
        if first:
            token = first
            route_path = "/" + rest

    return token, root_path + (route_path or "/")


def _authenticate(token: str) -> int | None:
    """Resolve a token to a user id via its SHA-256 hash, updating last_used_at."""
    from sqlalchemy import select

    from ..db import db_session
    from ..models import McpToken, utcnow
    from ..security import hash_mcp_token

    token_hash = hash_mcp_token(token)
    with db_session() as db:
        row = db.execute(select(McpToken).where(McpToken.token_hash == token_hash)).scalar_one_or_none()
        if row is None:
            return None
        row.last_used_at = utcnow()
        return row.user_id


async def _send_unauthorized(send: Send, message: str) -> None:
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class TokenAuthMiddleware:
    """Pure-ASGI middleware that authenticates MCP tokens for http requests.

    Non-http scopes (lifespan, websocket) pass straight through. On success the
    resolved user id is stashed in a ContextVar for the duration of the request
    so tools running anywhere downstream (including worker threads) can read it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope = dict(scope)
        token, new_path = _extract_token(scope)
        if new_path != scope["path"]:
            scope["path"] = new_path
            scope.pop("raw_path", None)

        if token is None:
            await _send_unauthorized(
                send,
                "Missing MCP token. Pass it as ?apiKey=, an Authorization: "
                "Bearer header, or the first path segment.",
            )
            return

        user_id = await to_thread.run_sync(_authenticate, token)
        if user_id is None:
            await _send_unauthorized(
                send,
                "Invalid MCP token. Copy the current MCP URL from your "
                "SportBroBot dashboard.",
            )
            return

        ctx_token = _user_id_var.set(user_id)
        try:
            await self.app(scope, receive, send)
        finally:
            _user_id_var.reset(ctx_token)


# --------------------------------------------------------------------------
# ASGI app + lifespan
# --------------------------------------------------------------------------

_streamable_app: ASGIApp | None = None


def _refresh_streamable_app() -> None:
    """(Re)build the FastMCP streamable-HTTP app.

    A StreamableHTTPSessionManager can only be run once; dropping a consumed
    manager lets each application lifespan (server restart, repeated test apps)
    get a fresh one from ``streamable_http_app()``.
    """
    global _streamable_app
    manager = mcp._session_manager
    if manager is not None and getattr(manager, "_has_started", False):
        mcp._session_manager = None
    _streamable_app = mcp.streamable_http_app()


async def _dispatch(scope: Scope, receive: Receive, send: Send) -> None:
    if _streamable_app is None:
        _refresh_streamable_app()
    assert _streamable_app is not None
    await _streamable_app(scope, receive, send)


def build_mcp_asgi_app() -> ASGIApp:
    """Return the FastMCP streamable-HTTP app wrapped in token authentication.

    Mount the result at ``/mcp``; the FastMCP route itself is registered at the
    mount root (``streamable_http_path="/"``).
    """
    return TokenAuthMiddleware(_dispatch)


@asynccontextmanager
async def mcp_lifespan() -> AsyncIterator[None]:
    """Run the FastMCP session manager for the duration of the app lifespan."""
    _refresh_streamable_app()
    async with mcp.session_manager.run():
        yield


# --------------------------------------------------------------------------
# Tool plumbing
# --------------------------------------------------------------------------


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _norm_date(value: str | None, param: str = "date") -> str:
    """Validate/normalize a YYYY-MM-DD date string, defaulting to today."""
    if value is None:
        return _today()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ToolError(f"Invalid {param} {value!r}: expected 'YYYY-MM-DD'.") from None
    return parsed.strftime("%Y-%m-%d")


def _fetch_sync(cache_key: str, ttl: int, call: Callable[[Any], Any]) -> Any:
    """Run ``call(garmin_data)`` for the authenticated user, with TTL caching.

    Garmin link problems are converted into actionable tool errors; imports are
    lazy so this module stays importable without the Garmin service.
    """
    from ..db import db_session
    from ..garmin import service as garmin

    user_id = current_user_id()

    def produce() -> Any:
        with db_session() as db:
            return call(garmin.get_client_for_user(db, user_id))

    try:
        return garmin.cached(user_id, cache_key, ttl, produce)
    except garmin.GarminNotLinked:
        raise ToolError(_NOT_LINKED_MSG) from None
    except garmin.GarminAuthRequired:
        raise ToolError(_REAUTH_MSG) from None
    except ValueError as exc:
        # The Garmin library validates inputs with ValueError (reversed date
        # ranges, out-of-range limits, non-numeric ids). Surface them as
        # actionable tool errors instead of opaque failures.
        raise ToolError(f"Invalid arguments: {exc}") from None


async def _fetch(cache_key: str, ttl: int, call: Callable[[Any], Any]) -> Any:
    """Async wrapper: run the blocking Garmin fetch in a worker thread.

    The authenticated user's ContextVar propagates into the thread via
    anyio's context copy.
    """
    return await to_thread.run_sync(lambda: _fetch_sync(cache_key, ttl, call))


def _fetch_strava_sync(cache_key: str, ttl: int, call: Callable[[Any], Any]) -> Any:
    """Strava twin of :func:`_fetch_sync` (same caching, Strava error mapping)."""
    from ..db import db_session
    from ..garmin.service import cached
    from ..strava import service as strava

    user_id = current_user_id()

    def produce() -> Any:
        with db_session() as db:
            return call(strava.get_client_for_user(db, user_id))

    try:
        return cached(user_id, cache_key, ttl, produce)
    except strava.StravaNotLinked:
        raise ToolError(_STRAVA_NOT_LINKED_MSG) from None
    except strava.StravaAuthRequired:
        raise ToolError(_STRAVA_REAUTH_MSG) from None
    except strava.StravaNotConfigured as exc:
        raise ToolError(str(exc)) from None
    except ValueError as exc:
        raise ToolError(f"Invalid arguments: {exc}") from None


async def _fetch_strava(cache_key: str, ttl: int, call: Callable[[Any], Any]) -> Any:
    return await to_thread.run_sync(lambda: _fetch_strava_sync(cache_key, ttl, call))


def _date_to_epoch(value: str | None, param: str, end_of_day: bool = False) -> int | None:
    """Convert a YYYY-MM-DD date to a UTC epoch bound for Strava's after/before."""
    if value is None:
        return None
    day = _norm_date(value, param)
    from datetime import timezone

    parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    epoch = int(parsed.timestamp())
    return epoch + 86400 if end_of_day else epoch


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@mcp.tool()
async def get_athlete_profile() -> dict[str, Any]:
    """Get the athlete's Garmin profile: name, gender, age, height, weight,
    unit system (metric/statute), VO2 max and key fitness settings. Call this
    first to personalize advice and to know which units the athlete uses."""
    return await _fetch("profile", PROFILE_TTL, lambda g: g.get_profile())


@mcp.tool()
async def get_daily_summary(date: str | None = None) -> dict[str, Any]:
    """Get the wellness summary for one day: steps, calories, distance,
    intensity minutes, resting/min/max heart rate, stress and sleep totals.

    Args:
        date: Day to fetch as 'YYYY-MM-DD'. Defaults to today.
    """
    day = _norm_date(date)
    return await _fetch(f"daily_summary:{day}", DAILY_TTL, lambda g: g.get_daily_summary(day))


@mcp.tool()
async def list_activities(
    limit: int = 10,
    start_date: str | None = None,
    end_date: str | None = None,
    activity_type: str | None = None,
) -> list[dict[str, Any]]:
    """List recorded activities (runs, rides, swims, strength, ...), most
    recent first. Each entry includes the activity id (for
    get_activity_details), name, type, date, distance, duration, average heart
    rate and pace/speed.

    Args:
        limit: Maximum number of activities to return (default 10).
        start_date: Only activities on/after this 'YYYY-MM-DD' date.
        end_date: Only activities on/before this 'YYYY-MM-DD' date.
        activity_type: Optional Garmin activity type filter, e.g. 'running',
            'cycling', 'swimming'.
    """
    if limit < 1:
        raise ToolError("limit must be >= 1.")
    limit = min(limit, 200)
    start = _norm_date(start_date, "start_date") if start_date is not None else None
    end = _norm_date(end_date, "end_date") if end_date is not None else None
    return await _fetch(
        f"activities:{limit}:{start}:{end}:{activity_type}",
        ACTIVITY_TTL,
        lambda g: g.list_activities(limit, start, end, activity_type),
    )


@mcp.tool()
async def get_activity_details(activity_id: int | str) -> dict[str, Any]:
    """Get one activity in depth: laps/splits, heart-rate zones, pace, power,
    cadence, elevation and training effect.

    Args:
        activity_id: Activity id as returned by list_activities.
    """
    return await _fetch(f"activity:{activity_id}", ACTIVITY_TTL, lambda g: g.get_activity(activity_id))


@mcp.tool()
async def get_sleep(date: str | None = None) -> dict[str, Any]:
    """Get sleep for one night: total duration, sleep stages (deep, light,
    REM, awake), sleep score, overnight resting heart rate and restlessness.

    Args:
        date: Wake-up day as 'YYYY-MM-DD'. Defaults to today.
    """
    day = _norm_date(date)
    return await _fetch(f"sleep:{day}", DAILY_TTL, lambda g: g.get_sleep(day))


@mcp.tool()
async def get_hrv(date: str | None = None) -> dict[str, Any]:
    """Get overnight heart-rate variability (HRV) for one day: last-night
    average, 7-day average, baseline range and HRV status
    (balanced/unbalanced/low).

    Args:
        date: Day to fetch as 'YYYY-MM-DD'. Defaults to today.
    """
    day = _norm_date(date)
    return await _fetch(f"hrv:{day}", DAILY_TTL, lambda g: g.get_hrv(day))


@mcp.tool()
async def get_training_status(date: str | None = None) -> dict[str, Any]:
    """Get Garmin's training status for one day: productive/maintaining/
    detraining etc., acute and chronic training load, load balance and VO2 max
    trend.

    Args:
        date: Day to fetch as 'YYYY-MM-DD'. Defaults to today.
    """
    day = _norm_date(date)
    return await _fetch(f"training_status:{day}", DAILY_TTL, lambda g: g.get_training_status(day))


@mcp.tool()
async def get_training_readiness(date: str | None = None) -> dict[str, Any]:
    """Get the training readiness score (0-100) for one day plus its inputs:
    sleep, recovery time, HRV status, acute load, sleep history and stress
    history. Use this to decide how hard today's session should be.

    Args:
        date: Day to fetch as 'YYYY-MM-DD'. Defaults to today.
    """
    day = _norm_date(date)
    return await _fetch(f"training_readiness:{day}", DAILY_TTL, lambda g: g.get_training_readiness(day))


@mcp.tool()
async def get_body_battery(start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    """Get Body Battery (Garmin's 0-100 energy estimate) over a date range:
    charged/drained totals and a downsampled level curve per day.

    Args:
        start_date: Range start as 'YYYY-MM-DD'. Defaults to today.
        end_date: Range end as 'YYYY-MM-DD'. Defaults to start_date.
    """
    start = _norm_date(start_date, "start_date")
    end = _norm_date(end_date, "end_date") if end_date is not None else start
    return await _fetch(f"body_battery:{start}:{end}", DAILY_TTL, lambda g: g.get_body_battery(start, end))


@mcp.tool()
async def get_stress(date: str | None = None) -> dict[str, Any]:
    """Get stress for one day: average and max stress level (0-100) and time
    spent in rest/low/medium/high stress.

    Args:
        date: Day to fetch as 'YYYY-MM-DD'. Defaults to today.
    """
    day = _norm_date(date)
    return await _fetch(f"stress:{day}", DAILY_TTL, lambda g: g.get_stress(day))


@mcp.tool()
async def get_steps(start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    """Get daily step counts over a date range, including step goal and
    distance per day.

    Args:
        start_date: Range start as 'YYYY-MM-DD'. Defaults to today.
        end_date: Range end as 'YYYY-MM-DD'. Defaults to start_date.
    """
    start = _norm_date(start_date, "start_date")
    end = _norm_date(end_date, "end_date") if end_date is not None else start
    return await _fetch(f"steps:{start}:{end}", DAILY_TTL, lambda g: g.get_steps(start, end))


@mcp.tool()
async def get_heart_rate(date: str | None = None) -> dict[str, Any]:
    """Get heart-rate data for one day: resting, min and max heart rate plus a
    downsampled intraday curve.

    Args:
        date: Day to fetch as 'YYYY-MM-DD'. Defaults to today.
    """
    day = _norm_date(date)
    return await _fetch(f"heart_rate:{day}", DAILY_TTL, lambda g: g.get_heart_rate(day))


@mcp.tool()
async def get_race_predictions() -> dict[str, Any]:
    """Get Garmin's current race time predictions for 5K, 10K, half marathon
    and marathon, based on the athlete's fitness level."""
    return await _fetch("race_predictions", PROFILE_TTL, lambda g: g.get_race_predictions())


@mcp.tool()
async def get_body_composition(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    """Get body composition measurements over a date range: weight, BMI, body
    fat percentage, muscle mass and body water (requires a connected scale or
    manual entries).

    Args:
        start_date: Range start as 'YYYY-MM-DD'. Defaults to today.
        end_date: Range end as 'YYYY-MM-DD'. Defaults to start_date.
    """
    start = _norm_date(start_date, "start_date")
    end = _norm_date(end_date, "end_date") if end_date is not None else start
    return await _fetch(
        f"body_composition:{start}:{end}", DAILY_TTL, lambda g: g.get_body_composition(start, end)
    )


@mcp.tool()
async def strava_get_athlete() -> dict[str, Any]:
    """Get the athlete's Strava profile: name, location, sex, weight, FTP and
    account details. Uses the Strava account linked on the dashboard."""
    return await _fetch_strava("strava:athlete", PROFILE_TTL, lambda s: s.get_athlete())


@mcp.tool()
async def strava_get_athlete_stats() -> dict[str, Any]:
    """Get the athlete's Strava totals: recent (last 4 weeks), year-to-date and
    all-time ride/run/swim counts, distance, moving time and elevation gain,
    plus biggest ride and climb."""
    return await _fetch_strava(
        "strava:stats", DAILY_TTL, lambda s: s.get_athlete_stats()
    )


@mcp.tool()
async def strava_list_activities(
    limit: int = 10,
    after_date: str | None = None,
    before_date: str | None = None,
) -> list[dict[str, Any]]:
    """List the athlete's Strava activities, most recent first: name, sport,
    date, distance, times, elevation, heart rate, power and cadence. Use
    strava_get_activity for splits and best efforts.

    Args:
        limit: Maximum number of activities to return (default 10, max 200).
        after_date: Only activities on/after this 'YYYY-MM-DD' date (UTC).
        before_date: Only activities on/before this 'YYYY-MM-DD' date (UTC).
    """
    if limit < 1:
        raise ToolError("limit must be >= 1.")
    after = _date_to_epoch(after_date, "after_date")
    before = _date_to_epoch(before_date, "before_date", end_of_day=True)
    return await _fetch_strava(
        f"strava:activities:{limit}:{after}:{before}",
        ACTIVITY_TTL,
        lambda s: s.list_activities(limit, after, before),
    )


@mcp.tool()
async def strava_get_activity(activity_id: int | str) -> dict[str, Any]:
    """Get one Strava activity in depth: description, calories, device,
    per-kilometre splits and best efforts.

    Args:
        activity_id: Activity id as returned by strava_list_activities.
    """
    return await _fetch_strava(
        f"strava:activity:{activity_id}",
        ACTIVITY_TTL,
        lambda s: s.get_activity(activity_id),
    )


__all__ = [
    "mcp",
    "TokenAuthMiddleware",
    "build_mcp_asgi_app",
    "mcp_lifespan",
    "current_user_id",
]
