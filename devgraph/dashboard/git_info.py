"""Real git log/status for the dashboard's Git history panel.

Backs `/api/repos/{repo_id}/git-log` and `/api/repos/{repo_id}/git-status` --
replaces the prototype's hardcoded commit list. Field names/conventions
mirror `indexer/git_history/extractor.py` (`hexsha`, `message.strip()`,
`author.name`, `authored_datetime`) since that's the existing convention for
reading commits in this codebase.

Every function opens its own `Repo` in a `with` block -- GitPython holds
open file handles into `.git/` for the life of a `Repo` object, and leaving
one open blocks its directory from being removed/renamed on Windows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from git import Repo


def get_git_log(repo_path: Path, limit: int) -> list[dict[str, Any]]:
    """Real commits, newest first, with real parent hashes.

    Parent hashes (not a fabricated "branch name") are what let the
    frontend lay out a graph/lanes view -- a commit can belong to many
    branches or none once merged, so a single "branch" field per commit
    would be inventing data git itself doesn't have. Merge detection
    (`len(parents) > 1`) and lane layout are derived client-side from this
    real parent graph.
    """
    with Repo(str(repo_path)) as repo:
        try:
            commits = list(repo.iter_commits(max_count=limit))
        except ValueError:
            return []  # repo has no commits yet -- a normal state, not an error
        return [
            {
                "hash": c.hexsha,
                "short_hash": c.hexsha[:7],
                "parents": [p.hexsha for p in c.parents],
                "author": c.author.name if c.author else None,
                "date": c.authored_datetime.isoformat(),
                "merge": len(c.parents) > 1,
                "title": c.summary,
                "body": c.message.strip()[len(c.summary) :].strip(),
            }
            for c in commits
        ]


def get_git_status(repo_path: Path) -> dict[str, Any]:
    """Real working-tree state: current branch + modified/untracked paths.

    Uses `git status --porcelain` directly (same approach as `mcp/tools.py`'s
    other `git_repo.git.<cmd>()` calls) rather than GitPython's diff/index
    APIs, which is simpler to get right for the modified/untracked/staged
    distinction the porcelain format already encodes per-line.
    """
    with Repo(str(repo_path)) as repo:
        try:
            branch = repo.active_branch.name
        except TypeError:
            try:
                branch = f"detached@{repo.head.commit.hexsha[:7]}"
            except (ValueError, TypeError):
                branch = "no commits yet"

        porcelain = repo.git.status("--porcelain")
        entries: list[dict[str, str]] = []
        for line in porcelain.splitlines():
            if not line.strip():
                continue
            code, path = line[:2], line[3:]
            if code == "??":
                state = "untracked"
            elif "M" in code:
                state = "modified"
            elif "A" in code:
                state = "added"
            elif "D" in code:
                state = "deleted"
            else:
                state = "changed"
            entries.append({"path": path, "state": state})

        return {"branch": branch, "uncommitted": entries}
