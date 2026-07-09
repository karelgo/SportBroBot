"""Connect your REAL Strava account and pull your data — all on your machine.

This runs the actual SportBroBot Strava OAuth flow against strava.com using a
temporary localhost callback, so you can confirm real data extraction without
deploying anything.

One-time setup (2 minutes):
  1. Go to https://www.strava.com/settings/api and create an application.
       • "Authorization Callback Domain" must be exactly:  localhost
  2. Copy the Client ID and Client Secret it shows you.

Then run, from the repo root:

    SPORTBRO_STRAVA_CLIENT_ID=xxxxx \\
    SPORTBRO_STRAVA_CLIENT_SECRET=yyyyy \\
    .venv/bin/python scripts/verify_strava_login.py

Your browser opens to Strava's sign-in page; approve read-only access and the
script prints your profile, lifetime stats and most recent activities. The
Client Secret is read only from the environment and never written anywhere.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import threading
import urllib.parse
import webbrowser

sys.path.insert(0, ".")

from sportbrobot.strava import service  # noqa: E402

CALLBACK_PORT = 8721
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"

_result: dict[str, str] = {}
_done = threading.Event()

_PAGE = (
    "<html><body style='font-family:system-ui;background:#0b0f14;color:#e5e7eb;"
    "text-align:center;padding-top:18vh'><h2 style='color:#a3e635'>{title}</h2>"
    "<p>{msg}</p><p style='color:#64748b'>You can close this tab and return to "
    "the terminal.</p></body></html>"
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = urllib.parse.parse_qs(parsed.query)
        _result["code"] = (params.get("code") or [""])[0]
        _result["scope"] = (params.get("scope") or [""])[0]
        _result["error"] = (params.get("error") or [""])[0]
        ok = bool(_result["code"]) and not _result["error"]
        body = _PAGE.format(
            title="Strava connected ✓" if ok else "Strava sign-in failed",
            msg="SportBroBot received read-only access."
            if ok
            else f"Strava returned: {_result['error'] or 'no authorization code'}",
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        _done.set()

    def log_message(self, *args: object) -> None:  # silence the default logging
        pass


def main() -> int:
    if not (
        os.environ.get("SPORTBRO_STRAVA_CLIENT_ID")
        and os.environ.get("SPORTBRO_STRAVA_CLIENT_SECRET")
    ):
        print(
            "✗ Set SPORTBRO_STRAVA_CLIENT_ID and SPORTBRO_STRAVA_CLIENT_SECRET first.\n"
            "  Create an app at https://www.strava.com/settings/api with callback "
            "domain 'localhost'."
        )
        return 1

    state = "sportbrobot-local-verify"
    authorize_url = service.build_authorize_url(REDIRECT_URI, state)

    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print(f"\n→ Opening Strava sign-in in your browser ...\n  {authorize_url}\n")
    print(
        "  If the browser didn't open, paste that URL into it manually.\n"
        "  (Make sure your Strava app's callback domain is 'localhost'.)\n"
    )
    try:
        webbrowser.open(authorize_url)
    except Exception:  # noqa: BLE001 - headless box; user pastes the URL
        pass

    if not _done.wait(timeout=300):
        print("✗ Timed out waiting for Strava (5 min). Try again.")
        return 1
    server.shutdown()

    if _result.get("error") or not _result.get("code"):
        print(f"✗ Strava sign-in was not completed: {_result.get('error') or 'no code'}")
        return 1

    print("→ Exchanging the authorization code for tokens ...")
    try:
        tokens = service.exchange_code(_result["code"])
    except service.StravaAuthRequired as exc:
        print(f"✗ Token exchange failed: {exc}")
        return 1

    athlete = tokens.get("athlete") or {}
    name = " ".join(
        p for p in (athlete.get("firstname"), athlete.get("lastname")) if p
    ) or f"athlete #{athlete.get('id')}"
    print(f"✓ Connected as {name} (scope: {_result.get('scope')})\n")

    data = service.StravaData(
        access_token=tokens["access_token"], athlete_id=int(athlete.get("id") or 0)
    )
    checks = [
        ("Profile", data.get_athlete),
        ("Lifetime / YTD stats", data.get_athlete_stats),
        ("Recent activities", lambda: data.list_activities(limit=5)),
    ]
    for label, fetch in checks:
        try:
            payload = fetch()
            print(f"✓ {label}:")
            print(json.dumps(payload, indent=2, default=str)[:1500], "\n")
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"✗ {label} failed: {exc}\n")

    print(
        "Real Strava data pulled through SportBroBot's own code paths. Run the web\n"
        "app with the same two env vars and 'Connect with Strava' does exactly this."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
