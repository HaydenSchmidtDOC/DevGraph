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


_REAL_PROJECT_TOOLS_TRUST = None


@pytest.fixture(autouse=True)
def _trust_project_tools_by_default(tmp_path, monkeypatch):
    """Treat every project tools file as trusted, so tests written before the
    per-repository opt-in keep exercising serving. Tests of the opt-in itself
    use `real_project_trust`. The registry lookup stays away from ~/.devgraph."""
    global _REAL_PROJECT_TOOLS_TRUST
    from devgraph.config import project_trust

    if _REAL_PROJECT_TOOLS_TRUST is None:
        _REAL_PROJECT_TOOLS_TRUST = project_trust.project_tools_trust
    monkeypatch.setattr(project_trust, "_registry_db_path", lambda: tmp_path / "no-registry" / "registry.db")
    monkeypatch.setattr(project_trust, "project_tools_trust", lambda repo_root, data: "trusted")


@pytest.fixture
def real_project_trust(monkeypatch):
    """The real trust lookup (undoing `_trust_project_tools_by_default`); returns the module
    so a test can point `_registry_db_path` at its own registry."""
    from devgraph.config import project_trust

    monkeypatch.setattr(project_trust, "project_tools_trust", _REAL_PROJECT_TOOLS_TRUST)
    return project_trust
