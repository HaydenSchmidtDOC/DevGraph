"""Runs the dashboard's out-of-order-reply checks (query_supersession.js) as
part of the normal suite.

The logic under test is browser JavaScript and the failure is invisible from
the server side: every response involved is well-formed and returns 200, but
`runQueryAndUpdateGraph` used to project a reply onto the canvas *before*
testing whether a newer request had already superseded it. A repo switch
landing on top of the initial load, or two quick entity-type selects, could
therefore converge on whichever reply happened to arrive last rather than the
last one asked for -- and in replace mode the stale reply also removed the
newer request's elements and re-settled the layout to the old scope. The JS
file drives the real functions out of index.html against stubs, with reply
ordering pinned by explicit deferred promises (no timers, no sleeps).

Finding: can quick calls from separate sources produce malformed Cypher
responses?
------------------------------------------------------------------------
No -- on the evidence in this repository the responses themselves are well
formed; what concurrency produces is out-of-order *delivery*, which is the
client-side defect fixed here.

* `devgraph/dashboard/routes.py:294-333` -- `POST /api/cypher` is declared
  with a plain `def` on a FastAPI router (`routes.py:23`), so each call runs
  on its own threadpool worker. Every local (`query`, `params`, `repo_id`,
  `result`) is per-invocation, and the handler returns a freshly constructed
  dict on both the success path (line 333) and the error path (line 327).
  There is no shared response buffer for two concurrent calls to interleave
  in.
* `devgraph/graph/engine.py:613-632` -- `run_cypher_graph` opens its own
  `self._driver.session()` per call and fully consumes the result inside that
  `with` block, so concurrent calls never share a session or a cursor.
* `devgraph/dashboard/query_log.py:31-36` -- the only process-wide mutable
  state either call touches is `QueryLog`'s bounded `deque`, appended to
  after the query has already run. It can affect the ordering of log entries,
  not the content of a response.
* `devgraph/dashboard/static/index.html:2320-2331` -- client side, `runCypher`
  issues each POST independently and parses its own `res.json()`; no buffer is
  shared between in-flight calls either.

Scope of the claim: this covers the dashboard's own request path only. It
asserts nothing about Neo4j server behaviour under concurrent load, and
nothing about clients outside this repository.

Skipped when node isn't on PATH -- the Python suite is the one that has to run
everywhere.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).with_name("query_supersession.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_query_supersession():
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
