"""Tests for `POST /api/repos`, the dashboard's one write into the registry.

Deliberately Neo4j-free: the route's whole job is orchestration (validate ->
`RepoRegistry.add_repo` -> schema/upsert/`full_scan`/`mark_indexed`), so a stub
engine plus a stubbed `full_scan` exercises the ordering, the partial-success
path and the error mapping far more precisely than a live graph could -- and
lets the failure cases be triggered on demand rather than by taking Neo4j down.

The registry itself is real (a temporary SQLite file), because persistence is
the point: the happy-path test reopens the database with a second
`RepoRegistry` to prove the row survived the request.
"""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from devgraph.dashboard import routes
from devgraph.dashboard.events import EventBroadcaster
from devgraph.registry.store import RepoRegistry


class StubEngine:
    """Records the graph calls the route makes, and can fail on demand.

    `fail_on` is read at call time so a test can arm a failure after the
    fixture has already built the client.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_on: str | None = None

    def init_schema(self) -> None:
        self._record(("init_schema",))

    def upsert_repository(self, repo_id: str, name: str, path: str) -> None:
        self._record(("upsert_repository", repo_id, name, path))

    def _record(self, call: tuple) -> None:
        self.calls.append(call)
        if self.fail_on == call[0]:
            raise RuntimeError(f"{call[0]} unavailable")

    @property
    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.sqlite3"


@pytest.fixture
def registry(registry_path: Path):
    reg = RepoRegistry(registry_path)
    yield reg
    reg.close()


@pytest.fixture
def engine() -> StubEngine:
    return StubEngine()


@pytest.fixture
def scan_calls() -> list[dict]:
    return []


@pytest.fixture
def client(registry, engine, scan_calls, monkeypatch):
    def fake_full_scan(engine_arg, repo_id, repo_root, docs_path=None, mentions_enabled=False):
        scan_calls.append(
            {
                "engine": engine_arg,
                "repo_id": repo_id,
                "repo_root": repo_root,
                "docs_path": docs_path,
                "mentions_enabled": mentions_enabled,
            }
        )
        if engine_arg.fail_on == "full_scan":
            raise RuntimeError("scan failed")
        return 7

    monkeypatch.setattr(routes, "full_scan", fake_full_scan)
    app = FastAPI()
    app.include_router(routes.build_router(engine, registry, EventBroadcaster()))
    return TestClient(app)


def _make_git_repo(tmp_path: Path, name: str = "sample-repo") -> Path:
    """A directory `RepoRegistry.add_repo` accepts.

    `add_repo` gates on a `.git` entry existing, nothing more, and `full_scan`
    is stubbed here -- so no `git init` subprocess is needed (or wanted: this
    suite shouldn't depend on a git binary being installed).
    """
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def test_register_repo_persists_record_and_runs_initial_scan(
    client, registry, registry_path, engine, scan_calls, tmp_path
):
    repo = _make_git_repo(tmp_path)

    res = client.post("/api/repos", json={"path": str(repo)})

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["repo_id"] == "sample-repo"
    assert body["path"] == str(repo.resolve())
    assert body["registered"] is True
    assert body["indexed"] is True
    assert body["files_indexed"] == 7
    assert body["warning"] is None
    assert body["active"] is True
    assert body["watch_enabled"] is True
    assert body["last_indexed"] is not None

    # Same call sequence `devgraph add` makes, in the same order.
    assert engine.call_names == ["init_schema", "upsert_repository"]
    assert engine.calls[1] == ("upsert_repository", "sample-repo", "sample-repo", str(repo.resolve()))
    assert len(scan_calls) == 1
    assert scan_calls[0]["engine"] is engine
    assert scan_calls[0]["repo_id"] == "sample-repo"
    assert Path(scan_calls[0]["repo_root"]) == repo.resolve()
    assert scan_calls[0]["docs_path"] is None
    assert scan_calls[0]["mentions_enabled"] is False

    # mark_indexed ran against the live registry...
    assert registry.get("sample-repo").last_indexed is not None

    # ...and the row is on disk, not just in this connection's cache.
    reopened = RepoRegistry(registry_path)
    try:
        persisted = reopened.get("sample-repo")
        assert persisted is not None
        assert persisted.path == repo.resolve()
        assert persisted.last_indexed == body["last_indexed"]
        assert persisted.active is True
    finally:
        reopened.close()


def test_register_repo_honours_an_explicit_repo_id(client, registry, tmp_path):
    repo = _make_git_repo(tmp_path)

    res = client.post("/api/repos", json={"path": str(repo), "repo_id": "Custom ID"})

    assert res.status_code == 201, res.text
    # The registry slugifies, so the response has to report what it actually
    # stored rather than echoing back what was asked for.
    assert res.json()["repo_id"] == "custom-id"
    assert registry.get("custom-id") is not None


def test_register_repo_reports_the_suffixed_id_on_a_custom_id_collision(client, registry, tmp_path):
    first = _make_git_repo(tmp_path, "first")
    second = _make_git_repo(tmp_path, "second")
    client.post("/api/repos", json={"path": str(first), "repo_id": "taken"})

    res = client.post("/api/repos", json={"path": str(second), "repo_id": "taken"})

    assert res.status_code == 201, res.text
    assert res.json()["repo_id"] == "taken-2"
    assert registry.get("taken").path == first.resolve()
    assert registry.get("taken-2").path == second.resolve()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"path": ""},
        {"path": "   "},
        {"path": None},
        {"path": 17},
        {"path": ["/tmp/repo"]},
        {"repo_id": "orphan"},
    ],
    ids=["missing", "blank", "whitespace", "null", "number", "list", "id-only"],
)
def test_register_repo_rejects_a_missing_or_malformed_path(client, registry, payload):
    res = client.post("/api/repos", json=payload)

    assert res.status_code == 400
    assert registry.list_repos() == []


def test_register_repo_rejects_a_blank_repo_id(client, registry, tmp_path):
    repo = _make_git_repo(tmp_path)

    res = client.post("/api/repos", json={"path": str(repo), "repo_id": "   "})

    assert res.status_code == 400
    assert registry.list_repos() == []


def test_register_repo_rejects_malformed_json(client, registry):
    res = client.post(
        "/api/repos", content=b"{not json", headers={"content-type": "application/json"}
    )

    assert res.status_code == 400
    assert registry.list_repos() == []


def test_register_repo_rejects_a_non_object_body(client, registry):
    res = client.post("/api/repos", json=["/tmp/repo"])

    assert res.status_code == 400
    assert registry.list_repos() == []


def test_register_repo_rejects_a_nonexistent_path(client, registry, tmp_path):
    res = client.post("/api/repos", json={"path": str(tmp_path / "nope")})

    assert res.status_code == 400
    assert "does not exist" in res.json()["detail"]
    assert registry.list_repos() == []


def test_register_repo_rejects_a_directory_that_is_not_a_git_repo(client, registry, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    res = client.post("/api/repos", json={"path": str(plain)})

    assert res.status_code == 400
    assert "not a git repository" in res.json()["detail"]
    assert registry.list_repos() == []


def test_register_repo_rejects_a_duplicate_path_without_adding_a_second_record(
    client, registry, tmp_path
):
    repo = _make_git_repo(tmp_path)
    assert client.post("/api/repos", json={"path": str(repo)}).status_code == 201

    res = client.post("/api/repos", json={"path": str(repo)})

    assert res.status_code == 400
    assert "already registered" in res.json()["detail"]
    assert len(registry.list_repos()) == 1


def test_register_repo_does_not_leak_internals_on_an_unexpected_registry_failure(
    client, registry, tmp_path, monkeypatch
):
    repo = _make_git_repo(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("database is locked: /srv/private/registry.sqlite3")

    monkeypatch.setattr(registry, "add_repo", boom)

    res = client.post("/api/repos", json={"path": str(repo)})

    assert res.status_code == 500
    assert res.json()["detail"] == "registration failed"


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "http://evil.example"},
        {"origin": "https://testserver"},
        {"sec-fetch-site": "cross-site"},
        {"sec-fetch-site": "same-site"},
    ],
    ids=["cross-origin", "cross-scheme", "cross-site", "same-site"],
)
def test_register_repo_rejects_a_cross_origin_request_before_writing(
    client, registry, engine, tmp_path, headers
):
    repo = _make_git_repo(tmp_path)

    res = client.post("/api/repos", json={"path": str(repo)}, headers=headers)

    assert res.status_code == 403
    assert registry.list_repos() == []
    assert engine.calls == []


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "http://testserver", "sec-fetch-site": "same-origin"},
        {"sec-fetch-site": "none"},
        {},  # non-browser client (curl, the CLI): no Origin, no Fetch Metadata
    ],
    ids=["same-origin", "user-initiated", "no-origin"],
)
def test_register_repo_allows_a_same_origin_or_non_browser_request(client, registry, tmp_path, headers):
    repo = _make_git_repo(tmp_path)

    res = client.post("/api/repos", json={"path": str(repo)}, headers=headers)

    assert res.status_code == 201, res.text
    assert len(registry.list_repos()) == 1


@pytest.mark.parametrize(
    "content_type",
    ["application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x"],
)
def test_register_repo_rejects_a_non_json_body(client, registry, engine, tmp_path, content_type):
    repo = _make_git_repo(tmp_path)

    res = client.post(
        "/api/repos",
        content=f"path={repo}".encode(),
        headers={"content-type": content_type},
    )

    assert res.status_code == 415
    assert registry.list_repos() == []
    assert engine.calls == []


def test_register_repo_accepts_a_charset_qualified_json_content_type(client, registry, tmp_path):
    repo = _make_git_repo(tmp_path)

    res = client.post(
        "/api/repos",
        content=json.dumps({"path": str(repo)}).encode(),
        headers={"content-type": "application/json; charset=utf-8"},
    )

    assert res.status_code == 201, res.text
    assert len(registry.list_repos()) == 1


@pytest.mark.parametrize("failing_step", ["init_schema", "upsert_repository", "full_scan"])
def test_indexing_failure_keeps_the_repo_registered(
    client, registry, registry_path, engine, tmp_path, failing_step
):
    """Matches `devgraph add`: registration is already committed to SQLite, so
    a Neo4j-side failure must not silently un-register the repo."""
    repo = _make_git_repo(tmp_path)
    engine.fail_on = failing_step

    res = client.post("/api/repos", json={"path": str(repo)})

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["registered"] is True
    assert body["indexed"] is False
    assert body["files_indexed"] is None
    assert body["repo_id"] == "sample-repo"
    assert body["last_indexed"] is None
    assert "devgraph rescan sample-repo" in body["warning"]

    record = registry.get("sample-repo")
    assert record is not None
    assert record.last_indexed is None

    reopened = RepoRegistry(registry_path)
    try:
        assert reopened.get("sample-repo") is not None
    finally:
        reopened.close()
