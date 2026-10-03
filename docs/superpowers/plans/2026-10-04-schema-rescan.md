# Schema Rescan Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record which schema the graph was built with, apply schema changes through a debounced full rescan (or immediately via `devgraph rescan`), pause filesystem-provider writes while a change is pending, and clean up removed user types.

**Architecture:** `schema_file_hash` (project_schema.py) fingerprints the file. `GraphEngine` reads/records applied state on the Repository node and deletes removed labels/relationship types. `dispatch.apply_project_schema` replaces #27's `_resync_filesystem_provider`; `schema_pending` gates provider writes. A new `devgraph/agent/schema_rescan.py` scheduler debounces and runs `full_scan`.

**Tech Stack:** Python 3.13, neo4j driver, Typer, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-schema-rescan-design.md`

**Working directory:** the repository root of this worktree (branch `epic1/a-rescan`, stacked on `epic1/base` = upstream master + #27 + #28). Python via `uv run ...`. Live tests need Neo4j at `bolt://127.0.0.1:7687` (`neo4j` / `devgraph-local-dev`); they must run, not skip.

## Global Constraints

- Applied state lives on the Repository node: `schema_hash` (`sha256:<hex>` | `absent`), `schema_labels` (list), `schema_relationship_types` (list of non-built-in declared types).
- Pending = current hash ≠ applied hash; no applied state + no file = not pending.
- Applying never deletes built-in labels/relationship types; labels/types read back from the graph are re-validated against the schema identifier patterns before reaching Cypher (and backtick-quoted).
- An invalid schema: apply returns False, graph untouched, provider stays paused; the scheduler does not retry until the file's hash changes.
- Quiet period 300 s, check interval 30 s; a new hash restarts the quiet period.
- A repository without a schema file indexes exactly as before.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

1. The user edits the schema several times in a minute → exactly one full rescan, 5 minutes after the last edit — pinned in Task 3.
2. The user saves a broken schema while the agent runs → no rescan loop, no graph change, a warning; fixing the file resumes normally — pinned in Tasks 2 and 3.
3. A repository registered before this change (no applied state, no schema) → never pending, no surprise full rescan on upgrade — pinned in Task 2.
4. A user type removed from the schema while another repository still declares the same label → only this repository's nodes are deleted — pinned in Task 2.
5. Files edited while a schema change is pending → built-in nodes update normally; filesystem nodes wait for the apply — pinned in Task 2.

---

### Task 1: Hash and engine state

**Files:**
- Modify: `devgraph/config/project_schema.py` (add `import hashlib`; `ABSENT_SCHEMA_HASH`, `schema_file_hash` after `project_schema_path`)
- Modify: `devgraph/graph/engine.py` (constants near the other module Cypher constants; four methods after `prune_extracted_nodes`)
- Test: `tests/config/test_project_schema.py` (append), `tests/graph/test_engine_applied_schema.py` (create)

**Interfaces:**
- Produces: `ABSENT_SCHEMA_HASH = "absent"`, `schema_file_hash(repo_root: Path) -> str`; `GraphEngine.read_applied_schema(repo_id) -> dict | None` (`{"hash", "labels", "relationship_types"}`, None when no Repository node or no hash recorded), `GraphEngine.record_applied_schema(repo_id, schema_hash: str, labels: list[str], relationship_types: list[str]) -> None`, `GraphEngine.delete_label_nodes(repo_id, label) -> int`, `GraphEngine.delete_relationship_type(repo_id, rel_type) -> int` (callers validate identifiers).

- [ ] **Step 1: Write the failing tests**

Append to `tests/config/test_project_schema.py` (add `ABSENT_SCHEMA_HASH, schema_file_hash` to its import list from `devgraph.config.project_schema`):

```python
def test_schema_file_hash_tracks_content(tmp_path):
    assert schema_file_hash(tmp_path) == ABSENT_SCHEMA_HASH
    write_schema(tmp_path, WIDGET)
    first = schema_file_hash(tmp_path)
    assert first.startswith("sha256:") and len(first) == len("sha256:") + 64
    assert schema_file_hash(tmp_path) == first
    (tmp_path / SCHEMA_FILENAME).write_text("version: 1\n")
    assert schema_file_hash(tmp_path) != first


def test_an_unreadable_schema_never_hashes_like_a_real_one(tmp_path):
    (tmp_path / SCHEMA_FILENAME).mkdir()
    value = schema_file_hash(tmp_path)
    assert value.startswith("unreadable:") and value != ABSENT_SCHEMA_HASH
```

Create `tests/graph/test_engine_applied_schema.py`:

```python
"""Applied-schema state and removed-type cleanup on a live Neo4j."""

import pytest

from devgraph.graph.engine import GraphEngine

REPO = "_smoketest_applied_schema"
OTHER = "_smoketest_applied_schema_other"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    for repo in (REPO, OTHER):
        test_engine.delete_repository(repo)
    yield test_engine
    for repo in (REPO, OTHER):
        test_engine.delete_repository(repo)
    test_engine.close()


def test_no_state_until_recorded(engine):
    assert engine.read_applied_schema(REPO) is None
    engine.upsert_repository(REPO, REPO, "/tmp/demo")
    assert engine.read_applied_schema(REPO) is None


def test_record_and_read_back(engine):
    engine.upsert_repository(REPO, REPO, "/tmp/demo")
    engine.record_applied_schema(REPO, "sha256:abc", ["Widget"], ["LINKS"])
    assert engine.read_applied_schema(REPO) == {"hash": "sha256:abc", "labels": ["Widget"], "relationship_types": ["LINKS"]}
    engine.record_applied_schema(REPO, "absent", [], [])
    assert engine.read_applied_schema(REPO) == {"hash": "absent", "labels": [], "relationship_types": []}


def test_delete_label_nodes_is_scoped_to_the_repo(engine):
    engine.run_cypher("CREATE (:ZzWidget {repo_id: $a, name: 'w1'}), (:ZzWidget {repo_id: $b, name: 'w2'})", {"a": REPO, "b": OTHER})
    assert engine.delete_label_nodes(REPO, "ZzWidget") == 1
    remaining = engine.run_cypher("MATCH (n:ZzWidget) RETURN n.repo_id AS r", {})
    assert [r["r"] for r in remaining] == [OTHER]


def test_delete_relationship_type_keeps_the_nodes(engine):
    engine.run_cypher(
        "CREATE (a:ZzWidget {repo_id: $r, name: 'a'})-[:ZZ_LINKS]->(b:ZzWidget {repo_id: $r, name: 'b'})",
        {"r": REPO},
    )
    assert engine.delete_relationship_type(REPO, "ZZ_LINKS") == 1
    assert engine.run_cypher("MATCH (n:ZzWidget {repo_id: $r}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 2}]
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/config/test_project_schema.py tests/graph/test_engine_applied_schema.py -q` → ImportError / AttributeError.

- [ ] **Step 3: Implement**

`devgraph/config/project_schema.py` — add `import hashlib` and, after `project_schema_path`:

```python
#: `schema_file_hash` of a repository with no schema file.
ABSENT_SCHEMA_HASH = "absent"


def schema_file_hash(repo_root: Path) -> str:
    """Fingerprint of the schema file's bytes: `sha256:<hex>`, or `absent`.

    An unreadable path (a directory, a permission error) gets a distinct
    `unreadable:<error>` value so it never equals a hash the graph was
    actually built with.
    """
    path = project_schema_path(repo_root)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ABSENT_SCHEMA_HASH
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"
    return "sha256:" + hashlib.sha256(data).hexdigest()
```

`devgraph/graph/engine.py` — constants:

```python
# Which project schema the repository's graph was last built with (see
# dispatch.apply_project_schema). Kept on the Repository node so every
# process -- CLI, agent, dashboard -- reads the same state.
_READ_APPLIED_SCHEMA_CYPHER = (
    "MATCH (r:Repository {repo_id: $repo_id}) WHERE r.schema_hash IS NOT NULL "
    "RETURN r.schema_hash AS hash, coalesce(r.schema_labels, []) AS labels, "
    "coalesce(r.schema_relationship_types, []) AS relationship_types"
)
_RECORD_APPLIED_SCHEMA_CYPHER = (
    "MERGE (r:Repository {repo_id: $repo_id}) "
    "SET r.schema_hash = $hash, r.schema_labels = $labels, "
    "r.schema_relationship_types = $relationship_types"
)
```

Methods (after `prune_extracted_nodes`):

```python
    def read_applied_schema(self, repo_id: str) -> dict[str, Any] | None:
        """The schema state the repo's graph was last built with, or None."""
        with self._driver.session() as session:
            result = _retry_transient(session.run, _READ_APPLIED_SCHEMA_CYPHER, repo_id=repo_id)
            records = [record.data() for record in result or []]
        return records[0] if records else None

    def record_applied_schema(
        self, repo_id: str, schema_hash: str, labels: list[str], relationship_types: list[str]
    ) -> None:
        with self._driver.session() as session:
            _retry_transient(
                session.run, _RECORD_APPLIED_SCHEMA_CYPHER, repo_id=repo_id, hash=schema_hash,
                labels=labels, relationship_types=relationship_types,
            )

    def delete_label_nodes(self, repo_id: str, label: str) -> int:
        """Delete one repo's nodes of a user label. The caller validates `label`."""
        with self._driver.session() as session:
            result = _retry_transient(
                session.run, f"MATCH (n:`{label}` {{repo_id: $repo_id}}) DETACH DELETE n RETURN count(n) AS n",
                repo_id=repo_id,
            )
            records = [record.data() for record in result or []]
        return records[0]["n"] if records else 0

    def delete_relationship_type(self, repo_id: str, rel_type: str) -> int:
        """Delete one repo's relationships of a user type. The caller validates `rel_type`."""
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                f"MATCH (a {{repo_id: $repo_id}})-[r:`{rel_type}`]->() DELETE r RETURN count(r) AS n",
                repo_id=repo_id,
            )
            records = [record.data() for record in result or []]
        return records[0]["n"] if records else 0
```

- [ ] **Step 4: Run to verify they pass** — `uv run pytest tests/config tests/graph -q -rs` (no skips of the new live file).

- [ ] **Step 5: Commit** — `git add devgraph/config/project_schema.py devgraph/graph/engine.py tests/config/test_project_schema.py tests/graph/test_engine_applied_schema.py` and `git commit -m "Record which project schema a repository's graph was built with"`.

---

### Task 2: Apply, pending, and dispatch wiring

**Files:**
- Modify: `devgraph/indexer/dispatch.py` — replace `_resync_filesystem_provider` with `apply_project_schema`; add `schema_pending`; rewire `index_paths`, `remove_paths`, `full_scan`
- Modify: `tests/indexer/test_filesystem_provider_live.py` — rewrite the three schema-save tests (`test_saving_a_new_schema_syncs_the_whole_worktree`, `test_renaming_a_label_in_the_schema_leaves_no_old_label_nodes`, `test_deleting_the_schema_prunes_every_filesystem_node`) for the new semantics
- Test: `tests/indexer/test_schema_apply_live.py` (create)

**Interfaces:**
- Consumes: Task 1; existing `_filesystem_spec`, `_touches_schema_file`, `_indexable_paths`, `_repo_relative`, `filesystem.*`, `provision_repository_schema`, `resolve_effective_schema`, `ProjectSchemaError`, `LABEL_PATTERN`, `RELATIONSHIP_TYPE_PATTERN` (project_schema), `NODE_LABELS`, `RELATIONSHIP_TYPES` (graph.schema).
- Produces: `dispatch.schema_pending(engine, repo_id, repo_root) -> bool`, `dispatch.apply_project_schema(engine, repo_id, repo_root) -> bool`; `index_paths(..., sync_provider: bool = True)` (replaces #27's `resync_on_schema_change`).

- [ ] **Step 1: Write the failing tests**

Create `tests/indexer/test_schema_apply_live.py` (reuses the fixtures/helpers pattern of `test_filesystem_provider_live.py`; import from it is fine if pytest allows — otherwise copy `WORKTREE`, `engine`, `repo`, `with_schema`, `fs_nodes`, `scan` into this file):

```python
"""Applying a project schema: recorded state, pending pause, removed-type cleanup."""

import textwrap

import pytest

from devgraph.config.project_schema import ABSENT_SCHEMA_HASH, schema_file_hash
from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import apply_project_schema, full_scan, index_paths, remove_paths, schema_pending

REPO = "_smoketest_schema_apply"
WORKTREE = """
    version: 1
    node_types:
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
      - label: Folder
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: folder}
    relationships:
      - type: IS_CHILD_OF
        provider: filesystem
        from: [File, Folder]
        to: Folder
"""
WIDGETS = """
      - label: ZzGadget
        key: [slug]
        metadata: [{name: slug}]
"""
LINKS = """
      - type: ZZ_LINKS
        provider: custom
        custom: {name: linker}
        from: ZzGadget
        to: ZzGadget
"""


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    test_engine.delete_repository(REPO)
    yield test_engine
    test_engine.delete_repository(REPO)
    test_engine.close()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("def run():\n    return 1\n")
    return root


def write_schema(root, text):
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(text))


def worktree_with_gadgets(links=True):
    text = WORKTREE.replace("    relationships:", WIDGETS.rstrip("\n") + "\n    relationships:")
    return text + (LINKS if links else "")


def fs_nodes(engine):
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.extractor = 'filesystem' RETURN labels(n)[0] + ':' + n.name AS k", {"r": REPO}
    )
    return sorted(r["k"] for r in rows)


def scan(engine, root):
    engine.upsert_repository(REPO, REPO, str(root))
    full_scan(engine, REPO, root)


def test_a_repo_without_a_schema_is_never_pending(engine, repo):
    engine.upsert_repository(REPO, REPO, str(repo))
    assert not schema_pending(engine, REPO, repo)  # no state recorded yet, no file
    scan(engine, repo)
    assert engine.read_applied_schema(REPO)["hash"] == ABSENT_SCHEMA_HASH
    assert not schema_pending(engine, REPO, repo)


def test_full_scan_applies_and_records_the_schema(engine, repo):
    write_schema(repo, worktree_with_gadgets())
    scan(engine, repo)
    state = engine.read_applied_schema(REPO)
    assert state["hash"] == schema_file_hash(repo)
    assert state["labels"] == ["File", "Folder", "ZzGadget"]
    assert state["relationship_types"] == ["IS_CHILD_OF", "ZZ_LINKS"]
    assert not schema_pending(engine, REPO, repo)
    assert "File:pkg/mod.py" in fs_nodes(engine)


def test_editing_the_schema_pauses_provider_writes_until_applied(engine, repo):
    scan(engine, repo)
    write_schema(repo, WORKTREE)
    assert schema_pending(engine, REPO, repo)
    index_paths(engine, REPO, repo, {repo / "devgraph.schema.yaml"})  # the watcher's event for the save
    (repo / "pkg" / "new.py").write_text("x = 1\n")
    index_paths(engine, REPO, repo, {repo / "pkg" / "new.py"})
    assert fs_nodes(engine) == []  # nothing written under an unapplied schema
    modules = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS n", {"r": REPO})
    assert "pkg/new.py" in {m["n"] for m in modules}  # built-in extraction carries on

    assert apply_project_schema(engine, REPO, repo)
    assert {"File:pkg/new.py", "Folder:pkg"} <= set(fs_nodes(engine))
    assert not schema_pending(engine, REPO, repo)


def test_removed_user_types_are_deleted_and_others_kept(engine, repo):
    write_schema(repo, worktree_with_gadgets())
    scan(engine, repo)
    engine.run_cypher(
        "CREATE (a:ZzGadget {repo_id: $r, slug: 'a', name: 'a'})-[:ZZ_LINKS]->(b:ZzGadget {repo_id: $r, slug: 'b', name: 'b'})",
        {"r": REPO},
    )
    write_schema(repo, worktree_with_gadgets(links=False))  # drop the relationship type only
    assert apply_project_schema(engine, REPO, repo)
    assert engine.run_cypher("MATCH ({repo_id: $r})-[x:ZZ_LINKS]->() RETURN count(x) AS n", {"r": REPO}) == [{"n": 0}]
    assert engine.run_cypher("MATCH (n:ZzGadget {repo_id: $r}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 2}]

    write_schema(repo, WORKTREE)  # now drop the node type too
    assert apply_project_schema(engine, REPO, repo)
    assert engine.run_cypher("MATCH (n:ZzGadget {repo_id: $r}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 0}]
    assert "File:pkg/mod.py" in fs_nodes(engine)
    assert engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN count(m) AS n", {"r": REPO})[0]["n"] > 0


def test_an_invalid_schema_changes_nothing(engine, repo):
    write_schema(repo, WORKTREE)
    scan(engine, repo)
    before = fs_nodes(engine)
    state = engine.read_applied_schema(REPO)
    (repo / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    assert apply_project_schema(engine, REPO, repo) is False
    full_scan(engine, REPO, repo)
    assert fs_nodes(engine) == before
    assert engine.read_applied_schema(REPO) == state
    assert schema_pending(engine, REPO, repo)


def test_a_tampered_label_list_never_reaches_cypher(engine, repo):
    write_schema(repo, WORKTREE)
    scan(engine, repo)
    engine.record_applied_schema(REPO, "sha256:old", ["File", "Folder", "Bad`) DETACH DELETE n //"], ["NOT VALID"])
    assert apply_project_schema(engine, REPO, repo)  # skips the invalid names, no Cypher error
    assert "File:pkg/mod.py" in fs_nodes(engine)
```

Rewrite the three schema-save tests in `tests/indexer/test_filesystem_provider_live.py` to the new semantics (keep their names minus "syncs"/"immediately" wording; import `apply_project_schema` and `schema_pending`):
- saving a new schema → after `index_paths({schema})` there are **no** filesystem nodes and `schema_pending` is True; after `apply_project_schema(...)` the full File/Folder set exists and the `file_repo_name`/`folder_repo_name` indexes exist.
- renaming a label → after the save event nothing changes; after `apply_project_schema` no `File:` nodes remain and `Entry:` nodes exist.
- deleting the schema → after `remove_paths({schema})` the nodes are still there (pending); after `apply_project_schema` there are none and Module nodes remain.

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/indexer/test_schema_apply_live.py tests/indexer/test_filesystem_provider_live.py -q` → ImportError for `apply_project_schema`/`schema_pending`.

- [ ] **Step 3: Implement** in `devgraph/indexer/dispatch.py`

1. Imports: add `ABSENT_SCHEMA_HASH, LABEL_PATTERN, RELATIONSHIP_TYPE_PATTERN, resolve_effective_schema, schema_file_hash` to the existing `devgraph.config.project_schema` import, and `from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES` (check for an existing import first).

2. Replace `_resync_filesystem_provider` entirely with:

```python
def schema_pending(engine: GraphEngine, repo_id: str, repo_root: Path) -> bool:
    """True when the schema file differs from the one the graph was built with.

    A repository with no recorded state and no schema file has nothing to
    apply, so repositories registered before schema tracking are never
    pending just for lacking state.
    """
    current = schema_file_hash(repo_root)
    applied = engine.read_applied_schema(repo_id)
    if applied is None:
        return current != ABSENT_SCHEMA_HASH
    return current != applied["hash"]


def apply_project_schema(engine: GraphEngine, repo_id: str, repo_root: Path) -> bool:
    """Bring the graph in line with the repository's current schema file.

    Provisions constraints/indexes, deletes nodes and relationships of user
    types the previously applied schema declared but this one doesn't
    (built-ins are never touched), re-syncs the filesystem provider, and
    records the applied state. An invalid schema or a provisioning failure
    returns False with the graph untouched.
    """
    current_hash = schema_file_hash(repo_root)
    try:
        effective = resolve_effective_schema(repo_root)
    except ProjectSchemaError as exc:
        logger.warning("project schema for %s is invalid; not applied: %s", repo_root, exc)
        return False
    try:
        provision_repository_schema(engine, repo_root)
    except Exception as exc:
        logger.warning("could not provision the project schema for %s; not applied: %s", repo_root, exc)
        return False

    labels = [node_type.label for node_type in effective.node_types]
    rel_types = list(dict.fromkeys(r.type for r in effective.relationships if r.type not in RELATIONSHIP_TYPES))
    previous = engine.read_applied_schema(repo_id) or {}
    # Re-validated: these names come back from the graph and are interpolated.
    for label in previous.get("labels") or []:
        if label not in labels and label not in NODE_LABELS and LABEL_PATTERN.fullmatch(label or ""):
            engine.delete_label_nodes(repo_id, label)
    for rel_type in previous.get("relationship_types") or []:
        if rel_type not in rel_types and rel_type not in RELATIONSHIP_TYPES and RELATIONSHIP_TYPE_PATTERN.fullmatch(rel_type or ""):
            engine.delete_relationship_type(repo_id, rel_type)

    spec = filesystem.filesystem_spec(effective)
    on_disk = {rel for p in _indexable_paths(repo_root) if (rel := _repo_relative(repo_root, p)) is not None}
    filesystem.reconcile(engine, repo_id, spec, on_disk)
    if spec is not None:
        filesystem.sync_present(engine, repo_id, spec, on_disk)
    engine.record_applied_schema(repo_id, current_hash, labels, rel_types)
    return True
```

3. `index_paths`: rename the parameter `resync_on_schema_change: bool = True` to `sync_provider: bool = True` (update its docstring: "False when the caller has just applied the schema"), and replace the tail block (from `if resync_on_schema_change and _touches_schema_file(...)` through the provider `sync_present`) with:

```python
    if sync_provider and not schema_pending(engine, repo_id, repo_root):
        ok, spec = _filesystem_spec(repo_root)
        if ok and spec is not None:
            present = {
                rel for p in paths
                if _is_provider_file(repo_root, Path(p)) and (rel := _repo_relative(repo_root, Path(p))) is not None
            }
            filesystem.sync_present(engine, repo_id, spec, present)
```

4. `remove_paths`: replace the tail block (from `if _touches_schema_file(...)` through `sync_absent`) with the same `if not schema_pending(...)` guard around the existing `_filesystem_spec` + `sync_absent` code. Delete `_touches_schema_file` if nothing else uses it.

5. `full_scan`: replace the `ok, spec = _filesystem_spec(...)` / `reconcile` block with `apply_project_schema(engine, repo_id, repo_root)` and pass `sync_provider=False` to `index_paths` (comment: "applied just above"); update its docstring to say a full scan applies the project schema (see apply_project_schema).

- [ ] **Step 4: Run** — `uv run pytest tests/indexer tests/graph tests/config tests/cli tests/dashboard -q -rs` → all pass, no live skips.

- [ ] **Step 5: Commit** — stage the dispatch module and the two live test files; `git commit -m "Apply project schema changes as a whole and pause provider writes while pending"`.

---

### Task 3: Debounced rescans in the agents, and `rescan --now`

**Files:**
- Create: `devgraph/agent/schema_rescan.py`
- Modify: `devgraph/agent/headless.py`, `devgraph/agent/tray.py` (construct, start, stop — same sites `_watcher.start()`/`_watcher.stop()` use; not the tray's pause toggle)
- Modify: `devgraph/cli/main.py` (`rescan`: add `--now`)
- Test: `tests/agent/test_schema_rescan.py` (create); `tests/cli/test_cli.py` (append one test)

**Interfaces:**
- Consumes: `schema_pending`, `full_scan` (dispatch); `schema_file_hash`, `resolve_effective_schema`, `ProjectSchemaError`; `RepoRegistry.list_repos(active_only=True)` records (`repo_id`, `path`, `docs_path`, `mentions_enabled`), `registry.mark_indexed(repo_id)`.
- Produces: `SchemaRescanScheduler(engine, registry, on_rescanned: Callable[[str, int], None] | None = None, *, quiet_s=300.0, interval_s=30.0, clock=time.monotonic)` with `run_once() -> list[str]`, `start()`, `stop()`, `running`.

- [ ] **Step 1: Write the failing tests**

Create `tests/agent/test_schema_rescan.py`:

```python
"""SchemaRescanScheduler debounce decisions, with stubs and a fake clock."""

import threading
from dataclasses import dataclass
from pathlib import Path

from devgraph.agent import schema_rescan
from devgraph.agent.schema_rescan import SchemaRescanScheduler


@dataclass
class Repo:
    repo_id: str
    path: Path
    docs_path: str | None = None
    mentions_enabled: bool = False


class Registry:
    def __init__(self, repos):
        self.repos = repos
        self.marked = []

    def list_repos(self, active_only=False):
        return list(self.repos)

    def mark_indexed(self, repo_id):
        self.marked.append(repo_id)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def setup(monkeypatch, tmp_path, *, valid=True):
    state = {"pending": True, "hash": "h1", "scans": []}
    monkeypatch.setattr(schema_rescan, "schema_pending", lambda e, r, p: state["pending"])
    monkeypatch.setattr(schema_rescan, "schema_file_hash", lambda p: state["hash"])

    def resolve(path):
        if not valid:
            raise schema_rescan.ProjectSchemaError("bad")

    monkeypatch.setattr(schema_rescan, "resolve_effective_schema", resolve)

    def scan(engine, repo_id, root, docs_path=None, mentions_enabled=False):
        state["scans"].append(repo_id)
        state["pending"] = False
        return 7

    monkeypatch.setattr(schema_rescan, "full_scan", scan)
    return state


def test_waits_for_the_quiet_period_then_rescans_once(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    clock, registry, done = Clock(), Registry([Repo("r", tmp_path)]), []
    sched = SchemaRescanScheduler(None, registry, on_rescanned=lambda r, n: done.append((r, n)), clock=clock)
    assert sched.run_once() == []           # first sighting starts the quiet period
    clock.now += 299
    assert sched.run_once() == []           # still quiet
    clock.now += 2
    assert sched.run_once() == ["r"]
    assert state["scans"] == ["r"] and registry.marked == ["r"] and done == [("r", 7)]
    clock.now += 1000
    assert sched.run_once() == []           # applied: nothing pending


def test_a_new_edit_restarts_the_quiet_period(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock)
    sched.run_once()
    clock.now += 200
    state["hash"] = "h2"
    assert sched.run_once() == []           # restarted
    clock.now += 200
    assert sched.run_once() == []           # 200 s since h2
    clock.now += 101
    assert sched.run_once() == ["r"]


def test_an_invalid_schema_is_not_retried_until_it_changes(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path, valid=False)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("r", tmp_path)]), clock=clock)
    sched.run_once()
    clock.now += 301
    assert sched.run_once() == [] and state["scans"] == []
    clock.now += 1000
    assert sched.run_once() == [] and state["scans"] == []


def test_one_failing_repo_does_not_stop_others(monkeypatch, tmp_path):
    state = setup(monkeypatch, tmp_path)
    original = schema_rescan.full_scan

    def flaky(engine, repo_id, root, **kw):
        if repo_id == "bad":
            raise RuntimeError("neo4j down")
        return original(engine, repo_id, root, **kw)

    monkeypatch.setattr(schema_rescan, "full_scan", flaky)
    clock = Clock()
    sched = SchemaRescanScheduler(None, Registry([Repo("bad", tmp_path), Repo("good", tmp_path)]), clock=clock)
    sched.run_once()
    clock.now += 301
    assert sched.run_once() == ["good"]


def test_thread_survives_failures_and_stops(monkeypatch, tmp_path):
    passes = []
    ran = threading.Event()

    class Broken:
        def list_repos(self, active_only=False):
            passes.append(1)
            if len(passes) >= 2:
                ran.set()
            raise ConnectionError("registry unavailable")

    sched = SchemaRescanScheduler(None, Broken(), interval_s=0.01)
    sched.start()
    try:
        assert sched.running and ran.wait(2)
    finally:
        sched.stop()
    assert not sched.running
```

Append to `tests/cli/test_cli.py`:

```python
def test_rescan_accepts_now(runner):
    result = runner.invoke(app, ["rescan", "--help"])
    assert result.exit_code == 0 and "--now" in result.output
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/agent/test_schema_rescan.py tests/cli/test_cli.py -q -k "schema or now"` → ImportError; `--now` missing.

- [ ] **Step 3: Implement**

Create `devgraph/agent/schema_rescan.py`:

```python
"""Debounced full rescans after a repository's devgraph.schema.yaml changes.

A schema edit leaves the repository *pending* (dispatch.schema_pending):
filesystem-provider writes pause until the schema is applied. This scheduler
applies it with a full rescan once the file has stopped changing for a quiet
period, so a burst of edits costs one rescan, not one per save.
`devgraph rescan` applies immediately instead.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from devgraph.config.project_schema import ProjectSchemaError, resolve_effective_schema, schema_file_hash
from devgraph.indexer.dispatch import full_scan, schema_pending

logger = logging.getLogger(__name__)

QUIET_PERIOD_S = 300.0
CHECK_INTERVAL_S = 30.0


class SchemaRescanScheduler:
    def __init__(
        self,
        engine: Any,
        registry: Any,
        on_rescanned: Callable[[str, int], None] | None = None,
        *,
        quiet_s: float = QUIET_PERIOD_S,
        interval_s: float = CHECK_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._on_rescanned = on_rescanned
        self._quiet_s = quiet_s
        self._interval_s = interval_s
        self._clock = clock
        # repo_id -> (pending hash, when it was first seen)
        self._seen: dict[str, tuple[str, float]] = {}
        # repo_id -> hash that failed to resolve; skipped until the file changes
        self._invalid: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None

    def run_once(self) -> list[str]:
        rescanned: list[str] = []
        now = self._clock()
        for repo in self._registry.list_repos(active_only=True):
            if self._stop.is_set():
                break
            try:
                if not schema_pending(self._engine, repo.repo_id, repo.path):
                    self._seen.pop(repo.repo_id, None)
                    self._invalid.pop(repo.repo_id, None)
                    continue
                current = schema_file_hash(repo.path)
                seen = self._seen.get(repo.repo_id)
                if seen is None or seen[0] != current:
                    self._seen[repo.repo_id] = (current, now)  # start or restart the quiet period
                    continue
                if now - seen[1] < self._quiet_s or self._invalid.get(repo.repo_id) == current:
                    continue
                try:
                    resolve_effective_schema(repo.path)
                except ProjectSchemaError as exc:
                    self._invalid[repo.repo_id] = current
                    logger.warning("project schema for %s is invalid; rescan skipped until it changes: %s", repo.repo_id, exc)
                    continue
                count = full_scan(
                    self._engine, repo.repo_id, repo.path,
                    docs_path=repo.docs_path, mentions_enabled=repo.mentions_enabled,
                )
                self._registry.mark_indexed(repo.repo_id)
                self._seen.pop(repo.repo_id, None)
                rescanned.append(repo.repo_id)
                logger.info("applied a changed project schema to %s with a full rescan (%d files)", repo.repo_id, count)
                if self._on_rescanned is not None:
                    try:
                        self._on_rescanned(repo.repo_id, count)
                    except Exception:
                        logger.debug("schema rescan callback failed for %s", repo.repo_id, exc_info=True)
            except Exception:
                logger.warning("schema rescan check failed for %s", repo.repo_id, exc_info=True)
        return rescanned

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="devgraph-schema-rescan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.warning("schema rescan pass failed", exc_info=True)
            self._stop.wait(self._interval_s)
```

Agents (`headless.py` and `tray.py`): import `SchemaRescanScheduler`; in `__init__` after `self._events = EventBroadcaster()`:

```python
        self._schema_rescans = SchemaRescanScheduler(self._engine, self._registry, on_rescanned=self._on_schema_rescanned)
```

add next to `_on_changes`:

```python
    def _on_schema_rescanned(self, repo_id: str, files: int) -> None:
        self._events.publish({"type": "reindexed", "repo_id": repo_id, "changed": files, "deleted": 0})
```

and `self._schema_rescans.start()` right after `self._watcher.start()` in `start()`, `self._schema_rescans.stop()` right after the shutdown `self._watcher.stop()` calls (headless `stop()`; tray `_quit` and the pystray-crash path — NOT the tray's pause/resume toggle).

CLI `rescan` in `devgraph/cli/main.py`: add after `full`:

```python
    now: bool = typer.Option(
        False, "--now",
        help="Apply a changed devgraph.schema.yaml right away instead of waiting for the agent's "
        "5-minute quiet period. A CLI rescan always applies immediately; this states it explicitly.",
    ),
```

(the body is unchanged: `full_scan` already applies the schema). Mention `--now` in the docstring.

- [ ] **Step 4: Run** — `uv run pytest tests/agent tests/cli -q`.

- [ ] **Step 5: Commit** — stage the new module, both agents, main.py and the two test files; `git commit -m "Rescan repositories after a quiet period once their schema changes"`.

---

### Task 4: Docs and verification

- [ ] **Step 1: Docs** — README "Project schema" section: replace the sentence about saving the schema re-syncing immediately with: "A changed `devgraph.schema.yaml` is applied by a full rescan: the DevGraph agent runs it once the file has gone 5 minutes without further edits, and `devgraph rescan <repo_id>` (or `--now`) applies it immediately. Until then filesystem nodes stay as last applied. Applying also removes nodes and relationships of user types dropped from the schema; built-in types are never touched." Update PROJECT_STATUS's filesystem-provider bullet the same way and add a shipped bullet for schema rescan semantics (state on the Repository node, pending pause, debounce, cleanup; constraint drop for removed labels and doctor drift reporting remain open). Update `docs/superpowers/specs/2026-10-04-filesystem-provider-design.md` only if it describes the immediate re-sync (add a one-line note pointing at this spec).
- [ ] **Step 2: Full suite** — `uv run pytest -q`.
- [ ] **Step 3: Commit** — `git commit -m "Document schema rescan semantics"`.
