"""Tests for the Garmin service layer: login/MFA flow, token persistence,
response trimming and the TTL cache. garminconnect.Garmin is stubbed out in
the service module — no network."""

from __future__ import annotations

import datetime
import json
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from sportbrobot import security
from sportbrobot.db import db_session, init_db
from sportbrobot.garmin import service
from sportbrobot.models import GarminLink, User

INITIAL_BLOB = json.dumps(
    {"di_token": "tok-initial", "di_refresh_token": "refresh-1", "di_client_id": "cid"}
)
FRESH_BLOB = json.dumps(
    {"di_token": "tok-fresh", "di_refresh_token": "refresh-2", "di_client_id": "cid"}
)
TODAY = datetime.date.today().isoformat()


class FakeClient:
    def __init__(self) -> None:
        self.blob = INITIAL_BLOB
        self.loaded_with: str | None = None
        self.api_responses: dict[str, Any] = {}

    def loads(self, blob: str) -> None:
        self.loaded_with = blob
        self.blob = blob

    def dumps(self) -> str:
        return self.blob

    def connectapi(self, path: str, **kwargs: Any) -> Any:
        value = self.api_responses.get(path)
        if isinstance(value, Exception):
            raise value
        return value


class FakeGarmin:
    """Stub for garminconnect.Garmin; behaviour is driven by class attrs
    (login outcome) and per-instance `responses` (data methods)."""

    mfa = False
    login_exc: Exception | None = None
    created: list["FakeGarmin"] = []

    def __init__(self, email=None, password=None, return_on_mfa=False, **kwargs):
        self.email = email
        self.password = password
        self.return_on_mfa = return_on_mfa
        self.client = FakeClient()
        self.display_name = None
        self.full_name = None
        self.unit_system = None
        self.responses: dict[str, Any] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.rotate_to: str | None = None
        self.resume_code: str | None = None
        type(self).created.append(self)

    def _mark_logged_in(self) -> None:
        self.display_name = "athlete-42"
        self.full_name = "Test Athlete"
        self.unit_system = "metric"
        self.client.blob = FRESH_BLOB

    def login(self):
        if type(self).login_exc is not None:
            raise type(self).login_exc
        if type(self).mfa:
            return ("needs_mfa", None)
        self._mark_logged_in()
        return (None, None)

    def resume_login(self, client_state, mfa_code):
        self.resume_code = mfa_code
        self._mark_logged_in()
        return (None, None)

    def _respond(self, name: str, *args: Any) -> Any:
        self.calls.append((name, args))
        if self.rotate_to is not None:
            self.client.blob = self.rotate_to
        value = self.responses.get(name)
        if isinstance(value, Exception):
            raise value
        return value

    def get_user_profile(self):
        return self._respond("get_user_profile")

    def get_user_summary(self, cdate):
        return self._respond("get_user_summary", cdate)

    def get_activities(self, start=0, limit=20, activitytype=None):
        return self._respond("get_activities", start, limit, activitytype)

    def get_activities_by_date(self, startdate, enddate=None, activitytype=None):
        return self._respond("get_activities_by_date", startdate, enddate, activitytype)

    def get_activity(self, activity_id):
        return self._respond("get_activity", activity_id)

    def get_activity_splits(self, activity_id):
        return self._respond("get_activity_splits", activity_id)

    def get_sleep_data(self, cdate):
        return self._respond("get_sleep_data", cdate)

    def get_hrv_data(self, cdate):
        return self._respond("get_hrv_data", cdate)

    def get_training_status(self, cdate):
        return self._respond("get_training_status", cdate)

    def get_training_readiness(self, cdate):
        return self._respond("get_training_readiness", cdate)

    def get_body_battery(self, startdate, enddate=None):
        return self._respond("get_body_battery", startdate, enddate)

    def get_stress_data(self, cdate):
        return self._respond("get_stress_data", cdate)

    def get_daily_steps(self, start, end):
        return self._respond("get_daily_steps", start, end)

    def get_heart_rates(self, cdate):
        return self._respond("get_heart_rates", cdate)

    def get_race_predictions(self):
        return self._respond("get_race_predictions")

    def get_body_composition(self, startdate, enddate=None):
        return self._respond("get_body_composition", startdate, enddate)

    def connectapi(self, path, **kwargs):
        return self._respond("connectapi", path, kwargs.get("params"))


@pytest.fixture(autouse=True)
def _clean_state():
    init_db()
    service._pending_logins.clear()
    service._cache.clear()
    yield
    service._pending_logins.clear()
    service._cache.clear()


@pytest.fixture()
def fake_garmin(monkeypatch):
    class Fake(FakeGarmin):
        mfa = False
        login_exc = None
        created: list[FakeGarmin] = []

    monkeypatch.setattr(service, "Garmin", Fake)
    return Fake


def make_garmin_data(fake_cls, responses=None, **kwargs):
    data = service.GarminData(
        token_blob=INITIAL_BLOB,
        display_name="athlete-42",
        full_name="Test Athlete",
        unit_system="metric",
        **kwargs,
    )
    fake = fake_cls.created[-1]
    if responses:
        fake.responses.update(responses)
    return data, fake


def make_linked_user(db, token_blob=INITIAL_BLOB, status="active"):
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.flush()
    link = GarminLink(
        user_id=user.id,
        garmin_email="garmin@example.com",
        display_name="stored-display",
        full_name="Stored Name",
        unit_system="statute_us",
        token_blob=security.encrypt_text(token_blob),
        status=status,
    )
    db.add(link)
    db.commit()
    return user, link


# ---------------------------------------------------------------- login flow


def test_start_login_success(fake_garmin):
    result = service.start_garmin_login("a@b.c", "pw")

    assert isinstance(result, service.LoginSuccess)
    fake = fake_garmin.created[0]
    assert (fake.email, fake.password) == ("a@b.c", "pw")
    assert fake.return_on_mfa is True
    assert result.token_blob == FRESH_BLOB
    assert result.display_name == "athlete-42"
    assert result.full_name == "Test Athlete"
    assert result.unit_system == "metric"
    assert result.garmin_email == "a@b.c"
    assert not service._pending_logins


def test_mfa_flow_preserves_instance(fake_garmin):
    fake_garmin.mfa = True
    pending = service.start_garmin_login("a@b.c", "pw")

    assert isinstance(pending, service.MfaPending)
    assert pending.pending_id
    assert len(fake_garmin.created) == 1
    parked = fake_garmin.created[0]

    success = service.complete_garmin_mfa(pending.pending_id, "123456")

    assert len(fake_garmin.created) == 1  # same live object resumed, no new Garmin
    assert parked.resume_code == "123456"
    assert success.token_blob == FRESH_BLOB
    assert success.display_name == "athlete-42"
    assert success.garmin_email == "a@b.c"
    # the pending entry is consumed
    with pytest.raises(KeyError):
        service.complete_garmin_mfa(pending.pending_id, "123456")


def test_complete_mfa_unknown_id_raises_keyerror(fake_garmin):
    with pytest.raises(KeyError):
        service.complete_garmin_mfa("nonexistent", "123456")


def test_login_loads_identity_when_library_leaves_it_unset(fake_garmin):
    """Garmin.login(return_on_mfa=True) returns before the library's profile
    fetch even without an MFA challenge; the service must fetch it itself."""

    class Bare(fake_garmin):
        def _mark_logged_in(self):
            self.client.blob = FRESH_BLOB  # login succeeds but sets no identity

    Bare.created = []
    import sportbrobot.garmin.service as service_module

    original = service_module.Garmin
    service_module.Garmin = Bare
    try:
        fake_client_profile = {"displayName": "bare-athlete", "fullName": "Bare Athlete"}
        # Pre-wire responses on the class so the instance created inside
        # start_garmin_login picks them up via its FakeClient.
        result = None
        try:
            service.start_garmin_login("a@b.c", "pw")
        except service.GarminConnectConnectionError:
            pass  # empty api_responses -> profile fetch fails cleanly
        Bare.created.clear()

        # Now with a working profile endpoint:
        class Bare2(Bare):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.client.api_responses = {
                    "/userprofile-service/socialProfile": fake_client_profile,
                    "/userprofile-service/userprofile/user-settings": {
                        "userData": {"measurementSystem": "metric"}
                    },
                }

        Bare2.created = []
        service_module.Garmin = Bare2
        result = service.start_garmin_login("a@b.c", "pw")
        assert isinstance(result, service.LoginSuccess)
        assert result.display_name == "bare-athlete"
        assert result.full_name == "Bare Athlete"
        assert result.unit_system == "metric"
    finally:
        service_module.Garmin = original


def test_login_rejects_unserializable_fallback_session(fake_garmin):
    """A cookie-only fallback login dumps null DI tokens; storing that blob
    would create a link that can never be restored."""
    null_blob = json.dumps(
        {"di_token": None, "di_refresh_token": None, "di_client_id": None}
    )

    class Fallback(fake_garmin):
        def _mark_logged_in(self):
            self.display_name = "athlete-42"
            self.full_name = "Test Athlete"
            self.unit_system = "metric"
            self.client.blob = null_blob

    Fallback.created = []
    import sportbrobot.garmin.service as service_module

    original = service_module.Garmin
    service_module.Garmin = Fallback
    try:
        with pytest.raises(service.GarminConnectConnectionError):
            service.start_garmin_login("a@b.c", "pw")
    finally:
        service_module.Garmin = original


def test_wrong_mfa_code_keeps_pending_entry_for_retry(fake_garmin):
    from garminconnect import GarminConnectAuthenticationError

    fake_garmin.mfa = True
    pending = service.start_garmin_login("a@b.c", "pw")
    parked = fake_garmin.created[0]

    original_resume = parked.resume_login

    def failing_resume(client_state, code):
        raise GarminConnectAuthenticationError("bad code")

    parked.resume_login = failing_resume
    with pytest.raises(GarminConnectAuthenticationError):
        service.complete_garmin_mfa(pending.pending_id, "000000")

    # Entry survived; a corrected code succeeds on the same instance.
    parked.resume_login = original_resume
    success = service.complete_garmin_mfa(pending.pending_id, "123456")
    assert isinstance(success, service.LoginSuccess)
    assert pending.pending_id not in service._pending_logins


def test_data_call_maps_401_connection_error_to_auth_required(fake_garmin):
    from garminconnect import GarminConnectConnectionError as ConnError

    data, fake = make_garmin_data(
        fake_garmin,
        {"get_user_summary": ConnError("API Error 401 - Unauthorized")},
    )
    with pytest.raises(service.GarminAuthRequired):
        data.get_daily_summary(TODAY)


def test_data_call_leaves_other_connection_errors_alone(fake_garmin):
    from garminconnect import GarminConnectConnectionError as ConnError

    data, fake = make_garmin_data(
        fake_garmin,
        {"get_user_summary": ConnError("API Error 500 - upstream boom")},
    )
    with pytest.raises(ConnError):
        data.get_daily_summary(TODAY)


def test_start_login_ignores_garmintokens_env(fake_garmin, monkeypatch):
    monkeypatch.setenv("GARMINTOKENS", "/some/operator/tokens")
    service.start_garmin_login("a@b.c", "pw")
    import os

    assert "GARMINTOKENS" not in os.environ


def test_mfa_pending_expires(fake_garmin):
    fake_garmin.mfa = True
    pending = service.start_garmin_login("a@b.c", "pw")

    with service._pending_lock:
        service._pending_logins[pending.pending_id].expires_at = time.monotonic() - 1

    with pytest.raises(KeyError):
        service.complete_garmin_mfa(pending.pending_id, "123456")
    assert pending.pending_id not in service._pending_logins  # purged


@pytest.mark.parametrize(
    "exc_type",
    [
        GarminConnectAuthenticationError,
        GarminConnectTooManyRequestsError,
        GarminConnectConnectionError,
    ],
)
def test_login_errors_keep_type_with_clean_message(fake_garmin, exc_type):
    fake_garmin.login_exc = exc_type("401 Client Error: raw-upstream-guts Traceback")
    with pytest.raises(exc_type) as excinfo:
        service.start_garmin_login("a@b.c", "pw")
    assert "raw-upstream-guts" not in str(excinfo.value)
    assert str(excinfo.value)


# ------------------------------------------------------- get_client_for_user


def test_get_client_for_user_not_linked(db, fake_garmin):
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x")
    db.add(user)
    db.commit()
    with pytest.raises(service.GarminNotLinked):
        service.get_client_for_user(db, user.id)


def test_get_client_for_user_happy_path(db, fake_garmin):
    user, _link = make_linked_user(db)
    data = service.get_client_for_user(db, user.id)

    fake = fake_garmin.created[-1]
    assert fake.email is None and fake.password is None
    assert fake.client.loaded_with == INITIAL_BLOB  # decrypted blob restored
    assert fake.display_name == "stored-display"
    assert fake.full_name == "Stored Name"
    assert fake.unit_system == "statute_us"

    fake.responses["get_daily_steps"] = [
        {"calendarDate": TODAY, "totalSteps": 9000, "totalDistance": 7100, "stepGoal": 8000}
    ]
    assert data.get_steps()[0]["totalSteps"] == 9000


def test_get_client_for_user_reauth_required_status(db, fake_garmin):
    user, _link = make_linked_user(db, status="reauth_required")
    with pytest.raises(service.GarminAuthRequired):
        service.get_client_for_user(db, user.id)


def test_get_client_for_user_undecryptable_blob(db, fake_garmin):
    user, link = make_linked_user(db)
    link.token_blob = "not-a-fernet-token"
    db.commit()
    with pytest.raises(service.GarminAuthRequired):
        service.get_client_for_user(db, user.id)


def test_token_rotation_persists_new_blob(db, fake_garmin):
    user, link = make_linked_user(db)
    data = service.get_client_for_user(db, user.id)

    fake = fake_garmin.created[-1]
    fake.responses["get_daily_steps"] = [{"calendarDate": TODAY, "totalSteps": 1}]
    fake.rotate_to = FRESH_BLOB
    data.get_steps()

    with db_session() as session:
        row = session.get(GarminLink, link.id)
        assert security.decrypt_text(row.token_blob) == FRESH_BLOB


def test_data_call_auth_error_raises_auth_required(fake_garmin):
    data, fake = make_garmin_data(fake_garmin)
    fake.responses["get_daily_steps"] = GarminConnectAuthenticationError("session dead")
    with pytest.raises(service.GarminAuthRequired):
        data.get_steps()


def test_data_call_connection_error_propagates(fake_garmin):
    data, fake = make_garmin_data(fake_garmin)
    fake.responses["get_stress_data"] = GarminConnectConnectionError("garmin down")
    with pytest.raises(GarminConnectConnectionError):
        data.get_stress()


def test_dates_default_to_today(fake_garmin):
    data, fake = make_garmin_data(fake_garmin)
    data.get_sleep()
    data.get_body_battery()
    data.get_steps()
    assert ("get_sleep_data", (TODAY,)) in fake.calls
    assert ("get_body_battery", (TODAY, TODAY)) in fake.calls
    assert ("get_daily_steps", (TODAY, TODAY)) in fake.calls


# ----------------------------------------------------------------- trimming


SLEEP_RESPONSE = {
    "dailySleepDTO": {
        "id": 1751925600000,
        "userProfilePK": 12345678,
        "calendarDate": "2026-07-07",
        "sleepTimeSeconds": 27360,
        "napTimeSeconds": 0,
        "sleepStartTimestampLocal": 1751932800000,
        "sleepEndTimestampLocal": 1751961600000,
        "deepSleepSeconds": 5460,
        "lightSleepSeconds": 15720,
        "remSleepSeconds": 6180,
        "awakeSleepSeconds": 1440,
        "averageSpO2Value": 95.0,
        "lowestSpO2Value": 90,
        "averageRespirationValue": 14.0,
        "avgSleepStress": 17.0,
        "sleepScores": {
            "totalDuration": {"qualifierKey": "GOOD"},
            "stress": {"qualifierKey": "FAIR"},
            "overall": {"value": 82, "qualifierKey": "GOOD"},
        },
    },
    "sleepMovement": [
        {"startGMT": f"2026-07-06T22:{i % 60:02d}:00.0", "activityLevel": i % 5}
        for i in range(480)
    ],
    "sleepHeartRate": [[1751932800000 + i * 60000, 50 + i % 10] for i in range(480)],
    "restingHeartRate": 47,
    "avgOvernightHrv": 62.0,
    "bodyBatteryChange": 58,
    "restlessMomentsCount": 21,
}


def test_get_sleep_trims(fake_garmin):
    data, _ = make_garmin_data(fake_garmin, {"get_sleep_data": SLEEP_RESPONSE})
    result = data.get_sleep("2026-07-07")

    assert result["calendarDate"] == "2026-07-07"
    assert result["sleepTimeSeconds"] == 27360
    assert result["deepSleepSeconds"] == 5460
    assert result["sleepScore"] == 82
    assert result["sleepScoreQualifier"] == "GOOD"
    assert result["restingHeartRate"] == 47
    assert result["avgOvernightHrv"] == 62.0
    assert "sleepMovement" not in result
    assert "sleepHeartRate" not in result
    assert "userProfilePK" not in result


HEART_RATE_RESPONSE = {
    "userProfilePK": 12345678,
    "calendarDate": "2026-07-08",
    "startTimestampGMT": "2026-07-08T00:00:00.0",
    "maxHeartRate": 154,
    "minHeartRate": 44,
    "restingHeartRate": 47,
    "lastSevenDaysAvgRestingHeartRate": 48,
    "heartRateValueDescriptors": [
        {"key": "timestamp", "index": 0},
        {"key": "heartrate", "index": 1},
    ],
    "heartRateValues": [[1751932800000 + i * 120000, 60 + i % 40] for i in range(720)],
}


def test_get_heart_rate_downsamples(fake_garmin):
    data, _ = make_garmin_data(fake_garmin, {"get_heart_rates": HEART_RATE_RESPONSE})
    result = data.get_heart_rate("2026-07-08")

    assert result["restingHeartRate"] == 47
    assert result["maxHeartRate"] == 154
    assert len(result["heartRateValues"]) <= 50
    assert result["heartRateValues"][0] == HEART_RATE_RESPONSE["heartRateValues"][0]
    assert result["heartRateValues"][-1] == HEART_RATE_RESPONSE["heartRateValues"][-1]
    assert "userProfilePK" not in result


STRESS_RESPONSE = {
    "userProfilePK": 12345678,
    "calendarDate": "2026-07-08",
    "maxStressLevel": 88,
    "avgStressLevel": 27,
    "stressValueDescriptorsDTOList": [
        {"key": "timestamp", "index": 0},
        {"key": "stressLevel", "index": 1},
    ],
    "stressValuesArray": [[1751932800000 + i * 180000, i % 100] for i in range(480)],
    "bodyBatteryValuesArray": [
        [1751932800000 + i * 180000, "MEASURED", 30 + i % 60, 1.0] for i in range(480)
    ],
}


def test_get_stress_downsamples(fake_garmin):
    data, _ = make_garmin_data(fake_garmin, {"get_stress_data": STRESS_RESPONSE})
    result = data.get_stress("2026-07-08")

    assert result["avgStressLevel"] == 27
    assert result["maxStressLevel"] == 88
    assert len(result["stressValues"]) <= 50
    assert len(result["bodyBatteryValues"]) <= 50
    assert "userProfilePK" not in result


BODY_BATTERY_RESPONSE = [
    {
        "date": "2026-07-08",
        "charged": 55,
        "drained": 42,
        "startTimestampGMT": "2026-07-08T00:00:00.0",
        "endTimestampGMT": "2026-07-09T00:00:00.0",
        "bodyBatteryValueDescriptorDTOList": [
            {"bodyBatteryValueDescriptorIndex": 0, "bodyBatteryValueDescriptorKey": "timestamp"},
            {
                "bodyBatteryValueDescriptorIndex": 1,
                "bodyBatteryValueDescriptorKey": "bodyBatteryStatus",
            },
            {
                "bodyBatteryValueDescriptorIndex": 2,
                "bodyBatteryValueDescriptorKey": "bodyBatteryLevel",
            },
            {
                "bodyBatteryValueDescriptorIndex": 3,
                "bodyBatteryValueDescriptorKey": "bodyBatteryVersion",
            },
        ],
        "bodyBatteryValuesArray": [
            [1751932800000 + i * 180000, "MEASURED", 30 + (i * 3) % 56, 1.0]
            for i in range(480)
        ],
    }
]


def test_get_body_battery_trims_and_downsamples(fake_garmin):
    data, _ = make_garmin_data(fake_garmin, {"get_body_battery": BODY_BATTERY_RESPONSE})
    result = data.get_body_battery("2026-07-08", "2026-07-08")

    assert len(result) == 1
    day = result[0]
    assert day["date"] == "2026-07-08"
    assert day["charged"] == 55
    assert day["drained"] == 42
    assert day["highestLevel"] == 85  # max of 30 + (i*3) % 56
    assert day["lowestLevel"] == 30
    assert len(day["values"]) <= 50
    assert all(len(pair) == 2 for pair in day["values"])  # [timestamp, level]
    assert "bodyBatteryValuesArray" not in day
    assert "bodyBatteryValueDescriptorDTOList" not in day


def _activity(activity_id: int) -> dict[str, Any]:
    return {
        "activityId": activity_id,
        "activityName": "Utrecht Running",
        "startTimeLocal": "2026-07-06 07:12:03",
        "startTimeGMT": "2026-07-06 05:12:03",
        "activityType": {"typeId": 1, "typeKey": "running", "parentTypeId": 17},
        "distance": 12030.0,
        "duration": 3841.0,
        "movingDuration": 3800.0,
        "elevationGain": 45.0,
        "elevationLoss": 44.0,
        "averageSpeed": 3.132,
        "maxSpeed": 3.9,
        "calories": 780.0,
        "averageHR": 152.0,
        "maxHR": 176.0,
        "averageRunningCadenceInStepsPerMinute": 172.0,
        "aerobicTrainingEffect": 3.4,
        "anaerobicTrainingEffect": 0.4,
        "activityTrainingLoad": 182.0,
        "vO2MaxValue": 52.0,
        "deviceId": 3999999999,
        "ownerId": 12345678,
        "hasPolyline": True,
        "privacy": {"typeKey": "private"},
    }


def test_list_activities_trims_and_limits(fake_garmin):
    data, fake = make_garmin_data(
        fake_garmin, {"get_activities": [_activity(1), _activity(2), _activity(3)]}
    )
    result = data.list_activities(limit=2)

    assert ("get_activities", (0, 2, None)) in fake.calls
    assert len(result) == 2
    first = result[0]
    assert first["activityId"] == 1
    assert first["activityType"] == "running"
    assert first["distance"] == 12030.0
    assert first["averageHR"] == 152.0
    assert "deviceId" not in first
    assert "ownerId" not in first
    assert "privacy" not in first


def test_list_activities_by_date(fake_garmin):
    data, fake = make_garmin_data(
        fake_garmin, {"connectapi": [_activity(1), _activity(2)]}
    )
    result = data.list_activities(
        limit=1, start_date="2026-07-01", end_date="2026-07-08", activity_type="running"
    )

    name, (path, params) = next(c for c in fake.calls if c[0] == "connectapi")
    assert path == "/activitylist-service/activities/search/activities"
    assert params["startDate"] == "2026-07-01"
    assert params["endDate"] == "2026-07-08"
    assert params["activityType"] == "running"
    assert params["limit"] == "1"  # bounded: never walks the whole history
    assert len(result) == 1


def test_list_activities_unwraps_activity_list_dict(fake_garmin):
    data, fake = make_garmin_data(
        fake_garmin, {"get_activities": {"activityList": [_activity(1), _activity(2)]}}
    )
    result = data.list_activities(limit=10)
    assert len(result) == 2


def test_list_activities_clamps_limit(fake_garmin):
    data, fake = make_garmin_data(fake_garmin, {"get_activities": []})
    data.list_activities(limit=5000)
    assert ("get_activities", (0, 200, None)) in fake.calls


ACTIVITY_DETAIL = {
    "activityId": 19583002114,
    "activityName": "Utrecht Running",
    "activityTypeDTO": {"typeId": 1, "typeKey": "running"},
    "summaryDTO": {
        "startTimeLocal": "2026-07-06T07:12:03.0",
        "distance": 12030.0,
        "duration": 3841.0,
        "movingDuration": 3800.0,
        "averageSpeed": 3.13,
        "maxSpeed": 3.9,
        "calories": 780.0,
        "averageHR": 152.0,
        "maxHR": 176.0,
        "elevationGain": 45.0,
        "elevationLoss": 44.0,
        "averageRunCadence": 172.0,
        "trainingEffect": 3.4,
        "anaerobicTrainingEffect": 0.4,
    },
    "metadataDTO": {"uploadedDate": "2026-07-06"},
}

ACTIVITY_SPLITS = {
    "activityId": 19583002114,
    "lapDTOs": [
        {
            "lapIndex": i + 1,
            "distance": 1000.0,
            "duration": 318.0,
            "averageSpeed": 3.14,
            "averageHR": 150 + i,
            "maxHR": 160 + i,
            "calories": 64.0,
            "elevationGain": 4.0,
            "lengthDTOs": [{"junk": True}] * 20,
        }
        for i in range(12)
    ],
}


def test_get_activity_merges_summary_and_splits(fake_garmin):
    data, _ = make_garmin_data(
        fake_garmin,
        {"get_activity": ACTIVITY_DETAIL, "get_activity_splits": ACTIVITY_SPLITS},
    )
    result = data.get_activity(19583002114)

    assert result["activityId"] == 19583002114
    assert result["activityType"] == "running"
    assert result["distance"] == 12030.0
    assert result["averageHR"] == 152.0
    assert "metadataDTO" not in result
    assert len(result["laps"]) == 12
    assert result["laps"][0]["lapIndex"] == 1
    assert result["laps"][0]["averageHR"] == 150
    assert "lengthDTOs" not in result["laps"][0]


def test_get_activity_tolerates_splits_failure(fake_garmin):
    data, _ = make_garmin_data(
        fake_garmin,
        {
            "get_activity": ACTIVITY_DETAIL,
            "get_activity_splits": GarminConnectConnectionError("404"),
        },
    )
    result = data.get_activity("19583002114")

    assert result["activityId"] == 19583002114
    assert "laps" not in result


HRV_RESPONSE = {
    "userProfilePk": 12345678,
    "hrvSummary": {
        "calendarDate": "2026-07-08",
        "weeklyAvg": 61,
        "lastNightAvg": 63,
        "lastNight5MinHigh": 78,
        "baseline": {"lowUpper": 55, "balancedLow": 58, "balancedUpper": 70, "markerValue": 0.5},
        "status": "BALANCED",
        "feedbackPhrase": "HRV_BALANCED_2",
        "createTimeStamp": "2026-07-08T06:00:00.0",
    },
    "hrvReadings": [
        {"hrvValue": 60 + i % 20, "readingTimeGMT": "2026-07-08T02:00:00.0"} for i in range(300)
    ],
}


def test_get_hrv_trims(fake_garmin):
    data, _ = make_garmin_data(fake_garmin, {"get_hrv_data": HRV_RESPONSE})
    result = data.get_hrv("2026-07-08")

    assert result["lastNightAvg"] == 63
    assert result["status"] == "BALANCED"
    assert result["baseline"]["balancedLow"] == 58
    assert "hrvReadings" not in result
    assert "createTimeStamp" not in result


TRAINING_STATUS_RESPONSE = {
    "userId": 12345678,
    "mostRecentVO2Max": {
        "userId": 12345678,
        "generic": {
            "calendarDate": "2026-07-06",
            "vo2MaxPreciseValue": 52.3,
            "vo2MaxValue": 52.0,
            "fitnessAge": 29,
            "maxMetCategory": 0,
        },
        "cycling": None,
        "heatAltitudeAcclimation": {
            "calendarDate": "2026-07-07",
            "heatAcclimationPercentage": 12,
            "altitudeAcclimation": 0,
        },
    },
    "mostRecentTrainingLoadBalance": {
        "userId": 12345678,
        "metricsTrainingLoadBalanceDTOMap": {
            "3999999999": {
                "calendarDate": "2026-07-08",
                "deviceId": 3999999999,
                "monthlyLoadAerobicLow": 312.0,
                "monthlyLoadAerobicHigh": 210.0,
                "monthlyLoadAnaerobic": 45.0,
                "monthlyLoadAerobicLowTargetMin": 240,
                "monthlyLoadAerobicLowTargetMax": 590,
                "trainingBalanceFeedbackPhrase": "BALANCED",
            }
        },
    },
    "mostRecentTrainingStatus": {
        "latestTrainingStatusData": {
            "3999999999": {
                "calendarDate": "2026-07-08",
                "sinceDate": "2026-06-15",
                "deviceId": 3999999999,
                "trainingStatus": 4,
                "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
                "fitnessTrend": 2,
                "weeklyTrainingLoad": 512,
                "loadTunnelMin": 350,
                "loadTunnelMax": 700,
                "acuteTrainingLoadDTO": {
                    "acwrPercent": 88,
                    "acwrStatus": "OPTIMAL",
                    "acwrStatusFeedback": "FEEDBACK_1",
                    "dailyTrainingLoadAcute": 410,
                    "dailyTrainingLoadChronic": 465,
                    "dailyAcuteChronicWorkloadRatio": 0.88,
                },
            }
        }
    },
}


def test_get_training_status_extracts_nested(fake_garmin):
    data, _ = make_garmin_data(
        fake_garmin, {"get_training_status": TRAINING_STATUS_RESPONSE}
    )
    result = data.get_training_status("2026-07-08")

    assert result["vo2Max"]["vo2MaxValue"] == 52.0
    assert result["vo2Max"]["fitnessAge"] == 29
    assert result["heatAltitudeAcclimation"]["heatAcclimationPercentage"] == 12
    assert result["trainingStatus"]["trainingStatusFeedbackPhrase"] == "PRODUCTIVE_1"
    assert result["trainingStatus"]["acuteTrainingLoadDTO"]["acwrPercent"] == 88
    assert result["trainingLoadBalance"]["monthlyLoadAerobicLow"] == 312.0
    assert "userId" not in result
    assert "deviceId" not in result["trainingStatus"]


TRAINING_READINESS_RESPONSE = [
    {
        "userProfilePK": 12345678,
        "calendarDate": "2026-07-08",
        "timestamp": "2026-07-08T05:30:00.0",
        "score": 74,
        "level": "HIGH",
        "feedbackLong": "UNLOCK_YOUR_POTENTIAL",
        "feedbackShort": "READY_TO_GO",
        "sleepScore": 82,
        "sleepScoreFactorPercent": 85,
        "recoveryTime": 14,
        "recoveryTimeFactorPercent": 70,
        "acwrFactorPercent": 90,
        "acuteLoad": 410,
        "stressHistoryFactorPercent": 80,
        "hrvFactorPercent": 95,
        "hrvWeeklyAverage": 61,
        "deviceId": 3999999999,
    }
]


def test_get_training_readiness_takes_first_entry(fake_garmin):
    data, _ = make_garmin_data(
        fake_garmin, {"get_training_readiness": TRAINING_READINESS_RESPONSE}
    )
    result = data.get_training_readiness("2026-07-08")

    assert result["score"] == 74
    assert result["level"] == "HIGH"
    assert result["hrvFactorPercent"] == 95
    assert "deviceId" not in result
    assert "userProfilePK" not in result


def test_get_race_predictions(fake_garmin):
    data, _ = make_garmin_data(
        fake_garmin,
        {
            "get_race_predictions": {
                "userId": 12345678,
                "calendarDate": "2026-07-08",
                "time5K": 1290.0,
                "time10K": 2712.0,
                "timeHalfMarathon": 6060.0,
                "timeMarathon": 12900.0,
            }
        },
    )
    result = data.get_race_predictions()

    assert result["time5K"] == 1290.0
    assert result["timeMarathon"] == 12900.0
    assert "userId" not in result


BODY_COMPOSITION_RESPONSE = {
    "startDate": "2026-05-09",
    "endDate": "2026-07-08",
    "dateWeightList": [
        {
            "samplePk": 1751932800000 + i,
            "calendarDate": f"2026-05-{(i % 28) + 1:02d}",
            "weight": 72500.0 - i * 10,
            "bmi": 22.4,
            "bodyFat": 15.2,
            "bodyWater": 58.1,
            "boneMass": 3200,
            "muscleMass": 33400,
            "sourceType": "INDEX_SCALE",
        }
        for i in range(60)
    ],
    "totalAverage": {
        "from": 1749340800000,
        "until": 1751932800000,
        "weight": 72480.0,
        "bmi": 22.4,
        "bodyFat": 15.3,
        "bodyWater": 58.0,
        "boneMass": 3195,
        "muscleMass": 33350,
        "physiqueRating": None,
        "visceralFat": None,
        "metabolicAge": None,
    },
}


def test_get_body_composition_downsamples(fake_garmin):
    data, _ = make_garmin_data(
        fake_garmin, {"get_body_composition": BODY_COMPOSITION_RESPONSE}
    )
    result = data.get_body_composition("2026-05-09", "2026-07-08")

    assert result["totalAverage"]["weight"] == 72480.0
    assert "physiqueRating" not in result["totalAverage"]  # None values dropped
    assert len(result["measurements"]) <= 50
    assert "samplePk" not in result["measurements"][0]
    assert result["measurements"][0]["weight"] == 72500.0


DAILY_SUMMARY_RESPONSE = {
    "userProfileId": 12345678,
    "uuid": "abc-def",
    "calendarDate": "2026-07-08",
    "totalKilocalories": 2450.0,
    "activeKilocalories": 620.0,
    "bmrKilocalories": 1830.0,
    "totalSteps": 12345,
    "dailyStepGoal": 8000,
    "totalDistanceMeters": 9345,
    "highlyActiveSeconds": 3200,
    "activeSeconds": 7000,
    "sedentarySeconds": 40000,
    "sleepingSeconds": 27000,
    "moderateIntensityMinutes": 25,
    "vigorousIntensityMinutes": 35,
    "intensityMinutesGoal": 150,
    "floorsAscended": 12.0,
    "floorsDescended": 10.0,
    "minHeartRate": 44,
    "maxHeartRate": 154,
    "restingHeartRate": 47,
    "lastSevenDaysAvgRestingHeartRate": 48,
    "averageStressLevel": 27,
    "maxStressLevel": 88,
    "stressDuration": 20000,
    "bodyBatteryChargedValue": 55,
    "bodyBatteryDrainedValue": 42,
    "bodyBatteryHighestValue": 88,
    "bodyBatteryLowestValue": 30,
    "bodyBatteryMostRecentValue": 61,
    "averageSpo2": 95.0,
    "lowestSpo2": 90,
    "avgWakingRespirationValue": 14.0,
    "burnedKilocalories": None,
    "privacyProtected": False,
}


def test_get_daily_summary_whitelists(fake_garmin):
    data, fake = make_garmin_data(
        fake_garmin, {"get_user_summary": DAILY_SUMMARY_RESPONSE}
    )
    result = data.get_daily_summary("2026-07-08")

    assert ("get_user_summary", ("2026-07-08",)) in fake.calls
    assert result["totalSteps"] == 12345
    assert result["restingHeartRate"] == 47
    assert result["bodyBatteryMostRecentValue"] == 61
    assert "uuid" not in result
    assert "userProfileId" not in result
    assert "privacyProtected" not in result


USER_SETTINGS_RESPONSE = {
    "id": 12345678,
    "userData": {
        "gender": "MALE",
        "weight": 72500.0,
        "height": 183.0,
        "birthDate": "1990-04-12",
        "vo2MaxRunning": 52.0,
        "vo2MaxCycling": None,
        "lactateThresholdSpeed": 3.35,
        "lactateThresholdHeartRate": 168.0,
        "activityLevel": 6,
        "ftpAutoDetected": True,
        "measurementSystem": "metric",
        "handedness": "RIGHT",
    },
    "userSleep": {
        "sleepTime": 79200,
        "defaultSleepTime": False,
        "wakeTime": 21600,
        "defaultWakeTime": False,
    },
}


def test_get_profile_trims(fake_garmin):
    data, _ = make_garmin_data(fake_garmin, {"get_user_profile": USER_SETTINGS_RESPONSE})
    result = data.get_profile()

    assert result["displayName"] == "athlete-42"
    assert result["fullName"] == "Test Athlete"
    assert result["unitSystem"] == "metric"
    assert result["gender"] == "MALE"
    assert result["vo2MaxRunning"] == 52.0
    assert result["sleepRoutine"] == {"sleepTime": 79200, "wakeTime": 21600}
    assert "vo2MaxCycling" not in result  # None dropped
    assert "handedness" not in result


def test_trimmers_tolerate_empty_responses(fake_garmin):
    data, _ = make_garmin_data(fake_garmin)  # every data method returns None

    assert data.get_profile()["displayName"] == "athlete-42"
    assert data.get_daily_summary() == {}
    assert data.list_activities() == []
    assert data.get_activity(123) == {}
    assert data.get_sleep() == {}
    assert data.get_hrv() == {}
    assert data.get_training_status() == {}
    assert data.get_training_readiness() == {}
    assert data.get_body_battery() == []
    assert data.get_stress() == {}
    assert data.get_steps() == []
    assert data.get_heart_rate() == {}
    assert data.get_race_predictions() == {}
    assert data.get_body_composition() == {}


def test_trimmers_tolerate_unexpected_shapes(fake_garmin):
    data, _ = make_garmin_data(
        fake_garmin,
        {
            "get_sleep_data": {"dailySleepDTO": "not-a-dict", "restingHeartRate": 50},
            "get_hrv_data": {"hrvSummary": ["odd"]},
            "get_training_status": {
                "mostRecentVO2Max": 5,
                "mostRecentTrainingStatus": {"latestTrainingStatusData": "?"},
            },
            "get_training_readiness": [],
            "get_body_battery": [{"charged": 10, "bodyBatteryValuesArray": "oops"}, "junk"],
            "get_heart_rates": {"heartRateValues": {"not": "a list"}, "restingHeartRate": 50},
            "get_race_predictions": [],
            "get_body_composition": {"dateWeightList": "nah", "totalAverage": 3},
            "get_daily_steps": [{"calendarDate": TODAY}, None, 42],
        },
    )

    assert data.get_sleep() == {"restingHeartRate": 50}
    assert data.get_hrv() == {}
    assert data.get_training_status() == {}
    assert data.get_training_readiness() == {}
    assert data.get_body_battery() == [{"charged": 10}, {}]
    assert data.get_heart_rate() == {"restingHeartRate": 50}
    assert data.get_race_predictions() == {}
    assert data.get_body_composition() == {}
    assert data.get_steps() == [{"calendarDate": TODAY}, {}, {}]


# -------------------------------------------------------------------- cache


def test_cached_hits_within_ttl_and_expires(monkeypatch):
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: clock.now))
    calls: list[int] = []

    def producer():
        calls.append(1)
        return {"value": len(calls)}

    assert service.cached(1, "steps", 60, producer) == {"value": 1}
    assert service.cached(1, "steps", 60, producer) == {"value": 1}
    assert len(calls) == 1

    # distinct key and distinct user are separate entries
    assert service.cached(1, "sleep", 60, producer)["value"] == 2
    assert service.cached(2, "steps", 60, producer)["value"] == 3

    clock.now += 61
    assert service.cached(1, "steps", 60, producer)["value"] == 4


def test_invalidate_user_cache():
    assert service.cached(1, "a", 300, lambda: "one") == "one"
    assert service.cached(2, "a", 300, lambda: "two") == "two"

    service.invalidate_user_cache(1)

    assert service.cached(1, "a", 300, lambda: "one-again") == "one-again"
    assert service.cached(2, "a", 300, lambda: "still-cached") == "two"
