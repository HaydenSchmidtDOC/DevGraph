"""Local git reads: commit history, blame recency and branch comparison."""

from __future__ import annotations

from os import PathLike

import git


def open_repo(path: str | PathLike[str]) -> git.Repo:
    """Open a GitPython `Repo` whose git commands never fetch from a remote.

    In a partial clone (`--filter=blob:none`, `tree:0`) a read of a missing tree
    or blob would otherwise make git fetch it from the promisor remote: network
    access, maybe a credential prompt, and no deadline. With the fetch disabled
    the read fails instead, and each caller degrades. Needs git 2.44+; older git
    ignores the variable. Set before the first command, so GitPython's persistent
    `cat-file` processes inherit it too.
    """
    repo = git.Repo(path)
    repo.git.update_environment(GIT_NO_LAZY_FETCH="1")
    return repo
