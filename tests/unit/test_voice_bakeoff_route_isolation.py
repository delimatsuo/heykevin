"""Narrow regression assertion for the planned isolated bakeoff module."""

from pathlib import Path


def test_production_app_does_not_import_the_planned_bakeoff_module():
    source = Path("app/main.py").read_text(encoding="utf-8")

    assert "app.experiments.voice_bakeoff_app" not in source
