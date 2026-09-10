"""Neo4j graph engine: connection lifecycle, schema init, idempotent upserts.

All writes are `MERGE`-based keyed on `(repo_id, name)` (or `(repo_id, path)`
for file-provenance nodes) so incremental reindexing updates existing nodes
in place instead of duplicating them. `repo_id` is a hard filter on every
read, never an optional convenience — see Design Brief Principle 3.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
from neo4j.graph import Node, Relationship

from devgraph.graph.schema import constraint_statements

logger = logging.getLogger(__name__)

# Shared by delete_nodes_by_source_file and _replace_file_nodes_tx.
#
# `source_file` (Module) and `file` (Class/Function/...) both name exactly
# one file per node by construction, so a direct delete is correct there.
# `source` (Container/Service/Network/Volume/API/Database-ish nodes) is
# different: several files can legitimately co-produce the same named node
# (e.g. a Service defined in both docker-compose.yml and an override file),
# so those nodes are never file-scoped and a blind delete on any one
# producing file would destroy a node the other file still claims. For that
# population we track every claiming file in `sources` and only delete once
# the last one is unclaimed — see _UNCLAIM_SOURCE_CYPHER.
_DELETE_BY_SOURCE_FILE_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE n.source_file = $file_name OR n.file = $file_name "
    "DETACH DELETE n"
)

_UNCLAIM_SOURCE_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE n.file IS NULL AND n.source_file IS NULL "
    "  AND (n.source = $file_name OR $file_name IN coalesce(n.sources, [])) "
    "SET n.sources = [s IN coalesce(n.sources, [n.source]) WHERE s <> $file_name] "
    "WITH n WHERE size(n.sources) = 0 "
    "DETACH DELETE n"
)

# Transient Neo4j failures worth retrying: a connection blip, an expired
# session, or a server-side transient error (e.g. a lock timeout). Permanent
# errors (syntax, constraint violations, unknown labels) are NOT retried.
_RETRYABLE_EXCEPTIONS = (ServiceUnavailable, SessionExpired, TransientError)
_MAX_RETRIES = 3
_BASE_DELAY_S = 0.5


def _retry_transient(fn, *args, **kwargs):
    """Run `fn` with bounded exponential-backoff retry on transient Neo4j errors.

    The neo4j driver's `session.execute_write`/`execute_read` already retry
    transient errors internally, but the autocommit `session.run` paths and
    `verify_connectivity` do not — so a Neo4j blip mid-scan would otherwise
    fail the whole operation. Every write here is an idempotent MERGE, so
    re-running after a transient failure is always safe.
    """
    delay = _BASE_DELAY_S
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except _RETRYABLE_EXCEPTIONS:
            if attempt >= _MAX_RETRIES:
                raise
            logger.warning(
                "transient Neo4j error (attempt %d/%d); retrying in %.1fs",
                attempt + 1,
                _MAX_RETRIES,
                delay,
                exc_info=True,
            )
            time.sleep(delay)
            delay *= 2


def _is_file_scoped(file: str | None) -> bool:
    """The single predicate for "does this node's MERGE key include `file`".

    Shared by `_group_nodes_by_label` (which drives the Cypher MERGE key)
    and `identity_key` (which the dashboard uses as its layout-cache key),
    so the two can never drift apart into disagreeing about which nodes are
    file-scoped.
    """
    return file is not None


def _group_nodes_by_label(
    nodes: list[dict[str, Any]],
) -> dict[tuple[str, bool], list[dict[str, Any]]]:
    """Group by (label, is_file_scoped).

    Every language extractor's Class/Function nodes carry a `file` property
    (the repo-relative path they're defined in); nothing else does — Module
    uses `source_file` and already has a unique name (the file path itself),
    Service/Endpoint/Database-ish nodes use `source` and are effectively
    singleton per repo. `file`'s presence is therefore exactly the signal
    for "this label can collide by bare name across files" (two files each
    defining a function called `main`, previously MERGEd into one shared
    node — see Design Brief follow-up on cross-file symbol collisions).
    Folding `file` into the MERGE key for just that population fixes the
    collision without touching the many labels that don't need it.
    """
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for node in nodes:
        properties = node.get("properties") or {}
        file = properties.get("file")
        groups.setdefault((node["label"], _is_file_scoped(file)), []).append(
            {
                "repo_id": node["repo_id"],
                "name": node["name"],
                "file": file,
                "properties": properties,
            }
        )
    return groups


# Unit separator (0x1F): a control character that cannot occur in a label,
# repo_id, name, or filesystem path, so it can never be produced by any
# combination of those fields and then misparsed back apart. Chosen over a
# printable delimiter like "|" or ":" because both appear legitimately in
# Windows paths (drive letters) and could appear in a symbol name.
_IDENTITY_KEY_SEP = "\x1f"


def identity_key(label: str, repo_id: str, name: str, file: str | None) -> str:
    """Stable, server-derived cache key for a node — independent of Neo4j's
    internal elementId(), which is a storage-slot pointer that shifts on any
    full re-index, restore, or delete/recreate and would silently invalidate
    a client-side position cache keyed on it.

    Mirrors the MERGE key `_upsert_nodes_tx` actually writes with: file is
    folded in only when `_is_file_scoped` says this node's MERGE key includes
    it, so the dashboard never has to reimplement that conditional itself
    (and risk it drifting from the Cypher above).
    """
    parts = [label, repo_id, name]
    if _is_file_scoped(file):
        parts.append(file)
    return _IDENTITY_KEY_SEP.join(parts)


def _group_rels_by_triple(
    rels: list[dict[str, Any]],
) -> dict[tuple[str, str, str, bool, bool], list[dict[str, Any]]]:
    """Group by (from_label, rel_type, to_label, has_from_file, has_to_file).

    from_file/to_file (see GraphRelationship) are only ever set by a caller
    that knows an endpoint's exact file at extraction time (currently: only
    CONTAINS, whose two ends are always the file just parsed). Grouping on
    their presence, not just the label triple, means every other
    relationship type keeps matching by bare name exactly as before —
    genuinely ambiguous by nature (a CALLS target could live anywhere in the
    repo) rather than a bug to paper over.
    """
    groups: dict[tuple[str, str, str, bool, bool], list[dict[str, Any]]] = {}
    for rel in rels:
        from_file = rel.get("from_file")
        to_file = rel.get("to_file")
        key = (rel["from_label"], rel["rel_type"], rel["to_label"], from_file is not None, to_file is not None)
        groups.setdefault(key, []).append(
            {
                "repo_id": rel["repo_id"],
                "from_name": rel["from_name"],
                "to_name": rel["to_name"],
                "from_file": from_file,
                "to_file": to_file,
                "properties": rel.get("properties") or {},
            }
        )
    return groups


def _upsert_nodes_tx(tx, nodes: list[dict[str, Any]]) -> None:
    for (label, file_scoped), rows in _group_nodes_by_label(nodes).items():
        merge_key = (
            "{repo_id: row.repo_id, name: row.name, file: row.file}"
            if file_scoped
            else "{repo_id: row.repo_id, name: row.name}"
        )
        # Non-file-scoped nodes accumulate every claiming `source` into
        # `sources` so delete_nodes_by_source_file can unclaim one producer
        # without destroying a node another file still produces. A no-op
        # when row.properties has no `source` (e.g. Module's `source_file`
        # already uniquely identifies its one node).
        sources_clause = (
            ""
            if file_scoped
            else " SET n.sources = CASE WHEN row.properties.source IS NULL THEN n.sources "
            "WHEN row.properties.source IN coalesce(n.sources, []) THEN n.sources "
            "ELSE coalesce(n.sources, []) + row.properties.source END"
        )
        tx.run(
            f"UNWIND $rows AS row "
            f"MERGE (n:{label} {merge_key}) "
            "SET n += row.properties" + sources_clause,
            rows=rows,
        )


def _upsert_relationships_tx(tx, rels: list[dict[str, Any]]) -> None:
    for (from_label, rel_type, to_label, has_from_file, has_to_file), rows in _group_rels_by_triple(rels).items():
        from_match = (
            "{repo_id: row.repo_id, name: row.from_name, file: row.from_file}"
            if has_from_file
            else "{repo_id: row.repo_id, name: row.from_name}"
        )
        to_match = (
            "{repo_id: row.repo_id, name: row.to_name, file: row.to_file}"
            if has_to_file
            else "{repo_id: row.repo_id, name: row.to_name}"
        )
        tx.run(
            f"UNWIND $rows AS row "
            f"MATCH (a:{from_label} {from_match}) "
            f"MATCH (b:{to_label} {to_match}) "
            f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += row.properties",
            rows=rows,
        )


def _replace_file_nodes_tx(
    tx, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
) -> None:
    tx.run(_DELETE_BY_SOURCE_FILE_CYPHER, repo_id=repo_id, file_name=file_name)
    tx.run(_UNCLAIM_SOURCE_CYPHER, repo_id=repo_id, file_name=file_name)
    _upsert_nodes_tx(tx, nodes)
    _upsert_relationships_tx(tx, rels)


class GraphEngine:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    def verify_connectivity(self) -> None:
        _retry_transient(self._driver.verify_connectivity)

    def init_schema(self) -> None:
        with self._driver.session() as session:
            for stmt in constraint_statements():
                _retry_transient(session.run, stmt)

    def upsert_repository(self, repo_id: str, name: str, path: str) -> None:
        with self._driver.session() as session:
            _retry_transient(
                session.run,
                "MERGE (r:Repository {repo_id: $repo_id}) "
                "SET r.name = $name, r.path = $path",
                repo_id=repo_id,
                name=name,
                path=path,
            )

    def upsert_node(
        self, label: str, repo_id: str, name: str, properties: dict[str, Any] | None = None
    ) -> None:
        """Idempotent MERGE on (repo_id, name[, file]) for a repo-scoped node label.

        Routed through the same `_is_file_scoped` predicate `_upsert_nodes_tx`
        uses, so a caller passing a `file` property for a Class/Function/
        Service node gets the same collision-proof MERGE key the batched
        indexer path does, instead of a second, un-unified implementation
        that always MERGEs on bare (repo_id, name).
        """
        props = properties or {}
        file = props.get("file")
        file_scoped = _is_file_scoped(file)
        merge_key = (
            "{repo_id: $repo_id, name: $name, file: $file}"
            if file_scoped
            else "{repo_id: $repo_id, name: $name}"
        )
        # See _upsert_nodes_tx for why non-file-scoped nodes accumulate
        # `sources`.
        sources_clause = (
            ""
            if file_scoped
            else " SET n.sources = CASE WHEN $properties.source IS NULL THEN n.sources "
            "WHEN $properties.source IN coalesce(n.sources, []) THEN n.sources "
            "ELSE coalesce(n.sources, []) + $properties.source END"
        )
        with self._driver.session() as session:
            _retry_transient(
                session.run,
                f"MERGE (n:{label} {merge_key}) "
                "SET n += $properties" + sources_clause,
                repo_id=repo_id,
                name=name,
                file=file,
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
            _retry_transient(
                session.run,
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
            result = _retry_transient(
                session.run,
                "MATCH (m:Module {repo_id: $repo_id})-[:IMPORTS]->"
                "(target:Module {repo_id: $repo_id, name: $module_name}) "
                "RETURN m.name as name",
                repo_id=repo_id,
                module_name=module_name,
            )
            return [record["name"] for record in result]

    def delete_nodes_by_source_file(self, repo_id: str, file_name: str) -> None:
        """Remove or unclaim every node whose provenance names this file, scoped to repo_id.

        Extractors record provenance under one of three property keys
        depending on which one wrote the node (source_file/file/source — an
        inconsistency inherited from how each extractor was built
        independently). `source_file`/`file` nodes name exactly one file by
        construction and are deleted outright; `source` nodes can be
        co-produced by several files, so this file is unclaimed from
        `sources` and the node is only deleted once no file claims it —
        see _UNCLAIM_SOURCE_CYPHER.
        """
        with self._driver.session() as session:
            _retry_transient(session.run, _DELETE_BY_SOURCE_FILE_CYPHER, repo_id=repo_id, file_name=file_name)
            _retry_transient(session.run, _UNCLAIM_SOURCE_CYPHER, repo_id=repo_id, file_name=file_name)

    def stage_recency(
        self,
        label: str,
        repo_id: str,
        name: str,
        created_at: str | None = None,
        last_modified_at: str | None = None,
        last_modified_by: str | None = None,
        file: str | None = None,
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

        Pass `file` for Function/Class labels (whose nodes are MERGE-keyed
        on (repo_id, name, file) — see _group_nodes_by_label) so this
        doesn't fall back to a bare-name match that could hit every
        same-named function across every file in the repo at once. Omit it
        for Module (whose `name` is already the unique file path).
        """
        merge_key = (
            "{repo_id: $repo_id, name: $name, file: $file}" if file is not None else "{repo_id: $repo_id, name: $name}"
        )
        with self._driver.session() as session:
            _retry_transient(
                session.run,
                f"MERGE (n:{label} {merge_key}) "
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
                file=file,
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
        file: str | None = None,
    ) -> None:
        """Overwrite recency from scratch — used only by the reconcile path,
        where the caller has just recomputed the authoritative value from a
        fresh full walk and must be able to move a value backward, not just
        forward (`stage_recency`'s ratchet deliberately can't).

        `last_modified_by` is only included in the SET when not None, so a
        caller running with `git_recency_track_author` off (always passing
        `last_modified_by=None`) never nulls out a previously-tracked author.

        See `stage_recency` for why `file` matters for Function/Class labels.
        """
        merge_key = (
            "{repo_id: $repo_id, name: $name, file: $file}" if file is not None else "{repo_id: $repo_id, name: $name}"
        )
        set_clauses = ["n.created_at = $created_at", "n.last_modified_at = $last_modified_at"]
        if last_modified_by is not None:
            set_clauses.append("n.last_modified_by = $last_modified_by")
        with self._driver.session() as session:
            _retry_transient(
                session.run,
                f"MERGE (n:{label} {merge_key}) "
                f"SET {', '.join(set_clauses)}",
                repo_id=repo_id,
                name=name,
                file=file,
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
            _retry_transient(
                session.run,
                "MATCH (c:Commit {repo_id: $repo_id}) WHERE c.name IN $shas DETACH DELETE c",
                repo_id=repo_id,
                shas=shas,
            )

    def delete_repository(self, repo_id: str) -> None:
        """Remove every node (and its relationships) scoped to this repo_id."""
        with self._driver.session() as session:
            _retry_transient(
                session.run,
                "MATCH (n {repo_id: $repo_id}) DETACH DELETE n",
                repo_id=repo_id,
            )

    def run_cypher(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict]:
        """Advanced escape hatch. Callers must gate this behind explicit config
        (see `Settings.enable_run_cypher`) — never wire it up as the default path.
        """
        with self._driver.session() as session:
            result = session.run(query, parameters or {})
            return [record.data() for record in result]

    def run_cypher_graph(self, query: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Same escape hatch as `run_cypher`, but preserves node/relationship
        graph structure instead of flattening records with `.data()`.

        Backs the dashboard's Cypher console (`dashboard/routes.py`'s
        `/api/cypher`), which renders results onto the Cytoscape canvas the
        same way Neo4j's HTTP transaction API's `resultDataContents:
        ["row","graph"]` shape does -- this mirrors that shape so the
        frontend needs no special-casing. Deliberately uses the *legacy*
        integer `.id` (deprecated on the driver, but still the only id Cypher's
        `id()` function returns) rather than `.element_id`: the frontend's
        node/relationship inspector round-trips this id back through a
        `WHERE id(n) = <id>` query, and Neo4j's HTTP API's own graph format
        was always legacy integer ids, never `elementId()` strings -- using
        `element_id` here would silently break that round trip. Same gating
        rule as `run_cypher`: never wire this up as a default/unauthenticated
        path.
        """
        with self._driver.session() as session:
            result = session.run(query, parameters or {})
            columns = list(result.keys())
            data: list[dict[str, Any]] = []
            for record in result:
                nodes: dict[int, dict[str, Any]] = {}
                rels: dict[int, dict[str, Any]] = {}

                def collect(value: Any) -> None:
                    if isinstance(value, Node):
                        properties = dict(value)
                        labels = list(value.labels)
                        # `key` alongside the legacy `id` above: the id is a
                        # storage-slot pointer that churns whenever
                        # _replace_file_nodes_tx recreates a changed file's
                        # nodes, so the dashboard cannot use it to recognise
                        # the same node across reindexes. Computed here rather
                        # than in the browser so the "is this file-scoped"
                        # rule lives in exactly one place (see identity_key).
                        # Absent when a node has no name to key on — a
                        # projection like `RETURN {x: 1}` never reaches this
                        # branch, but an aggregate or a node written outside
                        # the extractors might.
                        name = properties.get("name")
                        nodes[value.id] = {
                            "id": value.id,
                            "labels": labels,
                            "properties": properties,
                            "key": identity_key(
                                labels[0] if labels else "Unknown",
                                properties.get("repo_id") or "",
                                name,
                                properties.get("file"),
                            ) if isinstance(name, str) else None,
                        }
                    elif isinstance(value, Relationship):
                        rels[value.id] = {
                            "id": value.id,
                            "type": value.type,
                            "startNode": value.start_node.id,
                            "endNode": value.end_node.id,
                            "properties": dict(value),
                        }
                    elif isinstance(value, list):
                        for item in value:
                            collect(item)

                row: list[Any] = []
                for value in record.values():
                    collect(value)
                    if isinstance(value, (Node, Relationship)):
                        row.append(dict(value))
                    else:
                        row.append(value)

                data.append(
                    {
                        "row": row,
                        "graph": {"nodes": list(nodes.values()), "relationships": list(rels.values())},
                    }
                )
            return {"columns": columns, "data": data}
