"""Unit tests for the Phase 3 PR/issue ingestion opt-in gating and GitHubSource.

No real network calls: GitHubSource.fetch() is tested by monkeypatching
`requests.get` so importing/running this test suite never talks to GitHub.
"""

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devgraph.indexer.pr_issues.extractor import (
    ExtractionResult,
    GitHubSource,
    IssueNode,
    PullRequestNode,
    Relationship,
    index_pr_issues,
    _linked_issue_number,
)
from devgraph.registry.store import RepoRegistry


class TestGitHubSourceRequiresToken:
    def test_empty_token_raises(self):
        with pytest.raises(ValueError):
            GitHubSource("owner", "repo", "")


class TestLinkedIssueNumber:
    def test_closes_keyword(self):
        assert _linked_issue_number("This closes #42") == "42"

    def test_fixes_keyword(self):
        assert _linked_issue_number("Fixes #7 for real") == "7"

    def test_no_match(self):
        assert _linked_issue_number("No linked issue here") is None

    def test_case_insensitive(self):
        assert _linked_issue_number("RESOLVES #99") == "99"


class TestGitHubSourceFetch:
    def test_fetch_parses_prs_and_issues(self):
        source = GitHubSource("owner", "repo", "fake-token")

        mock_pulls_response = MagicMock()
        mock_pulls_response.json.return_value = [
            {"number": 7, "title": "Fix auth bug", "state": "closed", "html_url": "http://x/pr7", "body": "Closes #42"}
        ]
        mock_pulls_response.raise_for_status.return_value = None

        mock_issues_response = MagicMock()
        mock_issues_response.json.return_value = [
            {"number": 42, "title": "Auth bug", "state": "closed", "html_url": "http://x/42"},
            {"number": 7, "title": "Fix auth bug", "state": "closed", "pull_request": {}},  # filtered out
        ]
        mock_issues_response.raise_for_status.return_value = None

        with patch("requests.get", side_effect=[mock_pulls_response, mock_issues_response]):
            result = source.fetch("test-repo")

        assert len(result.pull_requests) == 1
        assert result.pull_requests[0].number == "7"
        assert len(result.issues) == 1
        assert result.issues[0].number == "42"
        assert len(result.relationships) == 1
        assert result.relationships[0].target_name == "42"


class _FakeSource:
    def __init__(self, result: ExtractionResult) -> None:
        self._result = result

    def fetch(self, repo_id: str) -> ExtractionResult:
        return self._result


@pytest.fixture
def temp_git_repo():
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
        yield repo_path


@pytest.fixture
def registry():
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = RepoRegistry(Path(tmpdir) / "registry.db")
        yield reg
        reg.close()


class TestIndexPrIssuesGating:
    def test_raises_if_not_opted_in(self, registry, temp_git_repo):
        record = registry.add_repo(temp_git_repo)
        fake_engine = MagicMock()
        source = _FakeSource(ExtractionResult())

        with pytest.raises(ValueError, match="opted in"):
            index_pr_issues(fake_engine, registry, record.repo_id, source)

    def test_raises_for_unknown_repo(self, registry):
        fake_engine = MagicMock()
        source = _FakeSource(ExtractionResult())
        with pytest.raises(ValueError, match="no such repo_id"):
            index_pr_issues(fake_engine, registry, "nonexistent", source)

    def test_upserts_only_opted_in_node_types(self, registry, temp_git_repo):
        record = registry.add_repo(temp_git_repo)
        registry.set_pr_source_enabled(record.repo_id, True)
        # issue_source_enabled left False

        fake_engine = MagicMock()
        result = ExtractionResult(
            pull_requests=[PullRequestNode(number="7", repo_id=record.repo_id, properties={})],
            issues=[IssueNode(number="42", repo_id=record.repo_id, properties={})],
        )
        source = _FakeSource(result)

        index_pr_issues(fake_engine, registry, record.repo_id, source)

        upsert_calls = fake_engine.upsert_node.call_args_list
        labels_upserted = {call.args[0] for call in upsert_calls}
        assert "PullRequest" in labels_upserted
        assert "Issue" not in labels_upserted

    def test_relationships_only_when_both_enabled(self, registry, temp_git_repo):
        record = registry.add_repo(temp_git_repo)
        registry.set_pr_source_enabled(record.repo_id, True)
        registry.set_issue_source_enabled(record.repo_id, True)

        fake_engine = MagicMock()
        result = ExtractionResult(
            pull_requests=[PullRequestNode(number="7", repo_id=record.repo_id, properties={})],
            issues=[IssueNode(number="42", repo_id=record.repo_id, properties={})],
            relationships=[
                Relationship("PullRequest", "7", "RESOLVES", "Issue", "42"),
            ],
        )
        source = _FakeSource(result)

        index_pr_issues(fake_engine, registry, record.repo_id, source)

        assert fake_engine.upsert_relationship.call_count == 1
