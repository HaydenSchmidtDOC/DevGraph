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

# Shared by delete_nodes_by_source_file and _replace_file_nodes_tx. See
# delete_nodes_by_source_file's docstring for why three property keys.
_DELETE_BY_SOURCE_FILE_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE n.source_file = $file_name OR n.file = $file_name OR n.source = $file_name "
    "DETACH DELETE n"
)


def _group_nodes_by_label(nodes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        groups.setdefault(node["label"], []).append(
            {
                "repo_id": node["repo_id"],
                "name": node["name"],
                "properties": node.get("properties") or {},
            }
        )
    return groups


def _group_rels_by_triple(
    rels: list[dict[str, Any]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for rel in rels:
        key = (rel["from_label"], rel["rel_type"], rel["to_label"])
        groups.setdefault(key, []).append(
            {
                "repo_id": rel["repo_id"],
                "from_name": rel["from_name"],
                "to_name": rel["to_name"],
                "properties": rel.get("properties") or {},
            }
        )
    return groups


def _upsert_nodes_tx(tx, nodes: list[dict[str, Any]]) -> None:
    for label, rows in _group_nodes_by_label(nodes).items():
        tx.run(
            f"UNWIND $rows AS row "
            f"MERGE (n:{label} {{repo_id: row.repo_id, name: row.name}}) "
            "SET n += row.properties",
            rows=rows,
        )


def _upsert_relationships_tx(tx, rels: list[dict[str, Any]]) -> None:
    for (from_label, rel_type, to_label), rows in _group_rels_by_triple(rels).items():
        tx.run(
            f"UNWIND $rows AS row "
            f"MATCH (a:{from_label} {{repo_id: row.repo_id, name: row.from_name}}) "
            f"MATCH (b:{to_label} {{repo_id: row.repo_id, name: row.to_name}}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += row.properties",
            rows=rows,
        )


def _replace_file_nodes_tx(
    tx, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
) -> None:
    tx.run(_DELETE_BY_SOURCE_FILE_CYPHER, repo_id=repo_id, file_name=file_name)
    _upsert_nodes_tx(tx, nodes)
    _upsert_relationships_tx(tx, rels)


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

    def upsert_nodes(self, nodes: list[dict[str, Any]]) -> None:
        """Batched idempotent MERGE for many nodes in one transaction.

        Each dict needs `label`/`repo_id`/`name`/`properties` (`properties`
        optional, defaults to `{}`). Cypher can't parameterize a label, so
        nodes are grouped by `label` and one `UNWIND` MERGE runs per group —
        this is what turns "one round-trip per node" into "one round-trip
        per distinct label in the batch".
        """
        if not nodes:
            return
        with self._driver.session() as session:
            session.execute_write(_upsert_nodes_tx, nodes)

    def upsert_relationship(
        self,
        from_label: str,
        from_name: str,
        rel_type: str,
        to_label: str,
        to_name: str,
        repo_id: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """MERGE an edge into existence; only materializes when both endpoints
        already exist as real nodes (MATCH-MATCH, not MERGE-MERGE).

        `properties`, when given, is SET onto the relationship after the
        MERGE (e.g. CALLS edges carry an optional `caller_class` property —
        see indexer/python/extractor.py). Omitted (None) by every other
        call site; behavior is unchanged for them.
        """
        with self._driver.session() as session:
            session.run(
                f"MATCH (a:{from_label} {{repo_id: $repo_id, name: $from_name}}) "
                f"MATCH (b:{to_label} {{repo_id: $repo_id, name: $to_name}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                "SET r += $properties",
                repo_id=repo_id,
                from_name=from_name,
                to_name=to_name,
                properties=properties or {},
            )

    def upsert_relationships(self, rels: list[dict[str, Any]]) -> None:
        """Batched MATCH-MATCH-MERGE for many relationships in one transaction.

        Each dict needs `from_label`/`from_name`/`rel_type`/`to_label`/
        `to_name`/`repo_id` (`properties` optional). Grouped by
        `(from_label, rel_type, to_label)` — same reasoning as `upsert_nodes`,
        since label/rel-type can't be parameterized. An edge whose endpoint
        doesn't exist yet is silently skipped, same as `upsert_relationship`.
        """
        if not rels:
            return
        with self._driver.session() as session:
            session.execute_write(_upsert_relationships_tx, rels)

    def replace_file_nodes(
        self, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
    ) -> None:
        """Atomically replace one file's provenance-tagged nodes: delete the
        old ones and upsert the new nodes/rels in a single transaction.

        Unlike calling `delete_nodes_by_source_file` followed by
        `upsert_nodes`/`upsert_relationships` separately, a reader can never
        observe the file's nodes as gone-but-not-yet-rebuilt — under
        read-committed isolation it sees either the pre-reindex state or the
        fully-rebuilt state, never in between.
        """
        with self._driver.session() as session:
            session.execute_write(_replace_file_nodes_tx, repo_id, file_name, nodes, rels)

    def find_importing_modules(self, repo_id: str, module_name: str) -> list[str]:
        """Return the repo-relative paths of every Module with an IMPORTS edge
        into `module_name` (direct importers only, one level).

        Used to widen an incremental reindex to a changed file's dependents:
        a CALLS/IMPORTS edge in an importer's own extracted source is only
        re-evaluated when that importer's file is itself reindexed, so a
        rename/removal in the imported file otherwise leaves the importer's
        edges stale until it happens to be edited again or a full rescan
        runs. See dispatch.py's index_paths.
        """
        with self._driver.session() as session:
            result = session.run(
                "MATCH (m:Module {repo_id: $repo_id})-[:IMPORTS]->"
                "(target:Module {repo_id: $repo_id, name: $module_name}) "
                "RETURN m.name as name",
                repo_id=repo_id,
                module_name=module_name,
            )
            return [record["name"] for record in result]

    def delete_nodes_by_source_file(self, repo_id: str, file_name: str) -> None:
        """Remove every node whose provenance property names this file, scoped to repo_id.

        Extractors record provenance under one of three property keys
        depending on which one wrote the node (source_file/file/source —
        an inconsistency inherited from how each extractor was built
        independently; matching all three here rather than picking one is
        the honest fix until they're unified onto a single property name).
        """
        with self._driver.session() as session:
            session.run(_DELETE_BY_SOURCE_FILE_CYPHER, repo_id=repo_id, file_name=file_name)

    def stage_recency(
        self,
        label: str,
        repo_id: str,
        name: str,
        created_at: str | None = None,
        last_modified_at: str | None = None,
        last_modified_by: str | None = None,
    ) -> None:
        """Ratchet-merge git-derived recency onto a node: `created_at` only
        moves earlier and `last_modified_at` only moves later, so this is
        safe to call repeatedly and out of chronological order (e.g. across
        incremental batches). See `set_recency` for the plain-overwrite
        variant used during reconciliation.

        The `last_modified_by` CASE deliberately mirrors the
        `last_modified_at` comparison rather than having its own condition —
        that's what stops an out-of-order call from clobbering a newer
        commit's author with an older one's.
        """
        with self._driver.session() as session:
            session.run(
                f"MERGE (n:{label} {{repo_id: $repo_id, name: $name}}) "
                "SET n.created_at = CASE WHEN $created_at IS NULL THEN n.created_at "
                "WHEN n.created_at IS NULL OR $created_at < n.created_at THEN $created_at "
                "ELSE n.created_at END, "
                "n.last_modified_at = CASE WHEN $last_modified_at IS NULL THEN n.last_modified_at "
                "WHEN n.last_modified_at IS NULL OR $last_modified_at > n.last_modified_at THEN $last_modified_at "
                "ELSE n.last_modified_at END, "
                "n.last_modified_by = CASE "
                "WHEN $last_modified_by IS NULL THEN n.last_modified_by "
                "WHEN n.last_modified_at IS NULL OR $last_modified_at > n.last_modified_at THEN $last_modified_by "
                "ELSE n.last_modified_by END",
                repo_id=repo_id,
                name=name,
                created_at=created_at,
                last_modified_at=last_modified_at,
                last_modified_by=last_modified_by,
            )

    def set_recency(
        self,
        label: str,
        repo_id: str,
        name: str,
        created_at: str | None = None,
        last_modified_at: str | None = None,
        last_modified_by: str | None = None,
    ) -> None:
        """Overwrite recency from scratch — used only by the reconcile path,
        where the caller has just recomputed the authoritative value from a
        fresh full walk and must be able to move a value backward, not just
        forward (`stage_recency`'s ratchet deliberately can't).

        `last_modified_by` is only included in the SET when not None, so a
        caller running with `git_recency_track_author` off (always passing
        `last_modified_by=None`) never nulls out a previously-tracked author.
        """
        set_clauses = ["n.created_at = $created_at", "n.last_modified_at = $last_modified_at"]
        if last_modified_by is not None:
            set_clauses.append("n.last_modified_by = $last_modified_by")
        with self._driver.session() as session:
            session.run(
                f"MERGE (n:{label} {{repo_id: $repo_id, name: $name}}) "
                f"SET {', '.join(set_clauses)}",
                repo_id=repo_id,
                name=name,
                created_at=created_at,
                last_modified_at=last_modified_at,
                last_modified_by=last_modified_by,
            )

    def delete_commits(self, repo_id: str, shas: list[str]) -> None:
        """Delete Commit nodes (and their relationships), scoped to repo_id.

        Used by the reconcile path to drop Commit nodes that are no longer
        reachable from HEAD after a rebase/reset/abandoned-branch switch.

        Matches on `c.name`, not `c.sha`: Commit nodes are MERGE-keyed on
        `(repo_id, name)` like every other repo-scoped label (see
        `constraint_statements`), and `extract_new_commits` upserts each
        commit's SHA into that `name` property (`upsert_node("Commit",
        repo_id, commit_node.sha, ...)`) rather than a separate `sha`
        property — there is no `sha` property on a Commit node to match on.
        """
        if not shas:
            return
        with self._driver.session() as session:
            session.run(
                "MATCH (c:Commit {repo_id: $repo_id}) WHERE c.name IN $shas DETACH DELETE c",
                repo_id=repo_id,
                shas=shas,
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
