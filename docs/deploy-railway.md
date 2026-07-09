# Deploy SportBroBot to Railway

Railway gives SportBroBot a public HTTPS URL, so your **MCP link works in
Claude Desktop / claude.ai from anywhere** and **Strava can redirect back to a
real domain**. The repo ships a `Dockerfile` and `railway.json`, so Railway
builds and runs it with no extra setup.

## 1. Create the service

Option A — dashboard:
1. [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub
   repo** → pick `karelgo/SportBroBot`, branch `claude/athlete-data-garmin-mcp-cor6ms`.
2. Railway detects the `Dockerfile` and builds automatically.

Option B — CLI:
```bash
npm i -g @railway/cli
railway login
railway init            # in the repo root
railway up
```

## 2. Add a persistent volume (important)

SportBroBot stores its SQLite database **and** its encryption keys under
`/data`. Without a volume, every redeploy wipes them — users would have to sign
up and reconnect again, and previously encrypted tokens become unreadable.

- Service → **Variables/Settings → Volumes → New Volume**
- Mount path: **`/data`**  (the image already sets `SPORTBRO_DATA_DIR=/data`)

## 3. Generate a public domain

Service → **Settings → Networking → Generate Domain**. You'll get something
like `sportbrobot-production.up.railway.app`.

You do **not** need to set `SPORTBRO_BASE_URL` — SportBroBot reads Railway's
`RAILWAY_PUBLIC_DOMAIN` automatically. (Set `SPORTBRO_BASE_URL` explicitly only
if you attach your own custom domain.)

## 4. Set environment variables

Service → **Variables**:

| Variable | Value | Why |
|---|---|---|
| `SPORTBRO_SECRET_KEY` | *(generate, see below)* | Stable session-cookie signing across redeploys |
| `SPORTBRO_FERNET_KEY` | *(generate, see below)* | Stable encryption key for Garmin/Strava tokens |
| `SPORTBRO_STRAVA_CLIENT_ID` | from strava.com/settings/api | Enables "Connect with Strava" |
| `SPORTBRO_STRAVA_CLIENT_SECRET` | from strava.com/settings/api | " |

Setting the two keys explicitly is belt-and-suspenders: even if the volume is
ever recreated, sessions and encrypted tokens still decrypt. Generate them
locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"                  # SPORTBRO_SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # SPORTBRO_FERNET_KEY
```

Keep both secret; rotating `SPORTBRO_FERNET_KEY` invalidates every stored
Garmin/Strava link (users must reconnect).

## 5. Point Strava at your Railway domain

In your [Strava API application](https://www.strava.com/settings/api), set
**Authorization Callback Domain** to your Railway host **without the scheme**,
e.g. `sportbrobot-production.up.railway.app`. Then on the SportBroBot
dashboard, **Connect with Strava** completes against your real domain.

## 6. Verify

- `https://<your-domain>/healthz` → `{"status": "ok"}`
- Sign up, open the dashboard, connect Strava (and/or Garmin).
- Copy your MCP link and add it in Claude — see `/mcp/setup`.

## Notes

- **Garmin** still uses credential login (no public API). It generally works
  from Railway, but Garmin occasionally rate-limits cloud IPs; if a connect
  attempt fails with a 429, wait and retry. The official partner route is in
  [garmin-partner-access.md](garmin-partner-access.md).
- **Cost**: SportBroBot idles comfortably within Railway's small instances; a
  volume of 1 GB is far more than the SQLite DB needs.
- **Redeploys**: with the volume + explicit keys above, users stay connected
  across every deploy.
