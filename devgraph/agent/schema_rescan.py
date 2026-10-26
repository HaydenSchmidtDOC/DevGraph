"""Debounced full rescans after a repository's devgraph.schema.yaml changes.

A schema edit leaves the repository *pending* (dispatch.schema_pending):
filesystem-provider writes pause until the schema is applied. This scheduler
applies it with a full rescan once the file has stopped changing for a quiet
period, so a burst of edits costs one rescan, not one per save.
`devgraph rescan` applies immediately instead.

It also upgrades an index of an older format, and rescans a repository whose
docs path or mentions setting changed since its last full scan
(dispatch.scan_options_pending), both at once.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from devgraph.config.project_schema import ProjectSchemaError, resolve_effective_schema, schema_file_hash
from devgraph.indexer.dispatch import (
    full_scan, full_scan_elsewhere, full_scan_turn, index_outdated, scan_options_pending, schema_pending,
)
from devgraph.indexer.walk import RepoRootUnavailable

logger = logging.getLogger(__name__)

QUIET_PERIOD_S = 300.0
CHECK_INTERVAL_S = 30.0
#: A failing index upgrade or settings rescan is retried after one interval, then doubling up to this.
UPGRADE_BACKOFF_MAX_S = 1800.0


class SchemaRescanScheduler:
    def __init__(
        self,
        engine: Any,
        registry: Any,
        on_rescanned: Callable[[str, int], None] | None = None,
        *,
        quiet_s: float = QUIET_PERIOD_S,
        interval_s: float = CHECK_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        is_paused: Callable[[], bool] | None = None,
        run_exclusive: Callable[[str, Callable[[], Any]], Any] | None = None,
        yield_lock: Callable[[str], bool] | None = None,
    ) -> None:
        """`run_exclusive(repo_id, fn)` is the watcher's: it runs the scan
        under the repository's batch lock, so it never interleaves with a
        live batch (W4). `yield_lock(repo_id)` is the watcher's
        `yield_batch_lock`: while a scan waits for another repository's
        (dispatch `full_scan_turn`), the repository's live batches run."""
        self._engine = engine
        self._registry = registry
        self._on_rescanned = on_rescanned
        self._quiet_s = quiet_s
        self._interval_s = interval_s
        self._clock = clock
        self._is_paused = is_paused
        self._run_exclusive = run_exclusive or (lambda repo_id, fn: fn())
        self._yield_lock = yield_lock
        # repo_id -> (pending hash, when it was first seen)
        self._seen: dict[str, tuple[str, float]] = {}
        # repo_id -> hash that failed to resolve; skipped until the file changes
        self._invalid: dict[str, str] = {}
        # repos whose last check raised; warn once per streak
        self._failing: set[str] = set()
        # repo_id -> (next upgrade attempt, current delay) after a failed upgrade
        self._upgrade_backoff: dict[str, tuple[float, float]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def run_once(self) -> list[str]:
        rescanned: list[str] = []
        if self._is_paused is not None and self._is_paused():
            return rescanned
        now = self._clock()
        for repo in self._registry.list_repos(active_only=True):
            if self._stop.is_set():
                break
            if not repo.watch_enabled:
                continue
            try:
                # Another process (`devgraph rescan`) is scanning it in full:
                # its index reads as outdated until that scan restamps it, and
                # the scan applies a changed schema too. Looked at again next pass.
                if full_scan_elsewhere(self._engine, repo.repo_id):
                    continue
                # A never-indexed repo has no stamp either: its first scan
                # (devgraph add / rescan, maybe running now) is left to them.
                if repo.last_indexed and index_outdated(self._engine, repo.repo_id):
                    # An index from an older format: upgrade it now, no quiet
                    # period; a failing upgrade backs off (reset on success).
                    backoff = self._upgrade_backoff.get(repo.repo_id)
                    if backoff is not None and now < backoff[0]:
                        continue
                    try:
                        result = self._exclusive(repo.repo_id, lambda: self._upgrade(repo))
                    except Exception:
                        delay = min(backoff[1] * 2, UPGRADE_BACKOFF_MAX_S) if backoff else self._interval_s
                        self._upgrade_backoff[repo.repo_id] = (now + delay, delay)
                        raise
                    self._upgrade_backoff.pop(repo.repo_id, None)
                    self._failing.discard(repo.repo_id)
                    if result is None:
                        continue
                    count, _ = result
                    rescanned.append(repo.repo_id)
                    logger.info("upgraded the graph index of %s with a full rescan (%d files)", repo.repo_id, count)
                    if self._on_rescanned is not None:
                        try:
                            self._on_rescanned(repo.repo_id, count)
                        except Exception:
                            logger.debug("schema rescan callback failed for %s", repo.repo_id, exc_info=True)
                    continue
                if repo.last_indexed and scan_options_pending(
                    self._engine, repo.repo_id, repo.docs_path, repo.mentions_enabled
                ):
                    # `devgraph mentions` or `annotate --docs-path` changed
                    # what a full scan indexes: apply it now, no quiet period;
                    # a failing rescan backs off as an upgrade does.
                    backoff = self._upgrade_backoff.get(repo.repo_id)
                    if backoff is not None and now < backoff[0]:
                        continue
                    try:
                        result = self._exclusive(repo.repo_id, lambda: self._apply_options(repo.repo_id))
                    except Exception:
                        delay = min(backoff[1] * 2, UPGRADE_BACKOFF_MAX_S) if backoff else self._interval_s
                        self._upgrade_backoff[repo.repo_id] = (now + delay, delay)
                        raise
                    self._upgrade_backoff.pop(repo.repo_id, None)
                    if result is None:
                        continue
                    count, _ = result
                    self._failing.discard(repo.repo_id)
                    rescanned.append(repo.repo_id)
                    logger.info(
                        "applied a changed docs path or mentions setting to %s with a full rescan (%d files)",
                        repo.repo_id, count,
                    )
                    if self._on_rescanned is not None:
                        try:
                            self._on_rescanned(repo.repo_id, count)
                        except Exception:
                            logger.debug("schema rescan callback failed for %s", repo.repo_id, exc_info=True)
                    continue
                if not schema_pending(self._engine, repo.repo_id, repo.path):
                    self._seen.pop(repo.repo_id, None)
                    self._invalid.pop(repo.repo_id, None)
                    self._failing.discard(repo.repo_id)
                    continue
                current = schema_file_hash(repo.path)
                seen = self._seen.get(repo.repo_id)
                if seen is None or seen[0] != current:
                    self._seen[repo.repo_id] = (current, now)  # start or restart the quiet period
                    continue
                if now - seen[1] < self._quiet_s or self._invalid.get(repo.repo_id) == current:
                    continue
                try:
                    resolve_effective_schema(repo.path)
                except ProjectSchemaError as exc:
                    self._invalid[repo.repo_id] = current
                    logger.warning("project schema for %s is invalid; rescan skipped until it changes: %s", repo.repo_id, exc)
                    continue
                result = self._exclusive(repo.repo_id, lambda: self._rescan(repo))
                if result is None:
                    continue
                count, applied = result
                self._failing.discard(repo.repo_id)
                if not applied:
                    self._invalid[repo.repo_id] = current
                    logger.warning(
                        "project schema for %s could not be applied; not retried until the file changes", repo.repo_id
                    )
                    continue
                self._seen.pop(repo.repo_id, None)
                rescanned.append(repo.repo_id)
                logger.info("applied a changed project schema to %s with a full rescan (%d files)", repo.repo_id, count)
                if self._on_rescanned is not None:
                    try:
                        self._on_rescanned(repo.repo_id, count)
                    except Exception:
                        logger.debug("schema rescan callback failed for %s", repo.repo_id, exc_info=True)
            except RepoRootUnavailable as exc:
                # A missing or unmounted folder: one line, no traceback.
                first = repo.repo_id not in self._failing
                self._failing.add(repo.repo_id)
                logger.log(logging.WARNING if first else logging.DEBUG, "schema rescan skipped for %s: %s", repo.repo_id, exc)
            except Exception:
                # A scan interrupted by shutdown (the engine refuses new
                # sessions once it closes) is not a failure worth a warning.
                first = repo.repo_id not in self._failing and not self._stop.is_set()
                self._failing.add(repo.repo_id)
                logger.log(
                    logging.WARNING if first else logging.DEBUG,
                    "schema rescan check failed for %s", repo.repo_id, exc_info=True,
                )
        return rescanned

    def _exclusive(self, repo_id: str, fn: Callable[[], Any]) -> Any:
        """`fn` under the batch lock, or None if stop came while waiting for it
        (the agent closes the graph engine right after stop) or the repo was
        removed meanwhile (its scan would outlive the removal's delete)."""
        def run() -> Any:
            if self._stop.is_set() or self._registry.get(repo_id) is None:
                return None
            return fn()

        return self._run_exclusive(repo_id, run)

    def _upgrade(self, repo: Any) -> tuple[int, bool] | None:
        """`_rescan` unless the index got upgraded while this pass waited on
        the batch lock (the start catch-up or a registration scan); None then."""
        if not index_outdated(self._engine, repo.repo_id):
            return None
        with full_scan_turn(repo.repo_id, lambda: self._waiting(repo.repo_id), self._engine):
            if not index_outdated(self._engine, repo.repo_id):
                return None  # upgraded by the scan this one waited for
            return self._rescan(repo)

    def _apply_options(self, repo_id: str) -> tuple[int, bool] | None:
        """`_rescan` with the registry's current settings, unless a scan
        applied them while this pass waited (None then)."""
        repo = self._registry.get(repo_id)
        if not scan_options_pending(self._engine, repo_id, repo.docs_path, repo.mentions_enabled):
            return None
        with full_scan_turn(repo_id, lambda: self._waiting(repo_id), self._engine):
            repo = self._registry.get(repo_id)
            if repo is None or not scan_options_pending(self._engine, repo_id, repo.docs_path, repo.mentions_enabled):
                return None  # applied by the scan this one waited for
            return self._rescan(repo)

    def _waiting(self, repo_id: str) -> bool:
        """While a scan waits its turn: let the repository's live batches run;
        False (call the scan off) once stopping."""
        if self._yield_lock is not None:
            self._yield_lock(repo_id)  # its answer is about git syncs, not this scan
        return not self._stop.is_set() and self._registry.get(repo_id) is not None

    def _rescan(self, repo: Any) -> tuple[int, bool]:
        """Full-scan `repo` and, if its schema got applied, stamp the scan's
        start (W7). Returns (files indexed, applied)."""
        started = datetime.now(timezone.utc)
        with full_scan_turn(repo.repo_id, lambda: self._waiting(repo.repo_id), self._engine):  # full_scan's own take is then at once
            count = full_scan(
                self._engine, repo.repo_id, repo.path,
                docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled,
            )
        if schema_pending(self._engine, repo.repo_id, repo.path):
            return count, False
        self._registry.mark_indexed(repo.repo_id, at=started)
        return count, True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="devgraph-schema-rescan", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("schema rescan pass failed", exc_info=True)
            self._stop.wait(self._interval_s)
