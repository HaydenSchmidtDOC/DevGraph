"""Applying a project schema: recorded state, pending pause, removed-type cleanup."""

import logging
import textwrap
import uuid

import pytest

from devgraph.config.project_schema import ABSENT_SCHEMA_HASH, schema_file_hash
from devgraph.graph.engine import GraphEngine
from devgraph.indexer import dispatch
from devgraph.indexer.dispatch import apply_project_schema, full_scan, index_paths, schema_pending
from devgraph.indexer.schema_constraints import constraint_drift, generated_objects
from tests.indexer.docs_live_helpers import assert_matches_fresh_apply

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

# Unique per run: these tests drop and re-create the labels' generated
# constraints, which are database-wide and shared with real repositories.
_TOKEN = uuid.uuid4().hex[:8]
FILE, FOLDER, ENTRY = (f"ZzFile{_TOKEN}", f"ZzFolder{_TOKEN}", f"ZzEntry{_TOKEN}")
GADGET = f"ZzGadget{_TOKEN}"
NOTE = f"ZzNote{_TOKEN}"
KADR, KRFC, KRUNBOOK = (f"ZzKeyAdr{_TOKEN}", f"ZzKeyRfc{_TOKEN}", f"ZzKeyRunbook{_TOKEN}")
_SHOWN = {FILE: "File", FOLDER: "Folder", ENTRY: "Entry"}


@pytest.fixture(scope="module", autouse=True)
def _drop_generated_constraints():
    yield
    cleanup = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        for label in (FILE, FOLDER, ENTRY, GADGET, NOTE, KADR, KRFC, KRUNBOOK):
            cleanup.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")
            cleanup.run_cypher(f"DROP INDEX {label.lower()}_repo_name IF EXISTS")
    except Exception:
        pass  # Neo4j unavailable: the tests were skipped
    finally:
        cleanup.close()


def _shown(key):
    """`Label:name` with the per-run label shown as the plain name the assertions use."""
    label, _, name = key.partition(":")
    return f"{_SHOWN.get(label, label)}:{name}"

WORKTREE = WORKTREE.replace("File", FILE).replace("Folder", FOLDER)
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
WIDGETS = WIDGETS.replace("ZzGadget", GADGET)
LINKS = LINKS.replace("ZzGadget", GADGET)


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
    return sorted(_shown(r["k"]) for r in rows)


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
    assert state["labels"] == [FILE, FOLDER, GADGET]
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
        f"CREATE (a:{GADGET} {{repo_id: $r, slug: 'a', name: 'a'}})-[:ZZ_LINKS]->(b:{GADGET} {{repo_id: $r, slug: 'b', name: 'b'}})",
        {"r": REPO},
    )
    write_schema(repo, worktree_with_gadgets(links=False))  # drop the relationship type only
    assert apply_project_schema(engine, REPO, repo)
    assert engine.run_cypher("MATCH ({repo_id: $r})-[x:ZZ_LINKS]->() RETURN count(x) AS n", {"r": REPO}) == [{"n": 0}]
    assert engine.run_cypher(f"MATCH (n:{GADGET} {{repo_id: $r}}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 2}]

    write_schema(repo, WORKTREE)  # now drop the node type too
    assert apply_project_schema(engine, REPO, repo)
    assert engine.run_cypher(f"MATCH (n:{GADGET} {{repo_id: $r}}) RETURN count(n) AS n", {"r": REPO}) == [{"n": 0}]
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
    engine.record_applied_schema(REPO, "sha256:old", [FILE, FOLDER, "Bad`) DETACH DELETE n //"], ["NOT VALID"])
    assert apply_project_schema(engine, REPO, repo)  # skips the invalid names, no Cypher error
    assert "File:pkg/mod.py" in fs_nodes(engine)


def test_switching_the_project_config_off_removes_user_nodes_on_rescan(engine, repo, tmp_path, monkeypatch):
    from devgraph.config import project_switch
    from devgraph.registry.store import RepoRegistry

    (repo / ".git").mkdir()
    registry = RepoRegistry(tmp_path / "switch.sqlite3")
    registry.add_repo(repo, repo_id=REPO)
    monkeypatch.setattr(project_switch, "_registry_db_path", lambda: tmp_path / "switch.sqlite3")

    write_schema(repo, WORKTREE)
    scan(engine, repo)
    assert "File:pkg/mod.py" in fs_nodes(engine)
    assert not schema_pending(engine, REPO, repo)

    registry.set_project_config_enabled(REPO, False)
    assert schema_pending(engine, REPO, repo)
    full_scan(engine, REPO, repo)
    assert fs_nodes(engine) == []
    assert engine.read_applied_schema(REPO)["hash"] == ABSENT_SCHEMA_HASH
    assert not schema_pending(engine, REPO, repo)

    registry.set_project_config_enabled(REPO, True)
    assert schema_pending(engine, REPO, repo)
    full_scan(engine, REPO, repo)
    assert "File:pkg/mod.py" in fs_nodes(engine)


NOTES = f"""
    version: 1
    node_types:
      - label: {NOTE}
        key: [path]
        metadata: [{{name: path}}, {{name: owner}}]
        source: {{provider: docs, paths: ["notes/*.md"]}}
    relationships:
      - type: ZZ_ABOUT
        provider: docs
        from: {NOTE}
        to: Module
        field: about
"""


def note_edges(engine):
    rows = engine.run_cypher("MATCH (a {repo_id: $r})-[:ZZ_ABOUT]->(b) RETURN a.name + '>' + b.name AS e", {"r": REPO})
    return sorted(r["e"] for r in rows)


@pytest.fixture
def noted(repo):
    (repo / "notes").mkdir()
    (repo / "notes" / "run.md").write_text("---\nowner: team-a\nabout: pkg/mod.py\n---\n# Run\n")
    write_schema(repo, NOTES)
    return repo


def test_apply_writes_docs_nodes_and_full_scan_their_edges(engine, noted):
    engine.upsert_repository(REPO, REPO, str(noted))
    assert apply_project_schema(engine, REPO, noted)
    rows = engine.run_cypher(f"MATCH (n:{NOTE} {{repo_id: $r}}) RETURN n.owner AS o", {"r": REPO})
    assert rows == [{"o": "team-a"}]
    assert note_edges(engine) == []  # the Module does not exist yet

    scan(engine, noted)
    assert note_edges(engine) == ["notes/run.md>pkg/mod.py"]

    assert apply_project_schema(engine, REPO, noted)
    assert note_edges(engine) == []  # every docs edge goes; full_scan's last step rebuilds them


def test_a_failed_apply_writes_no_docs_edges(engine, noted, monkeypatch):
    from devgraph.indexer import dispatch

    scan(engine, noted)
    engine.run_cypher("MATCH ({repo_id: $r})-[x:ZZ_ABOUT]->() DELETE x", {"r": REPO})

    def refuse(*args, **kwargs):
        raise RuntimeError("provisioning refused")

    monkeypatch.setattr(dispatch, "provision_repository_schema", refuse)
    full_scan(engine, REPO, noted)
    assert not schema_pending(engine, REPO, noted)  # still the applied schema: only apply's result gates it
    assert note_edges(engine) == []


# --- docs key switch (front-matter keys, K7) ----------------------------------


def keyed_schema(adr_key, rfc_key="rfc_id", adr_only=False):
    adr = f"""
    version: 1
    node_types:
      - label: {KADR}
        key: [{adr_key}]
        metadata: [{{name: path}}, {{name: adr_id}}]
        source: {{provider: docs, paths: ["decisions/*.md"], fields: {{adr_id: id}}}}
"""
    return adr if adr_only else adr + f"""      - label: {KRFC}
        key: [{rfc_key}]
        metadata: [{{name: path}}, {{name: rfc_id}}]
        source: {{provider: docs, paths: ["rfcs/*.md"], fields: {{rfc_id: id}}}}
      - label: {KRUNBOOK}
        key: [path]
        metadata: [{{name: path}}]
        source: {{provider: docs, paths: ["runbooks/*.md"]}}
    relationships:
      - {{type: ZZ_KEY_RUNBOOK_ADR, provider: docs, from: {KRUNBOOK}, to: {KADR}, field: adr}}
"""


@pytest.fixture
def keyed_labels(engine):
    """Drops the key tests' generated constraints before and after each test:
    every test here starts with no constraint on its labels."""
    def drop():
        for label in (KADR, KRFC, KRUNBOOK):
            engine.run_cypher(f"DROP CONSTRAINT {label.lower()}_repo_key IF EXISTS")

    drop()
    yield
    drop()


@pytest.fixture
def other_repo(engine, tmp_path, keyed_labels):
    """Repository B: scanned with only the Adr type, keyed on `adr_key`, holding one Adr, id ADR-1."""
    repo_id = f"_smoketest_schema_apply_b_{_TOKEN}"
    root = tmp_path / "other"

    def make(adr_key):
        page(root, "decisions/z.md", "id: ADR-1")
        write_schema(root, keyed_schema(adr_key, adr_only=True))
        engine.upsert_repository(repo_id, repo_id, str(root))
        full_scan(engine, repo_id, root)
        return repo_id

    engine.delete_repository(repo_id)
    yield make
    engine.delete_repository(repo_id)


def page(root, rel, front):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}\n---\n# Page\n")
    return path


def entries(engine, label):
    rows = engine.run_cypher(f"MATCH (n:{label} {{repo_id: $r}}) RETURN n.name AS n, n.path AS p", {"r": REPO})
    return {r["n"]: r["p"] for r in rows}


def constraint_key(engine, label):
    obj = generated_objects(engine).get(f"{label.lower()}_repo_key")
    return obj.properties[1:] if obj else None


def runbook_links(engine):
    rows = engine.run_cypher(
        f"MATCH (a:{KRUNBOOK} {{repo_id: $r}})-[:ZZ_KEY_RUNBOOK_ADR]->(b) RETURN b.name AS b", {"r": REPO}
    )
    return sorted(r["b"] for r in rows)


@pytest.fixture
def shared_id(repo):
    """Two Adr files sharing id ADR-1, and a runbook naming the entry both ways."""
    page(repo, "decisions/a.md", "id: ADR-1")
    page(repo, "decisions/b.md", "id: ADR-1")
    page(repo, "rfcs/r.md", "id: RFC-1")
    page(repo, "runbooks/deploy.md", "adr: [ADR-1, decisions/b.md]")
    return repo


def test_switching_a_docs_key_renames_the_entries_both_ways(engine, shared_id, keyed_labels):
    write_schema(shared_id, keyed_schema("path"))
    scan(engine, shared_id)
    assert entries(engine, KADR) == {"decisions/a.md": "decisions/a.md", "decisions/b.md": "decisions/b.md"}
    assert runbook_links(engine) == ["decisions/b.md"]

    write_schema(shared_id, keyed_schema("adr_id"))
    scan(engine, shared_id)
    assert entries(engine, KADR) == {"ADR-1": "decisions/a.md"}  # the owner's
    assert constraint_key(engine, KADR) == ("adr_id",)
    assert runbook_links(engine) == ["ADR-1"]
    assert_matches_fresh_apply(engine, REPO, shared_id)

    write_schema(shared_id, keyed_schema("path"))
    scan(engine, shared_id)
    assert entries(engine, KADR) == {"decisions/a.md": "decisions/a.md", "decisions/b.md": "decisions/b.md"}
    assert constraint_key(engine, KADR) == ("path",)
    assert runbook_links(engine) == ["decisions/b.md"]
    assert_matches_fresh_apply(engine, REPO, shared_id)


def test_a_key_another_repo_still_records_leaves_the_label_unwritten(engine, shared_id, other_repo, caplog):
    b_id = other_repo("adr_id")
    write_schema(shared_id, keyed_schema("adr_id"))
    scan(engine, shared_id)
    assert entries(engine, KADR) == {"ADR-1": "decisions/a.md"}

    # Both Adr and Rfc switch to [path]; only Adr's constraint is held by repo B.
    write_schema(shared_id, keyed_schema("path", rfc_key="path"))
    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
        assert apply_project_schema(engine, REPO, shared_id)

    assert entries(engine, KADR) == {}  # the duplicate ids break B's (repo_id, adr_id) constraint
    assert constraint_key(engine, KADR) == ("adr_id",)
    assert entries(engine, KRFC) == {"rfcs/r.md": "rfcs/r.md"}  # its own transaction
    assert constraint_key(engine, KRFC) == ("path",)
    assert entries(engine, KRUNBOOK) == {"runbooks/deploy.md": "runbooks/deploy.md"}
    assert (
        f"{REPO}: {KADR} entries were not written: {b_id} declares {KADR} keyed differently, so its constraint "
        f"keeps the old key and these entries break it; align the key or rename one label"
    ) in caplog.text
    assert [d for d in constraint_drift(engine) if d["label"] == KADR] == [{
        "repo_id": REPO, "label": KADR, "status": "conflict", "key": ("path",),
        "constraint_key": ("adr_id",), "declared_by": [b_id],
    }]
    assert_matches_fresh_apply(engine, REPO, shared_id)


def test_a_first_apply_under_another_repos_path_constraint_writes_unique_ids(
    engine, repo, other_repo, monkeypatch, caplog
):
    b_id = other_repo("path")
    assert constraint_key(engine, KADR) == ("path",)
    page(repo, "decisions/a.md", "id: ADR-1")
    page(repo, "decisions/b.md", "id: ADR-2")
    page(repo, "runbooks/deploy.md", "adr: ADR-2")
    write_schema(repo, keyed_schema("adr_id"))
    deferred = []
    real = dispatch._deferred_docs_labels
    monkeypatch.setattr(dispatch, "_deferred_docs_labels", lambda *a: deferred.append(real(*a)) or deferred[-1])

    with caplog.at_level(logging.WARNING):
        scan(engine, repo)

    assert KADR in deferred[0]
    assert entries(engine, KADR) == {"ADR-1": "decisions/a.md", "ADR-2": "decisions/b.md"}
    assert runbook_links(engine) == ["ADR-2"]
    assert constraint_key(engine, KADR) == ("path",)  # B still declares it
    assert (
        f"not replacing the generated constraint/index of {KADR} keyed on (adr_id): other repositories "
        f"disagree ({b_id} declares {KADR} keyed on (path)); align the key or rename one label"
    ) in caplog.text
    assert "entries were not written" not in caplog.text  # unique paths never break B's key
    assert [d for d in constraint_drift(engine) if d["label"] == KADR] == [{
        "repo_id": REPO, "label": KADR, "status": "conflict", "key": ("adr_id",),
        "constraint_key": ("path",), "declared_by": [b_id],
    }]
    assert_matches_fresh_apply(engine, REPO, repo)


def test_a_first_apply_under_another_repos_id_constraint_warns_its_entries_were_not_written(
    engine, shared_id, other_repo, caplog
):
    b_id = other_repo("adr_id")
    write_schema(shared_id, keyed_schema("path"))
    with caplog.at_level(logging.WARNING, logger="devgraph.indexer.dispatch"):
        scan(engine, shared_id)

    assert entries(engine, KADR) == {}
    assert entries(engine, KRUNBOOK) == {"runbooks/deploy.md": "runbooks/deploy.md"}
    assert (
        f"{REPO}: {KADR} entries were not written: {b_id} declares {KADR} keyed differently, so its constraint "
        f"keeps the old key and these entries break it; align the key or rename one label"
    ) in caplog.text
    assert [d["status"] for d in constraint_drift(engine) if d["label"] == KADR] == ["conflict"]
    assert_matches_fresh_apply(engine, REPO, shared_id)
