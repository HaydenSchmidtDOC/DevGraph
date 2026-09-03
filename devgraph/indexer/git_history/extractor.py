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

import logging
from dataclasses import dataclass, field
from pathlib import Path

from git import Repo

from devgraph.config.settings import get_settings
from devgraph.indexer.git_history.blame import compute_function_recency

logger = logging.getLogger(__name__)


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
                    # git already reports paths relative to the repo root
                    # with forward slashes — this must match how Module
                    # nodes are keyed (python/extractor.py's index_file:
                    # repo-relative path, not bare filename) or MODIFIES
                    # edges silently fail to resolve for any nested file.
                    module_name = changed_path
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


def _upsert_extraction_result(engine, repo_id: str, result: ExtractionResult) -> None:
    for commit_node in result.commits:
        engine.upsert_node("Commit", repo_id, commit_node.sha, commit_node.properties)
    for rel in result.relationships:
        engine.upsert_relationship(
            rel.source_label,
            rel.source_name,
            rel.relationship_type,
            rel.target_label,
            rel.target_name,
            repo_id,
        )


def _is_ancestor(repo: Repo, candidate_sha: str, rev_sha: str) -> bool:
    """Wrap `Repo.is_ancestor` so a pruned/garbage-collected SHA (or any other
    lookup failure) is treated as "not an ancestor" — i.e. routed to the
    reconcile path — never mistaken for the fast path.
    """
    try:
        return repo.is_ancestor(candidate_sha, rev_sha)
    except Exception:
        return False


def _stage_module_recency(engine, repo_id: str, result: ExtractionResult) -> None:
    """Ratchet Module recency from one extraction batch's own commits.

    min/max `authored_date` is computed only among *this batch's* commits —
    correct for the fast/initial path because a ratchet only ever needs to
    know "did this batch produce something more extreme than what's already
    staged," never the full history.
    """
    track_author = get_settings().git_recency_track_author
    commit_props = {c.sha: c.properties for c in result.commits}

    touched: dict[str, list[str]] = {}
    for rel in result.relationships:
        touched.setdefault(rel.target_name, []).append(rel.source_name)

    for module_name, shas in touched.items():
        dated = [
            (commit_props[sha]["authored_date"], sha) for sha in shas if sha in commit_props
        ]
        if not dated:
            continue
        min_iso = min(iso for iso, _ in dated)
        max_iso, max_sha = max(dated, key=lambda pair: pair[0])
        author = commit_props[max_sha].get("author") if track_author else None
        engine.stage_recency(
            "Module",
            repo_id,
            module_name,
            created_at=min_iso,
            last_modified_at=max_iso,
            last_modified_by=author,
        )


def _reconcile_module_recency(engine, repo_id: str) -> None:
    """Recompute every tracked Module's recency from scratch (fresh Cypher
    aggregation over the full, now-reconciled Commit set) and overwrite via
    `set_recency` — the reconcile path's authoritative truth.
    """
    track_author = get_settings().git_recency_track_author
    rows = engine.run_cypher(
        "MATCH (c:Commit {repo_id: $repo_id})-[:MODIFIES]->(m:Module {repo_id: $repo_id}) "
        "WITH m, c ORDER BY c.authored_date ASC "
        "WITH m, collect(c) AS commits "
        "RETURN m.name AS module, commits[0].authored_date AS created_at, "
        "commits[-1].authored_date AS last_modified_at, "
        "commits[-1].author AS last_modified_by",
        {"repo_id": repo_id},
    )
    for row in rows:
        engine.set_recency(
            "Module",
            repo_id,
            row["module"],
            created_at=row["created_at"],
            last_modified_at=row["last_modified_at"],
            last_modified_by=row["last_modified_by"] if track_author else None,
        )


def _current_py_files(engine, repo_id: str) -> set[str]:
    """Every `.py` file currently backing at least one Function/Class node."""
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $repo_id}) WHERE (n:Function OR n:Class) "
        "AND n.file ENDS WITH '.py' "
        "RETURN DISTINCT n.file AS file",
        {"repo_id": repo_id},
    )
    return {row["file"] for row in rows if row["file"]}


def _apply_function_recency(
    engine, repo: Repo, repo_id: str, file_path: str, overwrite: bool
) -> None:
    """Blame `file_path` once and stage/set recency on each Function/Class
    node whose stored line range overlaps a blamed hunk.

    Only files that already have Function/Class nodes indexed are blamed at
    all — skip everything else (non-Python files, or Python files the
    indexer hasn't touched yet).

    Per entity, uses the *latest* overlapping hunk's date/author rather than
    calling stage_recency/set_recency once per overlapping hunk: the
    reconcile path uses `set_recency` (plain overwrite, no MAX-guard), so
    calling once per hunk in blame's line order would leave whichever hunk
    happens to be processed last as the final value — not necessarily the
    most recent one. Picking the max up front is correct for both paths.
    """
    if not file_path.endswith(".py"):
        return

    entities = engine.run_cypher(
        "MATCH (n {repo_id: $repo_id, file: $file_path}) WHERE n:Function OR n:Class "
        "RETURN labels(n) AS labels, n.name AS name, "
        "n.start_line AS start_line, n.end_line AS end_line",
        {"repo_id": repo_id, "file_path": file_path},
    )
    if not entities:
        return

    try:
        hunks = compute_function_recency(repo, file_path)
    except Exception as exc:
        logger.warning(f"git blame failed for {file_path!r}, skipping: {exc}")
        return

    track_author = get_settings().git_recency_track_author
    write = engine.set_recency if overwrite else engine.stage_recency

    for entity in entities:
        start_line, end_line = entity.get("start_line"), entity.get("end_line")
        if start_line is None or end_line is None:
            continue
        overlapping = [h for h in hunks if h.start_line <= end_line and h.end_line >= start_line]
        if not overlapping:
            continue
        latest = max(overlapping, key=lambda h: h.last_modified_at)
        label = "Function" if "Function" in entity["labels"] else "Class"
        write(
            label,
            repo_id,
            entity["name"],
            last_modified_at=latest.last_modified_at,
            last_modified_by=latest.last_modified_by if track_author else None,
        )


def sync_git_history(engine, registry, repo_id: str, max_count: int | None = None) -> dict:
    """Bring a repo's Commit graph and staged recency up to date with HEAD.

    Unlike `index_repo_history`, this is safe to call after history has been
    rewritten out from under a previous sync (rebase, reset, amend, branch
    switch to an unrelated branch) — it detects that case and reconciles
    instead of assuming append-only, linear history.

    Args:
        engine: A GraphEngine instance.
        registry: A RepoRegistry instance.
        repo_id: Repository ID — must already be registered.
        max_count: Optional cap on commits walked during a *first* full scan
            only (mirrors `extract_new_commits`'s `max_count`); ignored on
            the fast path and never applied during reconciliation, since an
            incomplete reconcile could leave real orphans undetected.

    Returns:
        A dict with `mode` (`"noop"`, `"initial"`, `"fast"`, or
        `"reconcile"`), `commits_indexed`, and `commits_deleted`.

    Raises:
        ValueError: If repo_id is not registered.
    """
    repo_record = registry.get(repo_id)
    if repo_record is None:
        raise ValueError(f"no such repo_id: {repo_id}")

    repo = Repo(str(repo_record.path))
    try:
        head_sha = repo.head.commit.hexsha
        last = repo_record.last_indexed_commit

        if last == head_sha:
            return {"mode": "noop", "commits_indexed": 0, "commits_deleted": 0}

        if last is None:
            mode = "initial"
        elif _is_ancestor(repo, last, head_sha):
            mode = "fast"
        else:
            mode = "reconcile"

        extractor = GitHistoryExtractor(repo_id, repo_record.path)

        if mode == "reconcile":
            # Full reachable-set walk, no cap — correctness matters more
            # than speed for what should be a rare event, and capping it
            # risks leaving real orphans undetected.
            result = extractor.extract_new_commits(since_sha=None, max_count=None)
            _upsert_extraction_result(engine, repo_id, result)

            reachable_shas = {c.sha for c in result.commits}
            # Commit nodes are keyed on (repo_id, name) with the SHA stored
            # under `name`, not a separate `sha` property — see
            # GraphEngine.delete_commits's docstring.
            graphed = engine.run_cypher(
                "MATCH (c:Commit {repo_id: $repo_id}) RETURN c.name AS sha",
                {"repo_id": repo_id},
            )
            orphans = [row["sha"] for row in graphed if row["sha"] not in reachable_shas]
            engine.delete_commits(repo_id, orphans)

            _reconcile_module_recency(engine, repo_id)
            for file_path in _current_py_files(engine, repo_id):
                _apply_function_recency(engine, repo, repo_id, file_path, overwrite=True)

            commits_indexed = len(result.commits)
            commits_deleted = len(orphans)
        else:
            since_sha = None if mode == "initial" else last
            batch_max_count = max_count if mode == "initial" else None
            result = extractor.extract_new_commits(since_sha=since_sha, max_count=batch_max_count)
            _upsert_extraction_result(engine, repo_id, result)
            _stage_module_recency(engine, repo_id, result)

            touched_py_files = {
                rel.target_name for rel in result.relationships if rel.target_name.endswith(".py")
            }
            for file_path in touched_py_files:
                _apply_function_recency(engine, repo, repo_id, file_path, overwrite=False)

            commits_indexed = len(result.commits)
            commits_deleted = 0

        registry.set_last_indexed_commit(repo_id, head_sha)
        return {"mode": mode, "commits_indexed": commits_indexed, "commits_deleted": commits_deleted}
    finally:
        repo.close()
