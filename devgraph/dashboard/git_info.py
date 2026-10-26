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

from devgraph.indexer.git_history import open_repo


def get_git_log(repo_path: Path, limit: int) -> list[dict[str, Any]]:
    """Real commits, newest first, with real parent hashes.

    Parent hashes (not a fabricated "branch name") are what let the
    frontend lay out a graph/lanes view -- a commit can belong to many
    branches or none once merged, so a single "branch" field per commit
    would be inventing data git itself doesn't have. Merge detection
    (`len(parents) > 1`) and lane layout are derived client-side from this
    real parent graph.
    """
    with open_repo(repo_path) as repo:
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

    Uses `git status --porcelain=v1 -z` directly (same approach as `mcp/tools.py`'s
    other `git_repo.git.<cmd>()` calls) rather than GitPython's diff/index
    APIs, which is simpler to get right for the modified/untracked/staged
    distinction the porcelain format already encodes per-line.
    """
    with open_repo(repo_path) as repo:
        try:
            branch = repo.active_branch.name
        except TypeError:
            try:
                branch = f"detached@{repo.head.commit.hexsha[:7]}"
            except (ValueError, TypeError):
                branch = "no commits yet"

        # `-z`: paths as they are, NUL-terminated (without it a path with a
        # space or a non-ASCII character is C-quoted), and a rename or copy
        # as two records, the new path then the one it came from.
        records = repo.git.status("--porcelain=v1", "-z").split("\0")
        entries: list[dict[str, str]] = []
        i = 0
        while i < len(records):
            record = records[i]
            i += 1
            if len(record) < 4:
                continue
            code, path = record[:2], record[3:]
            old_path = None
            if code == "??":
                state = "untracked"
            elif "U" in code or code in ("AA", "DD"):
                state = "unmerged"  # a merge conflict: DD, AU, UD, UA, DU, AA, UU
            elif "R" in code or "C" in code:
                state = "renamed" if "R" in code else "copied"
                old_path = records[i]
                i += 1
            elif "M" in code:
                state = "modified"
            elif "A" in code:
                state = "added"
            elif "D" in code:
                state = "deleted"
            else:
                state = "changed"
            entry = {"path": path, "state": state}
            if old_path is not None:
                entry["old_path"] = old_path
            entries.append(entry)

        return {"branch": branch, "uncommitted": entries}
