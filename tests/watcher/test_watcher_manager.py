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

    def on_changes(repo_id: str, paths: set[Path]) -> None:
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

    def on_changes(repo_id: str, paths: set[Path]) -> None:
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

    def on_changes(repo_id: str, paths: set[Path]) -> None:
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


def test_watcher_manager_never_accepts_raw_paths(temp_registry_db, temp_git_repo):
    """Security test: verify WatcherManager has no method accepting raw paths."""

    def dummy_callback(repo_id: str, paths: set[Path]) -> None:
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
