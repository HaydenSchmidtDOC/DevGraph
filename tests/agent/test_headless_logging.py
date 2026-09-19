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
from unittest.mock import MagicMock, patch

try:  # pystray binds to an X display as a side effect of import (see
    # tests/conftest.py); `devgraph.agent.__init__` pulls it in transitively.
    import pystray  # noqa: F401
except Exception:  # pragma: no cover — depends on the host having a display
    sys.modules["pystray"] = types.ModuleType("pystray")

from devgraph.agent import headless

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
