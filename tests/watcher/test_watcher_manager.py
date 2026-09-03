"""Tests for WatcherManager."""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from devgraph.registry.store import RepoRegistry
from devgraph.watcher.manager import WatcherManager


@pytest.fixture
def temp_registry_db():
    """Create a temporary registry database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "registry.db"
        registry = RepoRegistry(db_path)
        yield registry
        registry.close()


@pytest.fixture
def temp_git_repo():
    """Create a temporary git repository."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        # Initialize git repo
        import subprocess

        subprocess.run(
            ["git", "init"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )
        yield repo_path


def test_watcher_manager_collects_changes(temp_registry_db, temp_git_repo):
    """Test that WatcherManager collects file changes and fires callback."""
    # Register the temp repo
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    # Set up callback collection
    changes_collected = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        changes_collected[repo_id] = paths

    # Create watcher with short debounce
    watcher = WatcherManager(registry, on_changes)
    watcher.start()

    try:
        # Give observer time to start
        time.sleep(0.2)

        # Touch a file
        test_file = temp_git_repo / "test.txt"
        test_file.write_text("hello")

        # Wait for debounce + callback
        time.sleep(1.0)

        # Verify callback was called
        assert repo_record.repo_id in changes_collected
        changed_paths = changes_collected[repo_record.repo_id]
        # Resolve both paths to handle Windows 8dot3 naming
        resolved_changed = {p.resolve() for p in changed_paths}
        assert test_file.resolve() in resolved_changed
    finally:
        watcher.stop()


def test_watcher_manager_respects_watch_enabled(temp_registry_db, temp_git_repo):
    """Test that WatcherManager only watches watch-enabled repos."""
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    # Disable watching
    registry.disable_watch(repo_record.repo_id)

    changes_collected = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        changes_collected[repo_id] = paths

    watcher = WatcherManager(registry, on_changes)
    watcher.start()

    try:
        time.sleep(0.2)

        # Touch a file
        test_file = temp_git_repo / "test.txt"
        test_file.write_text("hello")

        # Wait for debounce window
        time.sleep(1.0)

        # Callback should not have been called
        assert repo_record.repo_id not in changes_collected
    finally:
        watcher.stop()


def test_watcher_manager_refresh(temp_registry_db, temp_git_repo):
    """Test that refresh() rebuilds watcher set."""
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    changes_collected = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        changes_collected[repo_id] = paths

    watcher = WatcherManager(registry, on_changes)
    watcher.start()

    try:
        time.sleep(0.2)

        # Disable the repo
        registry.disable_watch(repo_record.repo_id)
        watcher.refresh()

        # Clear collected changes
        changes_collected.clear()
        time.sleep(0.2)

        # Touch a file
        test_file = temp_git_repo / "test2.txt"
        test_file.write_text("hello")

        # Wait for debounce
        time.sleep(1.0)

        # Callback should not have been called (watch disabled)
        assert repo_record.repo_id not in changes_collected

        # Re-enable the repo
        registry.enable_watch(repo_record.repo_id)
        watcher.refresh()

        time.sleep(0.2)

        # Touch another file
        test_file2 = temp_git_repo / "test3.txt"
        test_file2.write_text("hello again")

        # Wait for debounce
        time.sleep(1.0)

        # Callback should now have been called
        assert repo_record.repo_id in changes_collected
    finally:
        watcher.stop()


def test_watcher_manager_collects_deletions(temp_registry_db, temp_git_repo):
    """Test that WatcherManager reports deleted files separately from changed ones."""
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    test_file = temp_git_repo / "to_delete.txt"
    test_file.write_text("hello")

    deletions_collected = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        if deleted:
            deletions_collected[repo_id] = deleted

    watcher = WatcherManager(registry, on_changes)
    watcher.start()

    try:
        time.sleep(0.5)  # let the initial create settle before deleting
        test_file.unlink()
        time.sleep(1.0)

        assert repo_record.repo_id in deletions_collected
        resolved_deleted = {p.resolve() for p in deletions_collected[repo_record.repo_id]}
        assert test_file.resolve() in resolved_deleted
    finally:
        watcher.stop()


def test_watcher_manager_collects_atomic_rename_save(temp_registry_db, temp_git_repo):
    """Some editors/tools save by writing a temp file then renaming it onto
    the real path, rather than modifying the real path's inode in place.
    watchdog reports that as delete(real_path) + moved(temp, real_path), not
    a modify on real_path — a handler that only implements on_modified/
    on_deleted misses the file being changed entirely (and wrongly records
    a stray deletion). This was a real bug: live reindexing silently never
    fired for saves that go through this path.
    """
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    test_file = temp_git_repo / "test_normalization.py"
    test_file.write_text("def foo():\n    pass\n")

    changes_collected = {}
    deletions_collected = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        if paths:
            changes_collected.setdefault(repo_id, set()).update(paths)
        if deleted:
            deletions_collected.setdefault(repo_id, set()).update(deleted)

    watcher = WatcherManager(registry, on_changes)
    watcher.start()

    try:
        time.sleep(0.5)  # let the initial create settle

        tmp_file = temp_git_repo / "test_normalization.py.tmp"
        tmp_file.write_text("def foo():\n    pass\n\ndef devgraph_index_probe():\n    pass\n")
        tmp_file.replace(test_file)  # atomic rename onto the real path

        time.sleep(1.5)

        assert repo_record.repo_id in changes_collected, (
            "atomic-rename save never fired a change event — this is the "
            "silent live-reindexing failure mode"
        )
        resolved_changed = {p.resolve() for p in changes_collected[repo_record.repo_id]}
        assert test_file.resolve() in resolved_changed

        # The real path must not be left recorded as deleted alongside being changed.
        resolved_deleted = {p.resolve() for p in deletions_collected.get(repo_record.repo_id, set())}
        assert test_file.resolve() not in resolved_deleted
    finally:
        watcher.stop()


def test_watcher_manager_never_accepts_raw_paths(temp_registry_db, temp_git_repo):
    """Security test: verify WatcherManager has no method accepting raw paths."""

    def dummy_callback(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        pass

    watcher = WatcherManager(temp_registry_db, dummy_callback)

    # Verify there's no start_watching(path) or similar bypass method
    assert not hasattr(watcher, "start_watching")
    assert not hasattr(watcher, "watch_path")
    assert not hasattr(watcher, "add_path")

    # All public methods should operate on registry-fetched repos only
    public_methods = {m for m in dir(watcher) if not m.startswith("_")}
    allowed_methods = {"start", "stop", "refresh"}
    extra_methods = public_methods - allowed_methods
    assert not extra_methods, f"Unexpected public methods: {extra_methods}"


def test_watcher_manager_git_state_changed_callback(temp_registry_db, temp_git_repo):
    """Test that git state changes trigger on_git_state_changed callback after debounce."""
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    git_state_changes = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        pass

    def on_git_state_changed(repo_id: str) -> None:
        git_state_changes[repo_id] = True

    watcher = WatcherManager(registry, on_changes, on_git_state_changed)
    watcher.start()

    try:
        time.sleep(0.2)

        # Modify HEAD to simulate a git state change
        head_file = temp_git_repo / ".git" / "HEAD"
        if head_file.exists():
            head_file.write_text("ref: refs/heads/main\n")

            # Wait for debounce + callback
            time.sleep(1.0)

            # Verify callback was called
            assert repo_record.repo_id in git_state_changes
    finally:
        watcher.stop()


def test_watcher_manager_git_file_worktree_doesnt_crash(temp_registry_db):
    """Test that a repo with .git as a file (linked worktree) doesn't crash on startup."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)

        # Initialize a git repo
        import subprocess

        subprocess.run(
            ["git", "init"],
            cwd=str(repo_path),
            capture_output=True,
            check=True,
        )

        # Replace .git directory with a file to simulate a linked worktree
        git_dir = repo_path / ".git"
        if git_dir.is_dir():
            import shutil

            shutil.rmtree(git_dir)
        git_dir.write_text("gitdir: /some/other/path/.git\n")

        # Register the repo and start watcher
        registry = temp_registry_db
        repo_record = registry.add_repo(repo_path)

        git_state_changes = {}

        def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
            pass

        def on_git_state_changed(repo_id: str) -> None:
            git_state_changes[repo_id] = True

        # This should not crash even though .git is a file
        watcher = WatcherManager(registry, on_changes, on_git_state_changed)
        watcher.start()

        try:
            time.sleep(0.2)

            # Create a file to ensure the watcher still works for normal file changes
            test_file = repo_path / "test.txt"
            test_file.write_text("hello")

            time.sleep(1.0)

            # Git state callback should not have been triggered (no .git directory)
            assert repo_record.repo_id not in git_state_changes
        finally:
            watcher.stop()
