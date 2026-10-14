"""Tray startup on Linux: latin-1-safe icon text, the headless fallback when no
tray can be shown, and `devgraph tray start`/`status` reporting what really
happened.

The tray crashed on every Linux desktop: pystray's X11 backend encodes the
tooltip as latin-1, and "DevGraph (preparing graph indexes…)" has an ellipsis.
`tray start` still printed OK (it only spawned the process), and with no
display at all every CLI command died on an Xlib traceback, because importing
the agent package imported pystray. None of these tests need a display.
"""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from devgraph.agent import lifecycle

REPO_ROOT = Path(__file__).resolve().parents[2]


def _no_display_env():
    return {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY", "PYSTRAY_BACKEND")}


# --- latin-1-safe text --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["DevGraph (preparing graph indexes…)", "DevGraph (ok; dashboard port 8765 in use — see logs)", "‘x’ “y” ✓ 日本"],
)
def test_pystray_text_is_latin1_encodable(text):
    from devgraph.agent.tray import pystray_text

    out = pystray_text(text)
    out.encode("latin-1")  # what pystray's X11 set_wm_name does
    assert len(out) >= len(text)


def test_pystray_text_keeps_meaning_of_common_punctuation():
    from devgraph.agent.tray import pystray_text

    assert pystray_text("preparing graph indexes…") == "preparing graph indexes..."
    assert pystray_text("a — b") == "a - b"


def test_every_string_handed_to_pystray_is_latin1_safe():
    """The icon's title and every menu label, in every state the tray can show."""
    from devgraph.agent import tray

    app = tray.TrayApp.__new__(tray.TrayApp)
    app._preparing, app._paused, app._healthy = True, False, True
    app._sync = SimpleNamespace(running=False)
    app._dashboard_problem = SimpleNamespace(summary="dashboard port 8765 in use — another program")
    captured = {}

    class FakeIcon:
        def __init__(self, name, icon, title, menu):
            captured["title"], captured["menu"] = title, menu

        def run(self):
            pass

    class FakeMenuItem:
        def __init__(self, text, action):
            captured.setdefault("items", []).append(text)

    fake = SimpleNamespace(Icon=FakeIcon, Menu=lambda *items: items, MenuItem=FakeMenuItem)
    with patch.object(tray, "pystray", fake), patch.object(tray.threading, "Thread"):
        app._settings = SimpleNamespace(dashboard_enabled=False)
        app._stop_event = SimpleNamespace(set=lambda: None)
        app.start()
    captured["title"].encode("latin-1")
    for item in captured["items"]:
        (item(None) if callable(item) else item).encode("latin-1")


# --- no display: headless fallback, no traceback ------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="X11 display handling is Linux-only")
def test_the_cli_imports_without_a_display():
    result = subprocess.run(
        [sys.executable, "-c", "import devgraph.cli.main, devgraph.mcp.server, devgraph.agent.tray"],
        env=_no_display_env(), capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="X11 display handling is Linux-only")
def test_tray_ui_problem_names_the_missing_display():
    result = subprocess.run(
        [sys.executable, "-c", "from devgraph.agent import lifecycle; print(lifecycle.tray_ui_problem())"],
        env=_no_display_env(), capture_output=True, text=True, timeout=120, cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() not in ("", "None")


def test_tray_main_runs_the_headless_agent_when_no_tray_backend_loads():
    from devgraph.agent import headless, tray

    ran = []
    with patch.object(tray, "pystray", None), \
         patch.object(tray, "_PYSTRAY_PROBLEM", "DisplayNameError: Bad display name ''"), \
         patch.object(tray, "_configure_logging"), \
         patch.object(headless, "main", lambda: ran.append("headless")), \
         patch.object(tray, "TrayApp", side_effect=AssertionError("no tray without a backend")):
        tray.main()
    assert ran == ["headless"]


# --- waiting for the first heartbeat --------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    settings = SimpleNamespace(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    monkeypatch.setattr(lifecycle.time, "sleep", lambda s: None)
    return tmp_path


def _beat(home, when):
    (home / "tray_heartbeat.txt").write_text(when.isoformat(), encoding="utf-8")


def test_wait_for_tray_reports_running_once_a_fresh_heartbeat_lands(home, monkeypatch):
    since = datetime.now(timezone.utc)
    monkeypatch.setattr(lifecycle, "pid_is_running", lambda pid: True)
    _beat(home, since + timedelta(seconds=1))
    assert lifecycle.wait_for_tray(4242, since, timeout_s=5) == "running"


def test_wait_for_tray_ignores_a_heartbeat_from_an_earlier_run(home, monkeypatch):
    since = datetime.now(timezone.utc)
    monkeypatch.setattr(lifecycle, "pid_is_running", lambda pid: True)
    _beat(home, since - timedelta(minutes=5))
    assert lifecycle.wait_for_tray(4242, since, timeout_s=0.05) == "timeout"


def test_wait_for_tray_reports_an_exit_during_startup(home, monkeypatch):
    since = datetime.now(timezone.utc)
    monkeypatch.setattr(lifecycle, "pid_is_running", lambda pid: False)
    assert lifecycle.wait_for_tray(4242, since, timeout_s=5) == "exited"


def test_wait_for_tray_reports_a_crash_right_after_the_first_beat(home, monkeypatch):
    """The latin-1 crash: the health thread beats once, then the icon kills the process."""
    since = datetime.now(timezone.utc)
    alive = iter([True, False])
    monkeypatch.setattr(lifecycle, "pid_is_running", lambda pid: next(alive, False))
    _beat(home, since + timedelta(seconds=1))
    assert lifecycle.wait_for_tray(4242, since, timeout_s=5) == "exited"


# --- the CLI ---------------------------------------------------------------------


@pytest.fixture
def cli(home, monkeypatch):
    from devgraph.cli import main as cli_main

    monkeypatch.setattr(cli_main, "get_settings", lambda: SimpleNamespace(registry_db_path=home / "registry.sqlite3"))
    return cli_main


def test_tray_start_waits_for_the_heartbeat_before_ok(cli, monkeypatch):
    monkeypatch.setattr(lifecycle, "tray_ui_problem", lambda: None)
    monkeypatch.setattr(lifecycle, "start_tray_if_not_running", lambda: 4242)
    monkeypatch.setattr(lifecycle, "wait_for_tray", lambda pid, since, **kw: "running")
    result = CliRunner().invoke(cli.app, ["tray", "start"])
    assert result.exit_code == 0, result.stdout
    assert "[OK]" in result.stdout and "4242" in result.stdout


@pytest.mark.parametrize("outcome, words", [("exited", "exited"), ("timeout", "no heartbeat")])
def test_tray_start_reports_a_failed_start(cli, monkeypatch, outcome, words):
    monkeypatch.setattr(lifecycle, "tray_ui_problem", lambda: None)
    monkeypatch.setattr(lifecycle, "start_tray_if_not_running", lambda: 4242)
    monkeypatch.setattr(lifecycle, "wait_for_tray", lambda pid, since, **kw: outcome)
    result = CliRunner().invoke(cli.app, ["tray", "start"])
    assert result.exit_code == 1
    assert "[OK]" not in result.stdout
    assert words in result.stdout and "devgraph logs" in result.stdout


def test_tray_start_says_when_it_falls_back_to_the_headless_agent(cli, monkeypatch):
    monkeypatch.setattr(lifecycle, "tray_ui_problem", lambda: "DisplayNameError: Bad display name ''")
    monkeypatch.setattr(lifecycle, "start_tray_if_not_running", lambda: 4242)
    monkeypatch.setattr(lifecycle, "wait_for_tray", lambda pid, since, **kw: "running")
    result = CliRunner().invoke(cli.app, ["tray", "start"])
    assert result.exit_code == 0, result.stdout
    assert "headless" in result.stdout and "Traceback" not in result.stdout


def test_tray_status_exits_non_zero_when_the_agent_is_down(cli, monkeypatch):
    monkeypatch.setattr(lifecycle, "read_tray_pid", lambda: None)
    result = CliRunner().invoke(cli.app, ["tray", "status"])
    assert result.exit_code == 1
    assert "not running" in result.stdout


def test_tray_status_exits_zero_when_the_agent_is_up(cli, monkeypatch):
    monkeypatch.setattr(lifecycle, "read_tray_pid", lambda: 4242)
    monkeypatch.setattr(lifecycle, "pid_is_running", lambda pid: True)
    result = CliRunner().invoke(cli.app, ["tray", "status"])
    assert result.exit_code == 0
    assert "running" in result.stdout
