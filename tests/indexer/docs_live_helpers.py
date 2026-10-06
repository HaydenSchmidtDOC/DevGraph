"""Shared checks for the live docs-provider tests (not a test module itself)."""

from devgraph.graph.schema import RELATIONSHIP_TYPES
from devgraph.indexer.dispatch import full_scan


def docs_graph(engine, repo_id):
    """The docs part of one repository's graph: every docs node's (label, name, path)
    and every docs edge, with the target's file for file-scoped targets."""
    nodes = engine.run_cypher(
        "MATCH (n {repo_id: $r}) WHERE n.extractor = 'docs' "
        "RETURN labels(n)[0] AS label, n.name AS name, n.path AS path",
        {"r": repo_id},
    )
    edges = engine.run_cypher(
        "MATCH (a {repo_id: $r})-[x]->(b {repo_id: $r}) "
        "WHERE a.extractor = 'docs' AND NOT type(x) IN $builtin "
        "RETURN labels(a)[0] AS a, a.name AS an, type(x) AS t, labels(b)[0] AS b, b.name AS bn, "
        "coalesce(b.file, '') AS bf",
        {"r": repo_id, "builtin": list(RELATIONSHIP_TYPES)},
    )
    return (
        sorted((n["label"], n["name"], n["path"]) for n in nodes),
        sorted((e["a"], e["an"], e["t"], e["b"], e["bn"], e["bf"]) for e in edges),
    )


def assert_matches_fresh_apply(engine, repo_id, repo_root):
    """The docs graph a sequence of events left equals a fresh full apply of the same files.

    The fresh apply runs under a second repo_id over the same root, which is
    deleted again afterwards.
    """
    after_events = docs_graph(engine, repo_id)
    fresh = f"{repo_id}_fresh"
    engine.delete_repository(fresh)
    try:
        engine.upsert_repository(fresh, fresh, str(repo_root))
        full_scan(engine, fresh, repo_root)
        assert after_events == docs_graph(engine, fresh)
    finally:
        engine.delete_repository(fresh)
