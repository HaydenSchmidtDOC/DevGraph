"""Which files under a repository the indexer may read.

Shared by the dispatcher, the watcher, the schema providers and doctor, so
each scopes files the same way without importing the dispatcher.

The walk leaves out the directories named in `IGNORED_DIR_NAMES` and
whatever the repository's .gitignore files ignore (see `gitignore`). Which
walked files are worth extracting is judged by their content, separately
(`content_skip_reason`): a schema provider still represents a large or
binary file, such as an image.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from devgraph.indexer import gitignore
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


#: Why a file is left out of extraction (`content_skip_reason`, or a .gitignore).
TOO_LARGE = "too large"
BINARY = "binary"
GENERATED = "minified or generated"
GITIGNORED = "ignored by .gitignore"

#: The default of the `max_file_bytes` setting: larger files are not extracted.
DEFAULT_MAX_FILE_BYTES = 1024 * 1024
#: How much of a file is searched for a NUL byte, as git does.
BINARY_SNIFF_BYTES = 8192
#: A line this long is minified or generated, never hand-written.
MAX_LINE_BYTES = 10_000
#: A file of at least `AVERAGE_CHECK_BYTES` whose lines average more than this is too.
MAX_AVERAGE_LINE_BYTES = 200
AVERAGE_CHECK_BYTES = 4096
#: Prose has long lines (a paragraph per line), so its line length says nothing.
_PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}


def content_skip_reason(path: Path, max_bytes: int) -> str | None:
    """Why the file at `path` should not be extracted -- `TOO_LARGE` (over
    `max_bytes`), `BINARY` (a NUL byte in the first 8 KiB) or `GENERATED`
    (a line over `MAX_LINE_BYTES`, or long lines on average) -- or None.

    The size comes from a stat, so a too-large file is never read. Raises
    OSError when the file can't be read.
    """
    if os.stat(path).st_size > max_bytes:
        return TOO_LARGE
    with open(path, "rb") as f:
        data = f.read(max_bytes + 1)
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return BINARY
    if path.suffix.lower() in _PROSE_SUFFIXES:
        return None
    lines = data.splitlines() or [b""]
    if max(len(line) for line in lines) > MAX_LINE_BYTES:
        return GENERATED
    if len(data) >= AVERAGE_CHECK_BYTES and len(data) / len(lines) > MAX_AVERAGE_LINE_BYTES:
        return GENERATED
    return None


class RepoRootUnavailable(Exception):
    """A repository's root folder is missing, not a folder, or unreadable (an
    unmounted drive, a moved folder). Every scan entry point raises it before
    changing anything: walking such a root finds no files, and pruning
    against that would wipe the repository's graph."""

    def __init__(self, path: Path, problem: str, message: str | None = None) -> None:
        self.path = Path(path)
        self.problem = problem
        super().__init__(message or f"repository folder {problem}: {path}; nothing was changed")


class RepoRootEmpty(RepoRootUnavailable):
    """The root folder exists but holds no indexable file while the graph has
    files for it: what a mount point with nothing mounted looks like. Refused
    unless the caller forces it.

    `gitignored`: the folder does hold files, but its .gitignore files ignore
    every one, so the message says that instead of suggesting an unmount."""

    def __init__(self, path: Path, repo_id: str, graph_files: int, gitignored: bool = False) -> None:
        if gitignored:
            message = (
                f"repository folder has files but every file is ignored by .gitignore, while the graph has "
                f"{graph_files} for it: {path}; nothing was changed. Run `devgraph rescan {repo_id} --force` "
                f"to clear the graph"
            )
        else:
            message = (
                f"repository folder has no indexable files but the graph has {graph_files} for it "
                f"(an unmounted drive?): {path}; nothing was changed. If the files really are gone, "
                f"run `devgraph rescan {repo_id} --force`"
            )
        super().__init__(path, "empty", message)


def repo_root_problem(repo_root: Path) -> str | None:
    """Why `repo_root` can't be scanned ("not found", "not a folder", "not
    readable"), or None when it is a readable folder."""
    try:
        is_dir = stat.S_ISDIR(os.stat(repo_root).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return "not found"
    except OSError:
        return "not readable"
    if not is_dir:
        return "not a folder"
    try:
        with os.scandir(repo_root) as entries:
            next(entries, None)
    except OSError:
        return "not readable"
    return None


def check_repo_root(repo_root: Path) -> None:
    """Raise `RepoRootUnavailable` unless `repo_root` is a readable folder."""
    problem = repo_root_problem(repo_root)
    if problem is not None:
        raise RepoRootUnavailable(repo_root, problem)


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
    file, not under an ignored directory nor ignored by a .gitignore. Shared by full_scan (which indexes
    them) and prune_stale_files (which diffs them against the graph)."""
    return {path for path, _, _ in _walk(repo_root)}


def indexable_paths_under(repo_root: Path, directory: Path) -> set[Path]:
    """The `indexable_paths` of repo_root that lie under `directory`, found by
    walking only that directory. Empty for a directory outside the root, under
    an ignored directory, or a symlink (never followed, as in `_walk`)."""
    return {path for path, _, _ in _walk(repo_root, directory)}


def keyed_indexable_paths(
    repo_root: Path, *, keep_ignored_targets: bool = False, unreadable: list[str] | None = None
) -> list[tuple[Path, str]]:
    """Each of `indexable_paths` with its `repo_relative` key, leaving out one
    whose key is under an ignored directory (a symlink into one) unless
    `keep_ignored_targets`, as `prune_stale_files` needs: `index_paths` keys
    such a symlink by its target too.

    The root is resolved once. A file is keyed lexically unless it, or a
    directory above it, is a link; only those are resolved, so a symlink is
    still keyed by its target.

    `unreadable`, when given, collects each folder that could not be listed,
    as `_walk` reports it.
    """
    root = repo_root.resolve()
    keyed = []
    for path, rel, linked in _walk(repo_root, unreadable=unreadable):
        if linked:
            try:
                rel = path.resolve().relative_to(root).as_posix()
            except (OSError, ValueError):
                continue
            if not keep_ignored_targets and is_ignored_path(Path(rel)):
                continue
        keyed.append((path, rel))
    return keyed


def _walk(
    repo_root: Path, start: Path | None = None, unreadable: list[str] | None = None, honour_gitignore: bool = True
) -> Iterator[tuple[Path, str, bool]]:
    """(path, lexical repo-relative POSIX path, whether a link is on the way)
    for every indexable file: what `repo_root.rglob("*")` filtered by
    `is_indexable_file`, `is_ignored_path` and `links_outside` gives, without
    descending into ignored directories. Like rglob it does not follow a
    symlinked directory; a Windows junction it does descend into, so the files
    below one are marked as linked, but only one whose target is inside the
    repository and not under an ignored directory (the rule a symlinked file
    follows), so nothing outside the repository is ever walked. A path a
    .gitignore ignores is left out, and an ignored directory is not entered.

    `start`, a directory lexically under repo_root, walks only that subtree
    under the same rules, judged relative to repo_root.

    A folder that can't be listed is skipped; when `unreadable` is given, its
    lexical repo-relative prefix (`pkg/`, or "" for the root) is appended, so a
    caller can tell "no files here" from "couldn't look".

    `honour_gitignore=False` walks as if there were no .gitignore files.
    """
    if is_ignored_path(repo_root):
        return
    root: Path | None = None  # resolved at the first junction, if any
    stack: list[tuple[Path, str, bool, gitignore.Chain]] = [(repo_root, "", False, ())]
    if start is not None and start != repo_root:
        try:
            rel = start.relative_to(repo_root)
        except ValueError:
            return
        if is_ignored_path(rel) or start.is_symlink():
            return
        chain = gitignore.chain_to(repo_root, rel.as_posix())
        if chain is None:
            return
        junction = start.is_junction()
        if junction:
            root = repo_root.resolve()
            if not _junction_inside(start, root):
                return
        # chain_to read start's own .gitignore; the loop below reads it again.
        stack = [(start, f"{rel.as_posix()}/", junction, tuple(c for c in chain if c[0] != f"{rel.as_posix()}/"))]
    while stack:
        directory, prefix, linked, chain = stack.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            if unreadable is not None:
                unreadable.append(prefix)
            continue
        if honour_gitignore and any(entry.name == gitignore.GITIGNORE for entry in entries):
            rules = gitignore.rules_in(directory)
            if rules:
                chain = (*chain, (prefix, rules))
        for entry in entries:
            if is_ignored_dir_name(entry.name):
                continue
            path = directory / entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if chain and gitignore.matches(chain, prefix + entry.name, is_dir):
                continue
            if is_dir:
                junction = entry.is_junction()
                if junction:
                    root = root or repo_root.resolve()
                    if not _junction_inside(path, root):
                        continue
                stack.append((path, f"{prefix}{entry.name}/", linked or junction, chain))
                continue
            if not is_indexable_file(path) or (entry.is_symlink() and links_outside(path, repo_root)):
                continue
            yield path, prefix + entry.name, linked or entry.is_symlink()


def gitignore_hides_files(repo_root: Path) -> bool:
    """Whether the walk would find a file if no .gitignore applied: for a root
    whose walk found nothing, that .gitignore files, not an empty or
    unmounted folder, are why."""
    return next(_walk(repo_root, honour_gitignore=False), None) is not None


def _junction_inside(path: Path, root: Path) -> bool:
    """True for a junction whose target is inside the (resolved) root and not
    under an ignored directory there."""
    try:
        rel = path.resolve().relative_to(root)
    except (OSError, ValueError):
        logger.debug("skipping %s: junction target is outside %s", path, root)
        return False
    return not is_ignored_path(rel)


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
