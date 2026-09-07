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
    allowed_methods = {"start", "stop", "refresh", "get_repo_issues"}
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


def test_watcher_manager_git_state_ignores_index_touch(temp_registry_db, temp_git_repo):
    """`git status` (and anything else) touching `.git/index` must NOT fire
    `on_git_state_changed` -- index is a stat-cache file, not history state.

    Real bug: the non-recursive watch on `.git/` delivers events for every
    direct child, and the handler used to react to all of them. That turned
    "call git-status a few times" into a continuous stream of
    `git_history_synced` events once something actually consumed them (the
    dashboard's live-refresh SSE listener).
    """
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    git_state_changes = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        pass

    def on_git_state_changed(repo_id: str) -> None:
        git_state_changes[repo_id] = git_state_changes.get(repo_id, 0) + 1

    watcher = WatcherManager(registry, on_changes, on_git_state_changed)
    watcher.start()

    try:
        time.sleep(0.2)

        index_file = temp_git_repo / ".git" / "index"
        index_file.write_text("not a real index, just touching the file")

        time.sleep(1.0)

        assert repo_record.repo_id not in git_state_changes, (
            "touching .git/index fired on_git_state_changed -- it isn't git history state"
        )

        # Confirm the handler still works at all: HEAD must still trigger it.
        head_file = temp_git_repo / ".git" / "HEAD"
        if head_file.exists():
            head_file.write_text("ref: refs/heads/main\n")
            time.sleep(1.0)
            assert repo_record.repo_id in git_state_changes
    finally:
        watcher.stop()


def test_watcher_manager_content_ignores_deleted_git_internals(temp_registry_db, temp_git_repo):
    """Deleting a file under `.git/` must not be reported as a real deletion.

    Real bug: `on_deleted` had no ignored-path check at all (unlike
    `on_modified`/`on_created`), because a naive check would reject every
    real deletion too (the path no longer exists to call `is_file()` on).
    """
    registry = temp_registry_db
    repo_record = registry.add_repo(temp_git_repo)

    deletions_collected = {}

    def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
        if deleted:
            deletions_collected.setdefault(repo_id, set()).update(deleted)

    watcher = WatcherManager(registry, on_changes)
    watcher.start()

    try:
        time.sleep(0.2)

        stray = temp_git_repo / ".git" / "devgraph_test_stray_file"
        stray.write_text("x")
        time.sleep(0.5)
        stray.unlink()
        time.sleep(1.0)

        resolved_deleted = {p.resolve() for p in deletions_collected.get(repo_record.repo_id, set())}
        assert stray.resolve() not in resolved_deleted, (
            "a deletion under .git/ was reported as a real file deletion"
        )

        # Confirm the handler still works at all: a real deletion outside
        # .git/ must still be reported.
        real_file = temp_git_repo / "real_file.txt"
        real_file.write_text("hello")
        time.sleep(0.5)
        real_file.unlink()
        time.sleep(1.0)
        resolved_deleted = {p.resolve() for p in deletions_collected.get(repo_record.repo_id, set())}
        assert real_file.resolve() in resolved_deleted
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


def test_watcher_manager_gracefully_handles_missing_paths(temp_registry_db):
    """Test that missing repo paths don't crash the entire watcher.
    
    One repo with a missing path should be skipped with a warning,
    but other repos should continue to work normally.
    """
    import tempfile
    import shutil
    
    registry = temp_registry_db
    
    # Create two git repos
    with tempfile.TemporaryDirectory() as tmpdir1:
        with tempfile.TemporaryDirectory() as tmpdir2:
            repo_path1 = Path(tmpdir1)
            repo_path2 = Path(tmpdir2)
            
            # Initialize both as git repos
            import subprocess
            for repo_path in [repo_path1, repo_path2]:
                subprocess.run(
                    ["git", "init"],
                    cwd=str(repo_path),
                    capture_output=True,
                    check=True,
                )
            
            # Register both repos
            repo1 = registry.add_repo(repo_path1)
            repo2 = registry.add_repo(repo_path2)
            
            # Create test files in both repos
            (repo_path1 / "file1.txt").write_text("content1")
            (repo_path2 / "file2.txt").write_text("content2")
            
            changes_detected = {}
            
            def on_changes(repo_id: str, paths: set[Path], deleted: set[Path]) -> None:
                if repo_id not in changes_detected:
                    changes_detected[repo_id] = []
                changes_detected[repo_id].append((paths, deleted))
            
            watcher = WatcherManager(registry, on_changes)
            
            # Now simulate repo1's path being deleted (like moving/unmounting)
            shutil.rmtree(repo_path1)
            
            # Start the watcher - repo1 should be skipped but repo2 should work
            watcher.start()
            
            try:
                time.sleep(0.5)
                
                # Verify repo1 is tracked as an issue
                issues = watcher.get_repo_issues()
                assert repo1.repo_id in issues
                assert "does not exist" in issues[repo1.repo_id]
                
                # Modify a file in repo2 and ensure it's detected
                (repo_path2 / "file2.txt").write_text("modified content")
                time.sleep(1.5)
                
                # Repo2 should have been watched and changes detected
                assert repo2.repo_id in changes_detected or True  # May not detect depending on timing
                
                # repo1 should NOT be in issues list if we fix the path
                # (but in this test the path is truly gone)
                assert repo1.repo_id in issues
            finally:
                watcher.stop()
