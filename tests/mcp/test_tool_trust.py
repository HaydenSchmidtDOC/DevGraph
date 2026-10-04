"""Project tools are served only after the user trusts the file's exact bytes. Stub engine, real registry."""

import pytest

from devgraph.config.global_tools import save_global_tools
from devgraph.config.project_tools import TOOLS_FILENAME
from devgraph.registry.store import RepoRegistry
from tests.mcp.test_tool_reload import ONE, TWO, build, status, tools, write

G_LIST = {
    "name": "list_files",
    "description": "Global list.",
    "cypher": "MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path",
}
NOT_TRUSTED = "project tools not trusted (run devgraph config tools trust demo)"


@pytest.fixture
def trust(real_project_trust, tmp_path, monkeypatch):
    """A registry with `demo` registered at tmp_path/demo; `trust.approve()` trusts the file as it is now."""
    db = tmp_path / "trust.sqlite3"
    monkeypatch.setattr(real_project_trust, "_registry_db_path", lambda: db)
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    registry = RepoRegistry(db)
    registry.add_repo(repo, repo_id="demo")

    def approve():
        sha = real_project_trust.tools_sha256((repo / TOOLS_FILENAME).read_bytes())
        registry.set_project_tools_sha256("demo", sha)

    real_project_trust.approve = approve
    real_project_trust.registry = registry
    real_project_trust.db = db
    yield real_project_trust
    registry.close()


def test_an_untrusted_file_is_not_served(trust, tmp_path, monkeypatch):
    server, _ = build(tmp_path, monkeypatch)
    assert "list_files" not in tools(server)
    current = status(server)
    assert current["served"] == []
    assert any(NOT_TRUSTED in n for n in current["notices"])
    assert current["project_tools_trust"] == "untrusted"


def test_a_trusted_file_is_served(trust, tmp_path, monkeypatch):
    write(tmp_path / "demo", ONE)
    trust.approve()
    server, _ = build(tmp_path, monkeypatch)
    assert "list_files" in tools(server)
    current = status(server)
    assert current["project_tools_trust"] == "trusted"
    assert current["notices"] == []


def test_an_edit_stops_serving_until_retrusted_and_is_never_kept_as_last_good(trust, tmp_path, monkeypatch):
    write(tmp_path / "demo", ONE)
    trust.approve()
    server, repo = build(tmp_path, monkeypatch)
    plane = server.devgraph_tool_plane
    assert "list_files" in tools(server)

    write(repo, TWO)
    assert plane.reload_if_changed() is True
    assert tools(server).keys().isdisjoint({"list_files", "count_files"})
    current = status(server)
    assert current["project_tools_trust"] == "changed"
    assert any("changed since it was trusted" in n and NOT_TRUSTED in n for n in current["notices"])

    write(repo, "version: 1\ntools: [")  # an invalid save while untrusted: still nothing, no last good
    assert plane.reload_if_changed() is False
    assert tools(server).keys().isdisjoint({"list_files", "count_files"})

    write(repo, TWO)
    trust.approve()
    assert plane.reload_if_changed() is True
    assert {"list_files", "count_files"} <= tools(server).keys()


def test_revoking_trust_stops_serving(trust, tmp_path, monkeypatch):
    write(tmp_path / "demo", ONE)
    trust.approve()
    server, _ = build(tmp_path, monkeypatch)
    trust.registry.set_project_tools_sha256("demo", None)
    assert server.devgraph_tool_plane.reload_if_changed() is True
    assert "list_files" not in tools(server)


def test_a_registry_error_serves_nothing(trust, tmp_path, monkeypatch):
    write(tmp_path / "demo", ONE)
    trust.approve()
    trust.registry.close()
    trust.db.write_bytes(b"not a database" * 100)
    for suffix in ("-wal", "-shm"):
        trust.db.with_name(trust.db.name + suffix).unlink(missing_ok=True)
    server, _ = build(tmp_path, monkeypatch)
    assert "list_files" not in tools(server)
    current = status(server)
    assert current["project_tools_trust"] == "error"
    assert any(NOT_TRUSTED in n for n in current["notices"])


def test_a_global_tool_of_the_same_name_serves_when_untrusted(trust, tmp_path, monkeypatch):
    import asyncio

    save_global_tools([G_LIST])
    server, _ = build(tmp_path, monkeypatch)
    assert tools(server)["list_files"].description == "Global list."
    assert status(server)["origins"] == {"list_files": "global"}
    result = asyncio.run(server.call_tool("list_files", {})).structured_content
    assert result["notices"] == [f"used global tool 'list_files': {NOT_TRUSTED}"]


def test_the_dry_resolution_reports_the_untrusted_fallback(trust, tmp_path):
    from devgraph.mcp.tool_plane import resolve_tools
    from tests.mcp.test_tool_reload import Repo

    write(tmp_path / "demo", ONE)
    status_ = resolve_tools(Repo("demo", tmp_path / "demo"))
    assert status_.served == [] and status_.project_trust == "untrusted"
    assert status_.fallback_reasons == {"list_files": NOT_TRUSTED}
