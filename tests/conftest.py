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


@pytest.fixture(autouse=True)
def _isolate_global_tools_store(tmp_path, monkeypatch):
    """Keep the global tools store away from the user's real ~/.devgraph."""
    from devgraph.config import global_tools

    monkeypatch.setattr(global_tools, "_default_path", lambda: tmp_path / "no-global" / global_tools.GLOBAL_TOOLS_FILENAME)


@pytest.fixture(autouse=True)
def _wide_cli_console(monkeypatch):
    """Keep Rich output from wrapping at the terminal width (e.g. long tmp paths)."""
    monkeypatch.setenv("COLUMNS", "1000")
    from devgraph.cli import main

    # The CLI console is created at import time, so it has already read COLUMNS.
    monkeypatch.setattr(main.console, "_width", 1000)
