"""A UTF-8 byte-order mark (Visual Studio, PowerShell 5's `-Encoding UTF8`)
hides neither a docs note nor Markdown front matter (audit 10 item 8a)."""

import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.docs.extractor import DocsExtractor
from devgraph.indexer.providers.docs import read_front_matter

BOM = "﻿"
NOTE = "---\ntype: design_decision\nid: use-neo4j\nlinks: [a.py]\n---\n# Use Neo4j\nBecause.\n"


def test_a_note_after_a_bom_is_extracted():
    result = DocsExtractor("r").extract_from_source(BOM + NOTE, "docs/adr.md")
    assert [(d.label, d.name, d.properties["title"]) for d in result.docs] == [
        ("DesignDecision", "use-neo4j", "Use Neo4j")
    ]
    assert ("Module", "a.py", "DOCUMENTED_BY") in {
        (r.source_label, r.source_name, r.relationship_type) for r in result.relationships
    }


def test_front_matter_after_a_bom_is_read(tmp_path):
    path = tmp_path / "runbook.md"
    path.write_text(BOM + "---\ntype: runbook\nowner: ops\n---\n# Runbook\n", encoding="utf-8")
    assert read_front_matter(path) == ({"type": "runbook", "owner": "ops"}, None)


def test_a_bom_note_is_indexed_like_one_without(tmp_path):
    engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        engine.verify_connectivity()
    except Exception as e:
        engine.close()
        pytest.skip(f"Neo4j not available: {e}")
    repo_ids = []
    try:
        graphs = []
        for tag, prefix in (("plain", ""), ("bom", BOM)):
            root = tmp_path / tag
            (root / "docs").mkdir(parents=True)
            (root / "a.py").write_text("def alpha():\n    return 1\n")
            (root / "docs" / "adr.md").write_text(prefix + NOTE, encoding="utf-8")
            repo_id = f"zz-bom-{tag}-{uuid.uuid4().hex[:8]}"
            repo_ids.append(repo_id)
            provision_repository_schema(engine, root)
            engine.upsert_repository(repo_id, repo_id, str(root))
            full_scan(engine, repo_id, root, docs_path="docs")
            graphs.append(engine.run_cypher(
                "MATCH (m:Module {repo_id: $r})-[x:DOCUMENTED_BY]->(d:DesignDecision) "
                "RETURN m.name AS m, d.name AS d, d.title AS t", {"r": repo_id},
            ))
        assert graphs[0] == graphs[1] == [{"m": "a.py", "d": "use-neo4j", "t": "Use Neo4j"}]
    finally:
        for repo_id in repo_ids:
            engine.delete_repository(repo_id)
        engine.close()
