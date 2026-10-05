# Filesystem Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `devgraph.schema.yaml` able to declare `File`/`Folder` node types and an `IS_CHILD_OF` relationship sourced from the filesystem, and have them indexed, kept current, pruned, and found by MCP search — with no change for repositories without a schema file.

**Architecture:** The schema loader learns a node `source` block and a `filesystem` relationship provider. A new `devgraph/indexer/providers/filesystem.py` builds the nodes/edges and owns their lifecycle via an `extractor = "filesystem"` property; `index_paths`, `remove_paths` and `full_scan` call it after built-in extraction. Two new `GraphEngine` methods delete/prune provider-owned nodes. `search_component` also matches a repository's declared labels.

**Tech Stack:** Python 3.13, Pydantic v2, PyYAML, neo4j driver, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-filesystem-provider-design.md`

**Working directory for every command:** the repository root of this worktree (branch `feat/filesystem-provider`, cut from upstream `master`). Python via `uv run ...`. Live tests need Neo4j 5.26 Community at `bolt://127.0.0.1:7687` (`neo4j` / `devgraph-local-dev`); they skip without it, and a skip is not a pass for this work.

## Global Constraints

- A repository without `devgraph.schema.yaml` must produce identical nodes and relationships (labels, names, `file`, relationship triples) to before this change.
- All existing tests stay green; no existing MCP tool signature changes.
- Filesystem node types: `key` exactly `[path]`, `path` metadata of type `string`, at most one per kind (`file`, `folder`). At most one `filesystem` relationship; its `to` is the folder-kind type; every `from` label is a filesystem type; no `custom` block; type not built in.
- Provider-owned node properties: `name` = `path` = repo-relative POSIX path, `extractor` = `"filesystem"`; repository root folder path `"."`. No `file`/`source_file`/`source`/`sources` on these nodes.
- `extractor` is a reserved node property name.
- An invalid schema never deletes or rewrites provider nodes: the provider is skipped (logged at warning) and built-in indexing proceeds.
- Labels and relationship types reach Cypher only after the schema's identifier-pattern validation; paths are always parameters.
- Commit messages: plain imperative summary; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock` (untracked), real names or personal paths.

## Review Focus

1. A whole directory deleted at once (watcher reports the directory, not each file) → every provider node under it is removed, and emptied ancestors with it — pinned in Tasks 2 and 3.
2. A sibling whose name shares a prefix (`src` vs `src2`) → deleting `src` never removes `src2/...` — pinned in Task 2.
3. The schema file becomes invalid mid-session → full scan neither prunes nor rewrites existing filesystem nodes — pinned in Task 3.
4. Files under ignored directories (`node_modules`, `.git`, `.venv`, `*.egg-info`) → never become File/Folder nodes, even when handed to `index_paths` directly — pinned in Task 3.
5. The schema is deleted from the repo → the next full scan removes all filesystem nodes — pinned in Task 3.

---

## File Structure

| File | Responsibility |
| :--- | :--- |
| `devgraph/graph/schema.py` (modify) | reserve `extractor` |
| `devgraph/config/project_schema.py` (modify) | `NodeSource`, filesystem provider, list `from`, cross-checks |
| `devgraph/graph/engine.py` (modify) | `delete_extracted_nodes`, `prune_extracted_nodes` |
| `devgraph/indexer/providers/__init__.py`, `devgraph/indexer/providers/filesystem.py` (create) | build/sync/prune filesystem nodes |
| `devgraph/indexer/dispatch.py` (modify) | call the provider from `index_paths`, `remove_paths`, `full_scan` |
| `devgraph/mcp/tools.py`, `devgraph/mcp/server.py` (modify) | `declared_node_labels`, `search_component(extra_labels=...)` |
| `README.md`, `PROJECT_STATUS.md` (modify) | docs |
| `tests/config/test_project_schema.py` (append) | loader rules |
| `tests/graph/test_engine_extracted_nodes.py` (create) | live delete/prune |
| `tests/indexer/test_filesystem_provider.py` (create) | provider unit tests |
| `tests/indexer/test_filesystem_provider_live.py` (create) | live end-to-end + backward compatibility |
| `tests/mcp/test_tools_declared_labels.py` (create) | MCP search |

---

### Task 1: Schema format

**Files:**
- Modify: `devgraph/graph/schema.py` (`RESERVED_NODE_PROPERTIES`, ~line 61)
- Modify: `devgraph/config/project_schema.py` (constants ~line 50-95; `NodeTypeDecl` ~131; `RelationshipDecl` ~219; `ProjectSchema` ~268; `resolve_declaration` ~447)
- Test: `tests/config/test_project_schema.py` (append; uses the file's existing `write_schema(repo_root, content)` helper and imports)

**Interfaces:**
- Produces: constants `NODE_SOURCE_PROVIDERS = ("filesystem",)`, `FILESYSTEM_KINDS = ("file", "folder")`, `FILESYSTEM_KEY = ("path",)`; `PROVIDER_KINDS = ("builtin", "custom", "filesystem")`; model `NodeSource(provider, kind)`; `NodeTypeDecl.source: NodeSource | None`; `RelationshipDecl.from_: str | tuple[str, ...]` and property `RelationshipDecl.from_labels -> tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/config/test_project_schema.py`:

```python
# --- Filesystem provider (worktree example) --------------------------------

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


def test_worktree_example_loads_and_resolves(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, WORKTREE))
    assert {"File", "Folder"} <= set(effective.node_labels)
    assert "IS_CHILD_OF" in effective.relationship_types
    (relationship,) = effective.relationships
    assert relationship.provider == "filesystem"
    assert relationship.from_labels == ("File", "Folder")
    kinds = {n.label: (n.source.provider, n.source.kind) for n in effective.node_types}
    assert kinds == {"File": ("filesystem", "file"), "Folder": ("filesystem", "folder")}


def test_a_single_from_label_is_still_accepted(tmp_path):
    effective = resolve_effective_schema(write_schema(tmp_path, WORKTREE.replace("from: [File, Folder]", "from: File")))
    assert effective.relationships[0].from_labels == ("File",)


def test_filesystem_node_types_must_be_keyed_on_path(tmp_path):
    text = WORKTREE.replace(
        "      - label: File\n        key: [path]\n        metadata: [{name: path}]",
        "      - label: File\n        key: [slug]\n        metadata: [{name: slug}]",
    )
    with pytest.raises(ProjectSchemaError, match=r"key must be exactly \[path\]"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_path_must_be_a_string(tmp_path):
    text = WORKTREE.replace(
        "      - label: File\n        key: [path]\n        metadata: [{name: path}]",
        "      - label: File\n        key: [path]\n        metadata: [{name: path, type: integer}]",
    )
    with pytest.raises(ProjectSchemaError, match="must be a string"):
        load_project_schema(write_schema(tmp_path, text))


def test_at_most_one_node_type_per_filesystem_kind(tmp_path):
    text = WORKTREE.replace(
        "    relationships:",
        "      - label: Doc\n        key: [path]\n        metadata: [{name: path}]\n"
        "        source: {provider: filesystem, kind: file}\n    relationships:",
    )
    with pytest.raises(ProjectSchemaError, match="both filesystem file types"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_must_point_to_the_folder_type(tmp_path):
    with pytest.raises(ProjectSchemaError, match="must point to the filesystem folder node type"):
        load_project_schema(write_schema(tmp_path, WORKTREE.replace("to: Folder", "to: File")))


def test_filesystem_relationship_needs_a_folder_type(tmp_path):
    text = """
        version: 1
        node_types:
          - label: File
            key: [path]
            metadata: [{name: path}]
            source: {provider: filesystem, kind: file}
        relationships:
          - type: IS_CHILD_OF
            provider: filesystem
            from: File
            to: File
    """
    with pytest.raises(ProjectSchemaError, match="none is declared"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_children_must_be_filesystem_types(tmp_path):
    text = WORKTREE.replace("from: [File, Folder]", "from: [File, Widget]").replace(
        "    relationships:",
        "      - label: Widget\n        key: [slug]\n        metadata: [{name: slug}]\n    relationships:",
    )
    with pytest.raises(ProjectSchemaError, match="'Widget' is not a filesystem node type"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_rejects_a_custom_block(tmp_path):
    text = WORKTREE.replace("        to: Folder", "        to: Folder\n        custom: {name: tree}")
    with pytest.raises(ProjectSchemaError, match="filesystem provider, which must not declare a custom block"):
        load_project_schema(write_schema(tmp_path, text))


def test_filesystem_relationship_cannot_reuse_a_builtin_type(tmp_path):
    with pytest.raises(ProjectSchemaError, match="cannot be redeclared by the filesystem provider"):
        load_project_schema(write_schema(tmp_path, WORKTREE.replace("type: IS_CHILD_OF", "type: CONTAINS")))


def test_at_most_one_filesystem_relationship(tmp_path):
    # Same indentation as WORKTREE's own relationship items (6 spaces).
    text = WORKTREE + (
        "      - type: IN_FOLDER\n"
        "        provider: filesystem\n"
        "        from: File\n"
        "        to: Folder\n"
    )
    with pytest.raises(ProjectSchemaError, match="at most one filesystem relationship"):
        load_project_schema(write_schema(tmp_path, text))


@pytest.mark.parametrize("from_value", ["[]", "[File, File]"])
def test_relationship_from_list_must_be_non_empty_and_unique(tmp_path, from_value):
    with pytest.raises(ProjectSchemaError):
        load_project_schema(write_schema(tmp_path, WORKTREE.replace("[File, Folder]", from_value)))


@pytest.mark.parametrize(
    "source",
    ["{provider: git, kind: file}", "{provider: filesystem, kind: symlink}", "{provider: filesystem}"],
)
def test_unknown_node_sources_are_rejected(tmp_path, source):
    text = WORKTREE.replace("{provider: filesystem, kind: file}", source)
    with pytest.raises(ProjectSchemaError):
        load_project_schema(write_schema(tmp_path, text))


def test_extractor_is_a_reserved_property(tmp_path):
    assert "extractor" in RESERVED_NODE_PROPERTIES
    # WIDGET's metadata list items sit at 10 spaces; this adds a second one.
    text = WIDGET + "          - name: extractor\n"
    with pytest.raises(ProjectSchemaError, match="reserved"):
        load_project_schema(write_schema(tmp_path, text))


def test_every_label_in_a_from_list_must_resolve(tmp_path):
    text = """
        version: 1
        node_types:
          - label: Widget
            key: [slug]
            metadata: [{name: slug}]
        relationships:
          - type: LINKS
            provider: custom
            custom: {name: linker}
            from: [Widget, Gadget]
            to: Widget
    """
    with pytest.raises(ProjectSchemaError, match="'Gadget' is not a node label"):
        resolve_effective_schema(write_schema(tmp_path, text))


def test_json_schema_describes_node_sources_and_list_from():
    defs = project_schema_json_schema()["$defs"]
    assert defs["NodeSource"]["properties"]["kind"]["enum"] == ["file", "folder"]
    assert "source" in defs["NodeTypeDecl"]["properties"]
    from_schema = defs["RelationshipDecl"]["properties"]["from"]
    assert {variant.get("type") for variant in from_schema["anyOf"]} == {"string", "array"}
```

If either string-surgery test fails with a YAML or unrelated validation error instead of its asserted message, the fixture text is wrong, not the code — fix the fixture and say so in the report.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/config/test_project_schema.py -q`
Expected: the new tests fail (unknown `source` key rejected by `extra="forbid"`, `filesystem` not a provider, `from_labels` missing, `extractor` not reserved); every pre-existing test still passes.

- [ ] **Step 3: Reserve `extractor`**

In `devgraph/graph/schema.py`, change `RESERVED_NODE_PROPERTIES` to:

```python
RESERVED_NODE_PROPERTIES: frozenset[str] = frozenset(
    {"repo_id", "name", "file", "source_file", "source", "sources", "extractor"}
)
```

and extend the comment above it with one sentence: "`extractor` marks nodes a schema-declared provider owns (see devgraph/indexer/providers/)."

- [ ] **Step 4: Extend the loader**

In `devgraph/config/project_schema.py`:

1. Replace the `PROVIDER_KINDS` block with:

```python
#: Who produces a declared relationship. "builtin" reuses one of DevGraph's
#: own relationship types; "custom" names an out-of-tree provider that this
#: module records as data and never loads; "filesystem" links each
#: filesystem node to its parent folder (devgraph/indexer/providers/).
PROVIDER_KINDS: tuple[str, ...] = ("builtin", "custom", "filesystem")

#: Where a user-declared node type's nodes come from. Built-in labels are
#: produced by DevGraph's own extractors and never declare a source.
NODE_SOURCE_PROVIDERS: tuple[str, ...] = ("filesystem",)

#: What a filesystem-sourced node type represents.
FILESYSTEM_KINDS: tuple[str, ...] = ("file", "folder")

#: The one key every filesystem node type must declare: its repo-relative path.
FILESYSTEM_KEY: tuple[str, ...] = ("path",)
```

2. Next to the other `Literal` aliases add:

```python
NodeSourceProvider = Literal[NODE_SOURCE_PROVIDERS]
FilesystemKind = Literal[FILESYSTEM_KINDS]
```

3. Before `class NodeTypeDecl`, add:

```python
class NodeSource(BaseModel):
    """Where a user-declared node type's nodes are extracted from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: NodeSourceProvider
    kind: FilesystemKind
```

4. In `NodeTypeDecl`, add the field `source: NodeSource | None = None` after `description`, and at the end of `_check_key_and_metadata` (before `return self`):

```python
        if self.source is not None:
            if self.key != FILESYSTEM_KEY:
                raise ValueError(
                    f"node type {self.label!r} is sourced from the filesystem, so "
                    f"its key must be exactly [path]: the provider writes one node "
                    f"per repo-relative path"
                )
            if by_name["path"].type != "string":
                raise ValueError(
                    f"node type {self.label!r} is sourced from the filesystem, so "
                    f"its path metadata field must be a string"
                )
```

5. In `RelationshipDecl`:
   - change the field to `from_: str | tuple[str, ...] = Field(alias="from")`;
   - change the existing endpoint validator's decorator from `@field_validator("from_", "to")` to `@field_validator("to")`;
   - add:

```python
    @field_validator("from_")
    @classmethod
    def _check_from(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        labels = (value,) if isinstance(value, str) else value
        if not labels:
            raise ValueError("relationship 'from' must name at least one label")
        if len(set(labels)) != len(labels):
            raise ValueError("relationship 'from' lists a label more than once")
        for label in labels:
            _require_identifier(label, LABEL_PATTERN, "relationship endpoint label")
        return value

    @property
    def from_labels(self) -> tuple[str, ...]:
        """`from` as a tuple, whether it was written as one label or a list."""
        return (self.from_,) if isinstance(self.from_, str) else tuple(self.from_)
```

   - in `_check_provider`, change the `else:` branch to `elif self.provider == "custom":` (body unchanged) and add:

```python
        else:
            if self.custom is not None:
                raise ValueError(
                    f"relationship {self.type!r} uses the filesystem provider, "
                    f"which must not declare a custom block"
                )
            if self.type in RELATIONSHIP_TYPES:
                raise ValueError(
                    f"relationship type {self.type!r} is built-in and cannot be "
                    f"redeclared by the filesystem provider"
                )
```

6. In `ProjectSchema`, add a second validator after `_check_labels`:

```python
    @model_validator(mode="after")
    def _check_filesystem(self) -> ProjectSchema:
        by_kind: dict[str, str] = {}
        for node_type in self.node_types:
            if node_type.source is None:
                continue
            kind = node_type.source.kind
            if kind in by_kind:
                raise ValueError(
                    f"node types {by_kind[kind]!r} and {node_type.label!r} are both "
                    f"filesystem {kind} types; declare at most one"
                )
            by_kind[kind] = node_type.label

        filesystem_relationships = [r for r in self.relationships if r.provider == "filesystem"]
        if len(filesystem_relationships) > 1:
            raise ValueError(
                "at most one filesystem relationship may be declared; found "
                + ", ".join(repr(r.type) for r in filesystem_relationships)
            )
        filesystem_labels = set(by_kind.values())
        folder = by_kind.get("folder")
        for relationship in filesystem_relationships:
            if folder is None or relationship.to != folder:
                where = f" {folder!r}" if folder else ", and none is declared"
                raise ValueError(
                    f"filesystem relationship {relationship.type!r} must point to "
                    f"the filesystem folder node type{where}"
                )
            for label in relationship.from_labels:
                if label not in filesystem_labels:
                    raise ValueError(
                        f"filesystem relationship {relationship.type!r} from label "
                        f"{label!r} is not a filesystem node type"
                    )
        return self
```

7. In `resolve_declaration`, change the endpoint loop to cover every `from` label:

```python
    for relationship in declaration.relationships:
        endpoints = [("from", label) for label in relationship.from_labels] + [("to", relationship.to)]
        for role, label in endpoints:
```

(keep the loop body unchanged).

Also update the module docstring's provider description if it says only builtin/custom exist.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/config tests/graph/test_engine_schema_constraints.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add devgraph/graph/schema.py devgraph/config/project_schema.py tests/config/test_project_schema.py
git commit -m "Declare filesystem-sourced node types in the project schema"
```

---

### Task 2: Engine support for provider-owned nodes

**Files:**
- Modify: `devgraph/graph/engine.py` (two Cypher constants near `_DELETE_BY_SOURCE_FILE_CYPHER`; two methods after `delete_nodes_by_source_file`)
- Test: `tests/graph/test_engine_extracted_nodes.py` (create)

**Interfaces:**
- Produces: `GraphEngine.delete_extracted_nodes(repo_id: str, extractor: str, paths: list[str]) -> None` (deletes provider nodes whose `name` equals a path or lies under it as a directory); `GraphEngine.prune_extracted_nodes(repo_id: str, extractor: str, keep: list[str]) -> int` (`keep` items are `"<Label>:<name>"`; deletes every other node with that `extractor` in the repo; returns the count).

- [ ] **Step 1: Write the failing tests**

Create `tests/graph/test_engine_extracted_nodes.py`:

```python
"""delete_extracted_nodes / prune_extracted_nodes against a live Neo4j."""

import pytest

from devgraph.graph.engine import GraphEngine

REPO = "_smoketest_extracted_nodes"
OTHER = "_smoketest_extracted_nodes_other"


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


def seed(engine, repo, label, paths, extractor="filesystem"):
    engine.upsert_nodes([
        {"label": label, "repo_id": repo, "name": p, "properties": {"path": p, "extractor": extractor}}
        for p in paths
    ])


def names(engine, repo):
    rows = engine.run_cypher("MATCH (n {repo_id: $r}) RETURN labels(n)[0] + ':' + n.name AS k", {"r": repo})
    return sorted(r["k"] for r in rows)


def test_delete_removes_exact_paths_and_everything_below_a_directory(engine):
    seed(engine, REPO, "FsFile", ["src/a.py", "src/sub/b.py", "src2/c.py", "top.py"])
    seed(engine, REPO, "FsFolder", [".", "src", "src/sub", "src2"])
    engine.delete_extracted_nodes(REPO, "filesystem", ["src", "top.py"])
    assert names(engine, REPO) == ["FsFile:src2/c.py", "FsFolder:.", "FsFolder:src2"]


def test_delete_leaves_other_extractors_and_repos_alone(engine):
    seed(engine, REPO, "FsFile", ["src/a.py"])
    seed(engine, REPO, "FsFile", ["src/b.py"], extractor="other")
    engine.upsert_nodes([{"label": "Module", "repo_id": REPO, "name": "src/c.py", "properties": {"source_file": "src/c.py"}}])
    seed(engine, OTHER, "FsFile", ["src/a.py"])
    engine.delete_extracted_nodes(REPO, "filesystem", ["src"])
    assert names(engine, REPO) == ["FsFile:src/b.py", "Module:src/c.py"]
    assert names(engine, OTHER) == ["FsFile:src/a.py"]


def test_delete_with_no_paths_is_a_no_op(engine):
    seed(engine, REPO, "FsFile", ["a.py"])
    engine.delete_extracted_nodes(REPO, "filesystem", [])
    assert names(engine, REPO) == ["FsFile:a.py"]


def test_prune_keeps_only_listed_nodes_and_counts_the_rest(engine):
    seed(engine, REPO, "FsFile", ["a.py", "b.py"])
    seed(engine, REPO, "FsFolder", ["."])
    seed(engine, REPO, "OldType", ["x"])
    seed(engine, REPO, "FsFile", ["kept-by-other-extractor.py"], extractor="other")
    pruned = engine.prune_extracted_nodes(REPO, "filesystem", ["FsFile:a.py", "FsFolder:."])
    assert pruned == 2
    assert names(engine, REPO) == ["FsFile:a.py", "FsFile:kept-by-other-extractor.py", "FsFolder:."]


def test_prune_with_an_empty_keep_list_removes_every_provider_node(engine):
    seed(engine, REPO, "FsFile", ["a.py"])
    assert engine.prune_extracted_nodes(REPO, "filesystem", []) == 1
    assert names(engine, REPO) == []


def test_delete_also_removes_relationships(engine):
    seed(engine, REPO, "FsFile", ["d/a.py"])
    seed(engine, REPO, "FsFolder", ["d"])
    engine.upsert_relationships([{
        "from_label": "FsFile", "from_name": "d/a.py", "rel_type": "IN_DIR",
        "to_label": "FsFolder", "to_name": "d", "repo_id": REPO, "properties": {},
    }])
    engine.delete_extracted_nodes(REPO, "filesystem", ["d/a.py"])
    rels = engine.run_cypher("MATCH ({repo_id: $r})-[x]->() RETURN count(x) AS n", {"r": REPO})
    assert rels == [{"n": 0}]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/graph/test_engine_extracted_nodes.py -q`
Expected: `AttributeError: 'GraphEngine' object has no attribute 'delete_extracted_nodes'` (not skips — Neo4j must be up).

- [ ] **Step 3: Implement**

In `devgraph/graph/engine.py`, add near `_DELETE_BY_SOURCE_FILE_CYPHER`:

```python
# Nodes a schema-declared provider owns (devgraph/indexer/providers/) are
# tagged with `extractor` and keyed by repo-relative path in `name`; they
# never carry `file`/`source_file`/`source`, so the built-in per-file cleanup
# above never touches them and these two queries are their whole lifecycle.
# A path deletes its own node and, as a directory, everything below it --
# the trailing '/' keeps `src` from matching `src2/...`.
_DELETE_EXTRACTED_PATHS_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor "
    "AND (n.name IN $paths OR any(p IN $paths WHERE n.name STARTS WITH p + '/')) "
    "DETACH DELETE n"
)
_PRUNE_EXTRACTED_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor "
    "AND NOT (labels(n)[0] + ':' + n.name) IN $keep "
    "DETACH DELETE n RETURN count(n) AS pruned"
)
```

and after `delete_nodes_by_source_file`:

```python
    def delete_extracted_nodes(self, repo_id: str, extractor: str, paths: list[str]) -> None:
        """Delete one provider's nodes at these repo-relative paths, or below them."""
        if not paths:
            return
        with self._driver.session() as session:
            _retry_transient(
                session.run, _DELETE_EXTRACTED_PATHS_CYPHER, repo_id=repo_id, extractor=extractor, paths=paths
            )

    def prune_extracted_nodes(self, repo_id: str, extractor: str, keep: list[str]) -> int:
        """Delete every node of one provider in a repo except `keep` ("Label:name").

        The full-scan reconcile for provider-owned nodes: it also removes nodes
        of a label the schema no longer declares.
        """
        with self._driver.session() as session:
            result = _retry_transient(
                session.run, _PRUNE_EXTRACTED_CYPHER, repo_id=repo_id, extractor=extractor, keep=keep
            )
            records = [record.data() for record in result or []]
        return records[0]["pruned"] if records else 0
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/graph -q`
Expected: all pass, none of the new tests skipped.

- [ ] **Step 5: Commit**

```bash
git add devgraph/graph/engine.py tests/graph/test_engine_extracted_nodes.py
git commit -m "Delete and prune provider-owned nodes by path"
```

---

### Task 3: Filesystem provider and indexing hooks

**Files:**
- Create: `devgraph/indexer/providers/__init__.py`, `devgraph/indexer/providers/filesystem.py`
- Modify: `devgraph/indexer/dispatch.py` (import; helper functions; end of `index_paths`; end of `remove_paths`; `full_scan`)
- Test: `tests/indexer/test_filesystem_provider.py`, `tests/indexer/test_filesystem_provider_live.py` (create)

**Interfaces:**
- Consumes: `resolve_effective_schema`, `ProjectSchemaError`, `EffectiveSchema`, `NodeTypeDecl.source`, `RelationshipDecl.from_labels` (Task 1); `delete_extracted_nodes`, `prune_extracted_nodes` (Task 2); `GraphEngine.upsert_nodes`/`upsert_relationships`; `dispatch.is_ignored_path`, `dispatch._is_indexable_file`, `dispatch._indexable_paths`.
- Produces (in `filesystem.py`): `EXTRACTOR = "filesystem"`, `ROOT_PATH = "."`, `FilesystemSpec(file_label, folder_label, relationship, child_labels)`, `filesystem_spec(effective) -> FilesystemSpec | None`, `load_filesystem_spec(repo_root) -> FilesystemSpec | None` (raises `ProjectSchemaError`), `ancestors(path) -> list[str]`, `build_graph(spec, repo_id, files) -> tuple[list[dict], list[dict]]`, `sync_present(engine, repo_id, spec, files)`, `sync_absent(engine, repo_id, repo_root, spec, paths, is_indexable)`, `reconcile(engine, repo_id, spec, files) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/indexer/test_filesystem_provider.py` (pure; no Neo4j):

```python
"""Filesystem provider building blocks, with a recording engine."""

import textwrap

from devgraph.config.project_schema import resolve_effective_schema
from devgraph.indexer.providers import filesystem
from devgraph.indexer.providers.filesystem import FilesystemSpec, ancestors, build_graph, filesystem_spec

SPEC = FilesystemSpec(file_label="File", folder_label="Folder", relationship="IS_CHILD_OF",
                      child_labels=frozenset({"File", "Folder"}))
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


class Recorder:
    def __init__(self):
        self.nodes, self.rels, self.deleted, self.kept = [], [], [], None

    def upsert_nodes(self, nodes):
        self.nodes.extend(nodes)

    def upsert_relationships(self, rels):
        self.rels.extend(rels)

    def delete_extracted_nodes(self, repo_id, extractor, paths):
        self.deleted.append((repo_id, extractor, sorted(paths)))

    def prune_extracted_nodes(self, repo_id, extractor, keep):
        self.kept = (repo_id, extractor, sorted(keep))
        return 0


def test_ancestors_run_from_nearest_to_root():
    assert ancestors("a/b/c.py") == ["a/b", "a", "."]
    assert ancestors("top.py") == ["."]


def test_spec_comes_from_the_effective_schema(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(WORKTREE))
    assert filesystem_spec(resolve_effective_schema(tmp_path)) == SPEC


def test_no_filesystem_types_means_no_spec(tmp_path):
    assert filesystem_spec(resolve_effective_schema(tmp_path)) is None


def test_build_graph_emits_files_folders_and_parent_edges():
    nodes, rels = build_graph(SPEC, "demo", {"a/b/c.py", "top.py"})
    assert sorted((n["label"], n["name"]) for n in nodes) == [
        ("File", "a/b/c.py"), ("File", "top.py"), ("Folder", "."), ("Folder", "a"), ("Folder", "a/b"),
    ]
    assert all(n["properties"] == {"path": n["name"], "extractor": "filesystem"} for n in nodes)
    assert all(n["repo_id"] == "demo" for n in nodes)
    assert sorted((r["from_label"], r["from_name"], r["to_name"]) for r in rels) == [
        ("File", "a/b/c.py", "a/b"), ("File", "top.py", "."), ("Folder", "a", "."), ("Folder", "a/b", "a"),
    ]
    assert {r["rel_type"] for r in rels} == {"IS_CHILD_OF"} and {r["to_label"] for r in rels} == {"Folder"}


def test_build_graph_respects_which_kinds_are_declared():
    files_only = FilesystemSpec("File", None, None, frozenset())
    nodes, rels = build_graph(files_only, "demo", {"a/b.py"})
    assert [(n["label"], n["name"]) for n in nodes] == [("File", "a/b.py")] and rels == []
    folders_link_only = FilesystemSpec("File", "Folder", "IN", frozenset({"Folder"}))
    _, rels = build_graph(folders_link_only, "demo", {"a/b.py"})
    assert [(r["from_label"], r["from_name"]) for r in rels] == [("Folder", "a")]


def test_sync_absent_deletes_paths_then_folders_left_empty_on_disk(tmp_path):
    (tmp_path / "keep").mkdir()
    (tmp_path / "keep" / "x.py").write_text("")
    engine = Recorder()
    filesystem.sync_absent(engine, "demo", tmp_path, SPEC, {"gone/sub/y.py", "keep/z.py"},
                           is_indexable=lambda p: p.is_file())
    assert engine.deleted[0] == ("demo", "filesystem", ["gone/sub/y.py", "keep/z.py"])
    # `gone` and `gone/sub` no longer hold any file; `keep` and the root still do.
    assert engine.deleted[1] == ("demo", "filesystem", ["gone", "gone/sub"])


def test_sync_absent_without_a_folder_type_only_deletes_paths(tmp_path):
    engine = Recorder()
    filesystem.sync_absent(engine, "demo", tmp_path, FilesystemSpec("File", None, None, frozenset()),
                           {"a/b.py"}, is_indexable=lambda p: p.is_file())
    assert engine.deleted == [("demo", "filesystem", ["a/b.py"])]


def test_reconcile_keeps_exactly_the_desired_nodes():
    engine = Recorder()
    filesystem.reconcile(engine, "demo", SPEC, {"a/b.py"})
    assert engine.kept == ("demo", "filesystem", ["File:a/b.py", "Folder:.", "Folder:a"])


def test_reconcile_without_a_spec_prunes_everything():
    engine = Recorder()
    filesystem.reconcile(engine, "demo", None, {"a/b.py"})
    assert engine.kept == ("demo", "filesystem", [])
```

Create `tests/indexer/test_filesystem_provider_live.py`:

```python
"""Worktree example end to end through dispatch, against a live Neo4j."""

import shutil
import textwrap

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.indexer.providers import filesystem

REPO = "_smoketest_fs_provider"
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
    (root / "pkg" / "sub").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("class Widget:\n    def run(self):\n        return 1\n")
    (root / "pkg" / "sub" / "util.py").write_text("def helper():\n    return 2\n")
    (root / "README.md").write_text("# Demo\n")
    (root / "node_modules" / "dep").mkdir(parents=True)
    (root / "node_modules" / "dep" / "index.js").write_text("module.exports = 1;\n")
    return root


def with_schema(root, text=WORKTREE):
    (root / "devgraph.schema.yaml").write_text(textwrap.dedent(text))
    return root


def fs_nodes(engine):
    rows = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.extractor = 'filesystem' RETURN labels(n)[0] + ':' + n.name AS k",
        {"r": REPO},
    )
    return sorted(r["k"] for r in rows)


def fs_edges(engine):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x:IS_CHILD_OF]->(b {repo_id: $r}) RETURN a.name + '>' + b.name AS e",
        {"r": REPO},
    )
    return sorted(r["e"] for r in rows)


def snapshot(engine):
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) RETURN labels(n) AS labels, n.name AS name, n.file AS file", {"r": REPO}
    )
    rels = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn",
        {"r": REPO},
    )
    return (
        sorted((tuple(sorted(n["labels"])), n["name"] or "", n["file"] or "") for n in nodes),
        sorted((r["a"], r["an"] or "", r["t"], r["b"], r["bn"] or "") for r in rels),
    )


def scan(engine, root):
    provision_repository_schema(engine, root)
    engine.upsert_repository(REPO, REPO, str(root))
    full_scan(engine, REPO, root)


def test_full_scan_builds_the_worktree_graph_beside_builtin_nodes(engine, repo):
    scan(engine, with_schema(repo))
    assert fs_nodes(engine) == [
        "File:README.md", "File:devgraph.schema.yaml", "File:pkg/mod.py", "File:pkg/sub/util.py",
        "Folder:.", "Folder:pkg", "Folder:pkg/sub",
    ]
    assert fs_edges(engine) == [
        "README.md>.", "devgraph.schema.yaml>.", "pkg/mod.py>pkg", "pkg/sub/util.py>pkg/sub",
        "pkg/sub>pkg", "pkg>.",
    ]
    modules = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS n", {"r": REPO})
    assert "pkg/mod.py" in {m["n"] for m in modules}  # built-in extraction unaffected


def test_ignored_directories_never_become_nodes(engine, repo):
    scan(engine, with_schema(repo))
    index_paths(engine, REPO, repo, {repo / "node_modules" / "dep" / "index.js"})
    assert not [k for k in fs_nodes(engine) if "node_modules" in k]


def test_new_file_and_folder_are_added_incrementally(engine, repo):
    scan(engine, with_schema(repo))
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.md").write_text("# Guide\n")
    index_paths(engine, REPO, repo, {repo / "docs" / "guide.md"})
    assert {"File:docs/guide.md", "Folder:docs"} <= set(fs_nodes(engine))
    assert {"docs/guide.md>docs", "docs>."} <= set(fs_edges(engine))


def test_deleting_the_last_file_removes_emptied_folders(engine, repo):
    scan(engine, with_schema(repo))
    (repo / "pkg" / "sub" / "util.py").unlink()
    remove_paths(engine, REPO, repo, {repo / "pkg" / "sub" / "util.py"})
    nodes = fs_nodes(engine)
    assert "File:pkg/sub/util.py" not in nodes and "Folder:pkg/sub" not in nodes
    assert "Folder:pkg" in nodes and "Folder:." in nodes


def test_deleting_a_whole_directory_removes_everything_below_it(engine, repo):
    scan(engine, with_schema(repo))
    shutil.rmtree(repo / "pkg")
    remove_paths(engine, REPO, repo, {repo / "pkg"})
    assert not [k for k in fs_nodes(engine) if ":pkg" in k]
    assert "Folder:." in fs_nodes(engine)


def test_removing_the_schema_prunes_every_filesystem_node_on_rescan(engine, repo):
    scan(engine, with_schema(repo))
    (repo / "devgraph.schema.yaml").unlink()
    scan(engine, repo)
    assert fs_nodes(engine) == []


def test_an_invalid_schema_leaves_existing_filesystem_nodes_alone(engine, repo):
    scan(engine, with_schema(repo))
    before = fs_nodes(engine)
    (repo / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    (repo / "pkg" / "new.py").write_text("x = 1\n")
    full_scan(engine, REPO, repo)  # provisioning would refuse; the scan itself must not prune
    assert fs_nodes(engine) == before
    modules = engine.run_cypher("MATCH (m:Module {repo_id: $r}) RETURN m.name AS n", {"r": REPO})
    assert "pkg/new.py" in {m["n"] for m in modules}  # built-in indexing carried on


def test_without_a_schema_file_the_graph_is_unchanged(engine, repo, monkeypatch):
    scan(engine, repo)
    with_provider = snapshot(engine)
    assert fs_nodes(engine) == []

    engine.delete_repository(REPO)
    monkeypatch.setattr(filesystem, "sync_present", lambda *a, **k: None)
    monkeypatch.setattr(filesystem, "sync_absent", lambda *a, **k: None)
    monkeypatch.setattr(filesystem, "reconcile", lambda *a, **k: 0)
    scan(engine, repo)
    assert snapshot(engine) == with_provider
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/indexer/test_filesystem_provider.py tests/indexer/test_filesystem_provider_live.py -q`
Expected: `ModuleNotFoundError: No module named 'devgraph.indexer.providers'`.

- [ ] **Step 3: Implement the provider**

Create `devgraph/indexer/providers/__init__.py` containing only `"""Schema-declared extraction providers (see devgraph/config/project_schema.py)."""`.

Create `devgraph/indexer/providers/filesystem.py`:

```python
"""Filesystem provider: nodes for a repository's own files and folders.

Fills the node types a project schema sources from the filesystem (see
`NodeSource` in devgraph/config/project_schema.py): one file-kind node per
indexable file, one folder-kind node per ancestor directory ("." is the
repository root), and, if declared, an edge from each child to its parent
folder.

Every node is keyed by its repo-relative path, written to both `path` (the
declared key) and `name` (what the engine MERGEs and matches edges on), so
the engine's (repo_id, name) MERGE is exactly a merge on the declared key.
Nodes are tagged `extractor = "filesystem"` and carry none of the built-in
provenance properties, so this module alone decides when they go away.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from devgraph.config.project_schema import EffectiveSchema, resolve_effective_schema

EXTRACTOR = "filesystem"
ROOT_PATH = "."


@dataclass(frozen=True)
class FilesystemSpec:
    file_label: str | None
    folder_label: str | None
    relationship: str | None
    child_labels: frozenset[str]


def filesystem_spec(effective: EffectiveSchema) -> FilesystemSpec | None:
    """What the schema asks this provider to build, or None if nothing."""
    file_label = folder_label = None
    for node_type in effective.node_types:
        if node_type.source is None or node_type.source.provider != EXTRACTOR:
            continue
        if node_type.source.kind == "file":
            file_label = node_type.label
        else:
            folder_label = node_type.label
    if file_label is None and folder_label is None:
        return None
    relationship = next((r for r in effective.relationships if r.provider == EXTRACTOR), None)
    return FilesystemSpec(
        file_label=file_label,
        folder_label=folder_label,
        relationship=relationship.type if relationship else None,
        child_labels=frozenset(relationship.from_labels) if relationship else frozenset(),
    )


def load_filesystem_spec(repo_root: Path) -> FilesystemSpec | None:
    """Resolve the repository's schema; raises ProjectSchemaError if invalid."""
    return filesystem_spec(resolve_effective_schema(repo_root))


def ancestors(path: str) -> list[str]:
    """Folders containing `path`, nearest first, ending at the root "."."""
    out = []
    parent = PurePosixPath(path).parent
    while str(parent) != ROOT_PATH:
        out.append(str(parent))
        parent = parent.parent
    out.append(ROOT_PATH)
    return out


def _node(label: str, repo_id: str, path: str) -> dict[str, Any]:
    return {"label": label, "repo_id": repo_id, "name": path, "properties": {"path": path, "extractor": EXTRACTOR}}


def _edge(spec: FilesystemSpec, child_label: str, child: str, repo_id: str) -> dict[str, Any]:
    return {
        "from_label": child_label,
        "from_name": child,
        "rel_type": spec.relationship,
        "to_label": spec.folder_label,
        "to_name": ancestors(child)[0],
        "repo_id": repo_id,
        "properties": {},
    }


def build_graph(spec: FilesystemSpec, repo_id: str, files: set[str]) -> tuple[list[dict], list[dict]]:
    """Nodes and parent edges for these repo-relative files and their folders."""
    ordered = sorted(files)
    folders = sorted({folder for path in ordered for folder in ancestors(path)})
    nodes: list[dict[str, Any]] = []
    rels: list[dict[str, Any]] = []
    if spec.file_label:
        nodes += [_node(spec.file_label, repo_id, path) for path in ordered]
    if spec.folder_label:
        nodes += [_node(spec.folder_label, repo_id, folder) for folder in folders]
    if spec.relationship and spec.folder_label:
        if spec.file_label in spec.child_labels:
            rels += [_edge(spec, spec.file_label, path, repo_id) for path in ordered]
        if spec.folder_label in spec.child_labels:
            rels += [_edge(spec, spec.folder_label, folder, repo_id) for folder in folders if folder != ROOT_PATH]
    return nodes, rels


def sync_present(engine: Any, repo_id: str, spec: FilesystemSpec, files: set[str]) -> None:
    """Upsert these existing files, their ancestor folders and the edges between them."""
    if not files:
        return
    nodes, rels = build_graph(spec, repo_id, files)
    engine.upsert_nodes(nodes)
    engine.upsert_relationships(rels)


def sync_absent(
    engine: Any,
    repo_id: str,
    repo_root: Path,
    spec: FilesystemSpec,
    paths: set[str],
    is_indexable: Callable[[Path], bool],
) -> None:
    """Remove nodes at (or below) deleted paths, then folders now empty on disk.

    "Empty" means no indexable file remains anywhere below the folder,
    decided from the disk rather than the graph so a missed earlier event
    can't keep a dead folder alive.
    """
    if not paths:
        return
    engine.delete_extracted_nodes(repo_id, EXTRACTOR, sorted(paths))
    if not spec.folder_label:
        return
    candidates = {folder for path in paths for folder in ancestors(path) if folder != ROOT_PATH}
    dead = sorted(f for f in candidates if not _holds_indexable_file(repo_root / f, is_indexable))
    if dead:
        engine.delete_extracted_nodes(repo_id, EXTRACTOR, dead)


def _holds_indexable_file(folder: Path, is_indexable: Callable[[Path], bool]) -> bool:
    if not folder.is_dir():
        return False
    return any(is_indexable(p) for p in folder.rglob("*"))


def reconcile(engine: Any, repo_id: str, spec: FilesystemSpec | None, files: set[str]) -> int:
    """Prune this provider's nodes that the current schema and disk don't produce.

    With no spec (the schema declares no filesystem types, or the file is
    gone) every filesystem node in the repository is pruned.
    """
    keep: list[str] = []
    if spec is not None:
        nodes, _ = build_graph(spec, repo_id, files)
        keep = [f"{n['label']}:{n['name']}" for n in nodes]
    return engine.prune_extracted_nodes(repo_id, EXTRACTOR, keep)
```

- [ ] **Step 4: Wire it into dispatch**

In `devgraph/indexer/dispatch.py`:

1. Imports: `from devgraph.config.project_schema import ProjectSchemaError` and `from devgraph.indexer.providers import filesystem` (call it as `filesystem.sync_present(...)` etc. — the live backward-compatibility test monkeypatches those module attributes).

2. Add these helpers after `is_ignored_path`:

```python
def _filesystem_spec(repo_root: Path) -> tuple[bool, filesystem.FilesystemSpec | None]:
    """(schema usable, spec). An invalid schema disables the provider for this
    call -- including any prune -- so a bad edit never deletes good nodes."""
    try:
        return True, filesystem.load_filesystem_spec(repo_root)
    except ProjectSchemaError as exc:
        logger.warning("project schema for %s is invalid; filesystem provider skipped: %s", repo_root, exc)
        return False, None


def _repo_relative(repo_root: Path, path: Path) -> str | None:
    """Repo-relative POSIX path, or None for a path outside the repository."""
    try:
        return Path(path).resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _is_provider_file(repo_root: Path, path: Path) -> bool:
    """A file the filesystem provider represents: what a full scan would index."""
    rel = _repo_relative(repo_root, path)
    return rel is not None and _is_indexable_file(path) and not is_ignored_path(Path(rel))
```

3. At the end of `index_paths`, just before `return indexed`:

```python
    ok, spec = _filesystem_spec(repo_root)
    if ok and spec is not None:
        present = {
            rel for p in paths
            if _is_provider_file(repo_root, Path(p)) and (rel := _repo_relative(repo_root, Path(p))) is not None
        }
        filesystem.sync_present(engine, repo_id, spec, present)
```

4. At the end of `remove_paths`, just before `return cleaned`:

```python
    ok, spec = _filesystem_spec(repo_root)
    if ok and spec is not None:
        gone = {rel for p in paths if (rel := _repo_relative(repo_root, Path(p))) is not None and rel != "."}
        filesystem.sync_absent(
            engine, repo_id, repo_root, spec, gone,
            is_indexable=lambda p: _is_provider_file(repo_root, p),
        )
```

5. In `full_scan`, between `prune_stale_files(...)` and `all_files = ...`, restructure to:

```python
    prune_stale_files(engine, repo_id, repo_root, docs_path=docs_path, mentions_enabled=mentions_enabled)
    all_files = _indexable_paths(repo_root)
    ok, spec = _filesystem_spec(repo_root)
    if ok:
        on_disk = {rel for p in all_files if (rel := _repo_relative(repo_root, p)) is not None}
        filesystem.reconcile(engine, repo_id, spec, on_disk)
    return index_paths(engine, repo_id, repo_root, all_files, docs_path=docs_path, mentions_enabled=mentions_enabled)
```

Also add a sentence to `full_scan`'s docstring: filesystem-provider nodes are reconciled the same way (see providers/filesystem.py).

Note `index_paths` resolves the schema again inside the `full_scan` call; that is one extra small YAML parse per full scan — acceptable, do not add caching.

If any existing test passes a fake engine object that lacks the two new engine methods to `full_scan`, add no-op methods to that fake (report which); do not change dispatch to tolerate missing methods.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/indexer tests/graph tests/config -q`
Expected: all pass; the live file's tests ran (not skipped).

- [ ] **Step 6: Commit**

```bash
git add devgraph/indexer/providers/__init__.py devgraph/indexer/providers/filesystem.py devgraph/indexer/dispatch.py \
  tests/indexer/test_filesystem_provider.py tests/indexer/test_filesystem_provider_live.py
git commit -m "Index filesystem-sourced node types from the worktree"
```

---

### Task 4: MCP search and docs

**Files:**
- Modify: `devgraph/mcp/tools.py` (`search_component`; new `declared_node_labels`), `devgraph/mcp/server.py` (`search_component` wrapper)
- Modify: `README.md` ("Project schema constraints" section, ~line 69), `PROJECT_STATUS.md` (`devgraph/config/` code-map bullet, ~line 36; a shipped bullet)
- Test: `tests/mcp/test_tools_declared_labels.py` (create)

**Interfaces:**
- Consumes: `resolve_effective_schema`, `ProjectSchemaError`, `LABEL_PATTERN` (Task 1); `RepoRegistry.get(repo_id) -> RepoRecord | None` (`.path`).
- Produces: `tools.declared_node_labels(registry, repo_id) -> tuple[str, ...]`; `tools.search_component(..., extra_labels: tuple[str, ...] = ())`.

- [ ] **Step 1: Write the failing tests**

Create `tests/mcp/test_tools_declared_labels.py`:

```python
"""search_component covers a repository's schema-declared labels."""

import asyncio
import textwrap
from dataclasses import dataclass
from pathlib import Path

from devgraph.config.settings import Settings
from devgraph.mcp import server as mcp_server
from devgraph.mcp import tools
from devgraph.mcp.tools import declared_node_labels, search_component

BUILTIN_PREDICATE = "(n:Service OR n:Module OR n:Class OR n:Function OR n:Endpoint)"
SCHEMA = """
    version: 1
    node_types:
      - label: File
        key: [path]
        metadata: [{name: path}]
        source: {provider: filesystem, kind: file}
"""


class StubEngine:
    def __init__(self):
        self.queries = []

    def run_cypher(self, query, params=None):
        self.queries.append((query, params or {}))
        return []


@dataclass
class Repo:
    path: Path


class StubRegistry:
    def __init__(self, repos):
        self.repos = repos

    def get(self, repo_id):
        return self.repos.get(repo_id)


def test_without_extra_labels_the_query_is_unchanged():
    engine = StubEngine()
    search_component(engine, "demo", "widget")
    assert BUILTIN_PREDICATE in engine.queries[0][0]


def test_extra_labels_join_the_label_predicate():
    engine = StubEngine()
    search_component(engine, "demo", "readme", extra_labels=("File", "Folder"))
    query = engine.queries[0][0]
    assert "(n:Service OR n:Module OR n:Class OR n:Function OR n:Endpoint OR n:File OR n:Folder)" in query


def test_labels_that_are_not_identifiers_are_never_interpolated():
    engine = StubEngine()
    search_component(engine, "demo", "x", extra_labels=("File", "Bad) DETACH DELETE n //"))
    query = engine.queries[0][0]
    assert "DETACH" not in query and "n:File" in query


def test_declared_labels_come_from_the_repo_schema(tmp_path):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(SCHEMA))
    registry = StubRegistry({"demo": Repo(tmp_path)})
    assert declared_node_labels(registry, "demo") == ("File",)


def test_unknown_repo_missing_or_invalid_schema_declare_nothing(tmp_path):
    assert declared_node_labels(StubRegistry({}), "demo") == ()
    assert declared_node_labels(StubRegistry({"demo": Repo(tmp_path)}), "demo") == ()
    (tmp_path / "devgraph.schema.yaml").write_text("version: 1\nnode_types: [oops\n")
    assert declared_node_labels(StubRegistry({"demo": Repo(tmp_path)}), "demo") == ()
    assert declared_node_labels(None, "demo") == ()


def test_the_mcp_tool_searches_declared_labels(tmp_path, monkeypatch):
    (tmp_path / "devgraph.schema.yaml").write_text(textwrap.dedent(SCHEMA))
    monkeypatch.setattr(mcp_server, "get_settings", lambda: Settings(registry_db_path=tmp_path / "r.sqlite3"))
    engine = StubEngine()
    server = mcp_server.build_server(engine, StubRegistry({"demo": Repo(tmp_path)}))
    asyncio.run(server.call_tool("search_component", {"repo_id": "demo", "query": "readme"}))
    assert any("n:File" in q for q, _ in engine.queries)
```

Note: check how existing tests invoke a tool through the server (`tests/mcp/test_server.py` or `test_tools_cycles.py`'s `test_calling_the_tool_through_the_server_returns_the_envelope`) and use the same call form if `server.call_tool` differs.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/mcp/test_tools_declared_labels.py -q`
Expected: ImportError for `declared_node_labels`.

- [ ] **Step 3: Implement**

In `devgraph/mcp/tools.py`:

1. Import `from devgraph.config.project_schema import LABEL_PATTERN, ProjectSchemaError, resolve_effective_schema`.
2. Add a module constant before `search_component`:

```python
# Labels search_component always covers. A repository's schema-declared
# labels are appended per call (see declared_node_labels).
_SEARCH_LABELS: tuple[str, ...] = ("Service", "Module", "Class", "Function", "Endpoint")
```

3. Add the parameter `extra_labels: tuple[str, ...] = ()` as the last parameter of `search_component`, document it in the docstring Args ("Schema-declared labels of this repository to search as well; anything that isn't a valid label identifier is ignored"), and replace the hardcoded `WHERE (n:Service OR n:Module OR n:Class OR n:Function OR n:Endpoint)` with:

```python
    labels = _SEARCH_LABELS + tuple(
        label for label in extra_labels if LABEL_PATTERN.fullmatch(label) and label not in _SEARCH_LABELS
    )
    label_predicate = " OR ".join(f"n:{label}" for label in labels)
```

and in the f-string `WHERE ({label_predicate})`. With no extras this renders exactly the old predicate.

4. Add after `search_component`:

```python
def declared_node_labels(registry: RepoRegistry | None, repo_id: str) -> tuple[str, ...]:
    """Node labels a repository's devgraph.schema.yaml declares; () if none.

    A missing or invalid schema declares nothing -- searching just the
    built-in labels is the safe fallback, not an error for the agent.
    """
    repo = registry.get(repo_id) if registry is not None else None
    if repo is None:
        return ()
    try:
        effective = resolve_effective_schema(repo.path)
    except ProjectSchemaError:
        return ()
    return tuple(node_type.label for node_type in effective.node_types)
```

In `devgraph/mcp/server.py`'s `search_component` wrapper, pass the labels:

```python
        return devgraph_tools.search_component(
            engine, repo_id, query, cross_repo, max_results, modified_within_commits,
            extra_labels=devgraph_tools.declared_node_labels(registry, repo_id),
        )
```

and append to its docstring: "Also searches node types the repository's devgraph.schema.yaml declares (for example File/Folder from the filesystem provider)."

- [ ] **Step 4: Docs**

README.md — rename the section heading "Project schema constraints" to "Project schema", and replace its first paragraph with:

"A repository may declare extra node types in an optional `devgraph.schema.yaml` at its root. Registration and `devgraph rescan` resolve that file and provision a uniqueness constraint for each declared node type, keyed on `repo_id` plus the declared key. Node types can be sourced from the repository's own files and folders with the filesystem provider:"

followed by the worktree YAML example from the spec in a ```yaml block, then:

"Every indexable file becomes a `File` node and every directory containing one a `Folder` node (`.` is the repository root), keyed by repo-relative path, with an `IS_CHILD_OF` edge to the parent folder. The watcher keeps them current; `search_component` finds them. A new or changed schema takes effect on the next `devgraph rescan`. Other user-declared node types are still constraint-only: nothing extracts them yet."

Keep the existing bullet list after it, and add one bullet: "An invalid file never removes filesystem nodes: indexing skips the provider and carries on with the built-in extractors."

PROJECT_STATUS.md — in the `devgraph/config/` bullet, replace "loader only, so nothing in indexing reads it yet, no custom provider is loaded or run" with "the loader feeds the filesystem provider (`devgraph/indexer/providers/filesystem.py`), which indexes declared File/Folder-style node types and parent edges; custom providers are still validated as data only and never run". Add a shipped bullet: "Filesystem provider (#1, worktree example) shipped: `devgraph.schema.yaml` node types can declare `source: {provider: filesystem, kind: file|folder}` and a `filesystem` relationship; `index_paths`/`remove_paths`/`full_scan` maintain those nodes (tagged `extractor = "filesystem"`, keyed by repo-relative path), `search_component` searches declared labels, and a repository without a schema file indexes exactly as before. Schema-hash-triggered rescans, general declared-key MERGE, the `devgraph config` CLI and the configurable tool plane remain open."

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/mcp -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add devgraph/mcp/tools.py devgraph/mcp/server.py tests/mcp/test_tools_declared_labels.py README.md PROJECT_STATUS.md
git commit -m "Search schema-declared node types and document the filesystem provider"
```

---

### Task 5: Full verification and PR

- [ ] **Step 1: Full suite** — `uv run pytest -q`; expected all pass (Neo4j up).

- [ ] **Step 2: Manual worktree check** — copy the spec's worktree YAML into a scratch copy of a real repository (or this worktree, with a throwaway registry via `DEVGRAPH_REGISTRY_DB_PATH=<scratch>/fs-registry.sqlite3`), run `uv run devgraph add <path>`, then confirm through Cypher (`uv run python -c` with `GraphEngine`) that File/Folder counts match `find` of non-ignored files/dirs and that an MCP `search_component` query for a file name returns a `File` hit. Remove the scratch schema file if it was written into a real worktree, `devgraph remove` the repo, delete the throwaway registry. Record outputs.

- [ ] **Step 3: Push and open the PR — only after the user confirms**

```bash
git push -u origin feat/filesystem-provider
gh pr create -R HaydenSchmidtDOC/DevGraph --base master --head <fork-owner>:feat/filesystem-provider \
  --title "Index filesystem-sourced node types (worktree example)" --body-file <scratch>/pr-body.md
```

Body: what changed (schema format, provider, dispatch hooks, engine methods, MCP search), backward compatibility evidence, known limits (rescan needed after schema edits; general declared-key MERGE, config CLI, tool plane, UI still open), validation, and `Part of #1.` No AI attribution.
