"""find_related_prs and issue_history_for never reach the network: with the
repository's PR or issue source off they say how to turn it on."""

import subprocess

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.mcp import tools
from devgraph.mcp.tools import find_related_prs, issue_history_for
from devgraph.registry.store import RepoRecord


class _Registry:
    def __init__(self, record):
        self.record = record

    def get(self, repo_id):
        return self.record if repo_id == self.record.repo_id else None


class _Graph:
    def __init__(self, file_exists=True):
        self.queries = []
        self.params = []
        self.file_exists = file_exists

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        self.queries.append(query)
        self.params.append(params)
        return ([{"found": 1}] if self.file_exists and query.endswith("RETURN 1 LIMIT 1") else []), False


@pytest.fixture
def no_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a subprocess was started: {args}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


@pytest.mark.parametrize(
    ("tool", "command"),
    [(find_related_prs, "devgraph pr-source widget enable"), (issue_history_for, "devgraph issue-source widget enable")],
)
def test_a_source_that_is_off_returns_a_notice_and_runs_nothing(tmp_path, no_subprocess, tool, command):
    registry = _Registry(RepoRecord("widget", tmp_path, True, True, None))
    graph = _Graph()
    result = tool(graph, "widget", "widget/core.py", registry=registry)
    assert result["count"] == 0 and result["results"] == [] and result["truncated"] is False
    assert command in result["notice"]
    assert len(graph.queries) == 1 and graph.queries[0].endswith("RETURN 1 LIMIT 1")  # only: does the file exist


@pytest.mark.parametrize("tool", [find_related_prs, issue_history_for])
def test_an_unknown_file_is_an_error_even_with_the_source_off(tmp_path, no_subprocess, tool):
    registry = _Registry(RepoRecord("widget", tmp_path, True, True, None))
    with pytest.raises(ToolError, match="no file named 'widget/nope.py'"):
        tool(_Graph(file_exists=False), "widget", "widget/nope.py", registry=registry)


@pytest.mark.parametrize(
    ("tool", "flag"), [(find_related_prs, "pr_source_enabled"), (issue_history_for, "issue_source_enabled")]
)
def test_a_source_that_is_on_reads_the_graph(tmp_path, no_subprocess, tool, flag):
    registry = _Registry(RepoRecord("widget", tmp_path, True, True, None, **{flag: True}))
    graph = _Graph()
    result = tool(graph, "widget", "widget/core.py", registry=registry)
    assert "notice" not in result
    assert len(graph.queries) == 2  # the read, then (it being empty) whether the file exists
    assert "MODIFIES" in graph.queries[0]


def test_no_gh_fallback_is_left():
    assert not any(hasattr(tools, name) for name in ("_gh_pr_list", "_gh_issue_list", "_resolve_gh_repo"))


class _Down:
    uri = "bolt://127.0.0.1:9"

    def run_read_cypher(self, query, params, *, timeout_s, max_rows):
        from neo4j.exceptions import ServiceUnavailable

        raise ServiceUnavailable("refused")


@pytest.mark.parametrize(("tool", "command"), [(find_related_prs, "pr-source"), (issue_history_for, "issue-source")])
def test_with_the_source_off_and_neo4j_down_the_notice_comes_first(tmp_path, no_subprocess, tool, command):
    registry = _Registry(RepoRecord("widget", tmp_path, True, True, None))
    result = tool(_Down(), "widget", ".\\widget\\core.py", registry=registry)
    assert f"devgraph {command} widget enable" in result["notice"]


def test_a_path_is_normalised(tmp_path, no_subprocess):
    registry = _Registry(RepoRecord("widget", tmp_path, True, True, None, pr_source_enabled=True))
    graph = _Graph()
    find_related_prs(graph, "widget", ".\\widget\\core.py", registry=registry)
    assert graph.params[0]["component_name"] == "widget/core.py"
