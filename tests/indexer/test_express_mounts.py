"""What a JS/TS file records about Express routers across files
(apis/routes.py MOUNT_PROPERTIES), and the prefixes a MountTable gives a
router file. Fixture code is fictional."""

import pytest

from devgraph.indexer.apis.routes import MOUNT_PIN_SEP, MOUNT_SEP, MountTable, mount_entry, mount_routes
from devgraph.indexer.jsts.extractor import extract_js_file


def recorded(source, path="app.js"):
    props = extract_js_file(source, path, "r").nodes[0].properties
    mounts = [entry.split(MOUNT_SEP) for entry in props["route_mounts"] or []]
    return [(base, prefix, export, files.split(MOUNT_PIN_SEP)[0]) for base, prefix, export, files in mounts], [
        tuple(entry.split(MOUNT_SEP)) for entry in props["route_exports"] or []
    ]


@pytest.mark.parametrize("source, mounts", [
    ("import express from 'express';\nimport users from './users';\nconst app = express();\napp.use('/api', users);\n",
     [("", "/api", "default", "users.js")]),
    ("const { adminRouter: admin } = require('./r/admin');\napp.use('/admin', auth, admin);\n",
     [("", "/admin", "adminRouter", "r/admin.js")]),
    ("const app = express();\nconst api = express.Router();\napp.use('/api', api);\napi.use('/v1', require('./v1'));\n",
     [("", "/api/v1", "default", "v1.js")]),
    # Mounted on a router the file exports: relative to that router's own mounts.
    ("import teams from './teams';\nconst router = Router();\nrouter.use('/teams', teams);\nmodule.exports = router;\n",
     [("default", "/teams", "default", "teams.js")]),
    # On a router neither mounted nor exported, through a variable, from a package: nothing.
    ("import teams from './teams';\nconst router = Router();\nrouter.use('/teams', teams);\n", []),
    ("import teams from './teams';\napp.use(prefix, teams);\nimport cors from 'cors';\napp.use('/x', cors);\n", []),
])
def test_mounts(source, mounts):
    assert recorded(source)[0] == mounts


@pytest.mark.parametrize("source, exports", [
    ("const router = Router();\nrouter.get('/:id', show);\nexport default router;\n", [("default", "GET", "/:id")]),
    ("export const usersRouter = Router();\nusersRouter.post('/', create);\n", [("usersRouter", "POST", "/")]),
    ("const r = Router();\nr.get('/a', h);\nmodule.exports = { users: r };\nexports.alias = r;\n",
     [("alias", "GET", "/a"), ("users", "GET", "/a")]),
    # A router the file mounts itself, or one it doesn't export, defers nothing.
    ("const app = express();\nconst router = Router();\napp.use('/x', router);\nrouter.get('/a', h);\nexport default router;\n", []),
    ("const router = Router();\nrouter.get('/a', h);\n", []),
])
def test_exports(source, exports):
    assert recorded(source)[1] == exports


def test_prefixes_follow_at_most_two_mounts_and_stop_at_a_cycle():
    table = MountTable({
        "app.js": [mount_entry("", "/api", "default", ["api.js"])],
        "api.js": [mount_entry("default", "/users", "default", ["users.js"])],
        "users.js": [mount_entry("default", "/teams", "default", ["teams.js"])],
        "a.js": [mount_entry("default", "/b", "default", ["b.js"])],
        "b.js": [mount_entry("default", "/a", "default", ["a.js"])],
    })
    assert table.prefixes("api.js", "default") == {"/api"}
    assert table.prefixes("users.js", "default") == {"/api/users"}
    assert table.prefixes("teams.js", "default") == set()
    assert table.prefixes("a.js", "default") == set()
    exports = [MOUNT_SEP.join(("default", "GET", "/:id"))]
    assert table.route_prefixes("users.js", exports) == [MOUNT_SEP.join(("default", "/api/users"))]
    table.set("app.js", None)
    assert table.route_prefixes("users.js", exports) is None


def test_mount_routes_renames_the_endpoint_and_its_edges():
    own = "GET ?/:id (users.js)"
    nodes = [{"label": "Module", "name": "users.js", "properties": {}}, {"label": "Endpoint", "name": own, "properties": {}}]
    rels = [{"from_label": "Endpoint", "from_name": own, "rel_type": "IMPLEMENTS"}]
    exports = [MOUNT_SEP.join(("default", "GET", "/:id"))]
    prefixes = [MOUNT_SEP.join(("default", p)) for p in ("/a", "/b")]
    nodes, rels = mount_routes(nodes, rels, "users.js", exports, prefixes)
    assert [n["name"] for n in nodes if n["label"] == "Endpoint"] == ["GET /a/:id", "GET /b/:id"]
    assert [r["from_name"] for r in rels] == ["GET /a/:id", "GET /b/:id"]
