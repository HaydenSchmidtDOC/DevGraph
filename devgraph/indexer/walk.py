"""Which files under a repository the indexer may read.

Shared by the dispatcher, the watcher, the schema providers and doctor, so
each scopes files the same way without importing the dispatcher.
"""

from __future__ import annotations

import logging
from pathlib import Path

from devgraph.paths import is_within

logger = logging.getLogger(__name__)

# Mirrors this project's own .gitignore: directories no full_scan (and, via
# devgraph.watcher.manager, no live watch) should ever walk into. Without
# this, `devgraph add` on any Python repo with a local venv indexes thousands
# of third-party dependency files from .venv/site-packages alongside the
# repo's actual ~dozens of source files.
IGNORED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "build",
    "dist",
    ".pytest_cache",
    ".devgraph",
    "node_modules",
    "bin",
    "obj",
    "target",
    "vendor",
    # Kotlin/Gradle build + tool scratch (Kotlin extractor, "motonav" plan):
    # .gradle is the venv-equivalent (build cache + expanded AAR dependency
    # sources); .kotlin is the compiler session cache; the rest are IDE/MCP
    # tool scratch that's never source.
    ".gradle",
    ".kotlin",
    ".idea",
    ".serena",
    ".playwright-mcp",
    # C++ build-directory conventions (Implementation Plan #8, C++ row):
    # CLion/CMake's default out-of-source build dir names.
    "cmake-build-debug",
    "cmake-build-release",
    # Tool-generated scratch caches that can land inside a registered repo's
    # working tree (e.g. a research skill's local cache dir) rather than a
    # true temp directory. Never source, never worth graphing.
    ".firecrawl",
    # Agent worktrees (.worktrees/ at the repo root, .claude/worktrees/ for
    # ones a coding agent spawns). Each is a full checkout of the repo it
    # lives inside, so walking them indexes the entire repo again per live
    # worktree — the same function then legitimately exists at N paths and
    # becomes N nodes under the file-scoped MERGE key, silently multiplying
    # the graph by however many worktrees happen to be open at scan time.
    ".worktrees",
    "worktrees",
}


def is_ignored_dir_name(name: str) -> bool:
    return name in IGNORED_DIR_NAMES or name.endswith(".egg-info")


def is_ignored_path(path: Path) -> bool:
    return any(is_ignored_dir_name(part) for part in path.parts)


def is_indexable_file(path: Path) -> bool:
    """p.is_file(), but a file the OS can't even stat (locked, broken
    symlink, Windows reparse point) is skipped rather than aborting the
    whole scan."""
    try:
        return path.is_file()
    except OSError:
        return False


def indexable_paths(repo_root: Path) -> set[Path]:
    """Every file under repo_root that a full scan would index: a regular
    file, not under an ignored directory. Shared by full_scan (which indexes
    them) and prune_stale_files (which diffs them against the graph)."""
    return {
        p for p in repo_root.rglob("*")
        if is_indexable_file(p) and not is_ignored_path(p) and not links_outside(p, repo_root)
    }


def repo_relative(repo_root: Path, path: Path) -> str | None:
    """Repo-relative POSIX path, or None for a path outside the repository.

    Resolved first, so a symlink is keyed by its target.
    """
    try:
        return Path(path).resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def links_outside(path: Path, repo_root: Path) -> bool:
    """True for a symlink whose target resolves outside repo_root.

    A symlink whose target cannot be resolved (OSError, e.g. a loop) is also
    treated as outside, so it is skipped rather than followed.
    """
    try:
        if not path.is_symlink() or is_within(path.resolve(), repo_root):
            return False
    except OSError:
        pass
    logger.debug("skipping %s: symlink target is outside %s", path, repo_root)
    return True
