"""The agent's log file handler (see `_configure_logging` in tray.py and headless.py)."""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler


class AgentLogHandler(RotatingFileHandler):
    """A `RotatingFileHandler` whose rollover can fail without losing anything.

    On Windows a file another process has open (`devgraph logs` reading it, an
    editor, a virus scanner) can't be renamed. The stdlib rollover shifts the
    older files first and renames the current one last, so a failure there has
    already moved the backups, and it is tried again, shifting them again, on
    every record after. Here the current file is moved aside first: if that
    fails nothing else has moved, records keep going to the current file, and
    the rollover is tried again only after `retry_s`.
    """

    retry_s = 60.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._retry_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        return bool(super().shouldRollover(record))

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        pending = self.baseFilename + ".rolling"
        try:
            os.replace(self.baseFilename, pending)
        except OSError:
            self._retry_at = time.monotonic() + self.retry_s
        else:
            try:
                for i in range(self.backupCount - 1, 0, -1):
                    older = self.rotation_filename(f"{self.baseFilename}.{i}")
                    if os.path.exists(older):
                        os.replace(older, self.rotation_filename(f"{self.baseFilename}.{i + 1}"))
                os.replace(pending, self.rotation_filename(f"{self.baseFilename}.1"))
            except OSError:
                os.replace(pending, self.baseFilename)  # nothing written to it since it moved
                self._retry_at = time.monotonic() + self.retry_s
        self.stream = self._open()
