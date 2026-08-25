"""Regression tests for RepoRegistry's cross-thread SQLite access.

Real-world bug: RepoRegistry is created on the tray app's main thread, but
WatcherManager fires its debounce callback (which calls registry.get() and
registry.mark_indexed()) on a fresh threading.Timer thread every time a file
change settles. sqlite3.connect()'s default check_same_thread=True raised
"SQLite objects created in a thread can only be used in that same thread" on
essentially every watcher-triggered reindex — silently, since the exception
propagated out of the Timer thread and was swallowed by Python's default
threading excepthook rather than surfacing anywhere a user would see it. Live
auto-indexing looked "on" (tray running, heartbeat healthy) while silently
never actually reindexing anything after the first debounce fire.
"""

import tempfile
import threading
from pathlib import Path

import pytest

from devgraph.registry.store import RepoRegistry


@pytest.fixture
def temp_git_repo():
    import subprocess

    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        yield repo_path


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.db")
        yield reg
        reg.close()


def test_get_from_a_different_thread_than_the_one_that_created_the_registry(registry, temp_git_repo):
    """Mirrors WatcherManager's real usage: registry.get() invoked from a
    thread other than the one that constructed RepoRegistry."""
    record = registry.add_repo(temp_git_repo)

    result: dict = {}
    error: dict = {}

    def worker():
        try:
            result["repo"] = registry.get(record.repo_id)
        except Exception as e:
            error["exc"] = e

    # threading.Timer is what WatcherManager actually uses for its debounce
    # callback — use the same primitive here rather than a bare Thread.
    timer = threading.Timer(0.01, worker)
    timer.start()
    timer.join(timeout=5)

    assert "exc" not in error, f"cross-thread access raised: {error.get('exc')}"
    assert result.get("repo") is not None
    assert result["repo"].repo_id == record.repo_id


def test_mark_indexed_from_a_different_thread(registry, temp_git_repo):
    """mark_indexed is the other call WatcherManager's callback makes after
    a successful reindex — must also be safe from a Timer thread."""
    record = registry.add_repo(temp_git_repo)

    error: dict = {}

    def worker():
        try:
            registry.mark_indexed(record.repo_id)
        except Exception as e:
            error["exc"] = e

    timer = threading.Timer(0.01, worker)
    timer.start()
    timer.join(timeout=5)

    assert "exc" not in error, f"cross-thread access raised: {error.get('exc')}"
    updated = registry.get(record.repo_id)
    assert updated.last_indexed is not None


def test_repeated_cross_thread_calls_all_succeed(registry, temp_git_repo):
    """The real failure mode wasn't a one-off race — it recurred on every
    single debounce fire, since it's a thread-identity mismatch, not a
    timing coincidence. Fire several sequential Timer-thread calls and
    confirm every one succeeds, not just (coincidentally) the first."""
    record = registry.add_repo(temp_git_repo)

    errors = []

    def worker(i):
        try:
            registry.mark_indexed(record.repo_id)
            fetched = registry.get(record.repo_id)
            assert fetched is not None
        except Exception as e:
            errors.append((i, e))

    for i in range(5):
        timer = threading.Timer(0.01, worker, args=(i,))
        timer.start()
        timer.join(timeout=5)

    assert errors == [], f"cross-thread access failed on iteration(s): {errors}"


def test_concurrent_cross_thread_calls_do_not_corrupt_state(registry, temp_git_repo):
    """Multiple registered repos' watchers can fire debounce callbacks at
    roughly the same time on independent threads — the shared connection
    must serialize access correctly rather than interleaving badly."""
    records = [registry.add_repo(_make_repo()) for _ in range(4)]

    errors = []
    barrier = threading.Barrier(len(records))

    def worker(repo_id):
        try:
            barrier.wait(timeout=5)
            for _ in range(10):
                registry.mark_indexed(repo_id)
                assert registry.get(repo_id) is not None
        except Exception as e:
            errors.append((repo_id, e))

    threads = [threading.Thread(target=worker, args=(r.repo_id,)) for r in records]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"concurrent cross-thread access failed: {errors}"


def _make_repo():
    import subprocess

    tmpdir = tempfile.mkdtemp()
    repo_path = Path(tmpdir)
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    return repo_path
