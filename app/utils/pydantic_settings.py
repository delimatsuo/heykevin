"""Pydantic settings helpers for environment and dotenv handling."""

from __future__ import annotations

import os
from typing import Any

from pydantic_settings import BaseSettings

KEVIN_DISABLE_DOTENV = "KEVIN_DISABLE_DOTENV"


def dotenv_settings_kwargs() -> dict[str, Any]:
    """Return kwargs for BaseSettings to disable .env file reading when KEVIN_DISABLE_DOTENV=1.

    When KEVIN_DISABLE_DOTENV is set to exactly "1", returns {"_env_file": None}
    to instruct pydantic-settings not to read any .env file.
    For unset or any other value, returns {} to preserve default .env loading behavior.
    This helper never opens or reads any dotenv file itself.
    """
    if os.environ.get(KEVIN_DISABLE_DOTENV) == "1":
        return {"_env_file": None}
    return {}


class DotenvProtectedBaseSettings(BaseSettings):
    """BaseSettings subclass enforcing dotenv protection when KEVIN_DISABLE_DOTENV=1."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.update(dotenv_settings_kwargs())
        super().__init__(*args, **kwargs)
