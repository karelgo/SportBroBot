"""Garmin Connect service layer: login/MFA, token persistence, trimmed fetchers.

Everything that talks to Garmin Connect lives here, shared by the web layer
and the MCP server (this module never imports FastAPI):

- the two-step login flow (password, then optional MFA code); the live
  ``Garmin`` object is parked in an in-process pending store between steps
  because ``resume_login`` state lives on the instance,
- restoring authenticated clients from stored token blobs and persisting
  rotated tokens back to the ``garmin_links`` row,
- data fetchers trimmed to what an AI coach needs (long minute-by-minute
  series are downsampled to at most 50 points),
- a small thread-safe TTL cache callers can opt into via :func:`cached`.
"""

from __future__ import annotations

import datetime
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import security
from ..db import db_session
from ..models import GarminLink

_MAX_SERIES_POINTS = 50
_PENDING_TTL_SECONDS = 600.0


class GarminNotLinked(Exception):
    """The user has no Garmin account linked."""


class GarminAuthRequired(Exception):
    """The stored Garmin link is broken; the user must reconnect."""


@dataclass
class LoginSuccess:
    token_blob: str
    display_name: str | None
    full_name: str | None
    unit_system: str | None
    garmin_email: str


@dataclass
class MfaPending:
    pending_id: str


@contextmanager
def _clean_garmin_errors() -> Iterator[None]:
    """Re-raise garminconnect errors as the same types with clean messages.

    Callers catch these exception types; the replacement messages avoid
    leaking upstream stack traces or raw server responses to end users.
    """
    try:
        yield
    except GarminConnectAuthenticationError as exc:
        raise GarminConnectAuthenticationError(
            "Garmin rejected the credentials. Check your email and password "
            "(and that the account is not locked) and try again."
        ) from exc
    except GarminConnectTooManyRequestsError as exc:
        raise GarminConnectTooManyRequestsError(
            "Garmin is rate-limiting login attempts. Wait a few minutes and try again."
        ) from exc
    except GarminConnectConnectionError as exc:
        raise GarminConnectConnectionError(
            "Could not reach Garmin Connect. Try again shortly."
        ) from exc


@dataclass
class _PendingLogin:
    garmin: Any
    garmin_email: str
    expires_at: float


_pending_lock = threading.Lock()
_pending_logins: dict[str, _PendingLogin] = {}


def _purge_expired_pending_locked() -> None:
    now = time.monotonic()
    for pending_id in [
        pid for pid, entry in _pending_logins.items() if entry.expires_at <= now
    ]:
        del _pending_logins[pending_id]


def _load_identity(garmin: Any) -> None:
    """Populate display_name/full_name/unit_system after a raw login.

    ``Garmin.login(return_on_mfa=True)`` returns before the library's own
    profile fetch even when no MFA challenge occurs, leaving display_name
    None — which would permanently break every endpoint that embeds it in
    the URL path. Mirrors the library's post-login fetches.
    """
    if garmin.display_name is None:
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                profile = garmin.client.connectapi("/userprofile-service/socialProfile")
            except Exception as exc:  # noqa: BLE001 - retried, then surfaced clean
                last_exc = exc
                continue
            if isinstance(profile, dict) and profile.get("displayName"):
                garmin.display_name = profile.get("displayName")
                garmin.full_name = profile.get("fullName", "")
                break
        else:
            raise GarminConnectConnectionError(
                "Garmin sign-in succeeded but the profile could not be loaded. "
                "Try connecting again."
            ) from last_exc
    if garmin.unit_system is None:
        try:
            settings = garmin.client.connectapi(
                "/userprofile-service/userprofile/user-settings"
            )
            garmin.unit_system = _dig(settings, "userData", "measurementSystem")
        except Exception:  # noqa: BLE001 - unit system is a nice-to-have
            pass


def _build_login_success(garmin: Any, garmin_email: str) -> LoginSuccess:
    token_blob = garmin.client.dumps()
    # The bundled client can fall back to cookie-only auth whose session
    # cannot be serialized: dumps() then holds only null DI tokens and every
    # later restore would fail. Refuse to store such a link.
    try:
        parsed = json.loads(token_blob)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and not parsed.get("di_token"):
        raise GarminConnectConnectionError(
            "Garmin sign-in used a temporary fallback session that cannot be "
            "saved. Wait a few minutes and try connecting again."
        )
    return LoginSuccess(
        token_blob=token_blob,
        display_name=garmin.display_name,
        full_name=garmin.full_name,
        unit_system=garmin.unit_system,
        garmin_email=garmin_email,
    )


def start_garmin_login(email: str, password: str) -> LoginSuccess | MfaPending:
    """Start the Garmin credential login flow.

    Returns :class:`MfaPending` when Garmin asks for an MFA code; the live
    client is parked in the pending store (10-minute expiry) so
    :func:`complete_garmin_mfa` can resume the very same instance — the MFA
    state lives on the object itself.
    """
    # Garmin.login() silently falls back to the GARMINTOKENS env var as a
    # tokenstore; on a host where it points at the operator's tokens, every
    # user would "link" the operator's account without a credential check.
    os.environ.pop("GARMINTOKENS", None)
    garmin = Garmin(email=email, password=password, return_on_mfa=True)
    with _clean_garmin_errors():
        status, _ = garmin.login()
    if status == "needs_mfa":
        pending_id = secrets.token_urlsafe(24)
        with _pending_lock:
            _purge_expired_pending_locked()
            _pending_logins[pending_id] = _PendingLogin(
                garmin=garmin,
                garmin_email=email,
                expires_at=time.monotonic() + _PENDING_TTL_SECONDS,
            )
        return MfaPending(pending_id=pending_id)
    with _clean_garmin_errors():
        _load_identity(garmin)
        return _build_login_success(garmin, email)


def complete_garmin_mfa(pending_id: str, code: str) -> LoginSuccess:
    """Finish a pending MFA login. Raises KeyError if unknown or expired.

    The pending entry is only consumed on success: after a wrong code the
    user can retry with a fresh code, and after a transient connection error
    the same code can be resubmitted.
    """
    with _pending_lock:
        _purge_expired_pending_locked()
        entry = _pending_logins[pending_id]
    try:
        entry.garmin.resume_login({}, code)
    except GarminConnectAuthenticationError as exc:
        raise GarminConnectAuthenticationError(
            "Garmin did not accept that code. Check it and try again."
        ) from exc
    except GarminConnectTooManyRequestsError as exc:
        raise GarminConnectTooManyRequestsError(
            "Garmin is rate-limiting login attempts. Wait a few minutes and try again."
        ) from exc
    except GarminConnectConnectionError as exc:
        raise GarminConnectConnectionError(
            "Could not reach Garmin Connect. Try again shortly."
        ) from exc
    with _pending_lock:
        _pending_logins.pop(pending_id, None)
    with _clean_garmin_errors():
        _load_identity(entry.garmin)
        return _build_login_success(entry.garmin, entry.garmin_email)


def get_client_for_user(db: Session, user_id: int) -> GarminData:
    """Restore an authenticated :class:`GarminData` for the user's link.

    Raises :class:`GarminNotLinked` when no link exists and
    :class:`GarminAuthRequired` when the link is flagged for re-auth or the
    stored token blob cannot be decrypted/restored. Rotated tokens are
    re-encrypted and persisted back to the link row.
    """
    link = db.execute(
        select(GarminLink).where(GarminLink.user_id == user_id)
    ).scalar_one_or_none()
    if link is None:
        raise GarminNotLinked("No Garmin account is linked to this user.")
    if link.status == "reauth_required":
        raise GarminAuthRequired("The Garmin connection needs to be re-linked.")
    try:
        token_blob = security.decrypt_text(link.token_blob)
    except security.InvalidToken as exc:
        raise GarminAuthRequired(
            "Stored Garmin tokens could not be decrypted; please reconnect."
        ) from exc

    link_id = link.id

    def _persist_rotated(new_blob: str) -> None:
        # A fresh short-lived session keeps the callback safe from MCP
        # threadpool threads, long after the caller's session has closed.
        with db_session() as session:
            row = session.get(GarminLink, link_id)
            if row is not None:
                row.token_blob = security.encrypt_text(new_blob)

    return GarminData(
        token_blob=token_blob,
        display_name=link.display_name,
        full_name=link.full_name,
        unit_system=link.unit_system,
        on_tokens_rotated=_persist_rotated,
    )


def _today() -> str:
    return datetime.date.today().isoformat()


def _pick(data: Any, *keys: str) -> dict[str, Any]:
    """Copy the named keys from a dict, skipping missing/None; {} otherwise."""
    if not isinstance(data, dict):
        return {}
    return {key: data[key] for key in keys if data.get(key) is not None}


def _dig(data: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _first_value(mapping: Any) -> dict[str, Any]:
    """First dict value of a device-keyed map (Garmin nests per-device data)."""
    if isinstance(mapping, dict):
        for value in mapping.values():
            if isinstance(value, dict):
                return value
    return {}


def _downsample(series: Any, max_points: int = _MAX_SERIES_POINTS) -> list[Any]:
    """Evenly downsample a list to at most max_points, keeping first and last."""
    if not isinstance(series, list):
        return []
    if len(series) <= max_points:
        return series
    step = (len(series) - 1) / (max_points - 1)
    return [series[round(i * step)] for i in range(max_points)]


_DAILY_SUMMARY_FIELDS = (
    "calendarDate",
    "totalKilocalories",
    "activeKilocalories",
    "bmrKilocalories",
    "totalSteps",
    "dailyStepGoal",
    "totalDistanceMeters",
    "highlyActiveSeconds",
    "activeSeconds",
    "sedentarySeconds",
    "sleepingSeconds",
    "moderateIntensityMinutes",
    "vigorousIntensityMinutes",
    "intensityMinutesGoal",
    "floorsAscended",
    "floorsDescended",
    "minHeartRate",
    "maxHeartRate",
    "restingHeartRate",
    "lastSevenDaysAvgRestingHeartRate",
    "averageStressLevel",
    "maxStressLevel",
    "stressDuration",
    "restStressDuration",
    "activityStressDuration",
    "lowStressDuration",
    "mediumStressDuration",
    "highStressDuration",
    "stressQualifier",
    "bodyBatteryChargedValue",
    "bodyBatteryDrainedValue",
    "bodyBatteryHighestValue",
    "bodyBatteryLowestValue",
    "bodyBatteryMostRecentValue",
    "averageSpo2",
    "lowestSpo2",
    "avgWakingRespirationValue",
)

_ACTIVITY_FIELDS = (
    "activityId",
    "activityName",
    "startTimeLocal",
    "startTimeGMT",
    "distance",
    "duration",
    "movingDuration",
    "elapsedDuration",
    "elevationGain",
    "elevationLoss",
    "averageSpeed",
    "maxSpeed",
    "calories",
    "averageHR",
    "maxHR",
    "averageRunningCadenceInStepsPerMinute",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "activityTrainingLoad",
    "avgPower",
    "maxPower",
    "normPower",
    "avgStrideLength",
    "vO2MaxValue",
    "steps",
    "avgVerticalOscillation",
    "avgGroundContactTime",
    "minTemperature",
    "maxTemperature",
)

_ACTIVITY_SUMMARY_DTO_FIELDS = (
    "startTimeLocal",
    "startTimeGMT",
    "distance",
    "duration",
    "movingDuration",
    "elapsedDuration",
    "averageSpeed",
    "maxSpeed",
    "calories",
    "bmrCalories",
    "averageHR",
    "maxHR",
    "minHR",
    "elevationGain",
    "elevationLoss",
    "averageRunCadence",
    "maxRunCadence",
    "averagePower",
    "maxPower",
    "normalizedPower",
    "trainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "activityTrainingLoad",
    "averageTemperature",
    "groundContactTime",
    "strideLength",
    "verticalOscillation",
    "vO2MaxValue",
)

_LAP_FIELDS = (
    "lapIndex",
    "startTimeGMT",
    "distance",
    "duration",
    "movingDuration",
    "averageSpeed",
    "maxSpeed",
    "averageHR",
    "maxHR",
    "averageRunCadence",
    "averagePower",
    "calories",
    "elevationGain",
    "elevationLoss",
)

_PROFILE_USER_DATA_FIELDS = (
    "gender",
    "weight",
    "height",
    "birthDate",
    "vo2MaxRunning",
    "vo2MaxCycling",
    "lactateThresholdSpeed",
    "lactateThresholdHeartRate",
    "activityLevel",
    "ftpAutoDetected",
)

_TRAINING_READINESS_FIELDS = (
    "calendarDate",
    "timestamp",
    "score",
    "level",
    "feedbackShort",
    "feedbackLong",
    "sleepScore",
    "sleepScoreFactorPercent",
    "sleepHistoryFactorPercent",
    "recoveryTime",
    "recoveryTimeFactorPercent",
    "acwrFactorPercent",
    "acuteLoad",
    "stressHistoryFactorPercent",
    "hrvFactorPercent",
    "hrvWeeklyAverage",
)


def _trim_activity_summary(activity: Any) -> dict[str, Any]:
    trimmed = _pick(activity, *_ACTIVITY_FIELDS)
    if isinstance(activity, dict):
        type_info = activity.get("activityType") or activity.get("activityTypeDTO")
        if isinstance(type_info, dict) and type_info.get("typeKey"):
            trimmed["activityType"] = type_info["typeKey"]
    return trimmed


def _trim_body_battery_day(day: Any) -> dict[str, Any]:
    trimmed = _pick(day, "date", "calendarDate", "charged", "drained")
    if not isinstance(day, dict):
        return trimmed
    ts_idx, level_idx = 0, 2
    descriptors = day.get("bodyBatteryValueDescriptorDTOList")
    if isinstance(descriptors, list):
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                continue
            index = descriptor.get("bodyBatteryValueDescriptorIndex")
            if not isinstance(index, int):
                continue
            key = descriptor.get("bodyBatteryValueDescriptorKey")
            if key == "timestamp":
                ts_idx = index
            elif key == "bodyBatteryLevel":
                level_idx = index
    values = day.get("bodyBatteryValuesArray")
    pairs: list[list[Any]] = []
    if isinstance(values, list):
        for entry in values:
            if (
                isinstance(entry, (list, tuple))
                and len(entry) > max(ts_idx, level_idx)
                and isinstance(entry[level_idx], (int, float))
            ):
                pairs.append([entry[ts_idx], entry[level_idx]])
    if pairs:
        levels = [pair[1] for pair in pairs]
        trimmed["highestLevel"] = max(levels)
        trimmed["lowestLevel"] = min(levels)
        trimmed["endOfDayLevel"] = pairs[-1][1]
        trimmed["values"] = _downsample(pairs)
    return trimmed


class GarminData:
    """Authenticated Garmin client restored from a stored token bundle.

    All public methods return trimmed, JSON-safe dicts/lists: summaries,
    scores and totals are kept while long time series are downsampled to at
    most 50 points, and missing keys never raise. Date arguments are
    ``YYYY-MM-DD`` strings defaulting to today (server date).

    A ``GarminConnectAuthenticationError`` from any data call is surfaced as
    :class:`GarminAuthRequired` (the stored session is dead); connection and
    rate-limit errors propagate unchanged. The bundled auth client can rotate
    tokens on any call, so after each successful fetch the session state is
    re-serialized and, when changed, handed to ``on_tokens_rotated``.
    """

    def __init__(
        self,
        token_blob: str,
        display_name: str | None = None,
        full_name: str | None = None,
        unit_system: str | None = None,
        on_tokens_rotated: Callable[[str], None] | None = None,
    ) -> None:
        self._garmin = Garmin()
        try:
            self._garmin.client.loads(token_blob)
        except Exception as exc:
            raise GarminAuthRequired(
                "Stored Garmin session could not be restored; please reconnect."
            ) from exc
        # Several endpoints embed display_name in the URL path, so the stored
        # link values must be restored onto the instance.
        self._garmin.display_name = display_name
        self._garmin.full_name = full_name
        self._garmin.unit_system = unit_system
        self._on_tokens_rotated = on_tokens_rotated
        self._token_blob = self._garmin.client.dumps()

    def _call(self, method_name: str, /, *args: Any, **kwargs: Any) -> Any:
        method = getattr(self._garmin, method_name)
        try:
            result = method(*args, **kwargs)
        except GarminConnectAuthenticationError as exc:
            raise GarminAuthRequired(
                "The Garmin session has expired; please reconnect the account."
            ) from exc
        except GarminConnectConnectionError as exc:
            # A dead/revoked session surfaces as "API Error 401/403" here:
            # the client's refresh swallows its own failures and re-raises
            # the original status as a connection error.
            message = str(exc)
            if "API Error 401" in message or "API Error 403" in message:
                raise GarminAuthRequired(
                    "The Garmin session has expired; please reconnect the account."
                ) from exc
            raise
        self._maybe_rotate_tokens()
        return result

    def _maybe_rotate_tokens(self) -> None:
        new_blob = self._garmin.client.dumps()
        if new_blob != self._token_blob:
            if self._on_tokens_rotated is not None:
                self._on_tokens_rotated(new_blob)
            self._token_blob = new_blob

    def get_profile(self) -> dict[str, Any]:
        raw = self._call("get_user_profile")
        profile: dict[str, Any] = {
            "displayName": self._garmin.display_name,
            "fullName": self._garmin.full_name,
            "unitSystem": self._garmin.unit_system,
        }
        profile.update(_pick(_dig(raw, "userData"), *_PROFILE_USER_DATA_FIELDS))
        sleep_routine = _pick(_dig(raw, "userSleep"), "sleepTime", "wakeTime")
        if sleep_routine:
            profile["sleepRoutine"] = sleep_routine
        return {key: value for key, value in profile.items() if value is not None}

    def get_daily_summary(self, date: str | None = None) -> dict[str, Any]:
        raw = self._call("get_user_summary", date or _today())
        return _pick(raw, *_DAILY_SUMMARY_FIELDS)

    def list_activities(
        self,
        limit: int = 10,
        start_date: str | None = None,
        end_date: str | None = None,
        activity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        if start_date or end_date:
            end = end_date or _today()
            start = start_date or (
                datetime.date.fromisoformat(end) - datetime.timedelta(days=90)
            ).isoformat()
            # One bounded request: the library's get_activities_by_date
            # paginates through the athlete's entire history regardless of
            # how few results the caller wants.
            params = {"startDate": start, "endDate": end, "start": "0", "limit": str(limit)}
            if activity_type:
                params["activityType"] = str(activity_type)
            raw = self._call(
                "connectapi",
                "/activitylist-service/activities/search/activities",
                params=params,
            )
        else:
            raw = self._call("get_activities", 0, limit, activity_type)
        if isinstance(raw, dict):
            raw = raw.get("activityList")  # dict-wrapped shape occurs in practice
        if not isinstance(raw, list):
            return []
        return [_trim_activity_summary(activity) for activity in raw[:limit]]

    def get_activity(self, activity_id: int | str) -> dict[str, Any]:
        raw = self._call("get_activity", str(activity_id))
        detail = _trim_activity_summary(raw)
        detail.update(_pick(_dig(raw, "summaryDTO"), *_ACTIVITY_SUMMARY_DTO_FIELDS))
        try:
            splits = self._call("get_activity_splits", str(activity_id))
        except GarminConnectConnectionError:
            splits = None  # splits are enrichment; keep the summary usable
        laps = _dig(splits, "lapDTOs")
        if isinstance(laps, list) and laps:
            detail["laps"] = [_pick(lap, *_LAP_FIELDS) for lap in _downsample(laps)]
        return detail

    def get_sleep(self, date: str | None = None) -> dict[str, Any]:
        raw = self._call("get_sleep_data", date or _today())
        dto = _dig(raw, "dailySleepDTO")
        trimmed = _pick(
            dto,
            "calendarDate",
            "sleepTimeSeconds",
            "napTimeSeconds",
            "sleepStartTimestampLocal",
            "sleepEndTimestampLocal",
            "deepSleepSeconds",
            "lightSleepSeconds",
            "remSleepSeconds",
            "awakeSleepSeconds",
            "averageSpO2Value",
            "lowestSpO2Value",
            "averageRespirationValue",
            "avgSleepStress",
        )
        overall = _dig(dto, "sleepScores", "overall")
        if isinstance(overall, dict):
            if overall.get("value") is not None:
                trimmed["sleepScore"] = overall["value"]
            if overall.get("qualifierKey") is not None:
                trimmed["sleepScoreQualifier"] = overall["qualifierKey"]
        trimmed.update(
            _pick(
                raw,
                "restingHeartRate",
                "avgOvernightHrv",
                "bodyBatteryChange",
                "restlessMomentsCount",
            )
        )
        return trimmed

    def get_hrv(self, date: str | None = None) -> dict[str, Any]:
        raw = self._call("get_hrv_data", date or _today())
        summary = _dig(raw, "hrvSummary")
        trimmed = _pick(
            summary,
            "calendarDate",
            "weeklyAvg",
            "lastNightAvg",
            "lastNight5MinHigh",
            "status",
            "feedbackPhrase",
        )
        baseline = _pick(
            _dig(summary, "baseline"),
            "lowUpper",
            "balancedLow",
            "balancedUpper",
            "markerValue",
        )
        if baseline:
            trimmed["baseline"] = baseline
        return trimmed

    def get_training_status(self, date: str | None = None) -> dict[str, Any]:
        raw = self._call("get_training_status", date or _today())
        result: dict[str, Any] = {}
        vo2max = _pick(
            _dig(raw, "mostRecentVO2Max", "generic"),
            "calendarDate",
            "vo2MaxPreciseValue",
            "vo2MaxValue",
            "fitnessAge",
        )
        if vo2max:
            result["vo2Max"] = vo2max
        vo2max_cycling = _pick(
            _dig(raw, "mostRecentVO2Max", "cycling"),
            "vo2MaxPreciseValue",
            "vo2MaxValue",
        )
        if vo2max_cycling:
            result["vo2MaxCycling"] = vo2max_cycling
        acclimation = _pick(
            _dig(raw, "mostRecentVO2Max", "heatAltitudeAcclimation"),
            "calendarDate",
            "heatAcclimationPercentage",
            "altitudeAcclimation",
        )
        if acclimation:
            result["heatAltitudeAcclimation"] = acclimation
        status = _pick(
            _first_value(_dig(raw, "mostRecentTrainingStatus", "latestTrainingStatusData")),
            "calendarDate",
            "sinceDate",
            "trainingStatus",
            "trainingStatusFeedbackPhrase",
            "fitnessTrend",
            "loadLevelTrend",
            "weeklyTrainingLoad",
            "loadTunnelMin",
            "loadTunnelMax",
            "acuteTrainingLoadDTO",
        )
        if isinstance(status.get("acuteTrainingLoadDTO"), dict):
            status["acuteTrainingLoadDTO"] = _pick(
                status["acuteTrainingLoadDTO"],
                "acwrPercent",
                "acwrStatus",
                "acwrStatusFeedback",
                "dailyTrainingLoadAcute",
                "dailyTrainingLoadChronic",
                "dailyAcuteChronicWorkloadRatio",
            )
        if status:
            result["trainingStatus"] = status
        balance = _pick(
            _first_value(
                _dig(raw, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap")
            ),
            "calendarDate",
            "monthlyLoadAerobicLow",
            "monthlyLoadAerobicHigh",
            "monthlyLoadAnaerobic",
            "monthlyLoadAerobicLowTargetMin",
            "monthlyLoadAerobicLowTargetMax",
            "monthlyLoadAerobicHighTargetMin",
            "monthlyLoadAerobicHighTargetMax",
            "monthlyLoadAnaerobicTargetMin",
            "monthlyLoadAnaerobicTargetMax",
            "trainingBalanceFeedbackPhrase",
        )
        if balance:
            result["trainingLoadBalance"] = balance
        return result

    def get_training_readiness(self, date: str | None = None) -> dict[str, Any]:
        raw = self._call("get_training_readiness", date or _today())
        entry = raw[0] if isinstance(raw, list) and raw else raw
        return _pick(entry, *_TRAINING_READINESS_FIELDS)

    def get_body_battery(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict[str, Any]]:
        start = start_date or _today()
        raw = self._call("get_body_battery", start, end_date or start)
        if not isinstance(raw, list):
            return []
        return [_trim_body_battery_day(day) for day in raw]

    def get_stress(self, date: str | None = None) -> dict[str, Any]:
        raw = self._call("get_stress_data", date or _today())
        trimmed = _pick(raw, "calendarDate", "avgStressLevel", "maxStressLevel")
        stress_values = _dig(raw, "stressValuesArray")
        if isinstance(stress_values, list) and stress_values:
            trimmed["stressValues"] = _downsample(stress_values)
        battery_values = _dig(raw, "bodyBatteryValuesArray")
        if isinstance(battery_values, list) and battery_values:
            trimmed["bodyBatteryValues"] = _downsample(battery_values)
        return trimmed

    def get_steps(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict[str, Any]]:
        start = start_date or _today()
        raw = self._call("get_daily_steps", start, end_date or start)
        if not isinstance(raw, list):
            return []
        return [
            _pick(day, "calendarDate", "totalSteps", "totalDistance", "stepGoal")
            for day in raw
        ]

    def get_heart_rate(self, date: str | None = None) -> dict[str, Any]:
        raw = self._call("get_heart_rates", date or _today())
        trimmed = _pick(
            raw,
            "calendarDate",
            "restingHeartRate",
            "minHeartRate",
            "maxHeartRate",
            "lastSevenDaysAvgRestingHeartRate",
        )
        values = _dig(raw, "heartRateValues")
        if isinstance(values, list) and values:
            trimmed["heartRateValues"] = _downsample(values)
        return trimmed

    def get_race_predictions(self) -> dict[str, Any]:
        raw = self._call("get_race_predictions")
        entry = raw[0] if isinstance(raw, list) and raw else raw
        return _pick(
            entry,
            "calendarDate",
            "time5K",
            "time10K",
            "timeHalfMarathon",
            "timeMarathon",
        )

    def get_body_composition(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> dict[str, Any]:
        start = start_date or _today()
        raw = self._call("get_body_composition", start, end_date or start)
        result = _pick(raw, "startDate", "endDate")
        average = _pick(
            _dig(raw, "totalAverage"),
            "weight",
            "bmi",
            "bodyFat",
            "bodyWater",
            "boneMass",
            "muscleMass",
            "metabolicAge",
            "visceralFat",
            "physiqueRating",
        )
        if average:
            result["totalAverage"] = average
        measurements = _dig(raw, "dateWeightList")
        if isinstance(measurements, list) and measurements:
            result["measurements"] = [
                _pick(
                    entry,
                    "calendarDate",
                    "weight",
                    "bmi",
                    "bodyFat",
                    "bodyWater",
                    "boneMass",
                    "muscleMass",
                )
                for entry in _downsample(measurements)
            ]
        return result


_cache_lock = threading.Lock()
_cache: dict[tuple[int, str], tuple[float, Any]] = {}


def cached(user_id: int, key: str, ttl: float, producer: Callable[[], Any]) -> Any:
    """Thread-safe in-process TTL cache.

    Data methods never cache on their own; callers opt in per call site. The
    producer runs outside the lock, so two racing threads may both produce —
    an acceptable trade against holding the lock across a network call.
    """
    cache_key = (user_id, key)
    with _cache_lock:
        entry = _cache.get(cache_key)
        if entry is not None and entry[0] > time.monotonic():
            return entry[1]
    value = producer()
    with _cache_lock:
        now = time.monotonic()
        _cache[cache_key] = (now + ttl, value)
        for stale in [k for k, (expires, _) in _cache.items() if expires <= now]:
            del _cache[stale]
    return value


def invalidate_user_cache(user_id: int) -> None:
    with _cache_lock:
        for cache_key in [k for k in _cache if k[0] == user_id]:
            del _cache[cache_key]
