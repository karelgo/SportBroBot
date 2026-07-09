"""Verify end to end that a real Garmin Connect account works with SportBroBot.

Run this on YOUR machine (Garmin blocks datacenter IPs), from the repo root:

    .venv/bin/python scripts/verify_garmin_login.py

It logs in with your credentials (MFA supported), then pulls your profile,
today's summary, last night's sleep and your most recent activities — the
same code paths the web app and MCP tools use. Nothing is stored unless you
pass --save-tokens PATH.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from datetime import date

sys.path.insert(0, ".")

from sportbrobot.garmin import service  # noqa: E402
from garminconnect import (  # noqa: E402
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="Garmin Connect email (prompted if omitted)")
    parser.add_argument(
        "--save-tokens", metavar="PATH", help="Write the reusable token bundle here"
    )
    args = parser.parse_args()

    email = args.email or input("Garmin email: ").strip()
    password = getpass.getpass("Garmin password (never stored): ")

    print("\n→ Logging in to Garmin Connect ...")
    try:
        result = service.start_garmin_login(email, password)
        if isinstance(result, service.MfaPending):
            code = input("→ Garmin sent you a code. Enter it: ").strip()
            result = service.complete_garmin_mfa(result.pending_id, code)
    except GarminConnectAuthenticationError as exc:
        print(f"✗ Authentication failed: {exc}")
        return 1
    except GarminConnectTooManyRequestsError as exc:
        print(f"✗ Rate limited: {exc}")
        return 1
    except GarminConnectConnectionError as exc:
        print(f"✗ Connection problem: {exc}")
        print("  (Corporate/VPN/datacenter networks are often blocked by Garmin —")
        print("   try from a residential connection.)")
        return 1

    print(f"✓ Logged in as {result.display_name} ({result.full_name or 'no name'})")

    data = service.GarminData(
        token_blob=result.token_blob,
        display_name=result.display_name,
        full_name=result.full_name,
        unit_system=result.unit_system,
    )

    today = date.today().isoformat()
    checks = [
        ("Profile", lambda: data.get_profile()),
        ("Daily summary", lambda: data.get_daily_summary(today)),
        ("Sleep (last night)", lambda: data.get_sleep(today)),
        ("Recent activities", lambda: data.list_activities(limit=3)),
        ("Training readiness", lambda: data.get_training_readiness(today)),
    ]
    failures = 0
    for label, fetch in checks:
        try:
            payload = fetch()
            print(f"\n✓ {label}:")
            print(json.dumps(payload, indent=2, default=str)[:1200])
        except Exception as exc:  # noqa: BLE001 - report and continue
            failures += 1
            print(f"\n✗ {label} failed: {exc}")

    if args.save_tokens:
        with open(args.save_tokens, "w") as handle:
            handle.write(result.token_blob)
        print(f"\nToken bundle written to {args.save_tokens} — treat it like a password.")

    print(
        "\nAll good — connect this account on the SportBroBot dashboard."
        if failures == 0
        else f"\n{failures} fetch(es) failed — the login itself worked."
    )
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
