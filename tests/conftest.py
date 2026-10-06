"""Shared pytest configuration for environments without a desktop display."""

import os
import socket

import pytest


if not os.environ.get("DISPLAY"):
    # pystray otherwise selects its X11 backend during module import and aborts
    # collection before tests that do not need a tray icon can run.
    os.environ.setdefault("PYSTRAY_BACKEND", "dummy")


def _neo4j_required() -> bool:
    return bool(os.environ.get("DEVGRAPH_REQUIRE_NEO4J"))


@pytest.fixture(scope="session")
def _local_neo4j_reachable():
    try:
        socket.create_connection(("127.0.0.1", 7687), timeout=1).close()
    except OSError:
        if _neo4j_required():
            pytest.fail("Neo4j required but not reachable at 127.0.0.1:7687")
        return False
    return True


@pytest.fixture(autouse=True)
def _fail_fast_without_neo4j(_local_neo4j_reachable, monkeypatch):
    """With no local Neo4j, fail connectivity checks at once rather than after
    the engine's transient-error retries (seconds per live test, ~10 s on
    Windows), so the live tests skip quickly. Not applied when
    DEVGRAPH_REQUIRE_NEO4J is set: an unreachable database is then a failure."""
    if _local_neo4j_reachable or _neo4j_required():
        return
    from neo4j.exceptions import ServiceUnavailable

    from devgraph.graph.engine import GraphEngine

    def unreachable(self):
        raise ServiceUnavailable("no Neo4j listening on 127.0.0.1:7687")

    monkeypatch.setattr(GraphEngine, "verify_connectivity", unreachable)


def _fail_neo4j_skip(report, required):
    """Turn a skip whose reason mentions Neo4j into a failure when Neo4j is required."""
    if not (required and report.skipped and isinstance(report.longrepr, tuple)):
        return
    reason = str(report.longrepr[2])
    if "neo4j" in reason.lower():
        report.outcome = "failed"
        report.longrepr = f"Neo4j required (DEVGRAPH_REQUIRE_NEO4J) but the test skipped: {reason}"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    _fail_neo4j_skip(outcome.get_result(), _neo4j_required())


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
