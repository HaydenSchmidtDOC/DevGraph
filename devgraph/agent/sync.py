"""Keeps the graph in step with what the watcher reports, for the tray and the
headless agent alike: live batches, catch-ups, stamps and failure floors
(spec W5, W7, W9).

`last_indexed` is the start of the work it covers. While a batch for a
repository has failed, every stamp is held back to the repository's *floor*:
the last good stamp before the failure (a live batch's events can predate its
start by as long as it waited on the batch lock). A catch-up from the floor
is requested; a catch-up from at or before the floor that succeeds clears it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from devgraph.indexer.dispatch import catch_up, index_paths, remove_paths

logger = logging.getLogger(__name__)

#: How long after a failed batch the catch-up that repairs it runs.
FAILURE_RETRY_DELAY_S = 30.0

#: While a repository keeps failing, how often its warning is repeated.
FAILURE_WARNING_INTERVAL = timedelta(minutes=5)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RepoSync:
    """`publish(event)` is the dashboard's broadcaster; `request_catch_up(repo_id,
    since, delay_s)` is the watcher's, which runs `on_catch_up` later under the
    repository's batch lock. Set `stopping` while the agent pauses or quits, so
    a failure caused by the shutdown is not reported as a warning."""

    def __init__(
        self,
        engine: Any,
        registry: Any,
        publish: Callable[[dict], None],
        request_catch_up: Callable[[str, datetime, float], None],
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._publish = publish
        self._request_catch_up = request_catch_up
        self._now = now
        self._lock = threading.Lock()  # guards _floors and _running
        self._floors: dict[str, datetime] = {}
        # repo_id -> when the current failure streak last warned
        self._warned: dict[str, datetime] = {}
        self._running = 0
        self.stopping = False

    @property
    def running(self) -> int:
        """How many catch-ups are running now."""
        with self._lock:
            return self._running

    def on_changes(self, repo_id: str, changed_paths: set[Path], deleted_paths: set[Path]) -> None:
        """Index one live batch, then stamp its start (held back by the floor)."""
        logger.info(
            "changes detected for %s: %d changed, %d deleted",
            repo_id,
            len(changed_paths),
            len(deleted_paths),
        )
        started = self._now()
        repo = None
        try:
            repo = self._registry.get(repo_id)
            if repo is None:
                return  # removed between the event firing and now
            indexed = removed = 0
            if changed_paths:
                indexed = index_paths(
                    self._engine, repo_id, repo.path, changed_paths,
                    docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled,
                )
            if deleted_paths:
                removed = remove_paths(self._engine, repo_id, repo.path, deleted_paths)
            self._registry.mark_indexed(repo_id, at=self._held_back(repo_id, started))
            self._publish({"type": "reindexed", "repo_id": repo_id, "changed": indexed, "deleted": removed})
        except Exception as exc:
            # The batch's events can predate `started` by as long as it waited
            # on the batch lock (a catch-up or schema rescan can hold it for
            # minutes), so the floor is the last good stamp.
            floor = started
            if repo is not None and repo.last_indexed:
                floor = min(floor, datetime.fromisoformat(repo.last_indexed))
            self._failed(repo_id, floor, exc)

    def on_catch_up(self, repo_id: str, since: datetime, reason: str = "start") -> bool:
        """Catch up on changes made since `since` (or the floor, if earlier).
        Returns whether it did (False when it failed, or the repository is gone).

        `reason` is "start" (the watcher started watching), "git" (after a
        git operation) or "retry" (after a failure); it only changes the logs.
        """
        with self._lock:
            floor = self._floors.get(repo_id)
        if floor is not None and floor < since:
            since = floor
        repo = self._registry.get(repo_id)
        if repo is None:
            return False
        logger.log(
            logging.INFO if reason == "start" else logging.DEBUG,
            "Checking %s for changes made while DevGraph wasn't watching…", repo_id,
        )
        with self._lock:
            self._running += 1
        try:
            self._publish({"type": "catch_up", "repo_id": repo_id, "state": "running", "changed": 0, "deleted": 0})
            started = self._now()
            clock = time.monotonic()
            result = catch_up(
                self._engine, repo_id, repo.path, since,
                docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled,
            )
            with self._lock:
                if repo_id in self._floors and since <= self._floors[repo_id]:
                    del self._floors[repo_id]
                    self._warned.pop(repo_id, None)
            self._registry.mark_indexed(repo_id, at=self._held_back(repo_id, started))
        except Exception as exc:
            error: Exception | None = exc
        else:
            error = None
        finally:
            # Before the closing event, so a listener reading `running` sees it done.
            with self._lock:
                self._running -= 1
        if error is not None:
            self._publish({"type": "catch_up", "repo_id": repo_id, "state": "failed", "changed": 0, "deleted": 0})
            self._failed(repo_id, since, error)
            return False
        elapsed = time.monotonic() - clock
        self._publish({
            "type": "catch_up", "repo_id": repo_id, "state": "done",
            "changed": result.indexed, "deleted": result.pruned,
        })
        found = result.indexed + result.pruned
        if found:
            self._publish({"type": "reindexed", "repo_id": repo_id, "changed": result.indexed, "deleted": result.pruned})
        self._log_result(repo_id, reason, result, found, elapsed)
        return True

    def retry_failed(self) -> None:
        """Ask for a catch-up of every repository with a floor (the health
        loop calls this when Neo4j comes back)."""
        with self._lock:
            floors = dict(self._floors)
        for repo_id, floor in floors.items():
            self._request_catch_up(repo_id, floor, 0.0)

    def _held_back(self, repo_id: str, started: datetime) -> datetime:
        with self._lock:
            floor = self._floors.get(repo_id)
        return started if floor is None or started <= floor else floor

    def _failed(self, repo_id: str, start: datetime, error: Exception) -> None:
        """Hold the repository's stamps back to `start`, warn (unless
        stopping; with the traceback once per failure streak, then a reminder
        every few minutes), and ask for a catch-up from the floor."""
        now = self._now()
        with self._lock:
            floor = self._floors.get(repo_id)
            if floor is None or start < floor:
                floor = self._floors[repo_id] = start
            last_warned = self._warned.get(repo_id)
            warn = not self.stopping and (last_warned is None or now - last_warned >= FAILURE_WARNING_INTERVAL)
            if warn:
                self._warned[repo_id] = now
        message = 'Couldn\'t update %s; DevGraph will retry, or run "devgraph rescan %s"'
        if not warn:
            logger.debug(message, repo_id, repo_id, exc_info=error)
        elif last_warned is None:
            logger.warning(message, repo_id, repo_id, exc_info=error)
        else:
            # Still failing: a reminder, without the traceback already logged.
            logger.warning(message + " (still failing: %s)", repo_id, repo_id, error)
        self._request_catch_up(repo_id, floor, FAILURE_RETRY_DELAY_S)

    @staticmethod
    def _log_result(repo_id: str, reason: str, result: Any, found: int, elapsed: float) -> None:
        if reason == "git":
            # Files git changed are offered whether or not the live batches
            # already indexed them; a miss is only what they can't have
            # handled: a file the graph didn't know, or one still in it after
            # it was deleted.
            missed = result.unknown + result.pruned
            if result.offered:
                logger.info("%s: checked %d files changed by a git operation", repo_id, result.offered)
            if missed:
                logger.info(
                    "%s: found %d files the live watcher missed after a git operation; updated them", repo_id, missed
                )
            if not (result.offered or missed):
                logger.debug("%s is up to date after a git operation (checked %d files)", repo_id, result.checked)
        elif found:
            logger.info(
                "Caught up on %s: %s files updated, %s removed (%.1f s)",
                repo_id, f"{result.indexed:,}", f"{result.pruned:,}", elapsed,
            )
        else:
            logger.info("%s is up to date (checked %s files in %.1f s)", repo_id, f"{result.checked:,}", elapsed)
