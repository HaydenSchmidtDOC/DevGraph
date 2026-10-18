"""Tests for `devgraph.agent.headless._configure_logging`.

`HeadlessAgent` is the tray app without the tray UI, so it must land its log
records in the same place: the configured `settings.log_file`, which is what
`devgraph logs` reads. These tests exercise the logging wiring directly — no
registry, no watcher, no live Neo4j.
"""

import logging
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from logging.handlers import RotatingFileHandler
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

try:  # pystray binds to an X display as a side effect of import (see
    # tests/conftest.py); `devgraph.agent.__init__` pulls it in transitively.
    import pystray  # noqa: F401
except Exception:  # pragma: no cover — depends on the host having a display
    sys.modules["pystray"] = types.ModuleType("pystray")

from devgraph.agent import headless
from devgraph.cli import main as cli_main
from devgraph.config.settings import LOG_BACKUP_COUNT, LOG_MAX_BYTES

_EXPECTED_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@contextmanager
def _isolated_root_logger():
    """Run with an empty root logger, restoring pytest's handlers afterwards.

    `logging.basicConfig` is a no-op while the root logger already has
    handlers, and pytest's logging plugin attaches its own for the duration of
    each test, so the handlers have to be cleared inside the test body.
    """
    root = logging.root
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved_handlers
        root.level = saved_level


def test_configure_logging_targets_the_settings_log_file(tmp_path):
    log_path = tmp_path / "logs" / "devgraph.log"
    settings = MagicMock()
    settings.log_file = log_path

    with _isolated_root_logger() as root, \
         patch.object(headless, "get_settings", return_value=settings):
        headless._configure_logging()

        assert log_path.parent.is_dir()  # parent created when missing
        assert root.level == logging.INFO

        file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 1
        handler = file_handlers[0]
        assert Path(handler.baseFilename) == Path(os.path.abspath(log_path))
        assert handler.formatter._fmt == _EXPECTED_FORMAT

        logging.getLogger("devgraph.agent.headless").info("headless log line")
        handler.flush()

    assert "headless log line" in log_path.read_text(encoding="utf-8")


def test_configure_logging_is_a_noop_without_a_log_file():
    settings = MagicMock()
    settings.log_file = None

    with _isolated_root_logger() as root, \
         patch.object(headless, "get_settings", return_value=settings):
        headless._configure_logging()

        assert root.handlers == []


def test_main_configures_logging_before_starting_the_agent():
    calls = []
    agent = MagicMock()
    agent.start.side_effect = lambda: calls.append("start")

    with patch.object(headless, "_configure_logging", side_effect=lambda: calls.append("configure")), \
         patch.object(headless, "HeadlessAgent", return_value=agent), \
         patch.object(headless.signal, "signal"):
        headless.main()

    assert calls == ["configure", "start"]


def test_main_starts_the_agent_when_no_log_file_is_configured():
    settings = MagicMock()
    settings.log_file = None
    agent = MagicMock()

    with _isolated_root_logger() as root, \
         patch.object(headless, "get_settings", return_value=settings), \
         patch.object(headless, "HeadlessAgent", return_value=agent), \
         patch.object(headless.signal, "signal"):
        headless.main()

        assert root.handlers == []

    agent.start.assert_called_once_with()


@pytest.mark.parametrize("module", ["headless", "tray"])
def test_the_agent_log_rotates_with_a_size_cap_and_a_few_backups(tmp_path, module):
    """One agent log line per live batch adds up; the file must not grow forever."""
    if module == "tray":
        from devgraph.agent import tray as agent
    else:
        agent = headless
    log_path = tmp_path / "devgraph.log"
    settings = MagicMock()
    settings.log_file = log_path

    with _isolated_root_logger() as root, \
         patch.object(agent, "get_settings", return_value=settings), \
         patch.object(agent, "LOG_MAX_BYTES", 2000):
        agent._configure_logging()
        [handler] = root.handlers
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == 2000 and handler.backupCount == LOG_BACKUP_COUNT
        for i in range(200):
            logging.getLogger("devgraph.agent").info("batch %03d done", i)
        handler.flush()

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["devgraph.log"] + [f"devgraph.log.{n}" for n in range(1, LOG_BACKUP_COUNT + 1)]
    assert all(p.stat().st_size <= 2000 for p in tmp_path.iterdir())
    assert "batch 199 done" in log_path.read_text(encoding="utf-8")
    assert LOG_MAX_BYTES == 5 * 1024 * 1024 and LOG_BACKUP_COUNT == 3


def test_devgraph_logs_reads_across_a_rollover(tmp_path):
    """Just after a rollover the current file holds a line or two; the
    requested tail comes from the newest older file too, in order."""
    log_path = tmp_path / "devgraph.log"
    (tmp_path / "devgraph.log.2").write_text("2026-01-01 INFO x: ancient\n", encoding="utf-8")
    (tmp_path / "devgraph.log.1").write_text(
        "".join(f"2026-01-02 INFO x: old {i}\n" for i in range(5)), encoding="utf-8"
    )
    log_path.write_text("2026-01-03 INFO x: new 0\n", encoding="utf-8")
    settings = MagicMock()
    settings.log_file = log_path

    with patch.object(cli_main, "get_settings", return_value=settings):
        result = CliRunner().invoke(cli_main.app, ["logs", "-n", "3"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "2026-01-02 INFO x: old 3", "2026-01-02 INFO x: old 4", "2026-01-03 INFO x: new 0",
    ]


def test_a_rollover_the_os_refuses_loses_no_record_and_moves_no_backup(tmp_path, monkeypatch):
    """On Windows another process holding the file open makes its rename fail."""
    from devgraph.agent import log_file

    log_path = tmp_path / "devgraph.log"
    (tmp_path / "devgraph.log.1").write_text("older\n", encoding="utf-8")
    now = [1000.0]
    monkeypatch.setattr(log_file.time, "monotonic", lambda: now[0])
    real_replace = os.replace
    attempts = []

    def held(src, dst):
        if os.fspath(src) == str(log_path):
            attempts.append(dst)
            raise PermissionError("the file is in use by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(log_file.os, "replace", held)
    handler = log_file.AgentLogHandler(log_path, maxBytes=200, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("devgraph.test.rollover")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for i in range(40):
            logger.warning("record %02d of the batch", i)
        assert len(attempts) == 1  # tried once, then not again until retry_s has passed
        assert sorted(p.name for p in tmp_path.iterdir()) == ["devgraph.log", "devgraph.log.1"]
        assert (tmp_path / "devgraph.log.1").read_text(encoding="utf-8") == "older\n"
        written = log_path.read_text(encoding="utf-8").splitlines()
        assert written == [f"record {i:02d} of the batch" for i in range(40)]

        # Once the file is free and retry_s has passed, it rolls over as usual.
        monkeypatch.setattr(log_file.os, "replace", real_replace)
        now[0] += handler.retry_s + 1
        logger.warning("after the retry")
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["devgraph.log", "devgraph.log.1", "devgraph.log.2"]
    assert (tmp_path / "devgraph.log.2").read_text(encoding="utf-8") == "older\n"
    assert log_path.read_text(encoding="utf-8") == "after the retry\n"


def test_devgraph_logs_skips_a_file_that_vanishes_under_it(tmp_path, monkeypatch):
    """A rollover can rename a file between the check and the read."""
    log_path = tmp_path / "devgraph.log"
    log_path.write_text("2026-01-03 INFO x: new 0\n", encoding="utf-8")
    (tmp_path / "devgraph.log.1").write_text("2026-01-02 INFO x: old\n", encoding="utf-8")
    real_read = Path.read_text

    def racing(self, *args, **kwargs):
        if self.name == "devgraph.log.1":
            raise FileNotFoundError(self)
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", racing)
    settings = MagicMock()
    settings.log_file = log_path
    with patch.object(cli_main, "get_settings", return_value=settings):
        result = CliRunner().invoke(cli_main.app, ["logs"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["2026-01-03 INFO x: new 0"]


def _failing_replace(monkeypatch, log_file, fails):
    """Make os.replace raise PermissionError for (source suffix, target suffix)
    pairs in `fails`, a set the test can change; suffixes after `devgraph.log`."""
    real = os.replace

    def replace(src, dst):
        pair = (os.fspath(src).rsplit("devgraph.log", 1)[1], os.fspath(dst).rsplit("devgraph.log", 1)[1])
        if pair in fails:
            raise PermissionError(f"in use: {pair}")
        return real(src, dst)

    monkeypatch.setattr(log_file.os, "replace", replace)


def _logger(handler, name):
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(name)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


def _all_lines(folder):
    return sorted(line for p in folder.iterdir() for line in p.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize("restore_fails", [False, True], ids=["restored", "restore refused too"])
def test_a_rollover_failing_half_way_loses_nothing(tmp_path, monkeypatch, restore_fails):
    """Stage 2 (into .1) refused after the current file moved aside; the move
    back may be refused as well. No line is lost, the record that triggered
    the rollover included, and logging carries on."""
    from devgraph.agent import log_file

    log_path = tmp_path / "devgraph.log"
    (tmp_path / "devgraph.log.1").write_text("older\n", encoding="utf-8")
    now = [1000.0]
    monkeypatch.setattr(log_file.time, "monotonic", lambda: now[0])
    fails = {(".rolling", ".1")} | ({(".rolling", "")} if restore_fails else set())
    _failing_replace(monkeypatch, log_file, fails)
    handler = log_file.AgentLogHandler(log_path, maxBytes=200, backupCount=3, encoding="utf-8")
    logger = _logger(handler, f"devgraph.test.half.{restore_fails}")
    expected = ["older"]
    try:
        for i in range(30):
            logger.warning("record %02d of the batch", i)
            expected.append(f"record {i:02d} of the batch")
        handler.flush()
        assert _all_lines(tmp_path) == sorted(expected)
        names = sorted(p.name for p in tmp_path.iterdir())
        if restore_fails:
            assert names == ["devgraph.log", "devgraph.log.2", "devgraph.log.rolling"]
        else:
            assert names == ["devgraph.log", "devgraph.log.2"]

        # The file is free again and retry_s has passed: it rolls over, a
        # leftover .rolling first, and nothing is shifted twice or lost.
        fails.clear()
        now[0] += handler.retry_s + 1
        logger.warning("after the retry")
        expected.append("after the retry")
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert _all_lines(tmp_path) == sorted(expected)
    assert log_path.read_text(encoding="utf-8") == "after the retry\n"
    assert not (tmp_path / "devgraph.log.rolling").exists()
    assert (tmp_path / f"devgraph.log.{3 if restore_fails else 2}").read_text(encoding="utf-8") == "older\n"


def test_a_rolling_file_left_by_a_crash_is_kept(tmp_path, monkeypatch):
    """Missing current file: moved back at start. Present: folded in as the
    newest backup at the next rollover, never overwritten."""
    from devgraph.agent import log_file

    rolling = tmp_path / "devgraph.log.rolling"
    rolling.write_text("from the crash\n", encoding="utf-8")
    handler = log_file.AgentLogHandler(tmp_path / "devgraph.log", maxBytes=100, backupCount=3, encoding="utf-8")
    handler.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["devgraph.log"]
    assert (tmp_path / "devgraph.log").read_text(encoding="utf-8") == "from the crash\n"

    rolling.write_text("from another crash\n", encoding="utf-8")
    handler = log_file.AgentLogHandler(tmp_path / "devgraph.log", maxBytes=100, backupCount=3, encoding="utf-8")
    logger = _logger(handler, "devgraph.test.leftover")
    try:
        logger.warning("x" * 120)
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["devgraph.log", "devgraph.log.1", "devgraph.log.2"]
    assert (tmp_path / "devgraph.log.2").read_text(encoding="utf-8") == "from another crash\n"
    assert (tmp_path / "devgraph.log.1").read_text(encoding="utf-8") == "from the crash\n"
    assert (tmp_path / "devgraph.log").read_text(encoding="utf-8") == "x" * 120 + "\n"


_SAMPLE_LOG = (
    "2026-10-18 09:00:00,001 INFO devgraph.watcher: indexed 2 file(s) (src/debug_panel.py, src/app.py)\n"
    "2026-10-18 09:00:01,002 DEBUG devgraph.indexer: pass 2 for 2 file(s)\n"
    "2026-10-18 09:00:02,003 INFO devgraph.watcher: indexed 1 file(s) (src/errors.py)\n"
    "2026-10-18 09:00:03,004 WARNING devgraph.agent: schema rescan skipped for demo: not found\n"
    "2026-10-18 09:00:04,005 ERROR devgraph.watcher: live batch failed\n"
    "Traceback (most recent call last):\n"
    '  File "src/app.py", line 3, in <module>\n'
    "ValueError: bad input\n"
    "2026-10-18 09:00:05,006 INFO devgraph.watcher: indexed 1 file(s) (src/app.py)\n"
)


def _logs(tmp_path, *args, current=_SAMPLE_LOG, previous=None):
    log_path = tmp_path / "devgraph.log"
    log_path.write_text(current, encoding="utf-8")
    if previous is not None:
        (tmp_path / "devgraph.log.1").write_text(previous, encoding="utf-8")
    settings = MagicMock()
    settings.log_file = log_path
    with patch.object(cli_main, "get_settings", return_value=settings):
        return CliRunner().invoke(cli_main.app, ["logs", *args])


def test_devgraph_logs_filters_by_each_records_level_field(tmp_path):
    """The level is the field after the timestamp, not any word in the line:
    a path holding "debug" or "errors" doesn't change a record's level."""
    result = _logs(tmp_path)
    assert result.exit_code == 0, result.output
    assert "src/debug_panel.py" in result.output and "src/errors.py" in result.output
    assert "pass 2 for 2 file(s)" not in result.output
    assert "pass 2 for 2 file(s)" in _logs(tmp_path, "--level", "debug").output


def test_devgraph_logs_keeps_a_traceback_with_its_error_record(tmp_path):
    result = _logs(tmp_path, "--level", "ERROR")
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "2026-10-18 09:00:04,005 ERROR devgraph.watcher: live batch failed",
        "Traceback (most recent call last):",
        '  File "src/app.py", line 3, in <module>',
        "ValueError: bad input",
    ]
    warnings = _logs(tmp_path, "--level", "WARNING").output.splitlines()
    assert warnings[0].endswith("schema rescan skipped for demo: not found") and len(warnings) == 5


def test_devgraph_logs_never_shows_part_of_a_record(tmp_path):
    """-n counts lines from the end, but the oldest record shown is whole."""
    lines = _logs(tmp_path, "-n", "3").output.splitlines()
    assert lines[0] == "2026-10-18 09:00:04,005 ERROR devgraph.watcher: live batch failed"
    assert lines[-1].endswith("indexed 1 file(s) (src/app.py)") and len(lines) == 5


def test_devgraph_logs_follows_a_record_across_a_rollover(tmp_path):
    """A rollover between a record's lines leaves its traceback at the top of
    the current file: it keeps the level of its record in `.1`."""
    head, _, tail = _SAMPLE_LOG.partition("Traceback")
    result = _logs(tmp_path, "--level", "ERROR", current="Traceback" + tail, previous=head)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[0].endswith("ERROR devgraph.watcher: live batch failed")
    assert result.output.splitlines()[-1] == "ValueError: bad input"


def test_devgraph_logs_refuses_an_unknown_level(tmp_path):
    result = _logs(tmp_path, "--level", "LOUD")
    assert result.exit_code == 2
    assert "DEBUG, INFO, WARNING, ERROR, CRITICAL" in " ".join(result.output.split())


def test_devgraph_logs_takes_warn_and_fatal_for_their_levels(tmp_path):
    lines = _logs(tmp_path, "--level", "warn").output.splitlines()
    assert lines[0].endswith("schema rescan skipped for demo: not found") and len(lines) == 5
    fatal = "2026-10-18 09:00:06,007 FATAL devgraph.agent: gave up\n"
    assert _logs(tmp_path, "--level", "FATAL", current=_SAMPLE_LOG + fatal).output.splitlines() == [fatal.strip()]


def test_a_record_with_a_level_name_of_its_own_still_ends_the_one_before(tmp_path):
    custom = "2026-10-18 09:00:06,007 TRACE devgraph.plugin: probing\n"
    lines = _logs(tmp_path, "--level", "ERROR", current=_SAMPLE_LOG + custom).output.splitlines()
    assert lines[-1] == "ValueError: bad input"  # not "probing", taken for part of the traceback
    assert _logs(tmp_path, "-n", "1", current=_SAMPLE_LOG + custom).output.splitlines() == [custom.strip()]


def test_devgraph_logs_reads_a_rolling_file_between_the_backup_and_the_current_file(tmp_path):
    """`.rolling` is the former current file, kept by a rollover whose last
    rename failed: newer than `.1`, older than the current file."""
    (tmp_path / "devgraph.log.rolling").write_text("2026-10-18 09:00:00,500 INFO x: middle\n", encoding="utf-8")
    result = _logs(
        tmp_path, current="2026-10-18 09:00:01,000 INFO x: newest\n", previous="2026-10-18 08:00:00,000 INFO x: oldest\n",
    )
    assert [line.rsplit(" ", 1)[1] for line in result.output.splitlines()] == ["oldest", "middle", "newest"]


def test_devgraph_logs_shows_bytes_that_are_not_utf8_replaced(tmp_path):
    log_path = tmp_path / "devgraph.log"
    log_path.write_bytes(b"2026-10-18 09:00:00,000 INFO x: cut \xff\xfe short\n")
    settings = MagicMock()
    settings.log_file = log_path
    with patch.object(cli_main, "get_settings", return_value=settings):
        result = CliRunner().invoke(cli_main.app, ["logs"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ["2026-10-18 09:00:00,000 INFO x: cut �� short"]
