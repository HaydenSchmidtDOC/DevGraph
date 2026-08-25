"""Git history extractor for Phase 3.

Walks a repo's commit log via GitPython, incrementally (only commits newer
than the registry's `last_indexed_commit` for that repo_id), and creates
`Commit` nodes linked `MODIFIES` to the `Module` nodes for files each commit
touched. This is purely local (reads the repo's own `.git`) — no network
calls, unlike PR/issue ingestion.

`Module` targets are matched by file path against Module.name as set by the
Python indexer's `index_file` (which uses the file's name/relative path as
the Module node's name) — a MODIFIES edge only materializes if that Module
node already exists (same constraint as every other cross-extractor edge in
this codebase; see indexer/python/extractor.py's index_file for precedent).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from git import Repo


@dataclass
class CommitNode:
    """A Commit node."""

    sha: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class Relationship:
    """A MODIFIES relationship: Commit -> Module."""

    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str


@dataclass
class ExtractionResult:
    """Result of git history extraction."""

    commits: list[CommitNode] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)


class GitHistoryExtractor:
    """Walks commit history for a single repo, incrementally by SHA."""

    def __init__(self, repo_id: str, repo_path: str | Path) -> None:
        self.repo_id = repo_id
        self.repo_path = Path(repo_path)

    def extract_new_commits(self, since_sha: str | None = None, max_count: int | None = None) -> ExtractionResult:
        """Extract commits newer than `since_sha` (exclusive), oldest first.

        Args:
            since_sha: Only commits after this SHA are returned. None means
                walk the full history (first index).
            max_count: Optional cap on number of commits walked, for safety
                on very large repos during a first full scan.

        Returns:
            ExtractionResult with Commit nodes and MODIFIES relationships,
            in chronological order (oldest first) so replaying them
            preserves history order.
        """
        result = ExtractionResult()
        repo = Repo(str(self.repo_path))
        try:
            commits = list(repo.iter_commits(max_count=max_count))
            commits.reverse()  # iter_commits is newest-first; we want oldest-first

            if since_sha is not None:
                seen_boundary = False
                filtered = []
                for commit in commits:
                    if not seen_boundary:
                        if commit.hexsha == since_sha:
                            seen_boundary = True
                        continue
                    filtered.append(commit)
                commits = filtered

            for commit in commits:
                sha = commit.hexsha
                result.commits.append(
                    CommitNode(
                        sha=sha,
                        repo_id=self.repo_id,
                        properties={
                            "message": commit.message.strip(),
                            "author": commit.author.name if commit.author else None,
                            "authored_date": commit.authored_datetime.isoformat(),
                        },
                    )
                )

                for changed_path in _changed_paths(commit):
                    module_name = Path(changed_path).name
                    result.relationships.append(
                        Relationship(
                            source_label="Commit",
                            source_name=sha,
                            relationship_type="MODIFIES",
                            target_label="Module",
                            target_name=module_name,
                        )
                    )
        finally:
            repo.close()

        return result


def _changed_paths(commit) -> list[str]:
    """Return file paths touched by a commit (diff against its first parent, or full tree for a root commit)."""
    if commit.parents:
        diffs = commit.parents[0].diff(commit)
    else:
        diffs = commit.diff(None)  # root commit: diff against empty tree isn't directly exposed; fall back to tree walk
        diffs = [d for d in commit.tree.traverse() if d.type == "blob"]
        return [d.path for d in diffs]

    paths = set()
    for d in diffs:
        if d.a_path:
            paths.add(d.a_path)
        if d.b_path:
            paths.add(d.b_path)
    return list(paths)


def index_repo_history(engine, registry, repo_id: str, max_count: int | None = None) -> int:
    """Incrementally index git history for a registered repo and upsert into the graph.

    Args:
        engine: A GraphEngine instance.
        registry: A RepoRegistry instance (source of truth for repo_path/last_indexed_commit).
        repo_id: Repository ID — must already be registered.
        max_count: Optional cap for a first full scan.

    Returns:
        Number of new commits indexed.

    Raises:
        ValueError: If repo_id is not registered.
    """
    repo = registry.get(repo_id)
    if repo is None:
        raise ValueError(f"no such repo_id: {repo_id}")

    extractor = GitHistoryExtractor(repo_id, repo.path)
    result = extractor.extract_new_commits(since_sha=repo.last_indexed_commit, max_count=max_count)

    for commit_node in result.commits:
        engine.upsert_node("Commit", commit_node.repo_id, commit_node.sha, commit_node.properties)

    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label,
            rel.source_name,
            rel.relationship_type,
            rel.target_label,
            rel.target_name,
            repo_id,
        )

    if result.commits:
        registry.set_last_indexed_commit(repo_id, result.commits[-1].sha)

    return len(result.commits)
