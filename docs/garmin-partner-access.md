# Official Garmin API access (Connect Developer Program)

SportBroBot works **today without any Garmin partnership**: it signs in with
the user's Garmin credentials against the same SSO endpoints the Garmin
Connect apps use (via the `garminconnect` library). The trade-offs of that
approach: users type their Garmin password into your app, Garmin's terms
don't formally cover it, and logins from datacenter IPs are often blocked by
Garmin's bot protection.

The official **Garmin Connect Developer Program** replaces that with the real
"sign in at Garmin" OAuth flow (the `sso.garmin.com` page) plus push
webhooks — but it is application-gated:

- Apply via the [access request form](https://www.garmin.com/en-US/forms/GarminConnectDeveloperAccess/)
  ([program overview](https://developer.garmin.com/gc-developer-program/),
  [FAQ](https://developer.garmin.com/gc-developer-program/program-faq/)).
- The [Health API](https://developer.garmin.com/gc-developer-program/health-api/)
  (sleep, HRV, stress, Body Battery…) and
  [Activity API](https://developer.garmin.com/gc-developer-program/activity-api/)
  are granted per use case. Evaluation access is free; **production Health
  API access carries a one-time $5,000 administrative fee**.
- Approval requires company details and acceptance of Garmin's agreements,
  so the application must be submitted by the business owner.

## Ready-to-paste application draft

Submitted-by: Karel Goense — karelgoense@freshminds.nl (Freshminds)

> **Application/product name:** SportBroBot
>
> **Description:** SportBroBot is a self-hosted companion app that lets an
> athlete connect their own Garmin account and query their training data
> (sleep, HRV, activities, training readiness) through AI assistants via the
> Model Context Protocol (MCP). Access is strictly read-only, per-athlete,
> and initiated by the athlete themselves.
>
> **APIs requested:** Health API and Activity API (pull + ping/webhooks).
>
> **Data handling:** OAuth tokens encrypted at rest (Fernet); no data resold
> or used for advertising or model training; athletes can disconnect and
> revoke at any time from the dashboard; data is stored only on the
> athlete's own self-hosted instance.
>
> **Expected volume:** Small — personal/team use, tens of athletes initially.

(Replace the volume line with your real numbers before submitting.)

## What changes in SportBroBot once approved

1. Swap the credential form for a "Sign in with Garmin" redirect
   (OAuth 1.0a/2.0 per Garmin's partner docs) — the code is already
   structured for this: only `garmin/service.py`'s login functions change,
   the data layer and MCP tools stay as they are (or move to the official
   REST endpoints gradually).
2. Register the callback URL (`{BASE_URL}/garmin/callback`).
3. Receive data by webhook instead of polling, removing the rate-limit risk.
