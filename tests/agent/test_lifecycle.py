"""Tests for devgraph/agent/lifecycle.py's tray-holder refcounting.

Multiple MCP clients can each spawn their own devgraph.mcp.server process;
the shared tray app should only actually stop once none of them are still
relying on it, not the moment any single one exits. These tests exercise
that bookkeeping directly against a temp directory — no real tray process,
no live Neo4j.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devgraph.agent import lifecycle


@pytest.fixture
def settings(tmp_path):
    mock_settings = MagicMock()
    mock_settings.registry_db_path = tmp_path / "registry.sqlite3"
    with patch.object(lifecycle, "get_settings", return_value=mock_settings):
        yield mock_settings


def test_no_holders_initially(settings):
    assert lifecycle.has_tray_holders() is False


def test_register_and_unregister_holder(settings):
    lifecycle.register_tray_holder(holder_pid=os.getpid())
    assert lifecycle.has_tray_holders() is True

    lifecycle.unregister_tray_holder(holder_pid=os.getpid())
    assert lifecycle.has_tray_holders() is False


def test_multiple_holders_only_clears_after_all_unregister(settings):
    # os.getpid() (this test process) is always alive, so use it plus a
    # second live PID (also this process, registered twice under itself is
    # not meaningful for a real refcount, so simulate a second holder with a
    # PID we control the liveness of via pid_is_running).
    real_pid = os.getpid()
    fake_other_pid = 555555

    with patch.object(lifecycle, "pid_is_running", side_effect=lambda pid: pid in (real_pid, fake_other_pid)):
        lifecycle.register_tray_holder(holder_pid=real_pid)
        lifecycle.register_tray_holder(holder_pid=fake_other_pid)
        assert lifecycle.has_tray_holders() is True

        lifecycle.unregister_tray_holder(holder_pid=real_pid)
        assert lifecycle.has_tray_holders() is True  # fake_other_pid still holding

        lifecycle.unregister_tray_holder(holder_pid=fake_other_pid)
        assert lifecycle.has_tray_holders() is False


def test_stale_holder_marker_from_crashed_process_is_ignored(settings):
    """A holder that crashed without calling unregister leaves a marker file
    behind; has_tray_holders() should treat it as not-holding once the PID
    is confirmed dead, and clean up the stale marker."""
    dead_pid = 424242
    with patch.object(lifecycle, "pid_is_running", side_effect=lambda pid: pid != dead_pid):
        lifecycle.register_tray_holder(holder_pid=dead_pid)
        assert lifecycle.has_tray_holders() is False
        # stale marker should have been swept
        assert not (lifecycle._holders_dir() / str(dead_pid)).exists()


def test_clear_tray_holders_removes_all(settings):
    lifecycle.register_tray_holder(holder_pid=111)
    lifecycle.register_tray_holder(holder_pid=222)
    with patch.object(lifecycle, "pid_is_running", return_value=True):
        assert lifecycle.has_tray_holders() is True
        lifecycle.clear_tray_holders()
        assert lifecycle.has_tray_holders() is False


def test_stop_tray_if_last_holder_keeps_tray_alive_with_other_holders(settings):
    real_pid = os.getpid()
    other_pid = 333333
    tray_pid = 987654

    lifecycle.tray_pid_path().parent.mkdir(parents=True, exist_ok=True)
    lifecycle.tray_pid_path().write_text(str(tray_pid), encoding="utf-8")

    with patch.object(lifecycle, "pid_is_running", side_effect=lambda pid: pid in (real_pid, other_pid, tray_pid)), \
         patch.object(lifecycle.os, "kill") as fake_kill:
        lifecycle.register_tray_holder(holder_pid=real_pid)
        lifecycle.register_tray_holder(holder_pid=other_pid)

        stopped = lifecycle.stop_tray_if_last_holder(holder_pid=real_pid)

        assert stopped is False
        fake_kill.assert_not_called()
        assert lifecycle.tray_pid_path().exists()  # tray left running


def test_stop_tray_if_last_holder_stops_tray_when_last_one_leaves(settings):
    real_pid = os.getpid()
    tray_pid = 987654

    lifecycle.tray_pid_path().parent.mkdir(parents=True, exist_ok=True)
    lifecycle.tray_pid_path().write_text(str(tray_pid), encoding="utf-8")

    with patch.object(lifecycle, "pid_is_running", side_effect=lambda pid: pid in (real_pid, tray_pid)), \
         patch.object(lifecycle.os, "kill") as fake_kill:
        lifecycle.register_tray_holder(holder_pid=real_pid)

        stopped = lifecycle.stop_tray_if_last_holder(holder_pid=real_pid)

        assert stopped is True
        fake_kill.assert_called_once_with(tray_pid, lifecycle.signal.SIGTERM)
        assert not lifecycle.tray_pid_path().exists()
