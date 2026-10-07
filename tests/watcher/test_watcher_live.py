"""Live watcher scenarios end to end (spec W1-W9): a running agent's graph ends
equal to a fresh full_scan of the same files, after renames, folder moves and
deletes, edits made while the agent was off, and git checkouts.

Each scenario computes the fresh snapshot once, then polls the live graph
against it (30 s deadline). Skipped without Neo4j.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import time
import uuid
from datetime import datetime, timezone

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.registry.store import RepoRegistry
from tests.indexer.docs_live_helpers import assert_matches_fresh_apply
from tests.watcher import live_helpers
from tests.watcher.live_helpers import fresh_snapshot, wait_until_equal

live_agent = live_helpers.live_agent  # the fixture

_TOKEN = uuid.uuid4().hex[:8]
FILE, FOLDER, ADR = (f"ZzFile{_TOKEN}", f"ZzFolder{_TOKEN}", f"ZzAdr{_TOKEN}")
SCHEMA = f"""
    version: 1
    node_types:
      - label: {FILE}
        key: [path]
        metadata: [{{name: path}}]
        source: {{provider: filesystem, kind: file}}
      - label: {FOLDER}
        key: [path]
        metadata: [{{name: path}}]
        source: {{provider: filesystem, kind: folder}}
      - label: {ADR}
        key: [adr_id]
        metadata: [{{name: path}}, {{name: adr_id}}]
        source: {{provider: docs, paths: ["decisions/**/*.md"], fields: {{adr_id: id}}}}
    relationships:
      - type: IS_CHILD_OF
        provider: filesystem
        from: [{FILE}, {FOLDER}]
        to: {FOLDER}
      - {{type: ZZ_SUPERSEDES, provider: docs, from: {ADR}, to: {ADR}, field: supersedes}}
"""
MOD = 'import redis\n\nCACHE_URL = "redis://cache:6379/0"\n\n\nclass Widget:\n    def run(self):\n        return 1\n'
RUN = 'import redis\n\nCACHE_URL = "redis://cache:6379/0"\n\n\ndef main():\n    return redis.from_url(CACHE_URL)\n'
EVENT_DEADLINE_S = 30.0
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test Author", "GIT_AUTHOR_EMAIL": "author@example.com",
    "GIT_COMMITTER_NAME": "Test Author", "GIT_COMMITTER_EMAIL": "author@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        for label in (FILE, FOLDER, ADR):
            cleanup.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
            cleanup.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")
    except Exception:
        pass  # Neo4j unavailable: the tests were skipped
    finally:
        cleanup.close()


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


def git(root, *args):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=root, check=True, capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


def _front(root, rel, front):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{textwrap.dedent(front).strip()}\n---\n# Notes\n")


@pytest.fixture
def repo(tmp_path, engine, live_agent):
    """The fixture repository, committed on `main`, registered in the agents'
    registry and scanned as `devgraph add` would. Yields (root, repo_id)."""
    root = tmp_path / "repo"
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "tools").mkdir()
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(SCHEMA))
    (root / "pkg" / "mod.py").write_text(MOD)
    (root / "pkg" / "sub" / "util.py").write_text("def helper():\n    return 2\n")
    (root / "tools" / "run.py").write_text(RUN)
    (root / "README.md").write_text("# Demo\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n")
    _front(root, "decisions/adr-1.md", "id: ADR-1")
    _front(root, "decisions/adr-2.md", "id: ADR-2\nsupersedes: ADR-1")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("module.exports = 1;\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Start")
    root = root.resolve()

    live_agent.registry_path.parent.mkdir(parents=True)
    registry = RepoRegistry(live_agent.registry_path)
    try:
        repo_id = registry.add_repo(root, repo_id=f"zz-watcher-live-{_TOKEN}-{uuid.uuid4().hex[:6]}").repo_id
        engine.delete_repository(repo_id)
        provision_repository_schema(engine, root)
        engine.upsert_repository(repo_id, repo_id, str(root))
        started = datetime.now(timezone.utc)
        full_scan(engine, repo_id, root)
        registry.mark_indexed(repo_id, at=started)
    finally:
        registry.close()
    yield root, repo_id
    live_agent.stop_all()
    engine.delete_repository(repo_id)
    engine.delete_repository(f"{repo_id}_fresh")


def catch_up_states(agent, repo_id):
    return [e["state"] for e in list(agent.events) if e.get("type") == "catch_up" and e.get("repo_id") == repo_id]


def wait_caught_up(agent, repo_id):
    """Wait for the start catch-up's closing event."""
    deadline = time.monotonic() + EVENT_DEADLINE_S
    while not {"done", "failed"} & set(catch_up_states(agent, repo_id)):
        if time.monotonic() > deadline:
            pytest.fail(f"no catch-up finished for {repo_id}: {agent.events}")
        time.sleep(0.05)
    assert "failed" not in catch_up_states(agent, repo_id)


def start(live_agent, repo_id):
    agent = live_agent.start()
    wait_caught_up(agent, repo_id)
    return agent


def converges(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    wait_until_equal(engine, repo_id, expected)
    assert_matches_fresh_apply(engine, repo_id, root)


def test_renames_keep_the_shared_node_and_the_incoming_link(engine, repo, live_agent):
    root, repo_id = repo
    start(live_agent, repo_id)

    (root / "pkg" / "mod.py").rename(root / "pkg" / "model.py")
    (root / "decisions" / "adr-1.md").rename(root / "decisions" / "0001-start.md")
    converges(engine, repo_id, root)

    shared = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.sources IS NOT NULL RETURN n.name AS name, n.sources AS sources",
        {"r": repo_id},
    )
    assert {(row["name"], tuple(row["sources"])) for row in shared} >= {("Redis", ("pkg/model.py", "tools/run.py"))}
    incoming = engine.run_cypher(
        f"MATCH (a:{ADR} {{repo_id: $r}})-[:ZZ_SUPERSEDES]->(b:{ADR} {{repo_id: $r, name: 'ADR-1'}}) "
        "RETURN a.name AS a, b.path AS p",
        {"r": repo_id},
    )
    assert [(row["a"], row["p"]) for row in incoming] == [("ADR-2", "decisions/0001-start.md")]


def test_a_folder_moved_to_the_trash_is_removed(engine, repo, live_agent, tmp_path):
    root, repo_id = repo
    start(live_agent, repo_id)

    (tmp_path / "trash").mkdir()
    shutil.move(root / "pkg" / "sub", tmp_path / "trash" / "sub")
    converges(engine, repo_id, root)


def test_folder_moves_and_a_top_level_rename(engine, repo, live_agent):
    root, repo_id = repo
    start(live_agent, repo_id)

    (root / "pkg" / "sub").rename(root / "tools" / "sub")
    converges(engine, repo_id, root)

    (root / "tools").rename(root / "scripts")
    converges(engine, repo_id, root)

    with (root / "scripts" / "run.py").open("a") as f:
        f.write("\n\ndef later():\n    return 3\n")
    converges(engine, repo_id, root)


def test_a_top_level_folder_deleted_and_recreated(engine, repo, live_agent):
    root, repo_id = repo
    start(live_agent, repo_id)

    shutil.rmtree(root / "pkg")
    converges(engine, repo_id, root)

    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(MOD)
    converges(engine, repo_id, root)

    with (root / "pkg" / "mod.py").open("a") as f:
        f.write("\n\nclass Gadget:\n    pass\n")
    converges(engine, repo_id, root)


def test_edits_made_while_off_are_caught_up_on_start(engine, repo, live_agent):
    root, repo_id = repo
    first = start(live_agent, repo_id)
    live_agent.stop(first)

    with (root / "pkg" / "mod.py").open("a") as f:
        f.write("\n\nclass Gadget:\n    pass\n")
    (root / "tools" / "run.py").unlink()
    (root / "logo.png").unlink()
    (root / "newtop").mkdir()
    (root / "newtop" / "c.py").write_text("def gamma():\n    return 3\n")
    _front(root, "decisions/adr-2.md", "id: ADR-3\nsupersedes: ADR-1")

    second = live_agent.start()
    converges(engine, repo_id, root)
    wait_caught_up(second, repo_id)
    states = catch_up_states(second, repo_id)
    assert states[:2] == ["running", "done"], second.events


def test_git_checkouts_of_another_branch(engine, repo, live_agent):
    root, repo_id = repo
    git(root, "checkout", "-q", "-b", "feature")
    git(root, "mv", "pkg", "lib")
    git(root, "rm", "-q", "decisions/adr-2.md")
    (root / "README.md").write_text("# Demo\n\nNow with a library.\n")
    (root / "lib" / "extra.py").write_text("def extra():\n    return 4\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "Move the package")
    git(root, "checkout", "-q", "main")
    start(live_agent, repo_id)

    git(root, "checkout", "-q", "feature")
    converges(engine, repo_id, root)

    git(root, "checkout", "-q", "main")
    converges(engine, repo_id, root)
