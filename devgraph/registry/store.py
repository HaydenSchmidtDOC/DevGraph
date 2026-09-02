"""SQLite-backed repository registry.

This is the single allowlist for DevGraph (Design Brief Principle 1). The
watcher and indexer must only ever operate on paths obtained by reading this
table — never on a path supplied directly by an MCP tool call or CLI flag
that hasn't first gone through `add_repo`.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    repo_id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    watch_enabled INTEGER NOT NULL DEFAULT 1,
    last_indexed TEXT,
    docs_path TEXT,
    pr_source_enabled INTEGER NOT NULL DEFAULT 0,
    issue_source_enabled INTEGER NOT NULL DEFAULT 0,
    last_indexed_commit TEXT
);
"""

# Added after the initial release; each ALTER is skipped once its column exists.
_MIGRATIONS = (
    ("docs_path", "ALTER TABLE repos ADD COLUMN docs_path TEXT"),
    (
        "pr_source_enabled",
        "ALTER TABLE repos ADD COLUMN pr_source_enabled INTEGER NOT NULL DEFAULT 0",
    ),
    (
        "issue_source_enabled",
        "ALTER TABLE repos ADD COLUMN issue_source_enabled INTEGER NOT NULL DEFAULT 0",
    ),
    ("last_indexed_commit", "ALTER TABLE repos ADD COLUMN last_indexed_commit TEXT"),
)

_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _slugify(name: str) -> str:
    slug = _SLUG_RE.sub("-", name.lower()).strip("-")
    return slug or "repo"


@dataclass(frozen=True)
class RepoRecord:
    repo_id: str
    path: Path
    active: bool
    watch_enabled: bool
    last_indexed: str | None
    docs_path: str | None = None
    pr_source_enabled: bool = False
    issue_source_enabled: bool = False
    last_indexed_commit: str | None = None


class RepoRegistry:
    """The authoritative list of repositories DevGraph is allowed to touch."""

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Marker file the long-lived tray process polls (see agent/tray.py's
        # health-check loop) to notice registry mutations made by a *different*
        # process -- e.g. `devgraph add`/`remove`/`watch enable|disable` run
        # from a fresh CLI invocation while the tray's WatcherManager is
        # already running in its own process with its own in-memory copy of
        # "which repos to watch". Without this, WatcherManager.refresh() is
        # unreachable from outside the tray process, so a repo added (or
        # re-enabled) after the tray started never gets a live OS watch until
        # the tray itself is restarted, even though `devgraph list`/`status`
        # correctly show it registered and `rescan` can index it on demand.
        self._change_marker_path = db_path.parent / "registry_changed.txt"
        # check_same_thread=False + an RLock: this registry is read/written
        # from multiple threads in practice (each registered repo's watcher
        # fires its debounce callback on its own threading.Timer thread, plus
        # the tray app's health-check thread and the main thread) — sqlite3's
        # default check_same_thread=True raised
        # "SQLite objects created in a thread can only be used in that same
        # thread" on essentially every watcher-triggered reindex, since the
        # Timer thread is never the thread that created this connection. The
        # RLock (not a plain Lock) serializes actual access, since a shared
        # SQLite connection is only safe for one thread to use at a time even
        # with check_same_thread=False — RLock specifically because a few
        # methods below (set_docs_path, set_last_indexed_commit) call self.get()
        # internally while already holding the lock.
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            existing_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(repos)")}
            for column, migration in _MIGRATIONS:
                if column not in existing_cols:
                    self._conn.execute(migration)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _touch_change_marker(self) -> None:
        """Record that the registry's watch-relevant state changed just now.

        Best-effort: a failure to write this marker shouldn't fail the
        registry mutation that triggered it (the mutation already committed
        to SQLite) -- it would just mean the running tray process picks up
        the change a bit later, on its next poll, or not until restarted.
        """
        try:
            self._change_marker_path.write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )
        except OSError:
            pass

    def last_changed_at(self) -> str | None:
        """Timestamp of the most recent add/remove/watch-flag mutation, if any.

        Read by the tray process's health-check loop to detect registry
        changes made by other `devgraph` CLI invocations; see the marker
        path comment in `__init__`.
        """
        try:
            return self._change_marker_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def add_repo(self, path: str | Path, repo_id: str | None = None) -> RepoRecord:
        resolved = Path(path).resolve()
        if not resolved.exists() or not resolved.is_dir():
            raise ValueError(f"path does not exist or is not a directory: {resolved}")
        if not (resolved / ".git").exists():
            raise ValueError(f"not a git repository: {resolved}")

        with self._lock:
            existing = self._conn.execute(
                "SELECT repo_id FROM repos WHERE path = ?", (str(resolved),)
            ).fetchone()
            if existing:
                raise ValueError(f"already registered as '{existing[0]}': {resolved}")

            candidate = _slugify(repo_id or resolved.name)
            final_id = candidate
            suffix = 2
            while self._conn.execute(
                "SELECT 1 FROM repos WHERE repo_id = ?", (final_id,)
            ).fetchone():
                final_id = f"{candidate}-{suffix}"
                suffix += 1

            self._conn.execute(
                "INSERT INTO repos (repo_id, path, active, watch_enabled, last_indexed) "
                "VALUES (?, ?, 1, 1, NULL)",
                (final_id, str(resolved)),
            )
            self._conn.commit()
        self._touch_change_marker()
        return RepoRecord(final_id, resolved, True, True, None)

    def remove_repo(self, repo_id: str) -> None:
        with self._lock:
            cur = self._conn.execute("DELETE FROM repos WHERE repo_id = ?", (repo_id,))
            self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no such repo_id: {repo_id}")
        self._touch_change_marker()

    def enable_watch(self, repo_id: str) -> None:
        self._set_flag(repo_id, "watch_enabled", True)

    def disable_watch(self, repo_id: str) -> None:
        self._set_flag(repo_id, "watch_enabled", False)

    def _set_flag(self, repo_id: str, column: str, value: bool) -> None:
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE repos SET {column} = ? WHERE repo_id = ?", (int(value), repo_id)
            )
            self._conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no such repo_id: {repo_id}")
        self._touch_change_marker()

    def mark_indexed(self, repo_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE repos SET last_indexed = ? WHERE repo_id = ?", (now, repo_id)
            )
            self._conn.commit()

    def set_docs_path(self, repo_id: str, docs_path: str | Path | None) -> None:
        """Set (or clear, with None) the repo-relative docs path for the docs extractor.

        Registry-scoped like every other path here: the caller must already
        have registered `repo_id` via add_repo; this never introduces a new
        filesystem root outside the repo itself.
        """
        with self._lock:
            repo = self.get(repo_id)
            if repo is None:
                raise ValueError(f"no such repo_id: {repo_id}")
            value = str(docs_path) if docs_path is not None else None
            self._conn.execute(
                "UPDATE repos SET docs_path = ? WHERE repo_id = ?", (value, repo_id)
            )
            self._conn.commit()

    def set_pr_source_enabled(self, repo_id: str, enabled: bool) -> None:
        """Opt this repo in/out of PR ingestion (Phase 3). Default is off (Principle 2)."""
        self._set_flag(repo_id, "pr_source_enabled", enabled)

    def set_issue_source_enabled(self, repo_id: str, enabled: bool) -> None:
        """Opt this repo in/out of issue ingestion (Phase 3). Default is off (Principle 2)."""
        self._set_flag(repo_id, "issue_source_enabled", enabled)

    def set_last_indexed_commit(self, repo_id: str, sha: str | None) -> None:
        """Record the most recently walked commit SHA for incremental git history indexing."""
        with self._lock:
            repo = self.get(repo_id)
            if repo is None:
                raise ValueError(f"no such repo_id: {repo_id}")
            self._conn.execute(
                "UPDATE repos SET last_indexed_commit = ? WHERE repo_id = ?", (sha, repo_id)
            )
            self._conn.commit()

    _COLUMNS = (
        "repo_id, path, active, watch_enabled, last_indexed, docs_path, "
        "pr_source_enabled, issue_source_enabled, last_indexed_commit"
    )

    def get(self, repo_id: str) -> RepoRecord | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {self._COLUMNS} FROM repos WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_repos(self, active_only: bool = False) -> list[RepoRecord]:
        query = f"SELECT {self._COLUMNS} FROM repos"
        if active_only:
            query += " WHERE active = 1"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        return [self._row_to_record(r) for r in rows]

    @staticmethod
    def _row_to_record(row: tuple) -> RepoRecord:
        (
            repo_id,
            path,
            active,
            watch_enabled,
            last_indexed,
            docs_path,
            pr_source_enabled,
            issue_source_enabled,
            last_indexed_commit,
        ) = row
        return RepoRecord(
            repo_id,
            Path(path),
            bool(active),
            bool(watch_enabled),
            last_indexed,
            docs_path,
            bool(pr_source_enabled),
            bool(issue_source_enabled),
            last_indexed_commit,
        )
