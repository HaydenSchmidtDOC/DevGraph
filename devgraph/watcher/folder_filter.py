"""Which folders the Linux root watch leaves out of the kernel watch
(`devgraph.watcher.inotify`). Kept apart from that module, which loads libc
through watchdog on import, so the rule itself can be checked on any platform.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from devgraph.indexer.gitignore import is_gitignored
from devgraph.indexer.walk import is_ignored_path

#: The folders of a repository's git state the root watch keeps, with
#: everything below the last: `.git` (HEAD, packed-refs, index.lock),
#: `.git/refs` (refs/stash) and `.git/refs/heads` (branch tips). Remote-
#: tracking refs and tags stay unwatched, so a background fetch triggers nothing.
_GIT_STATE = ((".git",), (".git", "refs"), (".git", "refs", "heads"))


def ignored_folder_filter(root: Path, *, watch_git: bool = False) -> Callable[[bytes], bool]:
    """Whether a folder under `root` is one the indexer never enters, and,
    with `watch_git`, not part of the git state (`_GIT_STATE`)."""

    def ignored(path: bytes) -> bool:
        try:
            rel = Path(os.fsdecode(path)).relative_to(root)
        except ValueError:
            return False
        if not rel.parts:
            return False
        if watch_git and rel.parts[0] == ".git":
            return not (rel.parts in _GIT_STATE or rel.parts[:3] == _GIT_STATE[2])
        return is_ignored_path(rel, root) or is_gitignored(root, rel.as_posix(), is_dir=True)

    return ignored
