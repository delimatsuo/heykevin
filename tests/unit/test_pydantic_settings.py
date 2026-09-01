"""Unit tests for Pydantic settings dotenv opt-out and helper kwargs forwarding."""

from __future__ import annotations

import os

import pytest

# Follow existing test convention: set only known dummy required settings
# within the test module after pytest conftest snapshots the pristine environment.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

import app.config
from app.services import public_demo_breaker
from app.utils.pydantic_settings import KEVIN_DISABLE_DOTENV, dotenv_settings_kwargs


def test_constant_name():
    assert KEVIN_DISABLE_DOTENV == "KEVIN_DISABLE_DOTENV"


def test_dotenv_settings_kwargs_exact_one(monkeypatch):
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, "1")
    assert dotenv_settings_kwargs() == {"_env_file": None}


def test_dotenv_settings_kwargs_unset(monkeypatch):
    monkeypatch.delenv(KEVIN_DISABLE_DOTENV, raising=False)
    assert dotenv_settings_kwargs() == {}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "0",
        "true",
        "TRUE",
        "True",
        "false",
        "FALSE",
        "yes",
        "2",
        " 1 ",
        "None",
    ],
)
def test_dotenv_settings_kwargs_non_one_values(monkeypatch, value):
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, value)
    assert dotenv_settings_kwargs() == {}


def test_app_config_get_settings_forwards_helper_kwargs(monkeypatch):
    captured_calls: list[dict] = []
    sentinel = object()

    def fake_settings(**kwargs):
        captured_calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(app.config, "dotenv_settings_kwargs", lambda: {"_env_file": None})
    monkeypatch.setattr(app.config, "Settings", fake_settings)

    assert app.config.get_settings() is sentinel
    assert captured_calls == [{"_env_file": None}]


def test_public_demo_breaker_get_breaker_settings_forwards_helper_kwargs(monkeypatch):
    captured_calls: list[dict] = []
    sentinel = object()

    def fake_settings(**kwargs):
        captured_calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(public_demo_breaker, "dotenv_settings_kwargs", lambda: {"_env_file": None})
    monkeypatch.setattr(public_demo_breaker, "PublicDemoBreakerSettings", fake_settings)

    assert public_demo_breaker.get_breaker_settings() is sentinel
    assert captured_calls == [{"_env_file": None}]


def test_breaker_settings_instance_initialized():
    assert isinstance(
        public_demo_breaker.breaker_settings,
        public_demo_breaker.PublicDemoBreakerSettings,
    )
