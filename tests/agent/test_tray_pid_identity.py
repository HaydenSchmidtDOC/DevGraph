"""A stale `tray.pid` must never adopt or signal an unrelated process.

After a reboot, a crash or a kill the file stays behind, and its pid can name
some other process of the user's. These tests start a real unrelated process,
point `tray.pid` at it in each way that can happen, and check that DevGraph
reports the agent as not running, starts a new one, and never kills it.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from devgraph.agent import lifecycle


@pytest.fixture
def home(tmp_path, monkeypatch):
    settings = SimpleNamespace(registry_db_path=tmp_path / "registry.sqlite3")
    monkeypatch.setattr(lifecycle, "get_settings", lambda: settings)
    return tmp_path


def _sleeper(*extra_args):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", *extra_args])


@pytest.fixture
def unrelated():
    proc = _sleeper()
    yield proc
    proc.kill()
    proc.wait()


def _legacy(pid):
    """A file from before the start identity was recorded: the pid alone."""
    lifecycle.tray_pid_path().write_text(str(pid), encoding="utf-8")


def _reused(pid):
    """The pid was reused: the recorded start identity is another process's."""
    lifecycle.tray_pid_path().write_text(f"{pid}\nstart-of-an-earlier-process\n", encoding="utf-8")


def _same_start_other_command(pid):
    """Even with the right start identity, a process not running the agent is not it."""
    lifecycle.record_tray_pid(pid)


RECORDS = [
    _legacy,
    _reused,
    pytest.param(
        _same_start_other_command,
        marks=pytest.mark.skipif(sys.platform == "win32", reason="the command line is not read on Windows"),
    ),
]


@pytest.mark.parametrize("write_record", RECORDS)
def test_status_says_not_running_and_drops_the_stale_record(home, unrelated, write_record):
    from devgraph.cli import main as cli_main

    write_record(unrelated.pid)
    result = CliRunner().invoke(cli_main.app, ["tray", "status"])
    assert result.exit_code == 1
    assert "not running" in result.stdout
    assert not lifecycle.tray_pid_path().exists()
    assert unrelated.poll() is None


@pytest.mark.parametrize("write_record", RECORDS)
def test_stop_never_signals_the_unrelated_process(home, unrelated, write_record):
    from devgraph.cli import main as cli_main

    write_record(unrelated.pid)
    result = CliRunner().invoke(cli_main.app, ["tray", "stop"])
    assert result.exit_code == 0, result.stdout
    assert "not running" in result.stdout

    write_record(unrelated.pid)
    assert lifecycle.stop_tray_if_last_holder(holder_pid=999_999_999) is False

    with pytest.raises(subprocess.TimeoutExpired):
        unrelated.wait(timeout=0.5)


@pytest.mark.parametrize("write_record", RECORDS)
def test_start_proceeds_past_the_stale_record(home, unrelated, write_record, monkeypatch):
    write_record(unrelated.pid)
    new_agent = SimpleNamespace(pid=unrelated.pid + 1)
    monkeypatch.setattr(lifecycle, "resolve_venv_python", lambda: Path(sys.executable))
    monkeypatch.setattr(lifecycle, "resolve_repo_root", lambda: home)
    with patch.object(lifecycle.subprocess, "Popen", return_value=new_agent) as popen:
        assert lifecycle.start_tray_if_not_running() == new_agent.pid
    popen.assert_called_once()
    assert unrelated.poll() is None


def test_the_agent_it_recorded_is_running_and_stops(home):
    """The genuine article still works: recorded at start, reported, stopped."""
    from devgraph.cli import main as cli_main

    agent = _sleeper("-m", lifecycle.AGENT_MODULE)
    try:
        lifecycle.record_tray_pid(agent.pid)
        status = CliRunner().invoke(cli_main.app, ["tray", "status"])
        assert status.exit_code == 0, status.stdout
        assert str(agent.pid) in status.stdout

        stop = CliRunner().invoke(cli_main.app, ["tray", "stop"])
        assert stop.exit_code == 0, stop.stdout
        assert "Sent stop signal" in stop.stdout
        agent.wait(timeout=10)
        assert not lifecycle.tray_pid_path().exists()
    finally:
        agent.kill()
        agent.wait()


def test_the_agent_forgets_only_its_own_record(home):
    lifecycle.tray_pid_path().write_text("4242\nstart-4242\n", encoding="utf-8")
    lifecycle.forget_tray_pid(4343)
    assert lifecycle.tray_pid_path().exists()
    lifecycle.forget_tray_pid(4242)
    assert not lifecycle.tray_pid_path().exists()


# --- upgrades, unreadable identities, races ------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the command line is not read on Windows")
def test_a_pre_upgrade_record_of_the_running_agent_is_adopted_and_stoppable(home):
    """`devgraph update` stops the agent a pre-upgrade `tray.pid` names; were that
    record dropped as stale, the old agent would keep running beside a new one."""
    from devgraph.cli import main as cli_main

    agent = _sleeper("-m", lifecycle.AGENT_MODULE)
    try:
        _legacy(agent.pid)
        assert lifecycle.read_tray_pid() == agent.pid
        # rewritten with its identity
        assert lifecycle.tray_pid_path().read_text(encoding="utf-8").splitlines() == [
            str(agent.pid), lifecycle.process_identity(agent.pid)
        ]
        stop = CliRunner().invoke(cli_main.app, ["tray", "stop"])
        assert "Sent stop signal" in stop.stdout
        agent.wait(timeout=10)
    finally:
        agent.kill()
        agent.wait()


def test_on_windows_a_pre_upgrade_record_counts_only_with_a_fresh_heartbeat(home, monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(lifecycle, "process_command", lambda pid: None)  # as on Windows
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: "win:132" if pid == 4242 else None)
    beat = home / "tray_heartbeat.txt"

    beat.write_text((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(), encoding="utf-8")
    _legacy(4242)
    assert lifecycle.read_tray_pid() is None
    assert not lifecycle.tray_pid_path().exists()

    beat.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    _legacy(4242)
    assert lifecycle.read_tray_pid() == 4242
    assert lifecycle.tray_pid_path().read_text(encoding="utf-8") == "4242\nwin:132\n"


@pytest.mark.skipif(sys.platform == "win32", reason="the command line is not read on Windows")
def test_an_agent_whose_identity_could_not_be_read_is_still_recognised(home, monkeypatch, caplog):
    agent = _sleeper("-m", lifecycle.AGENT_MODULE)
    try:
        with patch.object(lifecycle, "process_identity", return_value=None):
            lifecycle.record_tray_pid(agent.pid)
        assert "start time" in caplog.text
        assert lifecycle.read_tray_pid() == agent.pid
    finally:
        agent.kill()
        agent.wait()


def test_a_stale_check_never_deletes_a_record_written_meanwhile(home, monkeypatch):
    """A starter can replace `tray.pid` between a reader's check and its delete."""
    _reused(4242)
    fresh = "5151\nstart-5151\n"

    def identity(pid):
        if pid == 4242:  # the reader is checking the stale record: a starter writes now
            lifecycle.tray_pid_path().write_text(fresh, encoding="utf-8")
            return None
        return "start-5151"

    monkeypatch.setattr(lifecycle, "process_identity", identity)
    monkeypatch.setattr(lifecycle, "pid_is_running", lambda pid: False)
    assert lifecycle.read_tray_pid() is None
    assert lifecycle.tray_pid_path().read_text(encoding="utf-8") == fresh


def test_ps_runs_with_a_fixed_locale_and_time_zone(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="Tue Oct 14 01:02:03 2026\n")

    monkeypatch.setattr(lifecycle.subprocess, "run", fake_run)
    assert lifecycle._ps_field(4242, "lstart") == "ps:Tue Oct 14 01:02:03 2026"
    assert seen["env"]["LC_ALL"] == "C" and seen["env"]["TZ"] == "UTC0"
