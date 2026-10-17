"""Express routers mounted from another file (apis/routes.py MountTable)
against a live Neo4j: a router file's routes take the prefixes the files
mounting it give them, through up to two mounts, and every edit to either
side, a move of the router file and a deletion equal a fresh `full_scan`.
Fixture code is fictional."""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-mounts-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def endpoints(engine, repo_id):
    return sorted(
        row["name"] for row in engine.run_cypher("MATCH (e:Endpoint {repo_id: $r}) RETURN e.name AS name", {"r": repo_id})
    )


def handlers(engine, repo_id, endpoint):
    return sorted(
        (row["file"] or "", row["name"]) for row in engine.run_cypher(
            "MATCH (e:Endpoint {repo_id: $r, name: $e})-[:IMPLEMENTS]->(f:Function) RETURN f.file AS file, f.name AS name",
            {"r": repo_id, "e": endpoint},
        )
    )


def step(engine, repo_id, root, changed=(), gone=()):
    """Index one batch (changed files, then deleted ones, as the watcher does) and compare with a fresh scan."""
    if changed:
        index_paths(engine, repo_id, root, {root / rel for rel in changed})
    if gone:
        for rel in gone:
            (root / rel).unlink(missing_ok=True)
        remove_paths(engine, repo_id, root, {root / rel for rel in gone})
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


APP = """\
    const express = require('express');
    const usersRouter = require('./routes/users');
    const app = express();
    app.use('/api/users', authenticate, usersRouter);
    function authenticate(req, res, next) {}
    """
USERS = """\
    import { Router } from 'express';
    import teams from './teams';
    const router = Router();
    router.get('/:id', show);
    router.use('/teams', teams);
    function show(req, res) {}
    export default router;
    """
TEAMS = """\
    import { Router } from 'express';
    const teamsRouter = Router();
    teamsRouter.post('/', create);
    function create(req, res) {}
    export default teamsRouter;
    """


def test_a_router_takes_the_prefixes_its_mounts_give_it(engine, repo_id, tmp_path):
    write(tmp_path, "app.js", APP)
    write(tmp_path, "routes/users.ts", USERS)
    write(tmp_path, "routes/teams.ts", TEAMS)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert endpoints(engine, repo_id) == ["GET /api/users/:id", "POST /api/users/teams"]
    assert handlers(engine, repo_id, "GET /api/users/:id") == [("", "show"), ("routes/users.ts", "show")]

    # The mounting file changes its prefix, then a second file mounts the router too.
    write(tmp_path, "app.js", APP.replace("/api/users", "/v2/users"))
    step(engine, repo_id, tmp_path, changed=["app.js"])
    assert endpoints(engine, repo_id) == ["GET /v2/users/:id", "POST /v2/users/teams"]
    write(tmp_path, "admin.js", APP.replace("/api/users", "/admin/users"))
    step(engine, repo_id, tmp_path, changed=["admin.js"])
    assert "GET /admin/users/:id" in endpoints(engine, repo_id)

    # The router file itself changes, then the middle of the chain stops exporting its router.
    write(tmp_path, "routes/users.ts", USERS.replace("'/:id'", "'/:userId'"))
    step(engine, repo_id, tmp_path, changed=["routes/users.ts"])
    assert "GET /v2/users/:userId" in endpoints(engine, repo_id)
    write(tmp_path, "routes/users.ts", USERS.replace("export default router;", ""))
    step(engine, repo_id, tmp_path, changed=["routes/users.ts"])
    assert endpoints(engine, repo_id) == ["GET ?/:id (routes/users.ts)", "POST ?/ (routes/teams.ts)"]
    write(tmp_path, "routes/users.ts", USERS)
    step(engine, repo_id, tmp_path, changed=["routes/users.ts"])

    # The router file moves where the import can't find it, and back; then a mounting file goes.
    (tmp_path / "routes/users.ts").rename(tmp_path / "routes/people.ts")
    step(engine, repo_id, tmp_path, changed=["routes/people.ts"], gone=["routes/users.ts"])
    assert "GET ?/:id (routes/people.ts)" in endpoints(engine, repo_id)
    (tmp_path / "routes/people.ts").rename(tmp_path / "routes/users.ts")
    step(engine, repo_id, tmp_path, changed=["routes/users.ts"], gone=["routes/people.ts"])
    assert "GET /v2/users/:id" in endpoints(engine, repo_id)
    step(engine, repo_id, tmp_path, gone=["admin.js"])
    step(engine, repo_id, tmp_path, gone=["app.js"])
    assert endpoints(engine, repo_id) == ["GET ?/:id (routes/users.ts)", "POST ?/ (routes/teams.ts)"]


def test_a_chain_longer_than_two_mounts_stays_unmounted(engine, repo_id, tmp_path):
    write(tmp_path, "app.js", "import express from 'express';\nimport a from './a';\nconst app = express();\napp.use('/a', a);\n")
    for name, child in (("a", "b"), ("b", "c")):
        write(tmp_path, f"{name}.js", (
            f"import {{ Router }} from 'express';\nimport {child} from './{child}';\nconst router = Router();\n"
            f"router.get('/{name}', h);\nrouter.use('/{child}', {child});\nfunction h() {{}}\nexport default router;\n"
        ))
    write(tmp_path, "c.js", "import { Router } from 'express';\nconst router = Router();\nrouter.get('/c', h);\n"
          "function h() {}\nexport default router;\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    # a.js and b.js sort before the files mounting them, so the batch indexes them
    # again once the mounts are known; each file still counts once.
    paths = {tmp_path / name for name in ("app.js", "a.js", "b.js", "c.js")}
    assert index_paths(engine, repo_id, tmp_path, paths) == 4
    assert endpoints(engine, repo_id) == ["GET /a/a", "GET /a/b/b", "GET ?/c (c.js)"]
    full_scan(engine, repo_id, tmp_path)
    assert endpoints(engine, repo_id) == ["GET /a/a", "GET /a/b/b", "GET ?/c (c.js)"]
