"""Neo4j graph engine: connection lifecycle, schema init, idempotent upserts.

All writes are `MERGE`-based keyed on `(repo_id, name)` (or `(repo_id, path)`
for file-provenance nodes) so incremental reindexing updates existing nodes
in place instead of duplicating them. `repo_id` is a hard filter on every
read, never an optional convenience — see Design Brief Principle 3.
"""

from __future__ import annotations

import json
import logging
import posixpath
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from neo4j import Driver, GraphDatabase, NotificationDisabledClassification, Query, READ_ACCESS, unit_of_work
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired, TransientError
from neo4j.graph import Node, Relationship

from devgraph.graph.schema import (
    CLAIMED_LABELS,
    FILE_LABELS,
    FILE_SCOPED_LABELS,
    NAME_REF_SEP,
    NAMED_LABELS,
    NODE_LABELS,
    RELATIONSHIP_TYPES,
    RESERVED_NODE_PROPERTIES,
    SOURCE_FILE_LABELS,
    constraint_statements,
    lookup_index_statements,
    pin_kind,
    pin_value,
)
from devgraph.indexer.docs.extractor import DOC_NOTE_LABELS

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for annotations only: `devgraph.config.project_schema` imports
    # `devgraph.graph.schema`, and `devgraph.graph.__init__` imports this
    # module, so a module-level import here would be a real cycle. The
    # runtime import lives inside `provision_repository_schema`.
    from devgraph.config.project_schema import EffectiveSchema

logger = logging.getLogger(__name__)

# What identifies a provider-owned node: never cleared, whatever the schema keeps.
_EXTRACTED_IDENTITY_PROPERTIES = RESERVED_NODE_PROPERTIES | {"path"}


def _seek(label: str, prop: str, test: str) -> str:
    """One branch of `_union`: the `label` nodes of `$repo_id` whose `prop`
    passes `test`, through the label's `(repo_id, prop)` index. Hinted to
    seek, for the reason _end_match gives."""
    return (
        f"MATCH (n:{label}) USING INDEX SEEK n:{label}(repo_id, {prop}) "
        f"WHERE n.repo_id = $repo_id AND n.{prop} {test} RETURN n"
    )


def _union(branches: Iterable[str]) -> str:
    """The distinct nodes `n` of every branch, as one CALL subquery."""
    return "CALL () { " + " UNION ".join(branches) + " } "


def _owned_branches(test: str) -> list[str]:
    """The nodes whose file provenance passes `test` (a predicate such as
    "= $f" or "IN $files"): `file` or `source_file`, or a Module named by its
    path. Each label is sought through its own index, so the cost follows
    the files, not the database (a match on `{repo_id}` alone scanned every
    node)."""
    return (
        [_seek(label, "file", test) for label in FILE_LABELS]
        + [_seek(label, "source_file", test) for label in SOURCE_FILE_LABELS]
        + [_seek("Module", "name", test)]
    )


# Every shared node of the repository (see CLAIMED_LABELS): the candidates
# for a file's claims, filtered by the caller.
_CLAIMED_BRANCHES = [_seek(label, "source", "IS NOT NULL") for label in CLAIMED_LABELS]

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
_DELETE_BY_SOURCE_FILE_CYPHER = _union(_owned_branches("= $f")) + "DETACH DELETE n"

# A shared node's claims are read under its write lock (the no-op SET takes
# it), so concurrent claims and unclaims of one node run one after another
# instead of each overwriting the others from a stale read.
_LOCK_AND_READ = "SET n.name = n.name RETURN elementId(n) AS id, properties(n) AS props"

_UNCLAIM_FILTER = (
    "WITH n WHERE n.file IS NULL AND n.source_file IS NULL "
    "  AND (n.source = $file_name OR $file_name IN coalesce(n.sources, [])) "
    "  AND NOT any(p IN $keep WHERE labels(n)[0] = p[0] AND n.name = p[1]) "
)
_UNCLAIM_SOURCE_CYPHER = _union(_CLAIMED_BRANCHES) + _UNCLAIM_FILTER + _LOCK_AND_READ

# A code file's Module records the shared nodes its last replace claimed
# (`shared_claims`, "<label>\x1f<name>" entries), so its next replace or
# delete seeks just those by name instead of every shared node of the
# repository, whose count grows with the repository (each file's write
# scanned them all: quadratic in a full scan of a repository with many
# routes). A code file not indexed before (no Module yet) claims nothing; a
# file with no record (a compose file or Containerfile, which has no Module,
# or a Module an older index wrote) is unclaimed by the scan.
SHARED_CLAIM_SEP = "\x1f"
_RECORDED_CLAIMS_CYPHER = (
    "MATCH (m:Module {repo_id: $repo_id, name: $file_name}) RETURN properties(m)['shared_claims'] AS claims"
)
_UNCLAIM_RECORDED_CYPHER = (
    _union(_seek(label, "name", f"IN $claimed_{i}") for i, label in enumerate(CLAIMED_LABELS))
    + _UNCLAIM_FILTER + _LOCK_AND_READ
)


def _recorded_claims(tx, repo_id: str, file_name: str, code: bool = False) -> list[list[str]] | None:
    """The [label, name] pairs of the shared nodes `file_name`'s Module
    recorded claiming, None when it recorded none (see SHARED_CLAIM_SEP).
    A `code` file (one writing its Module) with no Module yet claims none."""
    record = tx.run(_RECORDED_CLAIMS_CYPHER, repo_id=repo_id, file_name=file_name).single()
    if record is None:
        return [] if code else None
    if record["claims"] is None:
        return None
    return [entry.split(SHARED_CLAIM_SEP, 1) for entry in record["claims"]]

# Shared nodes are attributed to their first source, whatever order the files
# claim them in (watcher spec W10). `claims` is a JSON string (Neo4j has no
# map properties) of {source: the properties that source wrote}; `sources`
# is its sorted keys, and the node's written properties are exactly
# claims[min(sources)]. Both directions run read-modify-write in Python inside
# one write transaction, under the node's lock, and write back only the
# properties the claim changed. These properties are never part of a claim:
# the identity, the bookkeeping itself, and what other passes write.
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


def _changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What `SET n += changes` needs to turn `before` into `after`: a null
    removes a property. Properties no claim touches are left out, so a
    concurrent writer's (`insight_*`) are never overwritten."""
    changes = {key: value for key, value in after.items() if before.get(key) != value}
    changes.update({key: None for key in before if key not in after})
    return changes


_WRITE_CHANGES_CYPHER = "UNWIND $rows AS row MATCH (n) WHERE elementId(n) = row.id SET n += row.changes"


def _claim_nodes_tx(tx, label: str, rows: list[dict[str, Any]]) -> None:
    """Claim shared nodes of one label for each row's `source`, in row order.

    A row claims every file-less node of its label and name, or the one it
    creates. A file-scoped node of the same name is never claimed: a handler
    stub `Function` stays its own node whichever file is written first."""
    keys = [list(key) for key in sorted({(row["repo_id"], row["name"]) for row in rows})]
    read: dict[str, dict[str, Any]] = {}
    current: dict[str, dict[str, Any]] = {}
    by_key: dict[tuple[str, str], list[str]] = {(key[0], key[1]): [] for key in keys}
    tx.run(
        f"UNWIND $keys AS k WITH k WHERE NOT EXISTS {{ "
        f"MATCH (m:{label} {{repo_id: k[0], name: k[1]}}) WHERE m.file IS NULL }} "
        f"CREATE (:{label} {{repo_id: k[0], name: k[1]}})",
        keys=keys,
    )
    for record in tx.run(
        f"UNWIND $keys AS k MATCH (n:{label} {{repo_id: k[0], name: k[1]}}) WHERE n.file IS NULL "
        "WITH k, n " + _LOCK_AND_READ + ", k[0] AS repo_id, k[1] AS name",
        keys=keys,
    ):
        read[record["id"]] = dict(record["props"])
        current[record["id"]] = dict(record["props"])
        by_key[(record["repo_id"], record["name"])].append(record["id"])
    for row in rows:
        written = {key: value for key, value in row["properties"].items() if value is not None}
        for node_id in by_key[(row["repo_id"], row["name"])]:
            claims = _claims_of(current[node_id])
            before = _attribution(claims)
            claims[written["source"]] = written
            current[node_id] = _reattributed(current[node_id], claims, before)
    tx.run(
        _WRITE_CHANGES_CYPHER,
        rows=[{"id": node_id, "changes": _changes(read[node_id], props)} for node_id, props in current.items()],
    )


# The files whose replace is a `service_api` one: Python's (its owning-service
# and API edges) and the languages whose routes are extracted
# (indexer/apis/routes.py: their Endpoint and owning-service CALLS edges).
SERVICE_API_SUFFIXES = (
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
)


def _delete_by_source_file_tx(tx, repo_id: str, file_name: str) -> None:
    recorded = _recorded_claims(tx, repo_id, file_name)
    _unclaim_foreign_sources_tx(tx, repo_id, file_name)
    if file_name.endswith(SERVICE_API_SUFFIXES):
        _unclaim_service_api_edges_tx(tx, repo_id, file_name, recorded)
    tx.run(_DELETE_BY_SOURCE_FILE_CYPHER, repo_id=repo_id, f=file_name)
    _unclaim_source_tx(tx, repo_id, file_name, [], recorded)


def _unclaim_source_tx(
    tx, repo_id: str, file_name: str, keep: list[list[str]], recorded: list[list[str]] | None = None
) -> None:
    """Drop `file_name`'s claim on every shared node except the `keep` ones
    ([label, name] pairs the file still claims, which the upsert that follows
    re-claims in place): re-attribute the node from the new first source, or
    delete it when no claim is left. `recorded` (`_recorded_claims`) limits
    the search to the nodes the file recorded claiming; None searches all."""
    gone: list[str] = []
    kept: list[dict[str, Any]] = []
    if recorded is None:
        records = tx.run(_UNCLAIM_SOURCE_CYPHER, repo_id=repo_id, file_name=file_name, keep=keep)
    else:
        names = {
            f"claimed_{i}": sorted({name for each, name in recorded if each == label})
            for i, label in enumerate(CLAIMED_LABELS)
        }
        records = tx.run(_UNCLAIM_RECORDED_CYPHER, repo_id=repo_id, file_name=file_name, keep=keep, **names)
    for record in records:
        props = dict(record["props"])
        claims = _claims_of(props)
        before = _attribution(claims)
        claims.pop(file_name, None)
        if claims:
            kept.append({"id": record["id"], "changes": _changes(props, _reattributed(props, claims, before))})
        else:
            gone.append(record["id"])
    if kept:
        tx.run(_WRITE_CHANGES_CYPHER, rows=kept)
    if gone:
        tx.run("MATCH (n) WHERE elementId(n) IN $ids DETACH DELETE n", ids=gone)


# Unclaim edge, applied to a bound `r`: remove the file `$f` from the
# edge's `origins` (the files that wrote it, kept sorted by
# _upsert_relationships_tx), and delete the edge once no writer is left. An
# edge with no `origins` was written before they were recorded, and counts
# as written by `$f` alone.
_UNCLAIM_EDGE = (
    "WITH r, [x IN coalesce(r.origins, [$f]) WHERE x <> $f] AS left "
    "FOREACH (_ IN CASE WHEN size(left) = 0 THEN [1] ELSE [] END | DELETE r) "
    "FOREACH (_ IN CASE WHEN size(left) > 0 THEN [1] ELSE [] END | SET r.origins = left)"
)

# Used by _replace_file_nodes_tx, in one scan over the nodes the file owns
# (`file` for Class/Function, `source_file` or its path as `name` for its
# Module): G2 set (a) unclaims the file from every edge out of them, so an
# edge the file no longer writes goes while one another file wrote out of
# the same node (a docs note's DOCUMENTED_BY, a Rust `impl` in another file)
# stays. Then it deletes only the file-scoped symbol nodes whose symbol is
# no longer in the file's current extraction. Unlike
# _DELETE_BY_SOURCE_FILE_CYPHER, this does NOT DETACH DELETE every node with
# the file's provenance -- surviving nodes keep their incoming edges
# (MODIFIES from git history, MENTIONS from docs, cross-file CALLS/IMPORTS),
# which a blanket delete-then-recreate silently destroyed. The Module node
# (the file itself) is always kept and MERGEd in place. `keep` is a list of
# [label, name, owner] triples for the file-scoped nodes the current
# extraction still produces; an empty list (file now has no
# classes/functions) correctly deletes them all. A node an index older than
# owners wrote (with `file` but no `owner`) is given the owner "" first, so
# a top-level symbol keeps its node and its incoming edges, and the upsert
# below MERGEs onto it instead of beside it; a method's old node is not kept
# (its owner is its class now) and is replaced.
_REPLACE_OWNED_NODES_CYPHER = (
    _union(_owned_branches("= $f"))
    + "CALL (n) { MATCH (n)-[r]->() " + _UNCLAIM_EDGE + " } "
    "WITH n WHERE NOT n:Module "
    "WITH n, any(key IN $keep WHERE labels(n)[0] = key[0] AND n.name = key[1] "
    "  AND coalesce(n.owner, '') = key[2]) AS kept "
    # A node is either given its owner or deleted, never both: one that
    # gains the owner key and goes in one transaction is left behind in the
    # transaction's view of the (repo_id, name, file, owner) index.
    "FOREACH (_ IN CASE WHEN kept AND n.file IS NOT NULL AND n.owner IS NULL THEN [1] ELSE [] END | "
    "  SET n.owner = '') "
    "FOREACH (_ IN CASE WHEN kept THEN [] ELSE [1] END | DETACH DELETE n)"
)

# G2 set (d): every edge a docs note writes touches one of the note's own
# nodes, so unclaiming the note file from the edges into and out of them
# (and nothing else) retracts the links it dropped. Anchored on the note
# labels so it scans only notes, not every node in the graph.
_UNCLAIM_DOC_NOTE_EDGES_CYPHER = (
    "MATCH (n:" + "|".join(DOC_NOTE_LABELS) + " {repo_id: $repo_id, source_file: $f}) "
    "CALL (n) { MATCH (n)<-[r:DOCUMENTED_BY|SATISFIES]-() " + _UNCLAIM_EDGE + " } "
    "CALL (n) { MATCH (n)-[r:SUPERSEDES|DECIDED_BY]->() " + _UNCLAIM_EDGE + " }"
)

# G2 set (b): edges the file wrote out of nodes another file owns (a Rust
# `impl Trait for Foo` writes `Foo -EXTENDS->`), found through the names its
# Module's `name_ref_sources` recorded when it was last written. Only edges
# that list the file are touched: a legacy edge with no `origins` can't be
# attributed, and is healed by set (a) on its source's own file instead.
_OLD_NAME_REF_SOURCES_CYPHER = (
    "MATCH (m:Module {repo_id: $repo_id, name: $f}) RETURN coalesce(m.name_ref_sources, []) AS sources"
)
# The sources are sought by name on every built-in label (NAMED_LABELS), not
# on `{repo_id}` alone, which scanned every node.
_UNCLAIM_FOREIGN_SOURCES_CYPHER = (
    _union(_seek(label, "name", "IN $sources") for label in NAMED_LABELS)
    + "MATCH (n)-[r]->() WHERE $f IN r.origins " + _UNCLAIM_EDGE
)


def _unclaim_foreign_sources_tx(tx, repo_id: str, file_name: str) -> None:
    """G2 set (b), run before the file's Module is overwritten or deleted."""
    sources = sorted({name for record in tx.run(_OLD_NAME_REF_SOURCES_CYPHER, repo_id=repo_id, f=file_name)
                      for name in record["sources"]})
    if sources:
        tx.run(_UNCLAIM_FOREIGN_SOURCES_CYPHER, repo_id=repo_id, f=file_name, sources=sources)


# G2 set (c): the edges a Python file (or another route file,
# SERVICE_API_SUFFIXES) writes out of nodes it doesn't own, the
# owning-service USES/CALLS and the API pass's Endpoint edges. Only the
# edges that list the file (or list no writer) are touched.
# The repository's Services and Endpoints are sought by repo_id on their
# `(repo_id, name)` index (every node has a name); a plain `{repo_id}` match
# scanned the label across every repository.
_UNCLAIM_SERVICE_USES_CYPHER = (
    "MATCH (s:Service)-[r:USES]->() USING INDEX SEEK s:Service(repo_id, name) "
    "WHERE s.repo_id = $repo_id AND s.name IS NOT NULL AND $f IN coalesce(r.origins, [$f]) " + _UNCLAIM_EDGE
)
_UNCLAIM_ENDPOINT_EDGES_CYPHER = (
    "MATCH (e:Endpoint)-[r:CALLS|IMPLEMENTS]->() USING INDEX SEEK e:Endpoint(repo_id, name) "
    "WHERE e.repo_id = $repo_id AND e.name IS NOT NULL AND $f IN coalesce(r.origins, [$f]) " + _UNCLAIM_EDGE
)


# Set (c)'s Endpoint edges when the file recorded its claims (SHARED_CLAIM_SEP):
# every Endpoint edge a file writes (its routes', its owning-service CALLS,
# their relinks) goes out of an Endpoint it claims.
_UNCLAIM_RECORDED_ENDPOINT_EDGES_CYPHER = (
    "MATCH (e:Endpoint)-[r:CALLS|IMPLEMENTS]->() USING INDEX SEEK e:Endpoint(repo_id, name) "
    "WHERE e.repo_id = $repo_id AND e.name IN $endpoints AND $f IN coalesce(r.origins, [$f]) " + _UNCLAIM_EDGE
)


def _unclaim_service_api_edges_tx(
    tx, repo_id: str, file_name: str, recorded: list[list[str]] | None = None
) -> None:
    # The owning-service edges come back only in index_paths' final service
    # pass, which needs every compose file in the batch, so between this
    # file's replace and that pass a reader can briefly miss them.
    tx.run(_UNCLAIM_SERVICE_USES_CYPHER, repo_id=repo_id, f=file_name)
    if recorded is None:
        tx.run(_UNCLAIM_ENDPOINT_EDGES_CYPHER, repo_id=repo_id, f=file_name)
    else:
        endpoints = sorted({name for label, name in recorded if label == "Endpoint"})
        if endpoints:
            tx.run(_UNCLAIM_RECORDED_ENDPOINT_EDGES_CYPHER, repo_id=repo_id, f=file_name, endpoints=endpoints)


# Nodes a schema-declared provider owns (devgraph/indexer/providers/) are
# tagged with `extractor`. Their `path` property is the repo-relative file
# that owns the entry and `name` is its key: the path itself for filesystem
# and path-keyed docs nodes, a front-matter value for field-keyed docs nodes.
# They never carry `file`/`source_file`/`source`, so the built-in per-file
# cleanup above never touches them and these queries are their whole
# lifecycle. Path-scoped queries match `path`; `keep` lists match the key.
# A path matches its own node and, as a directory, everything below it --
# the trailing '/' keeps `src` from matching `src2/...`.
# Each one follows the candidates GraphEngine._provider_nodes yields as `n`
# (the repository's nodes of its applied schema's labels), never a match on
# `{repo_id}` alone, which scanned every node in the database.
_EXTRACTED_AT_OR_BELOW = (
    "WITH n WHERE n.extractor = $extractor "
    "AND (n.path IN $paths OR any(p IN $paths WHERE n.path STARTS WITH p + '/')) "
)
_DELETE_EXTRACTED_PATHS_CYPHER = _EXTRACTED_AT_OR_BELOW + "DETACH DELETE n"
_EXTRACTED_NODES_AT_CYPHER = _EXTRACTED_AT_OR_BELOW + "RETURN DISTINCT labels(n)[0] AS label, n.name AS name"
_PRUNE_EXTRACTED_CYPHER = (
    "WITH n WHERE n.extractor = $extractor "
    "AND NOT (labels(n)[0] + ':' + n.name) IN $keep "
    "DETACH DELETE n RETURN count(n) AS pruned"
)
# The incremental reconcile for provider-owned nodes: only nodes at these
# exact paths are candidates (never anything below them), so a batch of
# changed files can't prune the rest of the provider's nodes.
_PRUNE_EXTRACTED_AT_CYPHER = (
    "WITH n WHERE n.extractor = $extractor AND n.path IN $paths "
    "AND NOT (labels(n)[0] + ':' + n.name) IN $keep "
    "DETACH DELETE n RETURN count(n) AS pruned"
)
# A provider's own edges are the outgoing ones of a non-built-in type; incoming
# edges belong to whoever wrote them. A null $paths means the whole repo.
_DELETE_EXTRACTED_EDGES_CYPHER = (
    "WITH n WHERE n.extractor = $extractor AND ($paths IS NULL OR n.path IN $paths) "
    "MATCH (n)-[r]->() WHERE NOT type(r) IN $builtin_types "
    "DELETE r RETURN count(r) AS deleted"
)
# Property keys present on one provider label, for clearing the ones the
# schema no longer declares (see GraphEngine.clear_extracted_properties).
_EXTRACTED_OF_LABEL = "WITH n WHERE n.extractor = $extractor AND labels(n)[0] = $label "
_EXTRACTED_PROPERTY_KEYS_CYPHER = _EXTRACTED_OF_LABEL + "UNWIND keys(n) AS key RETURN DISTINCT key"

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
# A CALLS edge matched by name alone ("name") or at package confidence (any
# file under an imported package, a class file found by suffix) is left out,
# in any language: either can fan one call out to many same-named
# functions. So is an IMPORTS edge at package confidence (a Java/Kotlin
# suffix import, a C# class in any C# file). An edge without a confidence
# (one written before its language resolved, or an import by path) stays in.
_LOAD_INSIGHT_EDGES_CYPHER = (
    "MATCH (a {repo_id: $repo_id})-[r]->(b {repo_id: $repo_id}) "
    "WHERE type(r) IN $types AND NOT a:Repository AND NOT b:Repository "
    "  AND NOT (type(r) = 'CALLS' AND coalesce(r.confidence, '') IN ['name', 'package']) "
    "  AND NOT (type(r) = 'IMPORTS' AND coalesce(r.confidence, '') = 'package') "
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
# MATCH, not MERGE: a refresh finishing after `devgraph remove` deleted the
# repository must not bring its Repository node back.
_WRITE_INSIGHTS_SUMMARY_CYPHER = (
    "MATCH (r:Repository {repo_id: $repo_id}) "
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
#: Level of the per-try retry message. The CLI lowers it to INFO: its user sees
#: the final one-line error, not every try.
RETRY_LOG_LEVEL = logging.WARNING
_BASE_DELAY_S = 0.5


#: How long a fail-fast `GraphEngine.run_read_cypher` waits before its one
#: retry of a transient failure (see `GraphEngine`'s `fail_fast`).
FAIL_FAST_RETRY_PAUSE_S = 0.5


#: How long `GraphEngine.close` waits, by default, for open sessions to end.
CLOSE_WAIT_S = 5.0


class EngineClosed(ServiceUnavailable):
    """A session was requested after `GraphEngine.close` began.

    A `ServiceUnavailable`, so callers already treating the database as
    unavailable handle it; never retried as a transient blip.
    """


class IndexesNotReady(ServiceUnavailable):
    """A query's hinted index was still being built after INDEX_WAIT_S.

    An index Neo4j is still POPULATING (an upgraded database builds the
    lookup indexes in the background) fails a `USING INDEX SEEK` query
    outright, where a missing one only warns. Like `EngineClosed`, the
    indexer lets it fail the whole batch, so the batch is never stamped and
    is retried, rather than skipping the file."""


#: What the indexer lets fail a whole batch or scan instead of one file or
#: pass: a shutdown, or an index still being built. Either way nothing is
#: stamped, and the work is redone.
ABORTS_BATCH = (EngineClosed, IndexesNotReady)

#: How long a query, or `init_schema`, waits for an index still being built.
INDEX_WAIT_S = 300.0
_INDEX_POLL_S = 2.0
_HINT_FAILURE = "Failed to fulfil the hints"


def _is_index_building(exc: Exception) -> bool:
    return isinstance(exc, Neo4jError) and any(
        _HINT_FAILURE in text for text in (str(exc), getattr(exc, "message", None) or "")
    )


def _wait_for_indexes(fn, *args, **kwargs):
    """Run `fn`, re-running it while its hinted index is still being built,
    for up to INDEX_WAIT_S; then raise `IndexesNotReady`."""
    deadline = None
    while True:
        try:
            return fn(*args, **kwargs)
        except Neo4jError as exc:
            if not _is_index_building(exc):
                raise
            now = time.monotonic()
            if deadline is None:
                deadline = now + INDEX_WAIT_S
                logger.warning("a graph index is still being built; waiting up to %.0f s for it", INDEX_WAIT_S)
            if now >= deadline:
                raise IndexesNotReady(f"a graph index was still being built after {INDEX_WAIT_S:.0f} s") from exc
            time.sleep(_INDEX_POLL_S)


class _IndexWaitingSession:
    """A session whose queries wait for an index still being built (see
    `_wait_for_indexes`); everything else is the driver's session."""

    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    def run(self, *args: Any, **kwargs: Any):
        return _wait_for_indexes(self._session.run, *args, **kwargs)

    def execute_write(self, *args: Any, **kwargs: Any):
        return _wait_for_indexes(self._session.execute_write, *args, **kwargs)

    def execute_read(self, *args: Any, **kwargs: Any):
        return _wait_for_indexes(self._session.execute_read, *args, **kwargs)


class _GatedDriver:
    """The driver behind a gate that closes it only once no session is open.

    Closing the neo4j driver under a running query closes that query's
    connection mid-read, which breaks it in its thread (a BufferError). So
    once a close is requested, new sessions are refused with `EngineClosed`
    and the close waits, up to its timeout, for the open ones to end. A query
    still running then is abandoned: the driver is left open under it (the
    process is exiting) rather than closed under it.
    """

    def __init__(self, driver: Driver) -> None:
        self._driver = driver
        self._cond = threading.Condition()
        self._active = 0
        self._closing = False

    def _enter(self) -> None:
        with self._cond:
            if self._closing:
                raise EngineClosed("the graph engine is closed")
            self._active += 1

    def _exit(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    @contextmanager
    def session(self, **kwargs: Any):
        self._enter()
        try:
            with self._driver.session(**kwargs) as session:
                yield _IndexWaitingSession(session)
        finally:
            self._exit()

    def verify_connectivity(self) -> None:
        self._enter()
        try:
            self._driver.verify_connectivity()
        finally:
            self._exit()

    def close(self, timeout: float) -> None:
        started = time.monotonic()
        with self._cond:
            self._closing = True
            if not self._cond.wait_for(lambda: self._active == 0, timeout=timeout):
                logger.warning(
                    "%d graph queries still running %.1f s after the graph engine began closing; abandoning them",
                    self._active,
                    time.monotonic() - started,
                )
                return
        self._driver.close()


def _retry_transient(fn, *args, **kwargs):
    """Run `fn` with bounded exponential-backoff retry on transient Neo4j errors.

    The neo4j driver's `session.execute_write`/`execute_read` already retry
    transient errors internally, but the autocommit `session.run` paths and
    `verify_connectivity` do not — so a Neo4j blip mid-scan would otherwise
    fail the whole operation. A retried statement must be safe to run
    again: the writes are MERGEs, deletes, and schema statements with
    `IF [NOT] EXISTS`, so a re-run leaves the same graph (a delete's
    returned count can come back low if its first run had committed before
    the error). Autocommit
    statements go through `_run`/`_run_records`, which consume the result
    inside the retry.
    """
    delay = _BASE_DELAY_S
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except EngineClosed:
            raise
        except _RETRYABLE_EXCEPTIONS as exc:
            if attempt >= _MAX_RETRIES:
                raise
            logger.log(
                RETRY_LOG_LEVEL,
                "%s on try %d of %d, retrying in %.1fs: %s",
                "Neo4j not answering" if isinstance(exc, ServiceUnavailable) else "transient Neo4j error",
                attempt + 1,
                _MAX_RETRIES + 1,
                delay,
                " ".join(str(exc).split()),
            )
            time.sleep(delay)
            delay *= 2


def _run(session, query: str, **params: Any) -> Any:
    """Run one autocommit statement and consume it, both inside `_retry_transient`;
    return its summary.

    `session.run` returns a lazy result, and the statement executes as that
    result is pulled, so an execution error (a deadlock, a lock timeout, a
    constraint violation) surfaces only when it is consumed. Left unconsumed,
    the session's next `run` raised it inside that statement's retry, which
    re-ran the wrong statement, or the session's close swallowed it into a
    warning, losing the write.
    """
    return _retry_transient(lambda: session.run(query, **params).consume())


def _run_records(session, query: str, **params: Any) -> list:
    """`_run` for a statement whose records the caller reads: they are
    buffered inside the retry, so an execution error is retried or raised."""
    return _retry_transient(lambda: list(session.run(query, **params)))


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
    Within a file a node is also keyed by its `owner` (the types it is
    declared in, "" at the top level), so two classes' same-named methods in
    one file are two nodes; a file-scoped row always writes it, "" when the
    extractor set none.
    """
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for node in nodes:
        properties = node.get("properties") or {}
        file = properties.get("file")
        file_scoped = _is_file_scoped(file)
        if file_scoped:
            properties = {**properties, "owner": properties.get("owner") or ""}
        groups.setdefault((node["label"], file_scoped), []).append(
            {
                "repo_id": node["repo_id"],
                "name": node["name"],
                "file": file,
                "owner": properties.get("owner"),
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


def identity_key(label: str, repo_id: str, name: str, file: str | None, owner: str | None = None) -> str:
    """Stable, server-derived cache key for a node — independent of Neo4j's
    internal elementId(), which is a storage-slot pointer that shifts on any
    full re-index, restore, or delete/recreate and would silently invalidate
    a client-side position cache keyed on it.

    Mirrors the MERGE key `_upsert_nodes_tx` actually writes with: file is
    folded in only when `_is_file_scoped` says this node's MERGE key includes
    it, so the dashboard never has to reimplement that conditional itself
    (and risk it drifting from the Cypher above). The `owner` is folded in
    after it only when it is not "", so a top-level node keeps the key it
    had before owners.
    """
    parts = [label, repo_id, name]
    if _is_file_scoped(file) and file is not None:
        parts.append(file)
        if owner:
            parts.append(owner)
    return _IDENTITY_KEY_SEP.join(parts)


def _pin(file: str | None) -> str:
    """How an edge end matches its node (graph/schema.py `pin_kind`): by bare
    name (`file` None), only the file-less node of that name (`file` "", a
    route's handler stub), the node in that one file, or, for a path pin
    (`a/b/.`, `/a/b/C.java`, `/a/b/.`, `a/b/`), every node of the name in a
    file the pin matches, less the files the row's `exclude` pins match. A
    path pin and "" are meant only for a file-scoped label
    (`FILE_SCOPED_LABELS`) or, for a directory or suffix pin, a Module
    target; other labels never carry `file`, so "" would match every node
    of the name. No file path is `.`, starts with "/" or ends with "/" or
    "/.", so a path pin is never mistaken for a file."""
    return pin_kind(file)


#: The edge types a resolver writes with a pin's `@` flag, whose edges into a
#: node therefore depend on its match keys (see GraphEngine.write_match_keys).
MATCH_KEY_EDGE_TYPES = ("CALLS", "IMPORTS")
#: The most nodes GraphEngine.write_match_keys handles in one transaction.
RETRACT_NODES = 500
#: The pin kinds that match a set of files rather than one (see `_pin`).
_PATH_PINS = frozenset({"dir", "loose_dir", "suffix", "suffix_dir", "prefix"})
#: The path pins whose files all start with the pin's own path (`pin_value`'s
#: `v`): a target seeks them as a prefix range of its file-keyed index.
_ANCHORED_PINS = frozenset({"dir", "loose_dir", "prefix"})


def _group_rels_by_triple(
    rels: list[dict[str, Any]],
) -> dict[tuple[str, str, str, str, str], list[dict[str, Any]]]:
    """Group by (from_label, rel_type, to_label, from pin, to pin); see `_pin`.

    from_file/to_file (see GraphRelationship) are only ever set by a caller
    that knows an endpoint's exact file at extraction time: a code edge's
    source when it is one of the parsed file's own nodes (see
    indexer.common.own_edges), CONTAINS's target, a compose Service. Grouping
    on their presence, not just the label triple, means every other end keeps
    matching by bare name exactly as before — genuinely ambiguous by nature
    (a CALLS target could live anywhere in the repo) rather than a bug to
    paper over.

    Within a group, the edges out of one source with the same properties and
    `origin` (the file that wrote them, None for a writer that doesn't record
    one), and for a source pinned to a file the same `from_owner`, share a
    row, whose `targets` lists each edge's `to_name` and `to_file`; for a
    target pinned to a file or a path also `to_owner`; for a path pin also `pin` (as
    graph/schema.py `pin_value` gives it) and the pins its `exclude` leaves
    out, split into `ex_files`
    (files, a list test) and `ex_pins` (other pins, as `pin_value` gives
    them); for a bare name also `no_self` (the edge may not end at its
    source). Only the keys its group's Cypher reads are sent. The source is
    then matched once for all of them: a resolved call names several
    candidate files per callee.
    """
    groups: dict[tuple[str, str, str, str, str], dict[tuple, dict[str, Any]]] = {}
    for rel in rels:
        from_file = rel.get("from_file")
        to_file = rel.get("to_file")
        key = (rel["from_label"], rel["rel_type"], rel["to_label"], _pin(from_file), _pin(to_file))
        if key[3] in _PATH_PINS:
            raise ValueError(f"a source end can't be pinned to a set of files: {from_file!r}")
        properties = rel.get("properties") or {}
        origin = rel.get("origin")
        from_owner = rel.get("from_owner") if key[3] == "file" else None
        source = (
            rel["repo_id"], rel["from_name"], from_file, from_owner, origin,
            json.dumps(properties, sort_keys=True, default=str),
        )
        row = groups.setdefault(key, {}).setdefault(source, {
            "repo_id": rel["repo_id"],
            "from_name": rel["from_name"],
            "from_file": from_file,
            "from_owner": from_owner,
            "properties": properties,
            "origin": origin,
            "targets": [],
        })
        target = {"to_name": rel["to_name"], "to_file": to_file}
        if key[4] == "file" or key[4] in _PATH_PINS:
            target["to_owner"] = rel.get("to_owner")
        if key[4] in _PATH_PINS:
            # File pins are left out by a plain list test, other pins by `_pin_test`.
            excluded = rel.get("exclude") or []
            target["pin"] = pin_value(to_file)
            target["ex_files"] = [pin for pin in excluded if pin_kind(pin) == "file"]
            target["ex_pins"] = [pin_value(pin) for pin in excluded if pin_kind(pin) != "file"]
        elif key[4] == "name":
            target["no_self"] = bool(rel.get("no_self"))
        row["targets"].append(target)
    return {key: list(rows.values()) for key, rows in groups.items()}


def _upsert_nodes_tx(tx, nodes: list[dict[str, Any]]) -> None:
    for (label, file_scoped), rows in _group_nodes_by_label(nodes).items():
        merge_key = (
            "{repo_id: row.repo_id, name: row.name, file: row.file, owner: row.owner}"
            if file_scoped
            else "{repo_id: row.repo_id, name: row.name}"
        )
        if label == "Module":
            # A Module's directory, its directory's own name and its
            # basename, from its path, for the directory, suffix-directory
            # and suffix pins of an IMPORTS row (see _end_match).
            rows = [
                {**row, "properties": {
                    **row["properties"], "dir": posixpath.dirname(row["name"]),
                    "dir_name": posixpath.basename(posixpath.dirname(row["name"])),
                    "basename": posixpath.basename(row["name"]),
                }}
                for row in rows
            ]
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


# Adds the row's `origin` to the edge's `origins`, kept sorted and without
# repeats, so an edge several files write is the same whatever order they
# write it in. A row without an origin leaves `origins` as it is.
_ADD_ORIGIN = (
    "SET r.origins = CASE "
    "WHEN row.origin IS NULL OR row.origin IN coalesce(r.origins, []) THEN r.origins "
    "ELSE [x IN coalesce(r.origins, []) WHERE x < row.origin] + [row.origin] "
    "   + [x IN coalesce(r.origins, []) WHERE x > row.origin] END"
)


def _pin_test(pin: str, file: str, node: str) -> str:
    """Cypher for graph/schema.py's `pin_value` map `pin` matching the node
    `node` at the path `file`, as devgraph/indexer/common.py `pin_matches`
    defines it (the node's `mkeys` are its keys)."""
    name = f"last(split({file}, '/'))"
    # graph/schema.py pin_root: the part of the path before what the pin matches.
    def cut(length: str) -> str:  # never a negative length, which left() rejects
        return f"left('/' + {file}, CASE WHEN {length} < 0 THEN 0 ELSE {length} END)"

    root = (
        f"CASE {pin}.k WHEN 'suffix' THEN {cut(f'size({file}) + 1 - size({pin}.v)')} "
        f"WHEN 'suffix_dir' THEN {cut(f'size({file}) + 1 - size({name}) - size({pin}.v)')} "
        f"ELSE {file} END"
    )
    return (
        f"(({pin}.ns = '' OR NOT {file} ENDS WITH {pin}.ns) "
        f"AND (size({pin}.os) = 0 OR any(s IN {pin}.os WHERE {file} ENDS WITH s)) "
        f"AND (size({pin}.nv) = 0 OR NOT any(s IN split({root}, '/') WHERE s IN {pin}.nv)) "
        f"AND (size({pin}.nr) = 0 OR NOT any(s IN {pin}.nr WHERE '/' + {root} + '/' CONTAINS s)) "
        f"AND (size({pin}.sc) = 0 OR any(s IN {pin}.sc WHERE s IN coalesce({node}.mkeys, []))) AND CASE {pin}.k "
        f"WHEN 'file' THEN {file} = {pin}.v "
        f"WHEN 'dir' THEN {file} STARTS WITH {pin}.v AND NOT substring({file}, size({pin}.v)) CONTAINS '/' "
        f"WHEN 'suffix' THEN '/' + {file} ENDS WITH {pin}.v "
        f"WHEN 'suffix_dir' THEN '/' + left({file}, size({file}) - size(last(split({file}, '/')))) "
        f"ENDS WITH {pin}.v "
        f"WHEN 'prefix' THEN {file} STARTS WITH {pin}.v ELSE false END)"
    )


def _path_pin_conditions(ref: str, path: str, node: str) -> list[str]:
    """Cypher conditions for a path-pinned target `ref` (see
    `_group_rels_by_triple`) matching the node `node` at the path `path`: its
    `pin` matches and no exclusion does."""
    return [
        _pin_test(f"{ref}.pin", path, node), f"NOT {path} IN {ref}.ex_files",
        f"NOT any(x IN {ref}.ex_pins WHERE {_pin_test('x', path, node)})",
    ]


def _end_match(var: str, label: str, end: str, pin: str, ref: str = "row", also: str | None = None) -> str:
    """The MATCH for one end of an edge row (`pin` from `_pin`), reading its
    `{end}_name`/`{end}_file` off `ref` (the row, or one of its targets). An
    end of a file-scoped label is hinted to seek its index: the (repo_id,
    name, file, owner) unique one when pinned to a file (any owner, `owner
    IS NOT NULL`, unless the row names one), the (repo_id, name) lookup one
    otherwise (a file-less end then keeps only `file IS NULL`). A plan
    made from index statistics sampled while the label was near-empty
    estimates 0 rows, and can then scan a whole index per row (a plain USING
    INDEX allows that scan) after the label has filled; in CI that made a
    5,000-module relink take 20 s instead of under 1 s. A path pin seeks by
    name and keeps the nodes whose file its `pin` matches and no `exclude`
    pin does (`ex_files`, `ex_pins`); a directory or prefix pin seeks the
    (repo_id, name, file, owner) index from its path (`STARTS WITH`), so its cost
    follows the directory's nodes of the name, not the repository's; on a Module target it seeks the
    Module's `dir` (a directory pin), `dir_name` (a suffix-directory pin:
    the last name of its directory) or `basename` (a suffix pin), the row's
    `to_name`, and tests the Module's path. `also` is one more condition on
    the match. An end pinned to a file or a path with an `{end}_owner` keeps
    only the nodes of that `owner` (two classes' same-named methods in one
    file)."""
    conditions = [also] if also else []
    if pin in _PATH_PINS:
        if label == "Module":
            key = {"dir": "dir", "suffix_dir": "dir_name", "suffix": "basename"}.get(pin)
            if key is None:
                raise ValueError(f"a Module target takes only a directory or suffix pin, not {pin!r}")
            path = f"{var}.name"
            match = (
                f"MATCH ({var}:Module {{repo_id: row.repo_id, {key}: {ref}.{end}_name}}) "
                f"USING INDEX SEEK {var}:Module(repo_id, {key}) "
            )
        else:
            path = f"{var}.file"
            match = f"MATCH ({var}:{label} {{repo_id: row.repo_id, name: {ref}.{end}_name}}) "
            if label in FILE_SCOPED_LABELS and pin in _ANCHORED_PINS:
                # The files under one directory are a prefix range of the
                # unique (repo_id, name, file, owner) index: a package's own
                # `Get`, not every `Get` of the repository (S10 fan-in).
                match += f"USING INDEX SEEK {var}:{label}(repo_id, name, file, owner) "
                conditions[:0] = [f"{path} STARTS WITH {ref}.pin.v", f"{var}.owner IS NOT NULL"]
            elif label in FILE_SCOPED_LABELS:
                match += f"USING INDEX SEEK {var}:{label}(repo_id, name) "
            if label in FILE_SCOPED_LABELS:
                # The members of one owner in the files the pin matches, when the row names it.
                conditions.append(f"({ref}.{end}_owner IS NULL OR {var}.owner = {ref}.{end}_owner)")
        conditions[:0] = _path_pin_conditions(ref, path, var)
        return match + "WHERE " + " AND ".join(conditions) + " "
    pinned = pin == "file"
    keys = "repo_id, name, file, owner" if pinned else "repo_id, name"
    hint = f"USING INDEX SEEK {var}:{label}({keys}) " if label in FILE_SCOPED_LABELS else ""
    if pinned:
        match = f"MATCH ({var}:{label} {{repo_id: row.repo_id, name: {ref}.{end}_name, file: {ref}.{end}_file}}) "
        if label in FILE_SCOPED_LABELS:
            # One of the file's same-named siblings, when the row names its
            # owner; every one of them otherwise. A node an index older than
            # owners wrote is left out until the upgrade rescan gives it one.
            conditions[:0] = [
                f"{var}.owner IS NOT NULL", f"({ref}.{end}_owner IS NULL OR {var}.owner = {ref}.{end}_owner)",
            ]
    else:
        match = f"MATCH ({var}:{label} {{repo_id: row.repo_id, name: {ref}.{end}_name}}) "
        if pin == "fileless":
            conditions.insert(0, f"{var}.file IS NULL")
    return match + hint + ("WHERE " + " AND ".join(conditions) + " " if conditions else "")


def _upsert_relationships_tx(tx, rels: list[dict[str, Any]]) -> None:
    for (from_label, rel_type, to_label, from_pin, to_pin), rows in _group_rels_by_triple(rels).items():
        tx.run(
            "UNWIND $rows AS row "
            + _end_match("a", from_label, "from", from_pin)
            + "UNWIND row.targets AS t "
            + _end_match("b", to_label, "to", to_pin, "t", also="(NOT t.no_self OR a <> b)" if to_pin == "name" else None)
            + f"MERGE (a)-[r:{rel_type}]->(b) "
            "SET r += row.properties " + _ADD_ORIGIN,
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
    tx,
    repo_id: str,
    file_name: str,
    nodes: list[dict[str, Any]],
    rels: list[dict[str, Any]],
    service_api: bool = False,
) -> None:
    # Unclaim the file from the edges out of the nodes it owns (G2 set (a)),
    # and delete only the file-scoped nodes (Class/Function, keyed on `file`)
    # whose symbol is no longer in the file's current extraction. The Module
    # node (keyed on `source_file`) and any surviving Class/Function nodes
    # are MERGEd in place below, so their incoming edges — MODIFIES from git
    # history, MENTIONS from docs, cross-file CALLS/IMPORTS — are preserved.
    # A blanket DETACH DELETE of every node with this file's provenance (the
    # old behavior) destroyed those edges on every reindex, which is why
    # git-history MODIFIES edges silently vanished for any file re-indexed
    # after history was synced.
    keep = [
        [node["label"], node["name"], (node.get("properties") or {}).get("owner") or ""]
        for node in nodes
        if node["label"] != "Module"
        and (
            (node.get("properties") or {}).get("file") == file_name
            or (node.get("properties") or {}).get("source_file") == file_name
        )
    ]
    code = any(node["label"] == "Module" and node["name"] == file_name for node in nodes)
    recorded = _recorded_claims(tx, repo_id, file_name, code)
    _unclaim_foreign_sources_tx(tx, repo_id, file_name)
    tx.run(_REPLACE_OWNED_NODES_CYPHER, repo_id=repo_id, f=file_name, keep=keep)
    if service_api:
        _unclaim_service_api_edges_tx(tx, repo_id, file_name, recorded)
    # Shared nodes the file still claims are re-claimed in place by the
    # upsert below, not deleted and recreated, so a single-claimant
    # Container or Datastore keeps its incoming edges (MENTIONS, USES).
    claimed = [
        [node["label"], node["name"]]
        for node in nodes
        if not (node.get("properties") or {}).get("file")
        and (node.get("properties") or {}).get("source") == file_name
    ]
    _unclaim_source_tx(tx, repo_id, file_name, claimed, recorded)
    # The file's Module records what it claims now, for its next write.
    record = sorted({SHARED_CLAIM_SEP.join(pair) for pair in claimed})
    nodes = [
        {**node, "properties": {**(node.get("properties") or {}), "shared_claims": record}}
        if node["label"] == "Module" and node["name"] == file_name else node
        for node in nodes
    ]
    _upsert_nodes_tx(tx, nodes)
    _upsert_relationships_tx(tx, rels)


def _replace_doc_note_tx(
    tx, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
) -> None:
    tx.run(_UNCLAIM_DOC_NOTE_EDGES_CYPHER, repo_id=repo_id, f=file_name)
    _upsert_nodes_tx(tx, nodes)
    _upsert_relationships_tx(tx, rels)


def _replace_mentions_tx(
    tx, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
) -> None:
    tx.run(
        "MATCH (:Document {repo_id: $repo_id, name: $f})-[r:MENTIONS]->() DELETE r",
        repo_id=repo_id,
        f=file_name,
    )
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
    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        fail_fast: bool = False,
        connection_timeout_s: float | None = None,
    ) -> None:
        """Omitted, both keep the driver's defaults: a managed transaction is
        retried for up to 30 s, and getting a connection may take 30-60 s.

        `connection_timeout_s` bounds getting a connection: opening it, the Bolt
        handshake, or waiting for a pooled one. `fail_fast` turns the driver's
        retries off; `run_read_cypher` then retries a read once,
        `FAIL_FAST_RETRY_PAUSE_S` later, when it failed once connected (a
        transient error, an expired session, the connection lost mid-query),
        and never when no connection could be had at all (refused, timed out).
        The MCP server sets both, so a request fails within a few seconds when
        Neo4j is down."""
        self.uri = uri
        self._fail_fast = fail_fast
        limits: dict[str, float] = {}
        if fail_fast:
            # Negative: the driver retries while `elapsed > limit` is false, and
            # a coarse clock can make the elapsed time exactly 0.
            limits["max_transaction_retry_time"] = -1
        if connection_timeout_s is not None:
            limits["connection_timeout"] = connection_timeout_s
            limits["connection_acquisition_timeout"] = connection_timeout_s
        # Hint notifications are off: a hinted index an older database hasn't
        # built yet (init_schema creates it) would log one per query.
        self._driver = _GatedDriver(GraphDatabase.driver(
            uri, auth=(user, password),
            notifications_disabled_classifications=[NotificationDisabledClassification.HINT],
            **limits,
        ))

    def close(self, timeout: float = CLOSE_WAIT_S) -> None:
        """Refuse new sessions, wait up to `timeout` for open ones, then close
        the driver (see `_GatedDriver`). Safe to call again after a timeout."""
        self._driver.close(timeout)

    def verify_connectivity(self) -> None:
        _retry_transient(self._driver.verify_connectivity)

    def init_schema(self, effective: EffectiveSchema | None = None, wait: bool = True) -> None:
        """Provision the built-in constraints, plus a repository's declared ones.

        Idempotent (every statement is `IF NOT EXISTS`/`IF EXISTS` guarded), so
        re-running it on an already-provisioned database is a no-op. Callers
        that aren't scoped to one repository omit `effective` and get exactly
        the built-in statements. Statements always come from
        `repository_constraint_statements`, never from
        `EffectiveSchema.constraint_statements()` directly. The built-in
        lookup indexes follow, and it returns once every index is ONLINE (up
        to INDEX_WAIT_S): an upgraded database builds new ones in the
        background, and a query hinted to seek one fails until it is built.
        `wait=False` returns at once (the MCP server, whose client's handshake
        would time out first); each query still waits for its index then.
        """
        with self._driver.session() as session:
            for stmt in repository_constraint_statements(effective) + lookup_index_statements():
                _run(session, stmt)
            if not wait:
                return
            building = [
                record["name"] for record in _run_records(
                    session, "SHOW INDEXES YIELD name, state WHERE state <> 'ONLINE' RETURN name"
                )
            ]
            if building:
                logger.info(
                    "Building %d graph indexes (%s); indexing waits up to %.0f s for them",
                    len(building), ", ".join(sorted(building)), INDEX_WAIT_S,
                )
            try:
                _run(session, "CALL db.awaitIndexes($timeout)", timeout=int(INDEX_WAIT_S))
            except Neo4jError:
                # Still building (or failed): each hinted query waits on its own.
                logger.warning("graph indexes not online after %.0f s; queries wait for them", INDEX_WAIT_S, exc_info=True)

    def upsert_repository(self, repo_id: str, name: str, path: str) -> None:
        with self._driver.session() as session:
            _run(
                session,
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
            _run(
                session,
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
        `to_name`/`repo_id` (`properties`, `from_file`/`to_file`, `exclude`,
        the pins whose files a path-pinned `to_file` leaves out (see `_pin`), and
        `origin`, the writing file added to the edge's `origins`, optional). Grouped by
        `(from_label, rel_type, to_label)` — same reasoning as `upsert_nodes`,
        since label/rel-type can't be parameterized. An edge whose endpoint
        doesn't exist yet is silently skipped, same as `upsert_relationship`.
        """
        if not rels:
            return
        with self._driver.session() as session:
            session.execute_write(_upsert_relationships_tx, rels)

    def replace_file_nodes(
        self,
        repo_id: str,
        file_name: str,
        nodes: list[dict[str, Any]],
        rels: list[dict[str, Any]],
        service_api: bool = False,
    ) -> None:
        """Atomically replace one file's provenance-tagged nodes: delete the
        old ones and upsert the new nodes/rels in a single transaction.

        Unlike calling `delete_nodes_by_source_file` followed by
        `upsert_nodes`/`upsert_relationships` separately, a reader can never
        observe the file's nodes as gone-but-not-yet-rebuilt — under
        read-committed isolation it sees either the pre-reindex state or the
        fully-rebuilt state, never in between.

        `service_api` (a file of SERVICE_API_SUFFIXES) also unclaims the file from the
        owning-service USES and the Endpoint CALLS/IMPLEMENTS edges it wrote,
        which `nodes`/`rels` and index_paths' service pass write again. The
        file is always unclaimed from the edges it wrote out of other files'
        nodes, found through its Module's old `name_ref_sources`.
        """
        with self._driver.session() as session:
            session.execute_write(_replace_file_nodes_tx, repo_id, file_name, nodes, rels, service_api)

    def replace_doc_note(
        self, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
    ) -> None:
        """Re-write one docs note file in one transaction: unclaim the file
        from the edges into and out of the nodes it wrote (`source_file`),
        deleting those it was the last writer of, then upsert `nodes` and
        `rels`. A link the note dropped therefore goes; an edge another
        writer also wrote stays."""
        with self._driver.session() as session:
            session.execute_write(_replace_doc_note_tx, repo_id, file_name, nodes, rels)

    def replace_mentions(
        self, repo_id: str, file_name: str, nodes: list[dict[str, Any]], rels: list[dict[str, Any]]
    ) -> None:
        """Re-write one Markdown file's Document in one transaction: delete
        its MENTIONS edges, then upsert `nodes` and `rels`. A mention the
        file dropped therefore goes. `replace_file_nodes` isn't used: it
        would delete a docs note at the same path as stale."""
        with self._driver.session() as session:
            session.execute_write(_replace_mentions_tx, repo_id, file_name, nodes, rels)

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
        nodes = self._provider_nodes(repo_id)
        if nodes is None:
            return
        with self._driver.session() as session:
            _run(
                session, nodes + _DELETE_EXTRACTED_PATHS_CYPHER, repo_id=repo_id, extractor=extractor, paths=paths
            )

    def extracted_nodes_at(self, repo_id: str, extractor: str, paths: list[str]) -> set[tuple[str, str]]:
        """(label, name) of one provider's nodes at these repo-relative paths, or below them --
        the nodes delete_extracted_nodes would delete."""
        if not paths:
            return set()
        nodes = self._provider_nodes(repo_id)
        if nodes is None:
            return set()
        with self._driver.session() as session:
            result = _run_records(
                session, nodes + _EXTRACTED_NODES_AT_CYPHER, repo_id=repo_id, extractor=extractor, paths=paths
            )
            return {(record["label"], record["name"]) for record in result or []}

    def extracted_entries(self, repo_id: str, extractor: str, labels: list[str]) -> set[tuple[str, str, str]]:
        """(label, name, path) of every node one provider wrote under these labels in a repo."""
        if not labels:
            return set()
        nodes = self._provider_nodes(repo_id, labels)
        if nodes is None:
            return set()
        with self._driver.session() as session:
            result = _run_records(
                session,
                nodes + "WITH n WHERE n.extractor = $extractor AND labels(n)[0] IN $labels "
                "RETURN labels(n)[0] AS label, n.name AS name, n.path AS path",
                repo_id=repo_id, extractor=extractor, labels=labels,
            )
            return {(record["label"], record["name"], record["path"]) for record in result or []}

    def list_extracted_paths(self, repo_id: str, extractor: str, labels: list[str] | None) -> set[str]:
        """The `path` of every node one provider wrote in a repo, only under
        these labels when `labels` is given: the provider's side of the graph's
        file list (see dispatch._graph_files)."""
        nodes = self._provider_nodes(repo_id, labels or ())
        if nodes is None:
            return set()
        with self._driver.session() as session:
            result = _run_records(
                session,
                nodes + "WITH n WHERE n.extractor = $extractor "
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
        nodes = self._provider_nodes(repo_id, [entry.split(":", 1)[0] for entry in keep])
        if nodes is None:
            return 0
        with self._driver.session() as session:
            result = _run_records(
                session, nodes + _PRUNE_EXTRACTED_CYPHER, repo_id=repo_id, extractor=extractor, keep=keep
            )
            records = [record.data() for record in result or []]
        return records[0]["pruned"] if records else 0

    def prune_extracted_at(self, repo_id: str, extractor: str, paths: list[str], keep: list[str]) -> int:
        """Delete one provider's nodes at exactly these paths, except `keep` ("Label:name")."""
        if not paths:
            return 0
        nodes = self._provider_nodes(repo_id, [entry.split(":", 1)[0] for entry in keep])
        if nodes is None:
            return 0
        with self._driver.session() as session:
            result = _run_records(
                session, nodes + _PRUNE_EXTRACTED_AT_CYPHER,
                repo_id=repo_id, extractor=extractor, paths=paths, keep=keep,
            )
            records = [record.data() for record in result or []]
        return records[0]["pruned"] if records else 0

    def delete_extracted_edges(self, repo_id: str, extractor: str, paths: list[str] | None) -> int:
        """Delete the outgoing non-built-in edges of one provider's nodes at these paths,
        or across the whole repo when `paths` is None."""
        if paths is not None and not paths:
            return 0
        nodes = self._provider_nodes(repo_id)
        if nodes is None:
            return 0
        with self._driver.session() as session:
            result = _run_records(
                session, nodes + _DELETE_EXTRACTED_EDGES_CYPHER, repo_id=repo_id, extractor=extractor,
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

        nodes = self._provider_nodes(repo_id, [label])
        if nodes is None:
            return []
        params = {"repo_id": repo_id, "extractor": extractor, "label": label}
        with self._driver.session() as session:
            result = _run_records(session, nodes + _EXTRACTED_PROPERTY_KEYS_CYPHER, **params)
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
                _run(session, nodes + _EXTRACTED_OF_LABEL + f"REMOVE {removals}", **params)
        return stale

    def read_applied_schema(self, repo_id: str) -> dict[str, Any] | None:
        """The schema state the repo's graph was last built with, or None."""
        with self._driver.session() as session:
            result = _run_records(session, _READ_APPLIED_SCHEMA_CYPHER, repo_id=repo_id)
            records = [record.data() for record in result or []]
        return records[0] if records else None

    def set_index_format(self, repo_id: str, version: int) -> None:
        """Stamp the graph index format the repository was last fully scanned with."""
        with self._driver.session() as session:
            _run(
                session,
                "MERGE (r:Repository {repo_id: $repo_id}) SET r.index_format = $version",
                repo_id=repo_id, version=version,
            )

    def read_resolver_config(self, repo_id: str) -> dict[str, dict]:
        """The resolver configuration fingerprints the repository's graph was
        built with, per language (see indexer/resolver_config.fingerprints);
        empty when none is recorded."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (r:Repository {repo_id: $repo_id}) RETURN r.resolver_config AS config",
                repo_id=repo_id,
            )
            records = [record["config"] for record in result or []]
        return json.loads(records[0]) if records and records[0] else {}

    def write_resolver_config(self, repo_id: str, config: dict[str, dict]) -> None:
        """Record the resolver configuration fingerprints the graph was built with."""
        with self._driver.session() as session:
            _run(
                session,
                "MATCH (r:Repository {repo_id: $repo_id}) SET r.resolver_config = $config",
                repo_id=repo_id, config=json.dumps(config, sort_keys=True),
            )

    def read_skipped_files(self, repo_id: str) -> dict[str, list]:
        """The files the indexer left out of extraction, by repo-relative path:
        [reason, the size limit it was judged under, the file's change stamp]
        (see dispatch.index_paths). Empty when none are recorded."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (r:Repository {repo_id: $repo_id}) RETURN r.skipped_files AS skipped",
                repo_id=repo_id,
            )
            records = [record.data() for record in result or []]
        raw = records[0]["skipped"] if records else None
        return json.loads(raw) if raw else {}

    def update_skipped_files(
        self, repo_id: str, add: dict[str, list] | None = None, drop: Iterable[str] = (), replace: bool = False
    ) -> None:
        """Add `read_skipped_files` entries and drop the ones at `drop`, or,
        with `replace`, set exactly `add`. Written only when something changes."""
        before = {} if replace else self.read_skipped_files(repo_id)
        dropped = set(drop)
        after = {path: entry for path, entry in before.items() if path not in dropped}
        after.update(add or {})
        if after == before and not replace:
            return
        with self._driver.session() as session:
            _run(
                session,
                "MERGE (r:Repository {repo_id: $repo_id}) SET r.skipped_files = $skipped",
                repo_id=repo_id, skipped=json.dumps(after, sort_keys=True) if after else None,
            )

    def index_format(self, repo_id: str) -> int | None:
        """The index format stamped by the last full scan, or None for an older index."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (r:Repository {repo_id: $repo_id}) RETURN r.index_format AS format",
                repo_id=repo_id,
            )
            records = [record.data() for record in result or []]
        return records[0]["format"] if records else None

    def read_all_applied_schemas(self) -> list[dict[str, Any]]:
        """Every repository's recorded user labels and keys, from the graph itself."""
        with self._driver.session() as session:
            result = _run_records(session, _READ_ALL_APPLIED_SCHEMAS_CYPHER)
            return [record.data() for record in result or []]

    def record_schema_labels(self, repo_id: str, labels: list[str]) -> None:
        """Record the user labels the repository's graph may hold, ahead of a
        schema apply that writes them (see dispatch._apply_project_schema)."""
        with self._driver.session() as session:
            _run(
                session,
                "MERGE (r:Repository {repo_id: $repo_id}) SET r.schema_labels = $labels",
                repo_id=repo_id, labels=labels,
            )

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
            _run(
                session, _RECORD_APPLIED_SCHEMA_CYPHER, repo_id=repo_id, hash=schema_hash,
                labels=labels, relationship_types=relationship_types, keys=keys or [],
            )

    def list_schema_objects(self) -> list[dict[str, Any]]:
        """Every constraint and every index no constraint owns, each tagged with `kind`."""
        with self._driver.session() as session:
            constraints = [
                {**record.data(), "kind": "constraint"}
                for record in _run_records(session, _SHOW_CONSTRAINTS_CYPHER) or []
            ]
            indexes = [
                {**record.data(), "kind": "index"}
                for record in _run_records(session, _SHOW_INDEXES_CYPHER) or []
            ]
        return constraints + indexes

    def run_schema_statement(self, statement: str) -> None:
        """Run one CREATE/DROP CONSTRAINT/INDEX statement. The caller builds it from validated names."""
        with self._driver.session() as session:
            _run(session, statement)

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
            result = _run_records(session, query)
            return bool([record for record in result or []])

    def label_has_nodes(self, label: str) -> bool:
        """Whether any node, in any repository, carries `label`. The caller validates `label`."""
        with self._driver.session() as session:
            result = _run_records(session, f"MATCH (n:`{label}`) RETURN n LIMIT 1")
            return bool([record for record in result or []])

    def existing_node_names(self, repo_id: str, label: str, names: list[str]) -> set[str]:
        """Which of `names` one repo's `label` nodes carry. The caller validates `label`."""
        if not names:
            return set()
        with self._driver.session() as session:
            result = _run_records(
                session,
                f"MATCH (n:`{label}` {{repo_id: $repo_id}}) WHERE n.name IN $names RETURN DISTINCT n.name AS name",
                repo_id=repo_id,
                names=names,
            )
            return {record["name"] for record in result or []}

    def delete_label_nodes(self, repo_id: str, label: str) -> int:
        """Delete one repo's nodes of a user label. The caller validates `label`."""
        with self._driver.session() as session:
            result = _run_records(
                session, f"MATCH (n:`{label}` {{repo_id: $repo_id}}) DETACH DELETE n RETURN count(n) AS n",
                repo_id=repo_id,
            )
            records = [record.data() for record in result or []]
        return records[0]["n"] if records else 0

    def delete_relationship_type(self, repo_id: str, rel_type: str) -> int:
        """Delete one repo's relationships of a user type. The caller validates `rel_type`."""
        with self._driver.session() as session:
            result = _run_records(
                session,
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
        are excluded, and so are `source`-keyed shared nodes
        (Container/Datastore/Endpoint, co-produced by several files): their
        files come from `list_claim_sources`. Sought label by label through
        the provenance indexes (see `_owned_branches`).
        """
        with self._driver.session() as session:
            result = _run_records(
                session,
                _union(_owned_branches("IS NOT NULL"))
                + "RETURN DISTINCT coalesce(n.source_file, n.file, n.name) AS path",
                repo_id=repo_id,
            )
            records = result or []
            return {record["path"] for record in records if record["path"]}

    def list_claim_sources(self, repo_id: str) -> set[str]:
        """Every repo-relative file claiming a shared node (`source`/`sources`)
        in this repo: the files, such as a Dockerfile, that may back nothing
        but a claim. Provider nodes are left out."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                _union(_CLAIMED_BRANCHES)
                + "WITH n WHERE n.file IS NULL AND n.source_file IS NULL AND n.extractor IS NULL "
                "  AND (n.sources IS NOT NULL OR n.source IS NOT NULL) "
                "UNWIND coalesce(n.sources, [n.source]) AS source "
                "RETURN DISTINCT source",
                repo_id=repo_id,
            )
            return {record["source"] for record in result or [] if record["source"]}

    def list_shared_claims(self, repo_id: str, files: list[str]) -> dict[tuple[str, str], set[str]]:
        """(label, name) -> every claiming file (`sources`) of each shared node
        one of `files` claims. index_paths reads it before and after a batch
        to tell a shared node the batch may have deleted and written back."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                _union(_CLAIMED_BRANCHES)
                + "WITH n, coalesce(n.sources, [n.source]) AS sources "
                "WHERE n.file IS NULL AND n.source_file IS NULL AND any(s IN sources WHERE s IN $files) "
                "RETURN labels(n)[0] AS label, n.name AS name, sources",
                repo_id=repo_id,
                files=files,
            )
            return {(record["label"], record["name"]): set(record["sources"]) for record in result or []}

    def delete_bare_modules(self, repo_id: str) -> int:
        """Delete this repo's `Module` nodes that have no file key at all (no
        `source_file`, `file` or `path`), with their edges, and return how
        many went. Every extractor writes `source_file` on its Module, so
        only a leftover of the git-history sync's old MERGE looks like this
        (a commit touching a README, an image or a since-deleted file)."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (m:Module {repo_id: $repo_id}) "
                "WHERE m.source_file IS NULL AND m.file IS NULL AND m.path IS NULL "
                "DETACH DELETE m RETURN count(m) AS n",
                repo_id=repo_id,
            )
            record = result[0] if result else None
            return record["n"] if record else 0

    def delete_bare_class_stubs(self, repo_id: str) -> int:
        """Delete this repo's `Class` nodes with no `file`, `source` or
        `source_file`, with their edges, and return how many went. Every
        extractor writes `file` on a Class, and a C++ out-of-class method's
        stub has carried `source` since index format 9, so only a stub an
        older index wrote looks like this; with no claim, nothing would ever
        delete it."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (c:Class {repo_id: $repo_id}) "
                "WHERE c.file IS NULL AND c.source IS NULL AND c.source_file IS NULL "
                "DETACH DELETE c RETURN count(c) AS n",
                repo_id=repo_id,
            )
            record = result[0] if result else None
            return record["n"] if record else 0

    def delete_docs_notes_outside(self, repo_id: str, folder: str) -> int:
        """Delete this repo's docs notes (Requirement, DesignDecision,
        ArchitectureNote) whose `source_file` is not under `folder` (a
        repo-relative POSIX path, not the root), with their edges, and return
        how many went. Notes under the docs path record their repo-relative
        path, so only a note an older scan keyed by its bare filename, or one
        left from a former docs path, looks like this."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (n {repo_id: $repo_id}) "
                "WHERE (n:Requirement OR n:DesignDecision OR n:ArchitectureNote) "
                "  AND n.source_file IS NOT NULL AND NOT n.source_file STARTS WITH $prefix "
                "DETACH DELETE n RETURN count(n) AS n",
                repo_id=repo_id,
                prefix=folder.rstrip("/") + "/",
            )
            record = result[0] if result else None
            return record["n"] if record else 0

    def list_docs_note_files(self, repo_id: str) -> set[str]:
        """The `source_file` of every docs note (Requirement, DesignDecision,
        ArchitectureNote) in this repo."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (n {repo_id: $repo_id}) "
                "WHERE (n:Requirement OR n:DesignDecision OR n:ArchitectureNote) AND n.source_file IS NOT NULL "
                "RETURN DISTINCT n.source_file AS path",
                repo_id=repo_id,
            )
            return {record["path"] for record in result}

    def _schema_labels(self, repo_id: str) -> list[str]:
        """The user labels the repository's applied schema records: the only
        labels a provider node of the repository can carry (applying a
        schema deletes the nodes of a label it drops). Re-validated, since
        they are interpolated."""
        from devgraph.config.project_schema import LABEL_PATTERN  # runtime import: see TYPE_CHECKING above

        applied = self.read_applied_schema(repo_id) or {}
        return [
            label for label in applied.get("labels") or []
            if label not in NODE_LABELS and LABEL_PATTERN.fullmatch(label or "")
        ]

    def _user_labels_in_use(self) -> list[str]:
        """Every label the database knows (`CALL db.labels()`) that is no
        built-in one: a provider node can carry a label no schema records any
        more (written before a crash cut its apply short, then dropped from
        the schema), and a console-made node any label at all. Validated,
        since they are interpolated. User labels are few, and each is matched
        by its own label scan."""
        from devgraph.config.project_schema import LABEL_PATTERN  # runtime import: see TYPE_CHECKING above

        with self._driver.session() as session:
            result = _run_records(session, "CALL db.labels() YIELD label RETURN label")
        return [
            record["label"] for record in result or []
            if record["label"] not in NAMED_LABELS and record["label"] != "Repository"
            and LABEL_PATTERN.fullmatch(record["label"] or "")
        ]

    def _provider_nodes(self, repo_id: str, labels: Iterable[str] = ()) -> str | None:
        """The repository's nodes of every label a provider node can carry, as
        a `_union` yielding `n`, each label matched on its own: its applied
        schema's labels, `labels` (one being written before the schema is
        recorded) and every other non-built-in label in use, which catches
        nodes of a label no record names any more. None when there are no
        such labels, so there is nothing to match. Labels that aren't a valid
        user label are dropped, since they are interpolated."""
        from devgraph.config.project_schema import LABEL_PATTERN  # runtime import: see TYPE_CHECKING above

        extra = [label for label in labels if label not in NODE_LABELS and LABEL_PATTERN.fullmatch(label or "")]
        names = sorted(set(self._schema_labels(repo_id)) | set(extra) | set(self._user_labels_in_use()))
        if not names:
            return None
        return _union(f"MATCH (n:{label}) WHERE n.repo_id = $repo_id RETURN n" for label in names)

    def repository_nodes(self, repo_ids: list[str]) -> str:
        """Every node of these repositories (the query's `$repo_ids`) as a
        `_union` yielding `n`: each named label through its `(repo_id, name)`
        index, the Repository nodes, and every other label in use (their
        applied schemas' and any a node carries without a record, see
        `_user_labels_in_use`), each by its own label scan. For
        whole-repository reads (the dashboard's counts), which matched
        `{repo_id}` alone and so scanned every node in the database.

        A built-in node without a `name` is left out: the `(repo_id, name)`
        index doesn't hold it, and finding one would scan every node of its
        label. Only a console or manual write makes one; the extractors key
        every node on its name."""
        user = sorted(
            {label for repo_id in repo_ids for label in self._schema_labels(repo_id)} | set(self._user_labels_in_use())
        )
        return _union(
            [f"MATCH (n:{label}) WHERE n.repo_id IN $repo_ids AND n.name IS NOT NULL RETURN n" for label in NAMED_LABELS]
            + ["MATCH (n:Repository) WHERE n.repo_id IN $repo_ids RETURN n"]
            + [f"MATCH (n:{label}) WHERE n.repo_id IN $repo_ids RETURN n" for label in user]
        )

    def entity_names(self, repo_id: str, names: list[str] | None) -> list[tuple[str, str]]:
        """(name, label) of every named node of the repository, or only of
        those named one of `names`: the mentions extractor's known entities.
        Each label is matched on its own (built-in ones through their
        `(repo_id, name)` index), not on `{repo_id}` alone, which scanned
        every node in the database."""
        test = "IN $names" if names is not None else "IS NOT NULL"
        query = _union(
            [_seek(label, "name", test) for label in NAMED_LABELS]
            + [f"MATCH (n:Repository {{repo_id: $repo_id}}) WHERE n.name {test} RETURN n"]
            + [
                f"MATCH (n:{label}) WHERE n.repo_id = $repo_id AND n.name {test} RETURN n"
                for label in self._schema_labels(repo_id)
            ]
        ) + "RETURN DISTINCT n.name AS name, labels(n)[0] AS label"
        records = self.run_cypher(query, {"repo_id": repo_id, "names": names})
        return [(record["name"], record["label"]) for record in records]

    def list_file_nodes(
        self, repo_id: str, files: list[str],
    ) -> set[tuple[str, str, str, str, tuple[str, ...] | None, bool]]:
        """Return (label, name, file, owner, mkeys, stale) for every node whose file provenance
        (`source_file`/`file`, a `Module` named by its path, or a schema
        provider's node, which its `path` owns) is one of `files`. `file` is
        that provenance: the Module's name, else `file`, `source_file` or
        `path`, in that order. A shared node one of `files` claims (in its
        `sources`: a `Container`, `Datastore`, `Endpoint` or handler stub) is
        returned with `file` "", besides any row for its own provenance.
        `owner` is a code symbol's (graph/schema.py FILE_SCOPED_LABELS), ""
        for any node without a `file`, and None for one an index older than
        owners wrote: the batch then counts the node it writes as added, so
        other files' edges into it are relinked (a file that failed during
        the upgrade rescan, indexed again). `mkeys` is the node's match keys,
        sorted (None when it has none), and `stale` whether a batch that
        changed them has yet to finish relinking its edges (see
        write_match_keys).

        index_paths snapshots this before re-indexing a batch so it can tell
        which nodes the batch *adds* -- only those can be the missing
        target of an edge from a file outside the batch. A node moving
        between two of the batch's files counts as added. It also tells
        which kept nodes' match keys change (see write_match_keys).

        Each label is matched on its own: the built-in ones through their
        provenance indexes, a provider's through the labels the repository's
        applied schema records (provider nodes exist only for those).
        """
        candidates = _union(
            _owned_branches("IN $files")
            + _CLAIMED_BRANCHES
            + [
                f"MATCH (n:{label}) WHERE n.repo_id = $repo_id AND n.extractor IS NOT NULL "
                "AND n.path IN $files RETURN n"
                for label in self._schema_labels(repo_id)
            ]
        )
        with self._driver.session() as session:
            result = _run_records(
                session,
                candidates
                + "WITH n, (n.source_file IN $files OR n.file IN $files "
                "   OR (n:Module AND n.name IN $files) "
                "   OR (n.extractor IS NOT NULL AND n.path IN $files)) AS owned, "
                "   any(s IN coalesce(n.sources, []) WHERE s IN $files) AS claimed "
                "WHERE owned OR claimed "
                "RETURN DISTINCT labels(n)[0] AS label, n.name AS name, owned, claimed, "
                "CASE WHEN n:Module THEN n.name ELSE coalesce(n.file, n.source_file, n.path) END AS file, "
                "CASE WHEN n.file IS NULL THEN '' ELSE n.owner END AS owner, "
                "n.mkeys AS mkeys, coalesce(n.mkeys_stale, false) AS stale",
                repo_id=repo_id,
                files=files,
            )
            records = [(r, None if r["mkeys"] is None else tuple(sorted(set(r["mkeys"])))) for r in result or []]
            return {
                (r["label"], r["name"], r["file"], r["owner"], keys, r["stale"]) for r, keys in records if r["owned"]
            } | {(r["label"], r["name"], "", "", keys, r["stale"]) for r, keys in records if r["claimed"]}

    def write_match_keys(
        self, repo_id: str, nodes: list[tuple[str, str, str, str, tuple[str, ...] | None, bool]],
    ) -> None:
        """Write these (label, name, file, owner, mkeys, retract) nodes' match keys
        (`mkeys`; None removes them), which a batch's own node writes leave
        as they were until now, so a pin's `@` flag (graph/schema.py
        `split_pin`) only ever sees one node's old keys with its old edges.
        A node to `retract` loses its CALLS and IMPORTS edges, which those
        pins linked under its old keys, in the same transaction, and stays
        `mkeys_stale` until settle_match_keys: the batch writes them again
        under the new keys, the way it writes an added node's, and a batch
        that fails before then finds it stale and does it again. Each
        label's (repo_id, name, file, owner) index is hinted to seek, for the reason
        _end_match gives, and RETRACT_NODES nodes go per transaction, so a
        format upgrade that rekeys a whole repository never holds one
        transaction over all of it. Every other edge stays."""
        self._per_label_chunk(
            repo_id, [((label, name, file, owner), {"mkeys": list(keys) if keys is not None else None,
                                                    "retract": retract})
                      for label, name, file, owner, keys, retract in nodes],
            "SET b.mkeys = row.mkeys, b.mkeys_stale = CASE WHEN row.retract THEN true END "
            f"WITH b, row WHERE row.retract MATCH ()-[r:{'|'.join(MATCH_KEY_EDGE_TYPES)}]->(b) DELETE r",
        )

    def settle_match_keys(self, repo_id: str, nodes: list[tuple[str, str, str, str]]) -> None:
        """Clear `mkeys_stale` (see write_match_keys) off these (label, name,
        file, owner) nodes once their edges are written again."""
        self._per_label_chunk(repo_id, [(node, {}) for node in nodes], "REMOVE b.mkeys_stale")

    def _per_label_chunk(
        self, repo_id: str, nodes: list[tuple[tuple[str, str, str, str], dict]], update: str
    ) -> None:
        """Run `update` on each (label, name, file, owner) node `b`, with its own
        `row` fields, RETRACT_NODES nodes per transaction (see
        write_match_keys), each consumed inside its retry (`_run`), so a
        chunk that fails, the last one included, raises."""
        groups: dict[str, list[dict[str, Any]]] = {}
        for (label, name, file, owner), fields in nodes:
            groups.setdefault(label, []).append({"name": name, "file": file, "owner": owner, **fields})
        with self._driver.session() as session:
            for label, rows in sorted(groups.items()):
                for start in range(0, len(rows), RETRACT_NODES):
                    _run(
                        session,
                        f"UNWIND $rows AS row MATCH (b:{label} "
                        "{repo_id: $repo_id, name: row.name, file: row.file, owner: row.owner}) "
                        f"USING INDEX SEEK b:{label}(repo_id, name, file, owner) " + update,
                        repo_id=repo_id,
                        rows=rows[start:start + RETRACT_NODES],
                    )

    def find_name_refs(self, repo_id: str, names: list[str], skip: list[str]) -> list[tuple[str, list[str]]]:
        """(Module name, entries) for every Module outside `skip` whose
        `name_refs` (see indexer/common.py `name_ref_properties`) target, or
        come from an unpinned source, named one of `names`, with just those
        entries. The short `name_ref_targets`/`name_ref_sources` lists filter
        the Modules before any entry is split. The Module index is hinted to
        seek, for the reason _end_match gives."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (m:Module {repo_id: $repo_id}) USING INDEX SEEK m:Module(repo_id, name) "
                "WHERE NOT m.name IN $skip "
                "  AND (any(t IN m.name_ref_targets WHERE t IN $names) "
                "       OR any(s IN m.name_ref_sources WHERE s IN $names)) "
                "RETURN m.name AS origin, "
                "       [e IN m.name_refs WHERE split(e, $sep)[5] IN $names OR split(e, $sep)[2] IN $names] AS refs",
                repo_id=repo_id,
                names=names,
                skip=skip,
                sep=NAME_REF_SEP,
            )
            records = result or []
            return [(record["origin"], list(record["refs"])) for record in records]

    def find_mentioning_documents(
        self, repo_id: str, pairs: list[tuple[str, str]], batch_keys: list[str]
    ) -> dict[tuple[str, str], set[str]]:
        """For each (label, name) in `pairs` that some node outside the batch
        (provenance not in `batch_keys`; for a shared node, a claim from
        outside the batch) already has, the Documents that MENTION a node of
        that label and name.

        Mention edges resolve by name, so a Document that mentions an
        existing `get` is exactly the set that should also link a newly added
        `get`; pairs absent from the result are new to the whole graph.
        """
        from devgraph.config.project_schema import LABEL_PATTERN  # runtime import: see TYPE_CHECKING above

        # Matched label by label (a built-in one through its (repo_id, name)
        # index), not on `{repo_id}` alone, which scanned every node.
        labels = sorted({label for label, _name in pairs if LABEL_PATTERN.fullmatch(label)})
        if not labels:
            return {}
        candidates = _union(
            _seek(label, "name", "IN $names")
            if label in NAMED_LABELS
            else f"MATCH (n:{label}) WHERE n.repo_id = $repo_id AND n.name IN $names RETURN n"
            for label in labels
        )
        with self._driver.session() as session:
            result = _run_records(
                session,
                candidates
                + "WITH n WHERE NOT coalesce(n.file, n.source_file, '') IN $batch_keys "
                "  AND NOT (n:Module AND n.name IN $batch_keys) "
                "  AND (coalesce(n.file, n.source_file) IS NOT NULL OR n.sources IS NULL "
                "       OR any(s IN n.sources WHERE NOT s IN $batch_keys)) "
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
        of that name. Hinted to seek by name: the planner otherwise scanned
        the `(repo_id, source)` index, every repository's stubs."""
        with self._driver.session() as session:
            result = _run_records(
                session,
                "MATCH (f:Function) USING INDEX SEEK f:Function(repo_id, name) "
                "WHERE f.repo_id = $repo_id AND f.name IN $names AND f.file IS NULL AND f.source IS NOT NULL "
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

        Both only annotate a node that already exists (MATCH, not MERGE): a
        commit also touches files that have no node (a README, an image, a
        file deleted since), and recency must not create one for them.

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
            _run(
                session,
                f"MATCH (n:{label} {merge_key}) "
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
            _run(
                session,
                f"MATCH (n:{label} {merge_key}) "
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
            _run(
                session,
                "MATCH (c:Commit {repo_id: $repo_id}) WHERE c.name IN $shas DETACH DELETE c",
                repo_id=repo_id,
                shas=shas,
            )

    def delete_commit_modifies(self, repo_id: str, shas: list[str]) -> None:
        """Delete the MODIFIES edges of these Commit nodes, scoped to
        repo_id, so a reconcile can write them again from scratch."""
        if not shas:
            return
        with self._driver.session() as session:
            _run(
                session,
                "MATCH (c:Commit {repo_id: $repo_id})-[r:MODIFIES]->() WHERE c.name IN $shas DELETE r",
                repo_id=repo_id,
                shas=shas,
            )

    def graph_repo_ids(self) -> set[str]:
        """Every repo_id some node carries: the Repository nodes' and any left
        without one (a write that landed after its repository was deleted)."""
        with self._driver.session() as session:
            result = _run_records(
                session, "MATCH (n) WHERE n.repo_id IS NOT NULL RETURN DISTINCT n.repo_id AS repo_id"
            )
            return {record["repo_id"] for record in result or []}

    def delete_repository(self, repo_id: str) -> None:
        """Remove every node (and its relationships) scoped to this repo_id."""
        with self._driver.session() as session:
            _run(
                session,
                "MATCH (n {repo_id: $repo_id}) DETACH DELETE n",
                repo_id=repo_id,
            )

    def load_insight_graph(
        self, repo_id: str, relationship_types: tuple[str, ...]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """One repository's edges of `relationship_types` and the nodes they touch,
        less the CALLS edges of confidence "name" or "package" and the
        IMPORTS edges of confidence "package", in any language.

        Identity is `elementId`, which is only promised stable within a
        transaction; it is used for the `write_insights` that immediately
        follows, and a node deleted in between is simply not matched there.
        """
        with self._driver.session() as session:
            edge_result = _run_records(
                session, _LOAD_INSIGHT_EDGES_CYPHER, repo_id=repo_id, types=list(relationship_types)
            )
            edges = [record.data() for record in edge_result or []]
            ids = sorted({e["source"] for e in edges} | {e["target"] for e in edges})
            node_result = _run_records(session, _LOAD_INSIGHT_NODES_CYPHER, repo_id=repo_id, ids=ids)
            nodes = [record.data() for record in node_result or []]
        return nodes, edges

    def write_insights(self, repo_id: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
        """Replace a repository's insight properties (see `_write_insights_tx`)."""
        with self._driver.session() as session:
            session.execute_write(_write_insights_tx, repo_id, rows, summary)

    def read_insights_summary(self, repo_id: str) -> dict[str, Any] | None:
        """The Repository node's insight summary, or None if never computed."""
        with self._driver.session() as session:
            result = _run_records(session, _READ_INSIGHTS_SUMMARY_CYPHER, repo_id=repo_id)
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

        connected = []

        @unit_of_work(timeout=timeout_s)
        def work(tx):
            connected.append(True)
            rows: list[dict] = []
            for record in tx.run(query, parameters):
                if len(rows) == max_rows:
                    return rows, True
                rows.append(record.data())
            return rows, False

        if not self._fail_fast:
            with self._driver.session(default_access_mode=READ_ACCESS) as session:
                return session.execute_read(work)
        try:
            with self._driver.session(default_access_mode=READ_ACCESS) as session:
                return session.execute_read(work)
        except ABORTS_BATCH:
            raise
        except _RETRYABLE_EXCEPTIONS as exc:
            timed_out = "TransactionTimedOut" in str(getattr(exc, "code", None) or "")
            if not connected or timed_out:  # Neo4j can't be reached, or the query ran out of time
                raise
        time.sleep(FAIL_FAST_RETRY_PAUSE_S)
        with self._driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)

    def probe_connectivity(self) -> None:
        """One connectivity check, not retried: raises at once when Neo4j can't be reached."""
        self._driver.verify_connectivity()

    def run_cypher_graph(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
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

        `timeout_s` is the server-side transaction timeout; `max_rows` stops
        pulling rows after that many (see `_graph_shaped`).
        """
        with self._driver.session() as session:
            result = session.run(Query(query, timeout=timeout_s), parameters or {})  # type: ignore[arg-type]
            return _graph_shaped(result, max_rows)

    def preview_cypher_graph(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """`run_cypher_graph`, but in a read transaction: the server refuses
        any write. Backs the console's live preview, which fires while the
        user is still typing and so must never change the graph."""

        @unit_of_work(timeout=timeout_s)
        def work(tx):
            return _graph_shaped(tx.run(query, parameters or {}), max_rows)

        with self._driver.session(default_access_mode=READ_ACCESS) as session:
            return session.execute_read(work)


def _graph_shaped(result: Any, max_rows: int | None = None) -> dict[str, Any]:
    """A Cypher result as Neo4j's HTTP `["row","graph"]` shape (see
    `GraphEngine.run_cypher_graph`), consumed fully before returning, or only
    its first `max_rows` rows: `truncated` says whether more existed."""
    columns = list(result.keys())
    data: list[dict[str, Any]] = []
    truncated = False
    for record in result:
        if max_rows is not None and len(data) == max_rows:
            # The rest is discarded, not sent; the query still runs to its end
            # (bounded by the transaction timeout), and a failure there is raised.
            result.consume()
            truncated = True
            break
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
                        properties.get("owner"),
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
    return {"columns": columns, "data": data, "truncated": truncated}


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
