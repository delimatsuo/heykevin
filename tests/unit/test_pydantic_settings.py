"""Unit tests for Pydantic settings dotenv opt-out and direct construction protection."""

from __future__ import annotations

import os
from typing import Any

import pytest
from pydantic_settings import BaseSettings

# Follow existing test convention: set only known dummy required settings
# within the test module after pytest conftest snapshots the pristine environment.
os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15005550006")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("USER_PHONE", "+15555550123")

from app.config import Settings, get_settings
from app.services import public_demo_breaker
from app.services.public_demo_breaker import PublicDemoBreakerSettings, get_breaker_settings
from app.utils.pydantic_settings import (
    KEVIN_DISABLE_DOTENV,
    DotenvProtectedBaseSettings,
    dotenv_settings_kwargs,
)

SETTINGS_CLASSES = [Settings, PublicDemoBreakerSettings]
NON_ONE_VALUES = [
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
]


@pytest.fixture
def capture_base_settings_init(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    def fake_init(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.append(kwargs.copy())

    monkeypatch.setattr(BaseSettings, "__init__", fake_init)
    return captured


def test_constant_name() -> None:
    assert KEVIN_DISABLE_DOTENV == "KEVIN_DISABLE_DOTENV"


def test_dotenv_settings_kwargs_exact_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, "1")
    assert dotenv_settings_kwargs() == {"_env_file": None}


def test_dotenv_settings_kwargs_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEVIN_DISABLE_DOTENV, raising=False)
    assert dotenv_settings_kwargs() == {}


@pytest.mark.parametrize("value", NON_ONE_VALUES)
def test_dotenv_settings_kwargs_non_one_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, value)
    assert dotenv_settings_kwargs() == {}


def test_settings_classes_inherit_dotenv_protected_base() -> None:
    assert issubclass(DotenvProtectedBaseSettings, BaseSettings)
    assert issubclass(Settings, DotenvProtectedBaseSettings)
    assert issubclass(PublicDemoBreakerSettings, DotenvProtectedBaseSettings)


@pytest.mark.parametrize("settings_cls", SETTINGS_CLASSES)
def test_direct_construction_exact_one_forces_none_env_file(
    monkeypatch: pytest.MonkeyPatch,
    capture_base_settings_init: list[dict[str, Any]],
    settings_cls: type[DotenvProtectedBaseSettings],
) -> None:
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, "1")
    settings_cls()
    assert capture_base_settings_init == [{"_env_file": None}]


@pytest.mark.parametrize("settings_cls", SETTINGS_CLASSES)
def test_direct_construction_exact_one_overrides_caller_env_file(
    monkeypatch: pytest.MonkeyPatch,
    capture_base_settings_init: list[dict[str, Any]],
    settings_cls: type[DotenvProtectedBaseSettings],
) -> None:
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, "1")
    settings_cls(_env_file=".env.custom")
    assert capture_base_settings_init == [{"_env_file": None}]


@pytest.mark.parametrize("settings_cls", SETTINGS_CLASSES)
def test_direct_construction_unset_preserves_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    capture_base_settings_init: list[dict[str, Any]],
    settings_cls: type[DotenvProtectedBaseSettings],
) -> None:
    monkeypatch.delenv(KEVIN_DISABLE_DOTENV, raising=False)
    settings_cls()
    settings_cls(_env_file=".env.custom")
    assert capture_base_settings_init == [{}, {"_env_file": ".env.custom"}]


@pytest.mark.parametrize("settings_cls", SETTINGS_CLASSES)
@pytest.mark.parametrize("value", NON_ONE_VALUES)
def test_direct_construction_non_one_preserves_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    capture_base_settings_init: list[dict[str, Any]],
    settings_cls: type[DotenvProtectedBaseSettings],
    value: str,
) -> None:
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, value)
    settings_cls()
    settings_cls(_env_file=".env.custom")
    assert capture_base_settings_init == [{}, {"_env_file": ".env.custom"}]


def test_get_settings_delegates_to_direct_construction(
    monkeypatch: pytest.MonkeyPatch,
    capture_base_settings_init: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, "1")
    get_settings()
    assert capture_base_settings_init == [{"_env_file": None}]

    monkeypatch.delenv(KEVIN_DISABLE_DOTENV, raising=False)
    get_settings()
    assert capture_base_settings_init == [{"_env_file": None}, {}]


def test_get_breaker_settings_delegates_to_direct_construction(
    monkeypatch: pytest.MonkeyPatch,
    capture_base_settings_init: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv(KEVIN_DISABLE_DOTENV, "1")
    get_breaker_settings()
    assert capture_base_settings_init == [{"_env_file": None}]

    monkeypatch.delenv(KEVIN_DISABLE_DOTENV, raising=False)
    get_breaker_settings()
    assert capture_base_settings_init == [{"_env_file": None}, {}]


def test_breaker_settings_instance_initialized() -> None:
    assert isinstance(
        public_demo_breaker.breaker_settings,
        public_demo_breaker.PublicDemoBreakerSettings,
    )
    assert isinstance(
        public_demo_breaker.breaker_settings,
        DotenvProtectedBaseSettings,
    )
