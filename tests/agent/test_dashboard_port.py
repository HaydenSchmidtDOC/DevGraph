"""A dashboard port another process holds: the agent says so instead of failing
silently, the tray and `devgraph status` show it, and `devgraph dashboard`
never opens a browser on a port DevGraph doesn't serve."""

import http.server
import logging
import socket
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from devgraph.cli import main as cli_main
from devgraph.config.settings import Settings
from devgraph.dashboard.serving import DashboardFailure
from devgraph.dashboard.url import DASHBOARD_PAGE_MARKER, probe_dashboard
from tests.graph.test_engine_close import fake_engine


@pytest.fixture
def held_port():
    """A port another program holds, answering HTTP like something that isn't DevGraph."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    server.RequestHandlerClass.log_message = lambda *a, **k: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _headless(monkeypatch, port):
    engine, _driver = fake_engine(monkeypatch)
    settings = MagicMock(
        health_check_interval_s=60, dashboard_enabled=True, dashboard_host="127.0.0.1", dashboard_port=port
    )
    with patch("devgraph.agent.headless.get_settings", return_value=settings), \
         patch("devgraph.agent.headless.RepoRegistry"), \
         patch("devgraph.agent.headless.GraphEngine", return_value=engine), \
         patch("devgraph.agent.headless.WatcherManager"):
        from devgraph.agent.headless import HeadlessAgent

        return HeadlessAgent()


def test_a_held_port_is_a_clear_warning_not_a_silent_exit(monkeypatch, held_port, caplog):
    agent = _headless(monkeypatch, held_port)
    caplog.set_level(logging.WARNING)
    agent._run_dashboard()  # returns; a SystemExit would escape the test
    (record,) = [r for r in caplog.records if "dashboard" in r.getMessage()]
    message = record.getMessage()
    assert f"port {held_port}" in message and "DEVGRAPH_DASHBOARD_PORT" in message
    assert record.exc_info is None
    assert agent._dashboard_problem.detail == message


def test_the_dashboard_serves_and_identifies_itself(monkeypatch):
    port = _free_port()
    agent = _headless(monkeypatch, port)
    thread = threading.Thread(target=agent._run_dashboard, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while probe_dashboard(url) != "devgraph" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert probe_dashboard(url) == "devgraph"
        assert agent._dashboard_problem is None
    finally:
        agent._dashboard_server.should_exit = True
        thread.join(5)


def test_probe_tells_another_program_from_nothing(held_port):
    assert probe_dashboard(f"http://127.0.0.1:{held_port}") == "other"
    assert probe_dashboard(f"http://127.0.0.1:{_free_port()}") == "none"


def test_the_tray_tooltip_names_the_held_port():
    with patch("devgraph.agent.tray.get_settings", return_value=MagicMock()), \
         patch("devgraph.agent.tray.RepoRegistry"), \
         patch("devgraph.agent.tray.GraphEngine"), \
         patch("devgraph.agent.tray.WatcherManager"):
        from devgraph.agent.tray import TrayApp

        app = TrayApp()
    assert app._status_text() == "DevGraph (ok)"
    app._dashboard_problem = DashboardFailure("dashboard port 8765 in use", "dashboard port 8765 on 127.0.0.1 is ...")
    assert app._status_text() == "DevGraph (ok; dashboard port 8765 in use)"


def _settings(tmp_path, port):
    return Settings(registry_db_path=tmp_path / "registry.db", dashboard_host="127.0.0.1", dashboard_port=port)


def test_devgraph_dashboard_refuses_a_port_another_program_holds(tmp_path, held_port):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path, held_port)), \
         patch.object(cli_main, "_tray_liveness_text", return_value="running"), \
         patch.object(cli_main.webbrowser, "open") as browser:
        result = CliRunner().invoke(cli_main.app, ["dashboard"])
    assert result.exit_code == 1
    browser.assert_not_called()
    assert f"{held_port}" in result.stdout and "DEVGRAPH_DASHBOARD_PORT" in result.stdout


def test_devgraph_status_names_a_held_dashboard_port(tmp_path, held_port):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path, held_port)), \
         patch.object(cli_main, "GraphEngine") as engine_cls:
        engine_cls.return_value.verify_connectivity.return_value = None
        engine_cls.return_value.index_format.return_value = None
        result = CliRunner().invoke(cli_main.app, ["status"])
    assert "Dashboard" in result.stdout
    assert f"port {held_port} is held by another program" in result.stdout


@pytest.fixture
def old_devgraph_port():
    """An older DevGraph agent: its page at `/`, but no `/api/health` (a 404)."""
    page = (Path(__file__).resolve().parents[2] / "devgraph" / "dashboard" / "static" / "index.html").read_bytes()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(page)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_probe_recognises_an_older_devgraph_without_the_health_route(old_devgraph_port):
    assert DASHBOARD_PAGE_MARKER.encode() in (
        Path(__file__).resolve().parents[2] / "devgraph" / "dashboard" / "static" / "index.html"
    ).read_bytes()
    assert probe_dashboard(f"http://127.0.0.1:{old_devgraph_port}") == "outdated"


def test_status_says_to_restart_an_older_devgraph_not_to_change_the_port(tmp_path, old_devgraph_port):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path, old_devgraph_port)), \
         patch.object(cli_main, "GraphEngine") as engine_cls:
        engine_cls.return_value.index_format.return_value = None
        result = CliRunner().invoke(cli_main.app, ["status"], terminal_width=400)
    dashboard = result.stdout.split("Dashboard", 1)[1]
    assert "older DevGraph" in dashboard and "restart" in dashboard
    assert "DEVGRAPH_DASHBOARD_PORT" not in dashboard


def test_devgraph_dashboard_opens_an_older_devgraph_and_says_to_restart(tmp_path, old_devgraph_port):
    with patch.object(cli_main, "get_settings", return_value=_settings(tmp_path, old_devgraph_port)), \
         patch.object(cli_main, "_tray_liveness_text", return_value="running"), \
         patch.object(cli_main.webbrowser, "open") as browser:
        result = CliRunner().invoke(cli_main.app, ["dashboard"], terminal_width=400)
    assert result.exit_code == 0, result.output
    browser.assert_called_once()
    assert "older DevGraph" in result.output and "DEVGRAPH_DASHBOARD_PORT" not in result.output


def test_a_second_agent_on_the_same_port_gets_the_port_in_use_failure(monkeypatch):
    """SO_REUSEADDR lets two binds succeed on Linux; only one may listen."""
    port = _free_port()
    first = _headless(monkeypatch, port)
    thread = threading.Thread(target=first._run_dashboard, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while probe_dashboard(f"http://127.0.0.1:{port}") != "devgraph" and time.monotonic() < deadline:
            time.sleep(0.05)
        second = _headless(monkeypatch, port)
        second._run_dashboard()
        assert second._dashboard_problem is not None
        assert second._dashboard_problem.summary == f"dashboard port {port} in use"
    finally:
        first._dashboard_server.should_exit = True
        thread.join(5)


def test_bind_listens_and_matches_asyncio_address_rules():
    from devgraph.dashboard.serving import _bind

    sockets = _bind("localhost", 0)
    try:
        assert all(_is_listening(s) for s in sockets)
        assert {s.family for s in sockets} <= {socket.AF_INET, socket.AF_INET6}
        assert socket.AF_INET in {s.family for s in sockets}
        assert len({s.getsockname()[1] for s in sockets}) == 1  # one port for every address
    finally:
        for s in sockets:
            s.close()
    if socket.has_ipv6:
        try:
            (wildcard,) = _bind("::", 0)
        except OSError:
            pytest.skip("no IPv6 on this host")
        with wildcard:
            assert wildcard.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only socket option")
def test_bind_takes_the_port_exclusively_on_windows():
    from devgraph.dashboard.serving import _bind

    (sock,) = _bind("127.0.0.1", 0)
    with sock:
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE) == 1


def _is_listening(sock) -> bool:
    return bool(sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)) if hasattr(socket, "SO_ACCEPTCONN") else True
