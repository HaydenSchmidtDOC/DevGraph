"""Shared pytest configuration for environments without a desktop display."""

import os

import pytest


if not os.environ.get("DISPLAY"):
    # pystray otherwise selects its X11 backend during module import and aborts
    # collection before tests that do not need a tray icon can run.
    os.environ.setdefault("PYSTRAY_BACKEND", "dummy")


@pytest.fixture(autouse=True)
def _isolate_project_switch_registry(tmp_path, monkeypatch):
    """Keep the project config switch lookup away from the user's real registry."""
    from devgraph.config import project_switch

    missing = tmp_path / "no-registry" / "registry.db"
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: missing)
