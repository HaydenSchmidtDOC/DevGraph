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
    every record after.

    Here a rollover goes in two stages. Stage 1 moves the current file aside
    to `<file>.rolling`; stage 2 shifts the backups up as far as the first
    free slot and moves `.rolling` to `<file>.1`. If stage 1 fails, nothing has moved. If stage 2 fails,
    `.rolling` is moved back to the current file; if that fails too, it stays
    where it is and the next rollover folds it in as the newest backup before
    anything else, and never overwrites it. Either way the stream is reopened,
    appending, on the current file (created if it was moved), so the record
    being written is kept, and after a failure the rollover is tried again only
    after `retry_s`. A `.rolling` left by a crash is moved back at start when
    the current file is missing, else folded in at the next rollover.
    """

    retry_s = 60.0

    def __init__(self, filename, *args, **kwargs) -> None:
        pending = os.fspath(filename) + ".rolling"
        if os.path.exists(pending) and not os.path.exists(filename):
            try:
                os.replace(pending, filename)
            except OSError:
                pass  # left for the first rollover to fold in
        super().__init__(filename, *args, **kwargs)
        self._retry_at = 0.0

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        return bool(super().shouldRollover(record))

    def _backup(self, i: int) -> str:
        return self.rotation_filename(f"{self.baseFilename}.{i}")

    def _shift_in(self, pending: str) -> None:
        """Stage 2: make `pending` the newest backup. The backups shift up only
        as far as the first free slot (the oldest is replaced only when every
        slot is taken), so a retry after a failure here shifts nothing twice."""
        free = 1
        while free <= self.backupCount and os.path.exists(self._backup(free)):
            free += 1
        for i in range(min(free, self.backupCount) - 1, 0, -1):
            os.replace(self._backup(i), self._backup(i + 1))
        os.replace(pending, self._backup(1))

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None  # type: ignore[assignment]
        pending = self.baseFilename + ".rolling"
        rolled = False
        try:
            if os.path.exists(pending):  # left by a crash or a failed restore: older than the current file
                self._shift_in(pending)
            os.replace(self.baseFilename, pending)  # stage 1
            try:
                self._shift_in(pending)  # stage 2
                rolled = True
            except OSError:
                try:
                    os.replace(pending, self.baseFilename)
                except OSError:
                    pass  # kept as .rolling, folded in at the next rollover
        except OSError:
            pass
        finally:
            self._retry_at = 0.0 if rolled else time.monotonic() + self.retry_s
            self.stream = self._open()
