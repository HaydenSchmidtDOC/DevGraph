"""History an older DevGraph stored with its authors' UTC offsets is migrated
to UTC in place (audit 10 item 1 review): no git re-walk, the history cap
kept, resumable, and the result equals a fresh scan's."""

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.git_history import extractor
from devgraph.indexer.git_history.blame import FunctionRecency
from devgraph.indexer.git_history.extractor import sync_git_history
from devgraph.registry.store import RepoRegistry

NZ = "2026-10-27T09:00:00+13:00"  # 2026-10-26T20:00Z
US = "2026-10-26T18:00:00-07:00"  # 2026-10-27T01:00Z, five hours later, yet sorts first as a string


def _git(root: Path, *args: str, date: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Zed", "GIT_AUTHOR_EMAIL": "z@example.com",
           "GIT_COMMITTER_NAME": "Zed", "GIT_COMMITTER_EMAIL": "z@example.com",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
                          capture_output=True, env=env, text=True).stdout


def _repo(root: Path) -> Path:
    """a.py's alpha edited from NZ then (later) from the US; c.py's gamma has
    one line from each; b.py only ever in UTC."""
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return 1\n")
    (root / "c.py").write_text("def gamma():\n    x = 1\n    y = 1\n    return x + y\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init", date="2026-10-20T12:00:00+00:00")
    (root / "a.py").write_text("def alpha():\n    return 2\n")
    (root / "c.py").write_text("def gamma():\n    x = 2\n    y = 1\n    return x + y\n")
    _git(root, "commit", "-qam", "nz", date=NZ)
    (root / "a.py").write_text("def alpha():\n    return 3\n")
    (root / "c.py").write_text("def gamma():\n    x = 2\n    y = 2\n    return x + y\n")
    _git(root, "commit", "-qam", "us", date=US)
    (root / "b.py").write_text("def beta():\n    return 2\n")
    _git(root, "commit", "-qam", "utc", date="2026-10-28T08:00:00+00:00")
    return root


def _old_blame(repo, file_path):
    """blame.compute_function_recency as an older DevGraph had it: the author's offset kept."""
    recency, line_no = [], 1
    for commit, lines in repo.blame("HEAD", file_path) or []:
        recency.append(FunctionRecency(line_no, line_no + len(lines) - 1, commit.authored_datetime.isoformat(),
                                       commit.author.name if commit.author else None))
        line_no += len(lines)
    return recency


def _old_sync(monkeypatch, engine, registry, repo_id, root, **kwargs):
    """A sync as an older DevGraph ran it: dates with offsets, no history stamp."""
    offsets = dict(line.split(" ", 1) for line in _git(root, "log", "--format=%H %aI").split("\n") if line)
    original = extractor.GitHistoryExtractor.extract_new_commits

    def extract(self, *args, **kw):
        result = original(self, *args, **kw)
        for commit in result.commits:
            commit.properties["authored_date"] = offsets[commit.sha]
        return result

    with monkeypatch.context() as m:
        m.setattr(extractor.GitHistoryExtractor, "extract_new_commits", extract)
        m.setattr(extractor, "compute_function_recency", _old_blame)
        sync_git_history(engine, registry, repo_id, **kwargs)
    engine.run_cypher("MATCH (r:Repository {repo_id: $r}) REMOVE r.history_format", {"r": repo_id})


def _dates(engine, repo_id):
    return engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n:Commit OR n:Module OR n:Function OR n:Class "
        "RETURN labels(n)[0] AS label, n.name AS name, n.authored_date AS a, n.created_at AS c, "
        "n.last_modified_at AS l ORDER BY label, name",
        {"r": repo_id},
    )


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.init_schema()
    yield test_engine
    test_engine.close()


@pytest.fixture
def setup(engine, tmp_path):
    """(engine, registry, repo_id, root, fresh) where fresh(**sync_kwargs) is a
    fresh scan and sync of the same root's dates under another id."""
    root = _repo(tmp_path / "repo")
    token = uuid.uuid4().hex[:8]
    ids = [f"zz-utcmig-{token}", f"zz-utcmig-fresh-{token}"]
    # One registry each: a registry holds a folder once.
    registries = [RepoRegistry(tmp_path / f"registry-{i}.sqlite3") for i in range(2)]
    registry = registries[0]
    for repo_id, reg in zip(ids, registries):
        reg.add_repo(root, repo_id=repo_id)
        engine.delete_repository(repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)

    def fresh(**kwargs):
        sync_git_history(engine, registries[1], ids[1], **kwargs)
        return _dates(engine, ids[1])

    try:
        yield engine, registry, ids[0], root, fresh
    finally:
        for repo_id in ids:
            engine.delete_repository(repo_id)
        for reg in registries:
            reg.close()


def test_the_migration_rewrites_mixed_offsets_as_a_fresh_scan_would(setup, monkeypatch):
    engine, registry, repo_id, root, fresh = setup
    _old_sync(monkeypatch, engine, registry, repo_id, root)
    old = {(r["label"], r["name"]): r for r in _dates(engine, repo_id)}
    assert old[("Module", "a.py")]["l"] == NZ  # the offset strings picked the earlier instant
    assert old[("Function", "gamma")]["l"] == NZ

    walked = []
    monkeypatch.setattr(extractor.GitHistoryExtractor, "extract_new_commits",
                        lambda self, *a, **k: walked.append(1))
    assert sync_git_history(engine, registry, repo_id)["mode"] == "upgraded"
    assert walked == []  # no git walk
    monkeypatch.undo()

    migrated = _dates(engine, repo_id)
    assert migrated == fresh()
    by_name = {(r["label"], r["name"]): r for r in migrated}
    assert by_name[("Module", "a.py")]["l"] == "2026-10-27T01:00:00+00:00"
    assert by_name[("Function", "gamma")]["l"] == "2026-10-27T01:00:00+00:00"
    assert all(d.endswith("+00:00") for r in migrated for d in (r["a"], r["c"], r["l"]) if d)

    assert sync_git_history(engine, registry, repo_id)["mode"] == "noop"  # a second run does nothing
    assert _dates(engine, repo_id) == migrated


def test_the_migration_keeps_a_capped_history_capped(setup, monkeypatch):
    engine, registry, repo_id, root, fresh = setup
    _old_sync(monkeypatch, engine, registry, repo_id, root, max_count=2)
    assert sync_git_history(engine, registry, repo_id)["mode"] == "upgraded"
    migrated = _dates(engine, repo_id)
    assert len([r for r in migrated if r["label"] == "Commit"]) == 2
    assert migrated == fresh(max_count=2)


def test_an_interrupted_migration_resumes_to_the_same_result(setup, monkeypatch):
    engine, registry, repo_id, root, fresh = setup
    _old_sync(monkeypatch, engine, registry, repo_id, root)
    with monkeypatch.context() as m:
        m.setattr(extractor, "CHUNK_ROWS", 1)
        m.setattr(extractor, "YIELD_EVERY_S", 0)
        calls = []
        # Stop at the third chunk boundary: part of the dates rewritten.
        result = sync_git_history(engine, registry, repo_id, between_chunks=lambda: calls.append(1) or len(calls) < 3)
    assert result["mode"] == "stopped"
    partial = _dates(engine, repo_id)
    assert any(d and not d.endswith("+00:00") for r in partial for d in (r["a"], r["c"], r["l"]))
    assert engine.run_cypher("MATCH (r:Repository {repo_id: $r}) RETURN r.history_format AS h", {"r": repo_id}) == [
        {"h": None}
    ]

    assert sync_git_history(engine, registry, repo_id)["mode"] == "upgraded"
    assert _dates(engine, repo_id) == fresh()


def test_utc_helper_reads_a_date_without_offset_as_utc():
    assert extractor._utc("2026-10-27T09:00:00+13:00") == "2026-10-26T20:00:00+00:00"
    assert extractor._utc("2026-10-27T09:00:00") == "2026-10-27T09:00:00+00:00"
    assert extractor._utc(None) is None
