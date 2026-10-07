"""Neo4j graph engine: connection lifecycle, schema init, idempotent upserts.

All writes are `MERGE`-based keyed on `(repo_id, name)` (or `(repo_id, path)`
for file-provenance nodes) so incremental reindexing updates existing nodes
in place instead of duplicating them. `repo_id` is a hard filter on every
read, never an optional convenience — see Design Brief Principle 3.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from neo4j import Driver, GraphDatabase, READ_ACCESS, unit_of_work
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError
from neo4j.graph import Node, Relationship

from devgraph.graph.schema import RELATIONSHIP_TYPES, RESERVED_NODE_PROPERTIES, constraint_statements

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for annotations only: `devgraph.config.project_schema` imports
    # `devgraph.graph.schema`, and `devgraph.graph.__init__` imports this
    # module, so a module-level import here would be a real cycle. The
    # runtime import lives inside `provision_repository_schema`.
    from devgraph.config.project_schema import EffectiveSchema

logger = logging.getLogger(__name__)

# What identifies a provider-owned node: never cleared, whatever the schema keeps.
# `claims` is the shared-node bookkeeping beside `sources` (see _claims_of).
_EXTRACTED_IDENTITY_PROPERTIES = RESERVED_NODE_PROPERTIES | {"path", "claims"}

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
# the last one is unclaimed — see _unclaim_source_tx.
_DELETE_BY_SOURCE_FILE_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE n.source_file = $file_name OR n.file = $file_name "
    "   OR (n:Module AND n.name = $file_name) "
    "DETACH DELETE n"
)

_UNCLAIM_SOURCE_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE n.file IS NULL AND n.source_file IS NULL "
    "  AND (n.source = $file_name OR $file_name IN coalesce(n.sources, [])) "
    "RETURN elementId(n) AS id, properties(n) AS props"
)

# Shared nodes are attributed to their first source, whatever order the files
# claim them in (watcher spec W10). `claims` is a JSON string (Neo4j has no
# map properties) of {source: the properties that source wrote}; `sources`
# is its sorted keys, and the node's written properties are exactly
# claims[min(sources)]. Both directions run read-modify-write in Python inside
# one write transaction. These properties are never part of a claim: the
# identity, the bookkeeping itself, and what other passes write.
_UNCLAIMED_PROPERTIES = frozenset({
    "repo_id", "name", "file", "source_file", "sources", "claims", "extractor",
    "created_at", "last_modified_at", "last_modified_by",
})


def _claims_of(props: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A shared node's claims. A legacy node (written before `claims`) has
    `sources` alone: each listed source without an entry claims the node's
    current properties."""
    claims = json.loads(props["claims"]) if props.get("claims") else {}
    listed = props.get("sources")
    if listed is None:
        listed = [props["source"]] if props.get("source") is not None else []
    if any(source not in claims for source in listed):
        current = {
            key: value for key, value in props.items()
            if key not in _UNCLAIMED_PROPERTIES and not key.startswith("insight_")
        }
        for source in listed:
            claims.setdefault(source, {**current, "source": source})
    return claims


def _attribution(claims: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return claims[min(claims)] if claims else {}


def _reattributed(
    props: dict[str, Any], claims: dict[str, dict[str, Any]], before: dict[str, Any]
) -> dict[str, Any]:
    """The node's whole property map once `claims` changed from an attribution
    of `before`: what `before` wrote and the new attribution lacks is removed."""
    after = _attribution(claims)
    updated = {key: value for key, value in props.items() if key not in before or key in after}
    updated.update(after)
    updated["sources"] = sorted(claims)
    updated["claims"] = json.dumps(claims, sort_keys=True)
    return updated


def _claim_nodes_tx(tx, label: str, rows: list[dict[str, Any]]) -> None:
    """Claim shared nodes of one label for each row's `source`, in row order.

    Matches what `MERGE (n:{label} {repo_id, name})` matches, so a row
    claims every node that MERGE would have updated, or creates one."""
    keys = sorted({(row["repo_id"], row["name"]) for row in rows})
    matched: dict[tuple[str, str], list[list[Any]]] = {key: [] for key in keys}
    for record in tx.run(
        f"UNWIND $keys AS k MATCH (n:{label} {{repo_id: k[0], name: k[1]}}) "
        "RETURN k[0] AS repo_id, k[1] AS name, elementId(n) AS id, properties(n) AS props",
        keys=[list(key) for key in keys],
    ):
        matched[(record["repo_id"], record["name"])].append([record["id"], dict(record["props"])])
    for row in rows:
        nodes = matched[(row["repo_id"], row["name"])]
        if not nodes:
            nodes.append([None, {"repo_id": row["repo_id"], "name": row["name"]}])
        written = {key: value for key, value in row["properties"].items() if value is not None}
        for entry in nodes:
            claims = _claims_of(entry[1])
            before = _attribution(claims)
            claims[written["source"]] = written
            entry[1] = _reattributed(entry[1], claims, before)
    existing = [{"id": node_id, "props": props} for nodes in matched.values() for node_id, props in nodes if node_id]
    created = [props for nodes in matched.values() for node_id, props in nodes if node_id is None]
    if existing:
        tx.run("UNWIND $rows AS row MATCH (n) WHERE elementId(n) = row.id SET n = row.props", rows=existing)
    if created:
        tx.run(
            f"UNWIND $rows AS props MERGE (n:{label} {{repo_id: props.repo_id, name: props.name}}) SET n = props",
            rows=created,
        )


def _delete_by_source_file_tx(tx, repo_id: str, file_name: str) -> None:
    tx.run(_DELETE_BY_SOURCE_FILE_CYPHER, repo_id=repo_id, file_name=file_name)
    _unclaim_source_tx(tx, repo_id, file_name)


def _unclaim_source_tx(tx, repo_id: str, file_name: str) -> None:
    """Drop `file_name`'s claim on every shared node: re-attribute the node from
    the new first source, or delete it when no claim is left."""
    gone: list[str] = []
    kept: list[dict[str, Any]] = []
    for record in tx.run(_UNCLAIM_SOURCE_CYPHER, repo_id=repo_id, file_name=file_name):
        props = dict(record["props"])
        claims = _claims_of(props)
        before = _attribution(claims)
        claims.pop(file_name, None)
        if claims:
            kept.append({"id": record["id"], "props": _reattributed(props, claims, before)})
        else:
            gone.append(record["id"])
    if kept:
        tx.run("UNWIND $rows AS row MATCH (n) WHERE elementId(n) = row.id SET n = row.props", rows=kept)
    if gone:
        tx.run("MATCH (n) WHERE elementId(n) IN $ids DETACH DELETE n", ids=gone)


# Used by _replace_file_nodes_tx: delete only the file-scoped symbol nodes
# (Class/Function) whose symbol no longer exists in the file's current
# extraction. Unlike _DELETE_BY_SOURCE_FILE_CYPHER, this does NOT DETACH
# DELETE every node with the file's provenance — surviving nodes keep their
# incoming edges (MODIFIES from git history, MENTIONS from docs, cross-file
# CALLS/IMPORTS), which a blanket delete-then-recreate silently destroyed.
# The Module node (the file itself) is always excluded and MERGEd in place.
# `keep` is a list of [label, name] pairs for the file-scoped nodes the
# current extraction still produces; an empty list (file now has no
# classes/functions) correctly deletes them all.
_DELETE_STALE_FILE_NODES_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE NOT n:Module "
    "  AND (n.file = $file_name OR n.source_file = $file_name) "
    "  AND NOT any(pair IN $keep WHERE labels(n)[0] = pair[0] AND n.name = pair[1]) "
    "DETACH DELETE n"
)

# Nodes a schema-declared provider owns (devgraph/indexer/providers/) are
# tagged with `extractor`. Their `path` property is the repo-relative file
# that owns the entry and `name` is its key: the path itself for filesystem
# and path-keyed docs nodes, a front-matter value for field-keyed docs nodes.
# They never carry `file`/`source_file`/`source`, so the built-in per-file
# cleanup above never touches them and these queries are their whole
# lifecycle. Path-scoped queries match `path`; `keep` lists match the key.
# A path matches its own node and, as a directory, everything below it --
# the trailing '/' keeps `src` from matching `src2/...`.
_EXTRACTED_AT_OR_BELOW = (
    "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor "
    "AND (n.path IN $paths OR any(p IN $paths WHERE n.path STARTS WITH p + '/')) "
)
_DELETE_EXTRACTED_PATHS_CYPHER = _EXTRACTED_AT_OR_BELOW + "DETACH DELETE n"
_EXTRACTED_NODES_AT_CYPHER = _EXTRACTED_AT_OR_BELOW + "RETURN DISTINCT labels(n)[0] AS label, n.name AS name"
_PRUNE_EXTRACTED_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor "
    "AND NOT (labels(n)[0] + ':' + n.name) IN $keep "
    "DETACH DELETE n RETURN count(n) AS pruned"
)
# The incremental reconcile for provider-owned nodes: only nodes at these
# exact paths are candidates (never anything below them), so a batch of
# changed files can't prune the rest of the provider's nodes.
_PRUNE_EXTRACTED_AT_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor AND n.path IN $paths "
    "AND NOT (labels(n)[0] + ':' + n.name) IN $keep "
    "DETACH DELETE n RETURN count(n) AS pruned"
)
# A provider's own edges are the outgoing ones of a non-built-in type; incoming
# edges belong to whoever wrote them. A null $paths means the whole repo.
_DELETE_EXTRACTED_EDGES_CYPHER = (
    "MATCH (a {repo_id: $repo_id})-[r]->() WHERE a.extractor = $extractor "
    "AND ($paths IS NULL OR a.path IN $paths) AND NOT type(r) IN $builtin_types "
    "DELETE r RETURN count(r) AS deleted"
)
# Property keys present on one provider label, for clearing the ones the
# schema no longer declares (see GraphEngine.clear_extracted_properties).
_EXTRACTED_PROPERTY_KEYS_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor AND labels(n)[0] = $label "
    "UNWIND keys(n) AS key RETURN DISTINCT key"
)

# Which project schema the repository's graph was last built with (see
# dispatch.apply_project_schema). Kept on the Repository node so every
# process -- CLI, agent, dashboard -- reads the same state.
_READ_APPLIED_SCHEMA_CYPHER = (
    "MATCH (r:Repository {repo_id: $repo_id}) WHERE r.schema_hash IS NOT NULL "
    "RETURN r.schema_hash AS hash, coalesce(r.schema_labels, []) AS labels, "
    "coalesce(r.schema_relationship_types, []) AS relationship_types, "
    "coalesce(r.schema_keys, []) AS keys"
)
_READ_ALL_APPLIED_SCHEMAS_CYPHER = (
    "MATCH (r:Repository) WHERE r.schema_hash IS NOT NULL "
    "RETURN r.repo_id AS repo_id, coalesce(r.schema_labels, []) AS labels, "
    "coalesce(r.schema_keys, []) AS keys"
)
_RECORD_APPLIED_SCHEMA_CYPHER = (
    "MERGE (r:Repository {repo_id: $repo_id}) "
    "SET r.schema_hash = $hash, r.schema_labels = $labels, "
    "r.schema_relationship_types = $relationship_types, r.schema_keys = $keys"
)
# Constraints, and the indexes no constraint owns: the objects a project
# schema can generate (see devgraph/indexer/schema_constraints.py).
_SHOW_CONSTRAINTS_CYPHER = "SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties"
_SHOW_INDEXES_CYPHER = (
    "SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, owningConstraint "
    "WHERE owningConstraint IS NULL RETURN name, type, entityType, labelsOrTypes, properties"
)
# Graph insights (devgraph/analytics/insights.py). The Repository node is the
# scoping root, not code, so it never takes part in the graph that's analysed.
_LOAD_INSIGHT_EDGES_CYPHER = (
    "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) "
    "WHERE type(r) IN $types AND NOT a:Repository AND NOT b:Repository "
    "RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type"
)
_LOAD_INSIGHT_NODES_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) WHERE elementId(n) IN $ids "
    "RETURN elementId(n) AS id, n.name AS name, labels(n) AS labels, n.file AS file"
)
_CLEAR_INSIGHTS_CYPHER = (
    "MATCH (n {repo_id: $repo_id}) "
    "WHERE n.insight_community IS NOT NULL OR n.insight_pagerank IS NOT NULL "
    "OR n.insight_betweenness IS NOT NULL "
    "REMOVE n.insight_community, n.insight_pagerank, n.insight_betweenness"
)
_WRITE_INSIGHTS_CYPHER = (
    "UNWIND $rows AS row MATCH (n) WHERE elementId(n) = row.id AND n.repo_id = $repo_id "
    "SET n.insight_community = row.community, n.insight_pagerank = row.pagerank, "
    "n.insight_betweenness = row.betweenness"
)
_WRITE_INSIGHTS_SUMMARY_CYPHER = (
    "MERGE (r:Repository {repo_id: $repo_id}) "
    "SET r.insights_computed_at = $computed_at, r.insights_node_count = $node_count, "
    "r.insights_community_count = $community_count, r.insights_modularity = $modularity, "
    "r.insights_communities = $communities"
)
_READ_INSIGHTS_SUMMARY_CYPHER = (
    "MATCH (r:Repository {repo_id: $repo_id}) WHERE r.insights_computed_at IS NOT NULL "
    "RETURN r.insights_computed_at AS computed_at, r.insights_node_count AS node_count, "
    "r.insights_community_count AS community_count, r.insights_modularity AS modularity, "
    "r.insights_communities AS communities"
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
    if _is_file_scoped(file) and file is not None:
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
        # A non-file-scoped row with a `source` claims a shared node that
        # several files can produce (see _claim_nodes_tx), so
        # delete_nodes_by_source_file can unclaim one producer without
        # destroying a node another file still produces. Every other row
        # (e.g. Module, whose `source_file` already identifies its one node)
        # is a plain MERGE.
        claims = not file_scoped
        claimed = [row for row in rows if claims and row["properties"].get("source") is not None]
        plain = [row for row in rows if not (claims and row["properties"].get("source") is not None)]
        if plain:
            tx.run(
                f"UNWIND $rows AS row "
                f"MERGE (n:{label} {merge_key}) "
                "SET n += row.properties",
                rows=plain,
            )
        if claimed:
            _claim_nodes_tx(tx, label, claimed)


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


def _write_insights_tx(tx, repo_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    """Clear-then-write in one transaction, so a reader never sees a repo
    half old and half new, and a node that lost its edges loses its scores."""
    tx.run(_CLEAR_INSIGHTS_CYPHER, repo_id=repo_id)
    if rows:
        tx.run(_WRITE_INSIGHTS_CYPHER, repo_id=repo_id, rows=rows)
    tx.run(_WRITE_INSIGHTS_SUMMARY_CYPHER, repo_id=repo_id, **summary)


def _replace_file_nodes_tx(
    tx, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
) -> None:
    # Delete only the file-scoped nodes (Class/Function, keyed on `file`)
    # whose symbol is no longer in the file's current extraction. The Module
    # node (keyed on `source_file`) and any surviving Class/Function nodes
    # are MERGEd in place below, so their incoming edges — MODIFIES from git
    # history, MENTIONS from docs, cross-file CALLS/IMPORTS — are preserved.
    # A blanket DETACH DELETE of every node with this file's provenance (the
    # old behavior) destroyed those edges on every reindex, which is why
    # git-history MODIFIES edges silently vanished for any file re-indexed
    # after history was synced.
    keep = [
        [node["label"], node["name"]]
        for node in nodes
        if node["label"] != "Module"
        and (
            (node.get("properties") or {}).get("file") == file_name
            or (node.get("properties") or {}).get("source_file") == file_name
        )
    ]
    tx.run(_DELETE_STALE_FILE_NODES_CYPHER, repo_id=repo_id, file_name=file_name, keep=keep)
    _unclaim_source_tx(tx, repo_id, file_name)
    _upsert_nodes_tx(tx, nodes)
    _upsert_relationships_tx(tx, rels)


def repository_constraint_statements(effective: EffectiveSchema | None = None) -> list[str]:
    """Constraint Cypher to provision for one repository.

    The built-in statements always come first and in full, byte-identically
    to `devgraph.graph.schema.constraint_statements()`, followed by whatever
    the repository's own schema declares. `effective is None` (no project
    file, or any caller that isn't repository-scoped) is therefore exactly
    today's statement list.

    This deliberately diverges from `EffectiveSchema.constraint_statements()`,
    which omits the built-ins under `extends: none` (project_schema.py, pinned
    by tests/config/test_project_schema.py::test_extends_none_inherits_nothing).
    That is the right answer for describing one project's declaration, but the
    wrong one to provision from: every registered repository shares a single
    Neo4j database and the indexer keeps writing built-in labels regardless of
    what any one project declares, so dropping the built-in constraints for a
    repository that opted out would leave the whole database unconstrained.
    Hence the built-ins are sourced here, never from that method; its output is
    only appended, through an order-preserving dedupe that collapses the
    built-ins it already replayed under `extends: default`.
    """
    statements = list(constraint_statements())
    if effective is None:
        return statements

    seen = set(statements)
    for statement in effective.constraint_statements():
        if statement not in seen:
            seen.add(statement)
            statements.append(statement)
    return statements


class GraphEngine:
    def __init__(self, uri: str, user: str, password: str) -> None:
        self._driver: Driver = GraphDatabase.driver(uri, auth=(user, password))

    def close(self) -> None:
        self._driver.close()

    def verify_connectivity(self) -> None:
        _retry_transient(self._driver.verify_connectivity)

    def init_schema(self, effective: EffectiveSchema | None = None) -> None:
        """Provision the built-in constraints, plus a repository's declared ones.

        Idempotent (every statement is `IF NOT EXISTS`/`IF EXISTS` guarded), so
        re-running it on an already-provisioned database is a no-op. Callers
        that aren't scoped to one repository omit `effective` and get exactly
        the built-in statements. Statements always come from
        `repository_constraint_statements`, never from
        `EffectiveSchema.constraint_statements()` directly.
        """
        with self._driver.session() as session:
            for stmt in repository_constraint_statements(effective):
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

        Routed through `_upsert_nodes_tx` itself, so a caller passing a `file` property for a Class/Function/
        Service node gets the same collision-proof MERGE key the batched
        indexer path does, and a `source` claims a shared node the same way.
        """
        with self._driver.session() as session:
            session.execute_write(
                _upsert_nodes_tx, [{"label": label, "repo_id": repo_id, "name": name, "properties": properties or {}}]
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
            records = result or []
            return [record["name"] for record in records]

    def delete_nodes_by_source_file(self, repo_id: str, file_name: str) -> None:
        """Remove or unclaim every node whose provenance names this file, scoped to repo_id.

        Extractors record provenance under one of three property keys
        depending on which one wrote the node (source_file/file/source — an
        inconsistency inherited from how each extractor was built
        independently). `source_file`/`file` nodes name exactly one file by
        construction and are deleted outright; `source` nodes can be
        co-produced by several files, so this file is unclaimed from
        `sources` and the node is only deleted once no file claims it —
        see _unclaim_source_tx.
        """
        with self._driver.session() as session:
            session.execute_write(_delete_by_source_file_tx, repo_id, file_name)

    def delete_extracted_nodes(self, repo_id: str, extractor: str, paths: list[str]) -> None:
        """Delete one provider's nodes at these repo-relative paths, or below them."""
        if not paths:
            return
        with self._driver.session() as session:
            _retry_transient(
                session.run, _DELETE_EXTRACTED_PATHS_CYPHER, repo_id=repo_id, extractor=extractor, paths=paths
            )

    def extracted_nodes_at(self, repo_id: str, extractor: str, paths: list[str]) -> set[tuple[str, str]]:
        """(label, name) of one provider's nodes at these repo-relative paths, or below them --
        the nodes delete_extracted_nodes would delete."""
        if not paths:
            return set()
        with self._driver.session() as session:
            result = _retry_transient(
                session.run, _EXTRACTED_NODES_AT_CYPHER, repo_id=repo_id, extractor=extractor, paths=paths
            )
            return {(record["label"], record["name"]) for record in result or []}

    def extracted_entries(self, repo_id: str, extractor: str, labels: list[str]) -> set[tuple[str, str, str]]:
        """(label, name, path) of every node one provider wrote under these labels in a repo."""
        if not labels:
            return set()
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor AND labels(n)[0] IN $labels "
                "RETURN labels(n)[0] AS label, n.name AS name, n.path AS path",
                repo_id=repo_id, extractor=extractor, labels=labels,
            )
            return {(record["label"], record["name"], record["path"]) for record in result or []}

    def list_extracted_paths(self, repo_id: str, extractor: str, labels: list[str] | None) -> set[str]:
        """The `path` of every node one provider wrote in a repo, only under
        these labels when `labels` is given: the provider's side of the graph's
        file list (see dispatch._graph_files)."""
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor "
                "AND ($labels IS NULL OR labels(n)[0] IN $labels) AND n.path IS NOT NULL "
                "RETURN DISTINCT n.path AS path",
                repo_id=repo_id, extractor=extractor, labels=labels,
            )
            return {record["path"] for record in result or []}

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

    def prune_extracted_at(self, repo_id: str, extractor: str, paths: list[str], keep: list[str]) -> int:
        """Delete one provider's nodes at exactly these paths, except `keep` ("Label:name")."""
        if not paths:
            return 0
        with self._driver.session() as session:
            result = _retry_transient(
                session.run, _PRUNE_EXTRACTED_AT_CYPHER,
                repo_id=repo_id, extractor=extractor, paths=paths, keep=keep,
            )
            records = [record.data() for record in result or []]
        return records[0]["pruned"] if records else 0

    def delete_extracted_edges(self, repo_id: str, extractor: str, paths: list[str] | None) -> int:
        """Delete the outgoing non-built-in edges of one provider's nodes at these paths,
        or across the whole repo when `paths` is None."""
        if paths is not None and not paths:
            return 0
        with self._driver.session() as session:
            result = _retry_transient(
                session.run, _DELETE_EXTRACTED_EDGES_CYPHER, repo_id=repo_id, extractor=extractor,
                paths=paths, builtin_types=list(RELATIONSHIP_TYPES),
            )
            records = [record.data() for record in result or []]
        return records[0]["deleted"] if records else 0

    def clear_extracted_properties(self, repo_id: str, extractor: str, label: str, keep: list[str]) -> list[str]:
        """Remove every property of one provider label's nodes that is not in `keep`,
        not an identity property (reserved names and `path`) and not `insight_*`.
        An empty `keep` clears every non-identity property. Returns the removed
        names, sorted.

        The names come from the graph, so each one must fullmatch the property-name
        pattern before it is interpolated; anything else is left in place.
        """
        # Runtime import: see the TYPE_CHECKING note at the top of this module.
        from devgraph.config.project_schema import PROPERTY_NAME_PATTERN

        params = {"repo_id": repo_id, "extractor": extractor, "label": label}
        with self._driver.session() as session:
            result = _retry_transient(session.run, _EXTRACTED_PROPERTY_KEYS_CYPHER, **params)
            present = [record["key"] for record in result or []]
            stale = sorted(
                key for key in present
                if key not in keep
                and key not in _EXTRACTED_IDENTITY_PROPERTIES
                and not key.startswith("insight_")
                and PROPERTY_NAME_PATTERN.fullmatch(key)
            )
            if stale:
                removals = ", ".join(f"n.`{key}`" for key in stale)
                _retry_transient(
                    session.run,
                    "MATCH (n {repo_id: $repo_id}) WHERE n.extractor = $extractor AND labels(n)[0] = $label "
                    f"REMOVE {removals}",
                    **params,
                ).consume()
        return stale

    def read_applied_schema(self, repo_id: str) -> dict[str, Any] | None:
        """The schema state the repo's graph was last built with, or None."""
        with self._driver.session() as session:
            result = _retry_transient(session.run, _READ_APPLIED_SCHEMA_CYPHER, repo_id=repo_id)
            records = [record.data() for record in result or []]
        return records[0] if records else None

    def read_all_applied_schemas(self) -> list[dict[str, Any]]:
        """Every repository's recorded user labels and keys, from the graph itself."""
        with self._driver.session() as session:
            result = _retry_transient(session.run, _READ_ALL_APPLIED_SCHEMAS_CYPHER)
            return [record.data() for record in result or []]

    def record_applied_schema(
        self,
        repo_id: str,
        schema_hash: str,
        labels: list[str],
        relationship_types: list[str],
        keys: list[str] | None = None,
    ) -> None:
        """Record the schema hash and user labels/relationship types/keys the repo's graph was last built with.

        `keys` holds one "Label:k1,k2" string per label.
        """
        with self._driver.session() as session:
            _retry_transient(
                session.run, _RECORD_APPLIED_SCHEMA_CYPHER, repo_id=repo_id, hash=schema_hash,
                labels=labels, relationship_types=relationship_types, keys=keys or [],
            )

    def list_schema_objects(self) -> list[dict[str, Any]]:
        """Every constraint and every index no constraint owns, each tagged with `kind`."""
        with self._driver.session() as session:
            constraints = [
                {**record.data(), "kind": "constraint"}
                for record in _retry_transient(session.run, _SHOW_CONSTRAINTS_CYPHER) or []
            ]
            indexes = [
                {**record.data(), "kind": "index"}
                for record in _retry_transient(session.run, _SHOW_INDEXES_CYPHER) or []
            ]
        return constraints + indexes

    def run_schema_statement(self, statement: str) -> None:
        """Run one CREATE/DROP CONSTRAINT/INDEX statement. The caller builds it from validated names."""
        with self._driver.session() as session:
            _retry_transient(session.run, statement).consume()

    def has_duplicate_keys(self, label: str, properties: tuple[str, ...]) -> bool:
        """Whether two `label` nodes share every one of `properties` (all non-null), i.e. a
        uniqueness constraint on them could not be created. The caller validates the names."""
        values = ", ".join(f"n.`{p}` AS `{p}`" for p in properties)
        present = " AND ".join(f"n.`{p}` IS NOT NULL" for p in properties)
        query = (
            f"MATCH (n:`{label}`) WHERE {present} WITH {values}, count(*) AS c "
            "WHERE c > 1 RETURN 1 AS dup LIMIT 1"
        )
        with self._driver.session() as session:
            result = _retry_transient(session.run, query)
            return bool([record for record in result or []])

    def label_has_nodes(self, label: str) -> bool:
        """Whether any node, in any repository, carries `label`. The caller validates `label`."""
        with self._driver.session() as session:
            result = _retry_transient(session.run, f"MATCH (n:`{label}`) RETURN n LIMIT 1")
            return bool([record for record in result or []])

    def existing_node_names(self, repo_id: str, label: str, names: list[str]) -> set[str]:
        """Which of `names` one repo's `label` nodes carry. The caller validates `label`."""
        if not names:
            return set()
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                f"MATCH (n:`{label}` {{repo_id: $repo_id}}) WHERE n.name IN $names RETURN DISTINCT n.name AS name",
                repo_id=repo_id,
                names=names,
            )
            return {record["name"] for record in result or []}

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

    def list_indexed_files(self, repo_id: str) -> set[str]:
        """Return every repo-relative path that currently backs file-provenance
        nodes for this repo.

        The "what's in the graph" side of full_scan's reconcile diff: a
        rescan must prune nodes whose file no longer exists on disk, and
        this is the authoritative list of files the graph believes it has
        indexed. Covers both provenance styles: `source_file`/`file`
        properties, and bare `Module` nodes whose `name` *is* the file path
        (the docs/mentions extractors' shape). `Commit`/`Repository` nodes
        and `source`-keyed nodes (Container/Service/API, co-produced by
        several files) are deliberately excluded — they're not keyed to a
        single file, so they can't be reconciled against the disk walk.
        """
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                "MATCH (n {repo_id: $repo_id}) "
                "WHERE n.source_file IS NOT NULL OR n.file IS NOT NULL "
                "   OR (n:Module AND n.name IS NOT NULL) "
                "RETURN DISTINCT coalesce(n.source_file, n.file, n.name) AS path",
                repo_id=repo_id,
            )
            records = result or []
            return {record["path"] for record in records if record["path"]}

    def list_file_nodes(self, repo_id: str, files: list[str]) -> set[tuple[str, str]]:
        """Return (label, name) for every node whose file provenance
        (`source_file`/`file`, a `Module` named by its path, or a schema
        provider's node, which its `path` owns) is one of `files`.

        index_paths snapshots this before re-indexing a batch so it can tell
        which nodes the batch *adds* -- only those can be the missing
        target of an edge from a file outside the batch.
        """
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                "MATCH (n {repo_id: $repo_id}) "
                "WHERE n.source_file IN $files OR n.file IN $files "
                "   OR (n:Module AND n.name IN $files) "
                "   OR (n.extractor IS NOT NULL AND n.path IN $files) "
                "RETURN DISTINCT labels(n)[0] AS label, n.name AS name",
                repo_id=repo_id,
                files=files,
            )
            records = result or []
            return {(record["label"], record["name"]) for record in records}

    def find_mentioning_documents(
        self, repo_id: str, pairs: list[tuple[str, str]], batch_keys: list[str]
    ) -> dict[tuple[str, str], set[str]]:
        """For each (label, name) in `pairs` that some node outside the batch
        (provenance not in `batch_keys`) already has, the Documents that
        MENTION a node of that label and name.

        Mention edges resolve by name, so a Document that mentions an
        existing `get` is exactly the set that should also link a newly added
        `get`; pairs absent from the result are new to the whole graph.
        """
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                "MATCH (n {repo_id: $repo_id}) "
                "WHERE n.name IN $names "
                "  AND NOT coalesce(n.file, n.source_file, '') IN $batch_keys "
                "  AND NOT (n:Module AND n.name IN $batch_keys) "
                "WITH DISTINCT labels(n)[0] AS label, n.name AS name "
                "WHERE [label, name] IN $pairs "
                "OPTIONAL MATCH (d:Document {repo_id: $repo_id})-[:MENTIONS]->(m {repo_id: $repo_id, name: name}) "
                "WHERE label IN labels(m) "
                "RETURN label, name, collect(DISTINCT d.name) AS docs",
                repo_id=repo_id,
                names=sorted({name for _label, name in pairs}),
                pairs=[[label, name] for label, name in pairs],
                batch_keys=batch_keys,
            )
            records = result or []
            return {(record["label"], record["name"]): set(record["docs"]) for record in records}

    def find_handler_stub_sources(self, repo_id: str, names: list[str]) -> set[str]:
        """Return the route files that left a file-less handler stub
        `Function` named one of `names` (see apis/extractor.py): the files
        whose Endpoint IMPLEMENTS edges can now resolve to a real function
        of that name."""
        with self._driver.session() as session:
            result = _retry_transient(
                session.run,
                "MATCH (f:Function {repo_id: $repo_id}) "
                "WHERE f.name IN $names AND f.file IS NULL AND f.source IS NOT NULL "
                "UNWIND coalesce(f.sources, [f.source]) AS source "
                "RETURN DISTINCT source",
                repo_id=repo_id,
                names=names,
            )
            records = result or []
            return {record["source"] for record in records}

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

    def load_insight_graph(
        self, repo_id: str, relationship_types: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """One repository's edges of `relationship_types` and the nodes they touch.

        Identity is `elementId`, which is only promised stable within a
        transaction; it is used for the `write_insights` that immediately
        follows, and a node deleted in between is simply not matched there.
        """
        with self._driver.session() as session:
            edge_result = _retry_transient(
                session.run, _LOAD_INSIGHT_EDGES_CYPHER, repo_id=repo_id, types=list(relationship_types)
            )
            edges = [record.data() for record in edge_result or []]
            ids = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
            node_result = _retry_transient(session.run, _LOAD_INSIGHT_NODES_CYPHER, repo_id=repo_id, ids=ids)
            nodes = [record.data() for record in node_result or []]
        return nodes, edges

    def write_insights(self, repo_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        """Replace a repository's insight properties (see `_write_insights_tx`)."""
        with self._driver.session() as session:
            session.execute_write(_write_insights_tx, repo_id, rows, summary)

    def read_insights_summary(self, repo_id: str) -> dict[str, Any] | None:
        """The Repository node's insight summary, or None if never computed."""
        with self._driver.session() as session:
            result = _retry_transient(session.run, _READ_INSIGHTS_SUMMARY_CYPHER, repo_id=repo_id)
            records = [record.data() for record in result or []]
        return records[0] if records else None

    def run_cypher(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict]:
        """Advanced escape hatch. Callers must gate this behind explicit config
        (see `Settings.enable_run_cypher`) — never wire it up as the default path.
        """
        with self._driver.session() as session:
            result = session.run(query, parameters or {})  # type: ignore[arg-type]
            return [record.data() for record in result]

    def run_read_cypher(
        self, query: str, parameters: dict[str, Any], *, timeout_s: float, max_rows: int
    ) -> tuple[list[dict], bool]:
        """Run one user-declared query read-only, bounded in time and rows.

        Read access mode makes the server refuse any write; the transaction
        timeout bounds the time; rows stop being pulled after `max_rows`, and
        the second value says whether more existed. Used by the MCP tool
        plane for `devgraph.tools.yaml` tools.
        """

        @unit_of_work(timeout=timeout_s)
        def work(tx):
            rows: list[dict] = []
            for record in tx.run(query, parameters):
                if len(rows) == max_rows:
                    return rows, True
                rows.append(record.data())
            return rows, False

        with self._driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)

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
            result = session.run(query, parameters or {})  # type: ignore[arg-type]
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
                            "startNode": value.start_node.id if value.start_node else None,
                            "endNode": value.end_node.id if value.end_node else None,
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


def provision_repository_schema(engine: GraphEngine, repo_root: Path | str) -> None:
    """Resolve a repository's optional schema file, then provision constraints.

    The single seam registration and rescan use in place of a bare
    `engine.init_schema()`, so both entry points provision the same statements
    from the same resolution.

    Resolution is pure filesystem work and happens *before* any session is
    opened, so an invalid `devgraph.schema.yaml` raises `ProjectSchemaError`
    with the loader's own message while the graph is still untouched: no
    constraint, no `upsert_repository`, no scan. Callers decide what that means
    — `devgraph rescan` exits non-zero, while `devgraph add` and
    `POST /api/repos` keep the repository registered and report a warning,
    exactly as they already do for an unreachable Neo4j.
    """
    # Function-local: see the TYPE_CHECKING note at the top of this module.
    from devgraph.config.project_schema import resolve_effective_schema

    effective = resolve_effective_schema(Path(repo_root))
    engine.init_schema(effective)
