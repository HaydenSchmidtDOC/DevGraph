"""The rescan-pending notice keeps a notice the answer already has, and names
the agent only for a repository it watches (audit 10 item 2 review)."""

from dataclasses import dataclass
from pathlib import Path

from devgraph.mcp import tools


@dataclass
class _Repo:
    repo_id: str = "r"
    path: Path = Path("/nowhere")
    docs_path: str | None = "docs"
    mentions_enabled: bool = True
    active: bool = True
    watch_enabled: bool = True


class _Registry:
    def __init__(self, repo):
        self.repo = repo

    def get(self, repo_id):
        return self.repo


class _Engine:
    """Records the last full scan used docs path `docs` with mentions off."""

    def run_read_cypher(self, query, parameters, *, timeout_s, max_rows):
        return [{"docs_path": "docs", "mentions": False}], False


def test_a_pending_notice_follows_an_existing_one():
    result = tools._with_notice({"count": 0, "notice": "results truncated"}, "rescan pending")
    assert result["notice"] == "results truncated; rescan pending"
    assert tools._with_notice({"count": 0}, "rescan pending")["notice"] == "rescan pending"
    assert tools._with_notice({"count": 0, "notice": "x"}, None) == {"count": 0, "notice": "x"}


def test_the_agent_is_named_only_for_a_watched_repository():
    watched = tools._rescan_pending_notice(_Engine(), _Registry(_Repo()), "r")
    assert "agent rescans it" in watched and "devgraph rescan r" in watched
    unwatched = tools._rescan_pending_notice(_Engine(), _Registry(_Repo(watch_enabled=False)), "r")
    assert "agent" not in unwatched and "devgraph rescan r" in unwatched
    inactive = tools._rescan_pending_notice(_Engine(), _Registry(_Repo(active=False)), "r")
    assert "agent" not in inactive
