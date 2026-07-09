"""Application settings, loaded lazily from SPORTBRO_* environment variables.

Secrets (session signing key, Fernet key) are generated on first run and
persisted under the data directory so restarts don't invalidate sessions or
encrypted Garmin tokens.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet


@dataclass(frozen=True)
class Settings:
    base_url: str
    database_url: str
    secret_key: str
    fernet_key: str
    data_dir: Path
    strava_client_id: str | None = None
    strava_client_secret: str | None = None


def _resolve_base_url() -> str:
    """Public base URL, used to build the MCP link and Strava callback.

    Explicit ``SPORTBRO_BASE_URL`` always wins. On Railway the public domain
    isn't known until the first deploy, so fall back to the injected
    ``RAILWAY_PUBLIC_DOMAIN`` to spare the operator a manual step.
    """
    explicit = os.environ.get("SPORTBRO_BASE_URL")
    if explicit:
        return explicit.rstrip("/")
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_domain:
        return f"https://{railway_domain}".rstrip("/")
    return "http://localhost:8000"


def _load_or_create(path: Path, generator) -> str:
    if path.exists():
        return path.read_text().strip()
    value = generator()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    path.chmod(0o600)
    return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("SPORTBRO_DATA_DIR", "./data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    secret_key = os.environ.get("SPORTBRO_SECRET_KEY") or _load_or_create(
        data_dir / "secret_key", lambda: secrets.token_urlsafe(48)
    )
    fernet_key = os.environ.get("SPORTBRO_FERNET_KEY") or _load_or_create(
        data_dir / "fernet_key", lambda: Fernet.generate_key().decode()
    )

    return Settings(
        base_url=_resolve_base_url(),
        database_url=os.environ.get(
            "SPORTBRO_DATABASE_URL", f"sqlite:///{data_dir / 'sportbrobot.db'}"
        ),
        secret_key=secret_key,
        fernet_key=fernet_key,
        data_dir=data_dir,
        strava_client_id=os.environ.get("SPORTBRO_STRAVA_CLIENT_ID") or None,
        strava_client_secret=os.environ.get("SPORTBRO_STRAVA_CLIENT_SECRET") or None,
    )
