# SportBroBot — Architecture

SportBroBot is a self-hostable web application modeled on athletedata.health:
athletes connect their **Garmin Connect** account and get a **personal MCP
server link** they can paste into Claude, ChatGPT, Cursor or any MCP client to
chat with their own training data.

## Stack

- Python 3.11, FastAPI + Uvicorn, Jinja2 templates, SQLite via SQLAlchemy 2.x
- `garminconnect` (0.3.x, bundled auth client) for Garmin Connect
- `mcp` Python SDK (FastMCP, streamable HTTP transport) for the MCP server
- `cryptography` (Fernet) for token encryption at rest, `itsdangerous` for
  signed session cookies

## Layout

```
sportbrobot/
├── config.py       # Settings loaded from SPORTBRO_* env vars (lazy, cached)
├── db.py           # lazy engine/sessionmaker, Base, init_db(), db_session()
├── models.py       # User, GarminLink, McpToken
├── security.py     # scrypt password hashing, Fernet encrypt, MCP tokens
├── main.py         # create_app(): routers first, /mcp mount LAST
├── garmin/
│   └── service.py  # Garmin login/MFA/token persistence + data fetchers
├── mcp_server/
│   └── server.py   # FastMCP tools + token-auth ASGI wrapper
├── web/
│   ├── deps.py     # templates env, get_db, current-user helpers
│   ├── auth.py     # /signup /login /logout
│   ├── dashboard.py# /dashboard, /garmin/*, /mcp/rotate
│   └── landing.py  # /, /mcp/setup, /mcp/setup/{client}
├── templates/      # base, landing, login, signup, dashboard, mfa, setup
└── static/         # style.css (self-contained, no CDNs)
```

## Auth model

- **App accounts**: email + password (stdlib `hashlib.scrypt`). Session is a
  signed cookie `sbb_session` (itsdangerous `URLSafeTimedSerializer`,
  payload `{"uid": <user id>}`, 30-day max age).
- **Garmin link**: user submits Garmin credentials once; we log in with
  `garminconnect` and store only the serialized OAuth token bundle
  (`client.dumps()` JSON), Fernet-encrypted, in `garmin_links.token_blob`.
  Credentials are never persisted. MFA is supported via a two-step flow —
  the pending `Garmin` object is held in an in-process store between the
  password step and the code step (`resume_login` state lives on the object).
- **MCP tokens**: `sbb_` + `secrets.token_urlsafe(32)`. DB stores the SHA-256
  hash (lookup) plus a Fernet-encrypted copy (so the dashboard can re-display
  the URL). One token per user; rotation replaces it.

## MCP server link

The personal URL shown on the dashboard is `{BASE_URL}/mcp?apiKey={token}`.
The `/mcp` endpoint (streamable HTTP, stateless, JSON responses) accepts the
token via, in order:

1. `?apiKey=` query parameter (works with claude.ai custom connectors and
   `npx mcp-remote`)
2. `Authorization: Bearer <token>` header (Claude Code, Cursor)
3. First path segment: `/mcp/{token}` (fallback)

`mcp_server.server.build_mcp_asgi_app()` returns the FastMCP streamable-http
ASGI app wrapped in `TokenAuthMiddleware`, which resolves the token to a user,
stashes the user id in a `ContextVar`, updates `mcp_tokens.last_used_at`, and
rejects unauthenticated requests with 401. Tools read the ContextVar and load
data through `garmin.service`. `main.py` runs the FastMCP session manager
inside the app lifespan.

Route-order invariant: `web` routers (which own `GET /mcp/setup...`) are
included **before** `app.mount("/mcp", ...)` so the specific GET routes win;
the mount handles everything else under `/mcp`.

## Garmin integration facts (garminconnect 0.3.2)

- `Garmin(email, password, return_on_mfa=True).login()` returns
  `("needs_mfa", None)` when MFA is required; MFA state is held on the
  instance — complete with `garmin.resume_login({}, code)`.
- Token bundle: `garmin.client.dumps()` → JSON string; restore with
  `garmin.client.loads(blob)`. After restoring you must set
  `garmin.display_name` (several endpoints embed it in the URL path).
- The client auto-refreshes expired tokens (proactively and on 401) inside
  `_run_request` — tokens can rotate on any call, so re-`dumps()` after use
  and persist when changed (`GarminData` invokes an `on_tokens_rotated`
  callback; `get_client_for_user` wires it to the DB row).
- Errors: `GarminConnectAuthenticationError`, `GarminConnectTooManyRequestsError`,
  `GarminConnectConnectionError` from `garminconnect`.

`garmin/service.py` public surface:

```python
class GarminNotLinked(Exception): ...
class GarminAuthRequired(Exception): ...   # link broken; user must reconnect

@dataclass
class LoginSuccess: token_blob: str; display_name: str | None; full_name: str | None; unit_system: str | None; garmin_email: str
@dataclass
class MfaPending: pending_id: str

def start_garmin_login(email, password) -> LoginSuccess | MfaPending
def complete_garmin_mfa(pending_id, code) -> LoginSuccess       # KeyError if expired
def get_client_for_user(db, user_id) -> GarminData              # raises the two exceptions above

class GarminData:   # all methods return trimmed, JSON-safe dicts/lists
    get_profile(); get_daily_summary(date); list_activities(limit, start_date, end_date, activity_type)
    get_activity(activity_id); get_sleep(date); get_hrv(date); get_training_status(date)
    get_training_readiness(date); get_body_battery(start_date, end_date); get_stress(date)
    get_steps(start_date, end_date); get_heart_rate(date); get_race_predictions()
    get_body_composition(start_date, end_date)

def cached(user_id, key, ttl, producer)   # in-process TTL cache (thread-safe)
def invalidate_user_cache(user_id)
```

Trimming principle: responses are shaped down to the fields an AI coach needs;
long minute-by-minute series are dropped or downsampled (≤ 50 points).

## MCP tools

All read-only `async def`s that offload blocking I/O to a worker thread
(FastMCP 1.x runs sync tools on the event loop), operating on the
authenticated user from the ContextVar. Dates are `YYYY-MM-DD` strings
defaulting to today (server date).

Garmin: `get_athlete_profile`, `get_daily_summary`, `list_activities`,
`get_activity_details`, `get_sleep`, `get_hrv`, `get_training_status`,
`get_training_readiness`, `get_body_battery`, `get_stress`, `get_steps`,
`get_heart_rate`, `get_race_predictions`, `get_body_composition`.

Strava: `strava_get_athlete`, `strava_get_athlete_stats`,
`strava_list_activities`, `strava_get_activity`.

## Strava integration

`strava/service.py` implements the self-serve Strava OAuth API: the operator
creates an API app (strava.com/settings/api) and sets
`SPORTBRO_STRAVA_CLIENT_ID/SECRET`; users authorize at Strava's own sign-in
page (`web/strava.py`: `/strava/connect` → strava.com → `/strava/callback`
with a signed `state`). Access/refresh tokens live Fernet-encrypted on
`strava_links`; access tokens refresh automatically near expiry and rotated
refresh tokens are persisted. Scope: `read,activity:read_all,profile:read_all`.

## Web routes

| Route | Method | Notes |
|---|---|---|
| `/` | GET | Landing page |
| `/signup`, `/login` | GET+POST | Forms: `email`, `password` (signup also `password_confirm`) |
| `/logout` | POST | Clears cookie |
| `/dashboard` | GET | Requires login; Garmin state, MCP URL, quick stats |
| `/garmin/connect` | POST | Form: `garmin_email`, `garmin_password` → success or MFA page |
| `/garmin/mfa` | POST | Form: `pending_id`, `mfa_code` |
| `/garmin/disconnect` | POST | Deletes link, invalidates cache |
| `/mcp/rotate` | POST | Regenerates MCP token |
| `/mcp/setup` | GET | Hub linking the per-client guides |
| `/mcp/setup/{client}` | GET | claude-desktop, claude-code, cursor, chatgpt |
| `/healthz` | GET | `{"status": "ok"}` |

Setup pages personalize with the logged-in user's real MCP URL (copy button);
logged-out visitors see a `YOUR_API_KEY` placeholder.

## Templates contract

All pages extend `base.html` (nav shows Login/Sign up or Dashboard/Logout via
`user` in context). Key contexts:

- `landing.html`: `user`
- `login.html` / `signup.html`: `user`, `error` (optional str)
- `dashboard.html`: `user`, `link` (GarminLink | None), `mcp_url` (str),
  `stats` (dict | None: `steps_today`, `resting_hr`, `sleep_hours`,
  `body_battery`, `last_activity` {name,type,date,distance_km,duration_min}),
  `stats_error` (str | None), `flash` (str | None)
- `garmin_mfa.html`: `user`, `pending_id`, `error` (optional)
- `mcp_setup.html`: `user`, `mcp_url` (str | None), `clients` (list)
- `mcp_setup_client.html`: `user`, `client` (slug), `client_name`,
  `mcp_url` (str | None)

## Testing

`pytest` with mocked Garmin clients (no live Garmin calls). The MCP suite
boots the real app under uvicorn on a free port and exercises the actual
streamable-HTTP handshake with the `mcp` client SDK.
