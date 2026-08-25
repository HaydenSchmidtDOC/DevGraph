"""PR/issue ingestion for Phase 3 — the first DevGraph component that talks
to an external service.

Per the Implementation Plan (Phase 3) and Design Brief Principle 2
(local-first, no cloud dependencies by default), this is:

  - **Opt-in per repo**, not global: gated by RepoRegistry's
    `pr_source_enabled` / `issue_source_enabled` flags (default off).
    Nothing in this module or elsewhere calls out automatically — a caller
    must explicitly check those flags and explicitly invoke a fetch.
  - **Source-specific and pluggable**: `PRIssueSource` is a small protocol;
    `GitHubSource` is the only concrete implementation for now (per the
    brief's GitHub/GitLab/Azure DevOps mention), instantiated only when the
    caller supplies credentials — never read from an implicit/global env var
    scan that could silently enable outbound calls.

Nodes are created as `PullRequest` / `Issue`, relationships as `RESOLVES`
(PullRequest -> Issue) and `REFERENCES` (Commit -> Issue/PullRequest), per
schema.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

_CLOSES_RE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)\b", re.IGNORECASE)


@dataclass
class PullRequestNode:
    number: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class IssueNode:
    number: str
    repo_id: str
    properties: dict = field(default_factory=dict)


@dataclass
class Relationship:
    source_label: str
    source_name: str
    relationship_type: str
    target_label: str
    target_name: str


@dataclass
class ExtractionResult:
    pull_requests: list[PullRequestNode] = field(default_factory=list)
    issues: list[IssueNode] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)


class PRIssueSource(Protocol):
    """A source of PR/issue data for one repo. Implementations own their own
    network client and credentials — nothing here constructs one implicitly.
    """

    def fetch(self, repo_id: str) -> ExtractionResult: ...


class GitHubSource:
    """Fetches PRs/issues from the GitHub REST API for one owner/repo.

    Requires an explicit token — never falls back to reading credentials
    from environment variables or config on its own. The caller (CLI/agent)
    is responsible for sourcing the token explicitly and only doing so once
    the repo has opted in via RepoRegistry.pr_source_enabled /
    issue_source_enabled.
    """

    def __init__(self, owner: str, repo: str, token: str) -> None:
        if not token:
            raise ValueError("GitHubSource requires an explicit token")
        self._owner = owner
        self._repo = repo
        self._token = token

    def fetch(self, repo_id: str) -> ExtractionResult:
        """Fetch open+closed PRs and issues via the GitHub REST API.

        Deliberately minimal: pulls number/title/state/linked-issue data
        only, via the `requests` library at call time (not imported at
        module load, so importing this module never implies network access).
        """
        import requests

        result = ExtractionResult()
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }
        base = f"https://api.github.com/repos/{self._owner}/{self._repo}"

        pulls = requests.get(f"{base}/pulls", headers=headers, params={"state": "all"}, timeout=30)
        pulls.raise_for_status()
        for pr in pulls.json():
            number = str(pr["number"])
            result.pull_requests.append(
                PullRequestNode(
                    number=number,
                    repo_id=repo_id,
                    properties={
                        "title": pr.get("title"),
                        "state": pr.get("state"),
                        "url": pr.get("html_url"),
                    },
                )
            )
            linked_issue = _linked_issue_number(pr.get("body") or "")
            if linked_issue:
                result.relationships.append(
                    Relationship(
                        source_label="PullRequest",
                        source_name=number,
                        relationship_type="RESOLVES",
                        target_label="Issue",
                        target_name=linked_issue,
                    )
                )

        issues = requests.get(f"{base}/issues", headers=headers, params={"state": "all"}, timeout=30)
        issues.raise_for_status()
        for issue in issues.json():
            if "pull_request" in issue:
                continue  # GitHub's /issues endpoint also returns PRs
            number = str(issue["number"])
            result.issues.append(
                IssueNode(
                    number=number,
                    repo_id=repo_id,
                    properties={
                        "title": issue.get("title"),
                        "state": issue.get("state"),
                        "url": issue.get("html_url"),
                    },
                )
            )

        return result


def _linked_issue_number(pr_body: str) -> str | None:
    match = _CLOSES_RE.search(pr_body)
    return match.group(1) if match else None


def index_pr_issues(engine, registry, repo_id: str, source: PRIssueSource) -> None:
    """Fetch and upsert PR/issue data for a repo — caller must gate this on opt-in.

    Args:
        engine: A GraphEngine instance.
        registry: A RepoRegistry instance, used only to verify opt-in flags.
        repo_id: Repository ID — must already be registered.
        source: A PRIssueSource implementation (e.g. GitHubSource) supplying
            already-authenticated fetch access.

    Raises:
        ValueError: If repo_id is not registered, or neither
            pr_source_enabled nor issue_source_enabled is set.
    """
    repo = registry.get(repo_id)
    if repo is None:
        raise ValueError(f"no such repo_id: {repo_id}")
    if not (repo.pr_source_enabled or repo.issue_source_enabled):
        raise ValueError(
            f"repo '{repo_id}' has not opted in to PR/issue ingestion "
            "(set pr_source_enabled/issue_source_enabled via the registry first)"
        )

    result = source.fetch(repo_id)

    if repo.pr_source_enabled:
        for pr in result.pull_requests:
            engine.upsert_node("PullRequest", pr.repo_id, pr.number, pr.properties)

    if repo.issue_source_enabled:
        for issue in result.issues:
            engine.upsert_node("Issue", issue.repo_id, issue.number, issue.properties)

    if repo.pr_source_enabled and repo.issue_source_enabled:
        for rel in result.relationships:
            engine.upsert_relationship(
                rel.source_label,
                rel.source_name,
                rel.relationship_type,
                rel.target_label,
                rel.target_name,
                repo_id,
            )
