"""BASE_URL resolution, including Railway's injected domain."""

from __future__ import annotations

import pytest

from sportbrobot import config


@pytest.fixture()
def clear_settings_cache():
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_explicit_base_url_wins(monkeypatch, clear_settings_cache):
    monkeypatch.setenv("SPORTBRO_BASE_URL", "https://coach.example.com/")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "ignored.up.railway.app")
    assert config.get_settings().base_url == "https://coach.example.com"


def test_railway_domain_used_when_base_url_absent(monkeypatch, clear_settings_cache):
    monkeypatch.delenv("SPORTBRO_BASE_URL", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "sportbrobot-production.up.railway.app")
    assert (
        config.get_settings().base_url
        == "https://sportbrobot-production.up.railway.app"
    )


def test_localhost_fallback(monkeypatch, clear_settings_cache):
    monkeypatch.delenv("SPORTBRO_BASE_URL", raising=False)
    monkeypatch.delenv("RAILWAY_PUBLIC_DOMAIN", raising=False)
    assert config.get_settings().base_url == "http://localhost:8000"
