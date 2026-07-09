"""Shared test bootstrap.

Sets SPORTBRO_* env vars to a temp directory BEFORE any sportbrobot module is
imported, so lazy settings/engine pick up the test configuration. Test modules
must import sportbrobot inside fixtures/tests (or after this module loads,
which pytest guarantees for conftest).
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="sportbrobot-test-")
os.environ.setdefault("SPORTBRO_DATA_DIR", _TMP)
os.environ.setdefault("SPORTBRO_DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("SPORTBRO_BASE_URL", "http://testserver")

import pytest


@pytest.fixture()
def app():
    from sportbrobot.db import init_db
    from sportbrobot.main import app as _app

    init_db()
    return _app


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    from sportbrobot.db import db_session

    with db_session() as session:
        yield session
