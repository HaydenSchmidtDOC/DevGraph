"""Debounced full rescans after a repository's devgraph.schema.yaml changes.

A schema edit leaves the repository *pending* (dispatch.schema_pending):
filesystem-provider writes pause until the schema is applied. This scheduler
applies it with a full rescan once the file has stopped changing for a quiet
period, so a burst of edits costs one rescan, not one per save.
`devgraph rescan` applies immediately instead.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from devgraph.config.project_schema import ProjectSchemaError, resolve_effective_schema, schema_file_hash
from devgraph.indexer.dispatch import full_scan, schema_pending

logger = logging.getLogger(__name__)

QUIET_PERIOD_S = 300.0
CHECK_INTERVAL_S = 30.0


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
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._on_rescanned = on_rescanned
        self._quiet_s = quiet_s
        self._interval_s = interval_s
        self._clock = clock
        self._is_paused = is_paused
        # repo_id -> (pending hash, when it was first seen)
        self._seen: dict[str, tuple[str, float]] = {}
        # repo_id -> hash that failed to resolve; skipped until the file changes
        self._invalid: dict[str, str] = {}
        # repos whose last check raised; warn once per streak
        self._failing: set[str] = set()
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
                count = full_scan(
                    self._engine, repo.repo_id, repo.path,
                    docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled,
                )
                self._failing.discard(repo.repo_id)
                if schema_pending(self._engine, repo.repo_id, repo.path):
                    self._invalid[repo.repo_id] = current
                    logger.warning(
                        "project schema for %s could not be applied; not retried until the file changes", repo.repo_id
                    )
                    continue
                self._registry.mark_indexed(repo.repo_id)
                self._seen.pop(repo.repo_id, None)
                rescanned.append(repo.repo_id)
                logger.info("applied a changed project schema to %s with a full rescan (%d files)", repo.repo_id, count)
                if self._on_rescanned is not None:
                    try:
                        self._on_rescanned(repo.repo_id, count)
                    except Exception:
                        logger.debug("schema rescan callback failed for %s", repo.repo_id, exc_info=True)
            except Exception:
                first = repo.repo_id not in self._failing
                self._failing.add(repo.repo_id)
                logger.log(
                    logging.WARNING if first else logging.DEBUG,
                    "schema rescan check failed for %s", repo.repo_id, exc_info=True,
                )
        return rescanned

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="devgraph-schema-rescan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("schema rescan pass failed", exc_info=True)
            self._stop.wait(self._interval_s)
