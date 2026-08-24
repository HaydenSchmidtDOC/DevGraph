"""Neo4j graph engine: connection lifecycle, schema init, idempotent upserts.

All writes are `MERGE`-based keyed on `(repo_id, name)` (or `(repo_id, path)`
for file-provenance nodes) so incremental reindexing updates existing nodes
in place instead of duplicating them. `repo_id` is a hard filter on every
read, never an optional convenience — see Design Brief Principle 3.
"""

from __future__ import annotations

from typing import Any

from neo4j import Driver, GraphDatabase

from devgraph.graph.schema import constraint_statements


class GraphEngine:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    def verify_connectivity(self) -> None:
        self._driver.verify_connectivity()

    def init_schema(self) -> None:
        with self._driver.session() as session:
            for stmt in constraint_statements():
                session.run(stmt)

    def upsert_repository(self, repo_id: str, name: str, path: str) -> None:
        with self._driver.session() as session:
            session.run(
                "MERGE (r:Repository {repo_id: $repo_id}) "
                "SET r.name = $name, r.path = $path",
                repo_id=repo_id,
                name=name,
                path=path,
            )

    def upsert_node(
        self, label: str, repo_id: str, name: str, properties: dict[str, Any] | None = None
    ) -> None:
        """Idempotent MERGE on (repo_id, name) for a repo-scoped node label."""
        props = properties or {}
        with self._driver.session() as session:
            session.run(
                f"MERGE (n:{label} {{repo_id: $repo_id, name: $name}}) "
                "SET n += $properties",
                repo_id=repo_id,
                name=name,
                properties=props,
            )

    def upsert_relationship(
        self,
        from_label: str,
        from_name: str,
        rel_type: str,
        to_label: str,
        to_name: str,
        repo_id: str,
    ) -> None:
        with self._driver.session() as session:
            session.run(
                f"MATCH (a:{from_label} {{repo_id: $repo_id, name: $from_name}}) "
                f"MATCH (b:{to_label} {{repo_id: $repo_id, name: $to_name}}) "
                f"MERGE (a)-[:{rel_type}]->(b)",
                repo_id=repo_id,
                from_name=from_name,
                to_name=to_name,
            )

    def delete_repository(self, repo_id: str) -> None:
        """Remove every node (and its relationships) scoped to this repo_id."""
        with self._driver.session() as session:
            session.run(
                "MATCH (n {repo_id: $repo_id}) DETACH DELETE n", repo_id=repo_id
            )

    def run_cypher(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict]:
        """Advanced escape hatch. Callers must gate this behind explicit config
        (see `Settings.enable_run_cypher`) — never wire it up as the default path.
        """
        with self._driver.session() as session:
            result = session.run(query, parameters or {})
            return [record.data() for record in result]
