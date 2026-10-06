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
    # Typer forces a colour terminal under GITHUB_ACTIONS/FORCE_COLOR, which
    # splits asserted option names with ANSI codes; let Rich auto-detect instead.
    from typer import rich_utils

    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", None)


@pytest.fixture(autouse=True)
def _isolate_project_trust_registry(tmp_path, monkeypatch):
    """The real trust lookup, against an empty registry in tmp: every project tools
    file is untrusted unless a test approves it or asks for `trusted_project_tools`.
    Keeps the lookup away from ~/.devgraph."""
    from devgraph.config import project_trust

    monkeypatch.setattr(project_trust, "_registry_db_path", lambda: tmp_path / "no-registry" / "registry.db")


@pytest.fixture
def trusted_project_tools(monkeypatch):
    """Treat every project tools file as trusted. For tests of serving that predate
    the per-repository opt-in; apply it with `pytestmark = pytest.mark.usefixtures(...)`."""
    from devgraph.config import project_trust

    monkeypatch.setattr(project_trust, "project_tools_trust", lambda repo_root, data: "trusted")


@pytest.fixture
def real_project_trust():
    """The trust module, with the real lookup (the default); a test points
    `_registry_db_path` at its own registry."""
    from devgraph.config import project_trust

    return project_trust
