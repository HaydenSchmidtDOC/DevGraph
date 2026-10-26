"""High-level MCP tool implementations against the DevGraph graph schema.

Each tool accepts a GraphEngine and returns structured data without exposing
raw Cypher to the AI. Tools filter by repo_id by default and support an
explicit cross_repo flag to opt-in to cross-repository results. A cross_repo
query reaches only `cross_repo_ids` (the registered repositories, when called
through the MCP server, which also refuses cross_repo unless
DEVGRAPH_ALLOW_CROSS_REPO is on).

All Cypher is parameterized — user input never concatenates directly into
query strings.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import os

# The SDK passes a ToolError's text to the client and hides any other exception's.
from mcp.server.mcpserver.exceptions import ToolError

from devgraph.config.project_schema import (
    LABEL_PATTERN,
    RELATIONSHIP_TYPE_PATTERN,
    ProjectSchemaError,
    resolve_effective_schema,
)
from devgraph.config.project_tools import DEFAULT_TIMEOUT_S
from devgraph.graph.engine import GraphEngine
from devgraph.graph import schema
from devgraph.indexer.source_text import decode_source_as, declared_encoding, is_python_path
from devgraph.paths import is_within
from devgraph.test_paths import TEST_PATH_CYPHER, is_test_path
from devgraph.registry.store import RepoRegistry
from devgraph.analytics.insights import INSIGHT_METRICS, community_members, read_insights, top_nodes

import re
import unicodedata

_SEARCH_STOPWORDS = frozenset({
    "how", "what", "why", "when", "where", "which", "who", "whom", "whose",
    "does", "did", "is", "are", "was", "were", "be", "been", "being",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "has", "have", "had", "the", "and", "but", "not", "for", "from", "with",
    "without", "into", "onto", "off", "that", "this", "these", "those", "there",
    "here", "its", "their", "them", "they", "about", "any", "all", "some",
    "work", "works", "working", "do", "in", "of", "to", "a", "an",
})

def _search_tokens(query: str) -> list[str]:
    """Lowercase word tokens from a search query, minus stopwords.
    Falls back to the unfiltered token list if every token is a stopword
    (so a query that's ALL stopwords still searches on something)."""
    raw = re.findall(r"[a-z0-9]+", query.lower())
    filtered = [t for t in raw if t not in _SEARCH_STOPWORDS and len(t) > 1]
    return filtered or raw


_MAX_LABEL_LEN = 500

def _sanitize_value(value: Any) -> Any:
    """Strip control characters and cap length on a single string value.
    Non-strings pass through unchanged. Applied to every string field in
    every tool result so a corpus string (commit message, PR title, node
    description) cannot inject control sequences or oversized payloads
    into the model's context."""
    if not isinstance(value, str):
        return value
    cleaned = "".join(
        ch for ch in value
        if unicodedata.category(ch)[0] != "C" or ch in ("\n", "\t")
    )
    return cleaned[:_MAX_LABEL_LEN]

def _sanitize_row(row: dict) -> dict:
    return {k: _sanitize_value(v) for k, v in row.items()}


def _source_off(registry: RepoRegistry | None, repo_id: str, flag: str) -> bool:
    """Whether `repo_id` is registered with its PR, issue or mentions source (`flag`) off."""
    repo = registry.get(repo_id) if registry is not None else None
    return repo is not None and not getattr(repo, flag)


def _source_off_notice(max_results: int, what: str, command: str, repo_id: str) -> dict[str, Any]:
    """An empty envelope saying how to turn the repository's source on. Never
    falls back to the network (no `gh`, no API call)."""
    return {
        **_envelope([], max_results),
        "notice": f"{what} ingestion is off for {repo_id}; enable it with `devgraph {command} {repo_id} enable`",
    }


#: How long a built-in tool's query may run before it is cancelled.
BUILTIN_TIMEOUT_S = 30

#: Rows a built-in tool's query may return; each already applies its own LIMIT
#: or aggregates, so this only guards against an unbounded result.
BUILTIN_MAX_ROWS = 100_000

#: Most files whose same-named targets `impact_analysis` traces apart; past it
#: one expansion covers them all (with a notice to pass `file`).
IMPACT_MAX_SPLIT = 5

#: How many CALLS/USES/DEPENDS_ON hops `impact_analysis` and
#: `impact_analysis_for_diff` follow for transitive dependents. An unbounded
#: variable-length path hangs on a real repository's call graph, and a
#: dependent further away than this says little about a change's impact.
IMPACT_MAX_DEPTH = 4


#: Cypher binding an Endpoint `e`'s `handlers` (the Functions, and a Django
#: class-based view's Class, it IMPLEMENTS) and `services` (the compose
#: services it CALLS); `_ENDPOINT_FIELDS` then names what `_endpoint_row` reads.
_ENDPOINT_DETAILS = """CALL (e) {
    OPTIONAL MATCH (e)-[:IMPLEMENTS]->(f) WHERE f:Function OR f:Class
    RETURN [h IN collect(DISTINCT {label: labels(f)[0], name: f.name, file: f.file, owner: f.owner})
            WHERE h.name IS NOT NULL]
           AS handlers
}
CALL (e) {
    OPTIONAL MATCH (e)-[:CALLS]->(s:Service)
    RETURN [n IN collect(DISTINCT s.name) WHERE n IS NOT NULL] AS services
}
"""
_ENDPOINT_FIELDS = (
    "{name: e.name, framework: e.framework, files: coalesce(e.sources, [e.source]), "
    "handlers: handlers, services: services, repo_id: e.repo_id}"
)


def _capped_endpoints(pairs: str) -> str:
    """Cypher binding `endpoint_count` and `endpoints` from `pairs`, a list of
    {e, file} maps (an Endpoint and the file of a target it reaches): how many
    distinct Endpoints there are, and the first `$max_results` of them by
    repository and name, as `_ENDPOINT_FIELDS` maps them plus `target_files`
    (their targets' files). Only those leave the database, however many
    routes a shared helper is under."""
    fields = _ENDPOINT_FIELDS[:-1] + ", target_files: target_files}"
    return f"""CALL ({pairs}) {{
  UNWIND {pairs} AS p
  WITH p.e AS e, collect(p.file) AS target_files
  ORDER BY e.repo_id, e.name
  WITH collect({{e: e, target_files: target_files}}) AS ranked
  RETURN size(ranked) AS endpoint_count, ranked[..$max_results] AS shown
}}
CALL (shown) {{
  UNWIND shown AS s
  WITH s.e AS e, s.target_files AS target_files
  {_ENDPOINT_DETAILS}
  RETURN collect({fields}) AS endpoints
}}
"""


def _impact_expansion(cross_repo: bool, with_ids: bool = False) -> str:
    """Cypher continuing from `WITH collect(n) AS l0` (the changed nodes): one
    CALL per hop collecting the distinct nodes with a CALLS/USES/DEPENDS_ON edge
    into the previous hop's nodes, never one seen at an earlier hop, then
    returning `direct_dependents` (hop 1), `transitive_dependents` (hops 2 to
    `IMPACT_MAX_DEPTH`), `direct_count`, `matched` (the changed nodes' {name, file, owner})
    and `endpoint_nodes`: the Endpoints that IMPLEMENTS a changed node or a
    dependent (an API route reaches its handler that way, one step past it),
    which `_capped_endpoints` reads; they are not dependents, so
    `direct_count` leaves them out. `with_ids` adds each dependent's `id` (its
    elementId).

    Expanding distinct nodes hop by hop does work proportional to the nodes
    and edges within reach; a variable-length path enumerates every path, which
    times out on a common name like `get` in a real repository.
    """
    scope = _cross_scope("d", "AND") if cross_repo else "AND d.repo_id = $repo_id"
    parts = []
    for hop in range(1, IMPACT_MAX_DEPTH + 1):
        seen = [f"l{k}" for k in range(hop)]
        parts.append(
            f"CALL ({', '.join(seen)}) {{ UNWIND l{hop - 1} AS m "
            f"MATCH (d)-[:CALLS|USES|DEPENDS_ON]->(m) WHERE true {scope} "
            f"AND {' AND '.join(f'NOT d IN {s}' for s in seen)} "
            f"RETURN collect(DISTINCT d) AS l{hop} }}"
        )
    levels = [f"l{k}" for k in range(IMPACT_MAX_DEPTH + 1)]
    endpoint_scope = _cross_scope("e", "AND") if cross_repo else "AND e.repo_id = $repo_id"
    parts.append(
        f"CALL ({', '.join(levels)}) {{ UNWIND {' + '.join(levels)} AS m "
        f"MATCH (e:Endpoint)-[:IMPLEMENTS]->(m) WHERE true {endpoint_scope} "
        "RETURN collect(DISTINCT e) AS endpoint_nodes }"
    )
    transitive = " + ".join(f"l{k}" for k in range(2, IMPACT_MAX_DEPTH + 1))
    ref = (
        "{name: d.name, type: labels(d)[0], file: coalesce(d.file, d.source_file), owner: "
        + _OWNER_OF.format(v="d") + (", id: elementId(d)}" if with_ids else "}")
    )
    parts.append(
        f"RETURN [d IN l1 | {ref}] AS direct_dependents, "
        f"[d IN {transitive} | {ref}] AS transitive_dependents, "
        "size(l1) AS direct_count, [n IN l0 | {name: n.name, file: n.file, owner: coalesce(n.owner, '')}] AS matched, "
        "endpoint_nodes"
    )
    return "\n".join(parts)


def _tests_last(
    rows: list[dict[str, Any]], include_tests: bool, path: str = "file"
) -> tuple[list[dict[str, Any]], int]:
    """`rows` with the test-code ones (by their `path` key, devgraph/test_paths.py
    `is_test_path`) after the rest, each part in its order, or left out when not
    `include_tests`; and how many test rows there are."""
    tests = [row for row in rows if is_test_path(row.get(path))]
    production = [row for row in rows if not is_test_path(row.get(path))]
    return (production + tests if include_tests else production), len(tests)


def _located(row: dict[str, Any]) -> dict[str, Any]:
    """A result row without the location keys it has no value for: `owner`
    and `start_line` dropped when None (a Module has no owner, a Service no
    line); `file` is always kept, null for a file-less node."""
    return {k: v for k, v in row.items() if v is not None or k not in ("owner", "start_line")}


@contextmanager
def _timeouts_as_tool_errors() -> Iterator[None]:
    """Turn a Neo4j transaction timeout into a ToolError the client can act on."""
    from neo4j.exceptions import Neo4jError

    try:
        yield
    except Neo4jError as exc:
        if "TransactionTimedOut" in str(getattr(exc, "code", None) or ""):
            raise ToolError(f"query timed out after {BUILTIN_TIMEOUT_S} s; narrow the request") from exc
        raise


def neo4j_unavailable(exc: BaseException, uri: str) -> ToolError | None:
    """A ToolError saying how to reach Neo4j at `uri` when `exc` is the driver
    failing to connect or authenticate; None for anything else. A closing
    engine or an index still being built is not a connection problem."""
    from neo4j.exceptions import AuthError, ServiceUnavailable

    from devgraph.graph.engine import EngineClosed, IndexesNotReady

    if isinstance(exc, AuthError):
        return ToolError(
            f"Neo4j at {uri} refused the username or password; check DEVGRAPH_NEO4J_USER and "
            "DEVGRAPH_NEO4J_PASSWORD (`devgraph doctor` checks the connection)"
        )
    if isinstance(exc, ServiceUnavailable) and not isinstance(exc, (EngineClosed, IndexesNotReady)):
        return ToolError(
            f"Neo4j isn't reachable at {uri}; start Neo4j (`podman start devgraph-neo4j`; on Windows "
            "and macOS run `podman machine start` first), then retry (`devgraph doctor` checks the connection)"
        )
    return None


#: Repository ids a cross_repo query may reach. The MCP server sets it, per
#: call, to the registered repositories (see `_with_cross_repo_gate` in
#: server.py); unset -- a direct library call -- it is None and does not restrict.
cross_repo_ids: ContextVar[list[str] | None] = ContextVar("cross_repo_ids", default=None)


def _cross_scope(var: str, keyword: str = "") -> str:
    """The repo filter a cross_repo query uses in place of `var.repo_id = $repo_id`:
    any repository in `cross_repo_ids`. `keyword` ("AND"/"WHERE") leads it."""
    clause = f"({var}.repo_id IS NOT NULL AND ($cross_repo_ids IS NULL OR {var}.repo_id IN $cross_repo_ids))"
    return f"{keyword} {clause}" if keyword else clause


def _with_cross_ids(cypher: str, params: dict[str, Any]) -> dict[str, Any]:
    """`params` plus `cross_repo_ids` when `cypher` refers to it."""
    return {**params, "cross_repo_ids": cross_repo_ids.get()} if "$cross_repo_ids" in cypher else params


def _query(engine: GraphEngine, cypher: str, params: dict[str, Any]) -> list[dict]:
    """Run a built-in tool's query read-only, bounded in time and rows. A
    timeout or a row overflow becomes a ToolError the client can act on."""
    with _timeouts_as_tool_errors():
        rows, more = engine.run_read_cypher(cypher, _with_cross_ids(cypher, params), timeout_s=BUILTIN_TIMEOUT_S, max_rows=BUILTIN_MAX_ROWS)
    if more:
        raise ToolError(f"query returned more than {BUILTIN_MAX_ROWS} rows; narrow the request")
    return rows


#: Rows a list-style built-in (find_callers, find_mentions, list_recent_changes,
#: blame_component) reads at most; past it the envelope is truncated, with a notice.
LIST_ROW_LIMIT = 10_000


def _list_query(engine: GraphEngine, cypher: str, params: dict[str, Any]) -> tuple[list[dict], bool]:
    """A list-style built-in's query (ending in `LIMIT LIST_ROW_LIMIT + 1`), read
    only, with a timeout: its first `LIST_ROW_LIMIT` rows and whether more exist."""
    with _timeouts_as_tool_errors():
        return engine.run_read_cypher(cypher, _with_cross_ids(cypher, params), timeout_s=BUILTIN_TIMEOUT_S, max_rows=LIST_ROW_LIMIT)


def _capped_envelope(rows: list[Any], more: bool, max_results: int) -> dict[str, Any]:
    """`_envelope`, marked truncated with a notice when the row cap cut the read short."""
    envelope = _envelope(rows, max_results)
    if more:
        envelope["truncated"] = True
        envelope["notice"] = (
            f"more than {LIST_ROW_LIMIT} matches; count covers the first {LIST_ROW_LIMIT}. Narrow the request"
        )
    return envelope


def _envelope(items: list[Any], max_results: int) -> dict[str, Any]:
    """Wrap a list result with count/truncation metadata so callers can see
    the full match count without paying token cost for every row.
    String values are sanitized (control chars stripped, length capped).
    """
    sanitized = [_sanitize_row(item) if isinstance(item, dict) else item for item in items]
    return {
        "count": len(sanitized),
        "results": sanitized[:max_results],
        "truncated": len(sanitized) > max_results,
    }


def _resolve_recency_cutoff(engine: GraphEngine, repo_id: str, modified_within_commits: int) -> Any:
    """Resolve the `last_modified_at` cutoff timestamp for the last
    `modified_within_commits` commits repo-wide.

    Finds the authored_date of the Nth-most-recent commit (by SKIP/LIMIT over
    commits ordered newest-first) and returns it as the cutoff: entities with
    `last_modified_at >= cutoff` were touched within that commit window. If
    fewer than `modified_within_commits` commits exist repo-wide, there is no
    cutoff to resolve and this returns None (meaning: don't filter).
    """
    skip = modified_within_commits - 1
    cypher = """
    MATCH (c:Commit {repo_id: $repo_id})
    RETURN c.authored_date AS d
    ORDER BY d DESC
    SKIP $skip
    LIMIT 1
    """
    results = _query(engine, cypher, {"repo_id": repo_id, "skip": skip})
    if not results:
        return None
    return results[0]["d"]


# Labels search_component always covers: code, routes, datastores (Cache
# included) and compose containers. A repository's schema-declared labels
# are appended per call (see declared_node_labels).
_SEARCH_LABELS: tuple[str, ...] = (
    "Service", "Module", "Class", "Function", "Endpoint", *schema.DATASTORE_LABELS, "Container",
)


def search_component(
    engine: GraphEngine,
    repo_id: str,
    query: str,
    cross_repo: bool = False,
    max_results: int = 15,
    modified_within_commits: int | None = None,
    extra_labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Search for components (services, modules, classes, functions, endpoints,
    datastores of every type, Cache included, and containers) by name or description.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID to search within (unless cross_repo=True)
        query: Search term (name substring or description keyword)
        cross_repo: If True, search across all repos; if False, limit to repo_id
        max_results: Maximum number of results to return in the envelope
        modified_within_commits: If given, only return components touched within
            the last N commits repo-wide (by `last_modified_at`, staged by git-history
            recency indexing). An entity never touched by that staging has no
            `last_modified_at` property and is excluded, never silently included.
            If fewer than N commits exist repo-wide, no cutoff applies and this
            filter is a no-op.
        extra_labels: Schema-declared labels of this repository to search as well;
            anything that isn't a valid label identifier is ignored. With
            cross_repo=True only the calling repository's declared labels are added.

    Returns:
        Dict with count, results, and truncated flag. Query is tokenized and
        stopword-filtered — multi-word natural-language queries match any token,
        not the whole phrase — and the whole query and each of its words
        (underscores kept, so `user_service`) are terms too. Results are ranked:
        the whole query as the exact name > the same ignoring case > name starts
        with the whole query > name contains it > name equal to one of its terms
        (`run` for `run_cypher`) > name starts with a term > name contains one >
        description contains one. At most `SEARCH_MAX_RESULTS` results are returned.
    """
    _at_least(1, max_results=max_results, modified_within_commits=modified_within_commits)
    max_results = min(max_results, SEARCH_MAX_RESULTS)
    cutoff = None
    if modified_within_commits is not None:
        cutoff = _resolve_recency_cutoff(engine, repo_id, modified_within_commits)

    tokens = _search_terms(query)
    labels = _SEARCH_LABELS + tuple(
        label for label in extra_labels if LABEL_PATTERN.fullmatch(label) and label not in _SEARCH_LABELS
    )
    rows, count, lower_bound = ranked_search(
        engine, labels, tokens, _name_variants(query, tokens), max_results,
        repo_id=None if cross_repo else repo_id, cutoff=cutoff, query=query,
    )
    envelope = _envelope([{k: v for k, v in row.items() if k != "id"} for row in rows], max_results)
    envelope["count"] = count
    envelope["truncated"] = count > len(envelope["results"])
    if lower_bound:
        envelope["count_is_lower_bound"] = True
    return envelope


#: Rows the index-backed exact/prefix stage of `ranked_search` reads at most.
_SEARCH_INDEX_CAP = 200

#: Most results search_component returns, whatever max_results asks for.
SEARCH_MAX_RESULTS = _SEARCH_INDEX_CAP


def _search_terms(query: str) -> list[str]:
    """`_search_tokens`, plus the whole query and each `[A-Za-z0-9_]+` word,
    lower-cased: a snake_case name is a token split at `_`, and only whole it
    can match exactly."""
    whole = [query.strip().lower(), *(w.lower() for w in re.findall(r"[A-Za-z0-9_]+", query))]
    return list(dict.fromkeys([*_search_tokens(query), *(t for t in whole if t)]))

# Rank of a matching node: the whole query as its name (`$exact`, trimmed),
# the same ignoring case (`$whole`, lower-cased), a name starting with or
# containing the whole query, a name equal to one of its terms (`run` for
# `run_cypher`), starting with or containing a term, description only.
_SEARCH_TIER = (
    "CASE WHEN n.name = $exact THEN 0 WHEN toLower(n.name) = $whole THEN 1 "
    "WHEN toLower(n.name) STARTS WITH $whole THEN 2 WHEN toLower(n.name) CONTAINS $whole THEN 3 "
    "WHEN toLower(n.name) IN $terms THEN 4 "
    "WHEN any(t IN $terms WHERE toLower(n.name) STARTS WITH t) THEN 5 "
    "WHEN any(t IN $terms WHERE toLower(n.name) CONTAINS t) THEN 6 ELSE 7 END"
)
_SEARCH_FILE = "CASE WHEN coalesce(n.file, '') <> '' THEN n.file ELSE coalesce(n.path, n.source_file, n.source) END"
_SEARCH_ROW = (
    "{id: elementId(n), name: n.name, labels: labels(n), repo_id: n.repo_id, description: n.description, "
    f"file: {_SEARCH_FILE}, owner: CASE WHEN n.file IS NOT NULL THEN coalesce(n.owner, '') END}}"
)


def _name_variants(query: str, terms: list[str]) -> list[str]:
    """Spellings of each term an exact-case index seek tries: the term, Capitalised,
    UPPER, and the query's own words that lower-case to it."""
    variants = [v for t in terms for v in (t, t.capitalize(), t.upper())]
    variants += [w for w in [query.strip(), *re.findall(r"[A-Za-z0-9_]+", query)] if w.lower() in terms]
    return list(dict.fromkeys(variants))


def _fold_target(var: str, out: str = "folded") -> str:
    """Cypher `CALL` binding `out` to the real Function the route handler stub
    `var` provably stands for, or to null: for any node that is no handler stub
    (a file-less Function), and for a stub that stands for no single Function.
    A stub stands for the one Function (or Class: a Django class-based view's)
    of its name its endpoint's handler reference resolved to (both implement
    one Endpoint), or, when it resolved to none, the one Function or Class of
    its name in the route files that claim the stub (its `sources`). Always one row:
    the aggregation has no grouping key. Tools answer with that Function or
    Class and leave the stub out; a stub that stands for none stays visible
    (an unresolved handler)."""
    twin = f"twin.repo_id = {var}.repo_id AND twin.name = {var}.name AND twin.file <> ''"
    linked = f"EXISTS {{ ({var})<-[:IMPLEMENTS]-(:Endpoint)-[:IMPLEMENTS]->(twin) }}"
    return (
        f"CALL ({var}) {{ WITH {var} WHERE {var}:Function AND coalesce({var}.file, '') = '' "
        f"CALL ({var}) {{ MATCH (twin:Function) WHERE {twin} RETURN twin "
        f"UNION MATCH (twin:Class) WHERE {twin} AND ({linked} OR twin.file IN coalesce({var}.sources, [])) "
        "RETURN twin } "
        f"WITH twin, {linked} AS linked, twin.file IN coalesce({var}.sources, []) AS local "
        "WITH collect(CASE WHEN linked THEN twin END) AS linked, collect(CASE WHEN local THEN twin END) AS local "
        f"RETURN CASE WHEN size(linked) = 1 THEN linked[0] WHEN size(linked) = 0 AND size(local) = 1 THEN local[0] "
        f"END AS {out} }}\n"
    )


def _shadowed_stub(var: str) -> str:
    """Cypher predicate: `var` is a route's handler stub (a file-less Function)
    that every route implementing it resolved to exactly one real Function or
    Class of its name (an IMPLEMENTS edge from the same Endpoint). Each route's
    handler is then that real node, so the stub, which a name shared by
    several routes' handlers (three controllers' `list`) makes one node for
    them all, stands for nothing of its own: tools leave it out where they
    would list it beside those handlers. A stub some route resolved to none,
    or to several, stays visible (an unresolved handler)."""
    twin = (
        f"(twin:Function OR twin:Class) AND twin.repo_id = {var}.repo_id AND twin.name = {var}.name "
        "AND twin.file <> ''"
    )
    return (
        f"({var}:Function AND coalesce({var}.file, '') = '' AND EXISTS {{ ({var})<-[:IMPLEMENTS]-(:Endpoint) }} "
        f"AND NOT EXISTS {{ MATCH ({var})<-[:IMPLEMENTS]-(e:Endpoint) "
        f"WHERE COUNT {{ MATCH (e)-[:IMPLEMENTS]->(twin) WHERE {twin} }} <> 1 }})"
    )


def _search_branches(labels: tuple[str, ...], where: str, unwind: bool = False) -> str:
    """`CALL () { ... }` over one MATCH per label, so each can use its label's indexes.

    A route's handler stub is left out when it stands for a real Function
    (`_fold_target`), or every route's handler is one (`_shadowed_stub`)."""
    branches = []
    for label in labels:
        stub = (
            f" {_fold_target('n')}  WITH n, folded WHERE folded IS NULL AND NOT {_shadowed_stub('n')}"
            if label == "Function" else ""
        )
        unwound = "UNWIND $names AS p " if unwind else ""
        branches.append(f"  {unwound}MATCH (n:`{label}`) WHERE {where}{stub} RETURN n")
    return "CALL () {\n" + "\n  UNION\n".join(branches) + "\n}\n"


def ranked_search(
    engine: GraphEngine,
    labels: tuple[str, ...],
    terms: list[str],
    names: list[str],
    limit: int,
    repo_id: str | None,
    cutoff: Any = None,
    query: str = "",
) -> tuple[list[dict], int, bool]:
    """Nodes of `labels` whose name or description contains a (lower-case) term,
    best first (`_SEARCH_TIER`): `query` itself as the name, then ignoring case,
    then a name starting with or containing `query`, then a name equal to,
    starting with or containing a term, description only.

    Names that start with one of `names` are found first through the
    `(repo_id, name)` indexes. Only when they don't fill `limit` does a substring
    scan fill the rest and count every match. Returns (rows, count,
    count_is_lower_bound): the count is a lower bound when the scan never ran.
    `repo_id` None searches every repository. Labels must be validated identifiers.
    """
    scope = "n.repo_id = $repo_id" if repo_id is not None else _cross_scope("n")
    if cutoff is not None:
        scope += " AND n.last_modified_at >= $cutoff"
    exact = query.strip()
    params: dict[str, Any] = {  # an empty query (None) matches no whole-query tier
        "repo_id": repo_id, "terms": terms, "names": names, "cutoff": cutoff,
        "exact": exact or None, "whole": exact.lower() or None,
    }
    order = (
        f"WITH n, {_SEARCH_TIER} AS tier, {_SEARCH_FILE} AS file "
        "ORDER BY tier, n.name, file IS NULL, file, elementId(n)\n"
    )
    indexed = _query(
        engine,
        _search_branches(labels, f"{scope} AND n.name STARTS WITH p", unwind=True)
        + order + f"LIMIT {max(_SEARCH_INDEX_CAP, limit + 1)} RETURN {_SEARCH_ROW} AS row",
        params,
    )
    found = [r["row"] for r in indexed]
    if len(found) > limit:
        return found[:limit], len(found), True
    substring = "any(t IN $terms WHERE toLower(n.name) CONTAINS t OR toLower(n.description) CONTAINS t)"
    rows = _query(
        engine,
        _search_branches(labels, f"{scope} AND {substring}") + order
        + "WITH collect(n) AS ns "
        f"RETURN size(ns) AS total, [n IN [n IN ns WHERE NOT elementId(n) IN $seen][..$fill] | {_SEARCH_ROW}] AS rest",
        {**params, "seen": [r["id"] for r in found], "fill": limit - len(found)},
    )
    row = rows[0] if rows else {"total": 0, "rest": []}
    return _rank_search_results(found + row["rest"], terms, exact), max(row["total"], len(found)), False


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


def _rank_search_results(results: list[dict], tokens: list[str], query: str = "") -> list[dict]:
    """Sort search rows in `_SEARCH_TIER`'s order: `query` as the exact name,
    then ignoring case, then a name starting with or containing `query`, then a
    name equal to, starting with or containing a token, then description-only
    matches last; by name and file within a tier (no file last), as
    `ranked_search`'s queries order them."""
    exact = query.strip()
    whole = exact.lower()

    def tier(row: dict) -> int:
        raw = row.get("name") or ""
        name = raw.lower()
        if exact and raw == exact:
            return 0
        if whole and name == whole:
            return 1
        if whole and name.startswith(whole):
            return 2
        if whole and whole in name:
            return 3
        if name in tokens:
            return 4
        if any(name.startswith(t) for t in tokens):
            return 5
        if any(t in name for t in tokens):
            return 6
        return 7
    return sorted(
        results,
        key=lambda row: (tier(row), row.get("name") or "", row.get("file") is None, row.get("file") or ""),
    )


# --- describe_node -------------------------------------------------------------

_DESCRIBE_MAX_CANDIDATES = 20
_DESCRIBE_MAX_SUGGESTIONS = 5
_DESCRIBE_MAX_GROUPS = 200
_DESCRIBE_MAX_PER_TYPE = 50
_DESCRIBE_MAX_FILTERS = 20
_DESCRIBE_MAX_PROPERTIES = 50
_DESCRIBE_MAX_LIST_ITEMS = 20
_DESCRIBE_ECHO = 100
_DESCRIBE_DIRECTIONS = ("both", "out", "in")
# Shown elsewhere in the response, or extractor/insight bookkeeping that other tools serve.
_DESCRIBE_HIDDEN = frozenset({"repo_id", "name"}) | schema.INTERNAL_NODE_PROPERTIES
_DESCRIBE_BUILTIN_LABELS = schema.NAMED_LABELS  # every built-in label but Repository, Cache included
_DESCRIBE_HINT = (
    "search_component searches by partial name; a file, folder or docs node is named by its "
    "repo-relative path or its id"
)

_DESCRIBE_NEIGHBOUR = (
    "raw.repo_id = $repo_id AND NOT raw:Repository "
    "AND ($types IS NULL OR type(r) IN $types) "
    "AND ($labels IS NULL OR any(l IN labels(raw) WHERE l IN $labels))"
)
#: The labels whose `owner` keys a node within its file (the declarations it
#: is in, "" at the top level; see devgraph/graph/schema.py FILE_SCOPED_LABELS).
_OWNED_LABELS = schema.FILE_SCOPED_LABELS
#: Cypher for node `{v}`'s owner: "" for a top-level symbol, null for any node
#: without a `file` (a declared type's `owner` field is its own).
_OWNER_OF = "CASE WHEN {v}.file IS NOT NULL THEN coalesce({v}.owner, '') END"

# Each neighbour (`raw`) is listed as the Function it folds into when it is a
# route's handler stub (`_fold_target`), or left out when each of its routes'
# handlers is a real Function (`_shadowed_stub`: it is a neighbour of an
# Endpoint, which lists that Function too), so a handler is listed once.
_DESCRIBE_GROUPS = f"""
MATCH (n) WHERE elementId(n) IN $ids
CALL (n) {{
  MATCH (n)-[r]->(raw)
  WHERE $direction IN ['both','out'] AND {_DESCRIBE_NEIGHBOUR}
  RETURN 'out' AS dir, type(r) AS rel, raw
  UNION
  MATCH (n)<-[r]-(raw)
  WHERE $direction IN ['both','in'] AND {_DESCRIBE_NEIGHBOUR}
  RETURN 'in' AS dir, type(r) AS rel, raw
}}
{_fold_target("raw")}WITH dir, rel, raw, folded WHERE folded IS NOT NULL OR NOT {_shadowed_stub("raw")}
WITH dir, rel, coalesce(folded, raw) AS m
WITH dir, rel, count(DISTINCT m) AS total
ORDER BY dir, rel
LIMIT {_DESCRIBE_MAX_GROUPS + 1}
CALL (dir, rel) {{
  MATCH (n) WHERE elementId(n) IN $ids
  CALL (n, dir, rel) {{
    MATCH (n)-[r:$(rel)]->(raw) WHERE dir = 'out' RETURN raw
    UNION
    MATCH (n)<-[r:$(rel)]-(raw) WHERE dir = 'in' RETURN raw
  }}
  WITH raw WHERE raw.repo_id = $repo_id AND NOT raw:Repository
    AND ($labels IS NULL OR any(l IN labels(raw) WHERE l IN $labels))
  {_fold_target("raw")}  WITH raw, folded WHERE folded IS NOT NULL OR NOT {_shadowed_stub("raw")}
  WITH DISTINCT coalesce(folded, raw) AS m
  ORDER BY m.name, coalesce(m.file, m.path), m.owner
  LIMIT $cap
  RETURN collect({{
    label: labels(m)[0], name: m.name, file: coalesce(m.file, m.path), owner: {_OWNER_OF.format(v="m")}
  }}) AS refs
}}
RETURN dir, rel, total, refs
"""


#: The handler stubs that stand for the Function or Class `$id` (`_fold_target`):
#: describe_node lists their relationships as that node's.
_FOLDED_STUBS = f"""
MATCH (real) WHERE elementId(real) = $id AND (real:Function OR real:Class)
MATCH (s:Function) WHERE s.repo_id = real.repo_id AND s.name = real.name AND coalesce(s.file, '') = ''
{_fold_target("s")}WITH s, folded WHERE folded = real
RETURN elementId(s) AS id
"""


def _echo(value: Any) -> str:
    """A caller value for an error message: cut to 100 characters, quoted."""
    return repr(str(value)[:_DESCRIBE_ECHO])


def _at_least(minimum: int, **values: int | None) -> None:
    """A ToolError naming the first given argument below `minimum`; None is not given."""
    for name, value in values.items():
        if value is not None and value < minimum:
            raise ToolError(f"{name} must be at least {minimum}, not {value}")


def _registered(registry: RepoRegistry, repo_id: str) -> Any:
    """The registry record for `repo_id`, or a ToolError naming the unknown id."""
    record = registry.get(repo_id)
    if record is None:
        raise ToolError(f"no such repo_id: {_echo(repo_id)}; run devgraph list to see registered repositories")
    return record


def _validated_identifiers(values: list[str] | None, pattern: re.Pattern[str], what: str) -> list[str] | None:
    """The filter list, or None for no filter; any value that isn't an identifier is an error."""
    if isinstance(values, str):
        raise ToolError(f"{what}s must be a list of names, not the string {_echo(values)}")
    if not values:
        return None
    if len(values) > _DESCRIBE_MAX_FILTERS:
        raise ToolError(f"at most {_DESCRIBE_MAX_FILTERS} {what}s, got {len(values)}")
    for value in values:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise ToolError(f"invalid {what} {_echo(value)}")
    return list(values)


def _label_branches(labels: tuple[str, ...], predicate: str) -> str:
    return "\n  UNION\n".join(
        f"  MATCH (n:`{label}`) WHERE n.repo_id = $repo_id AND {predicate} RETURN n" for label in labels
    )


def _lookup_cypher(labels: tuple[str, ...], declared: frozenset[str]) -> str:
    """One single-property branch per label, so each can seek on its own index.

    Every label gets a name branch; only declared (provider) labels carry
    `path`, so only they get a path branch. A route's handler stub is left out
    when it stands for a real Function (`_fold_target`, `_shadowed_stub`).
    Labels are validated identifiers.
    """
    tail = "AND NOT n:Repository AND ($file IS NULL OR $file IN [n.file, n.path, n.source_file])"
    unfolded = f" {_fold_target('n')}  WITH n, folded WHERE folded IS NULL AND NOT {_shadowed_stub('n')}"
    body = "\n  UNION\n".join(
        f"  MATCH (n:`{label}`) WHERE n.repo_id = $repo_id AND n.{key} = $name {tail}"
        f"{unfolded if label == 'Function' else ''} RETURN n"
        for label in labels
        for key in (("name", "path") if label in declared else ("name",))
    )
    return (
        f"CALL () {{\n{body}\n}}\n"
        "RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name, "
        f"coalesce(n.file, n.path) AS file, {_OWNER_OF.format(v='n')} AS owner, properties(n) AS properties\n"
        f"LIMIT {_DESCRIBE_MAX_CANDIDATES + 1}"
    )


def _owned_lookup_cypher(labels: tuple[str, ...], companion: bool = False) -> str:
    """`_lookup_cypher`'s match for a name qualified with its owner: the
    Class or Function of `labels` named `$member` whose owner is `$owner`,
    or with `companion` a member of `$owner`'s Kotlin companion."""
    owned = _COMPANION_MEMBER.format() if companion else "n.owner = $owner"
    body = "\n  UNION\n".join(
        f"  MATCH (n:`{label}`) WHERE n.repo_id = $repo_id AND n.name = $member AND {owned} "
        "AND ($file IS NULL OR n.file = $file) RETURN n"
        for label in labels if label in _OWNED_LABELS
    )
    return (
        f"CALL () {{\n{body}\n}}\n"
        "RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name, "
        f"n.file AS file, n.owner AS owner, properties(n) AS properties\n"
        f"LIMIT {_DESCRIBE_MAX_CANDIDATES + 1}"
    )


#: A member of `$owner`'s Kotlin companion object (anonymous `Companion` or
#: named), which a caller names through the class (`Repo.create` for owner
#: `Repo.Companion` or `Repo.Factory`): the companion is a Class of kind
#: `companion` owned by `$owner` in the member's file.
_COMPANION_MEMBER = (
    "n.owner STARTS WITH $owner + '.' AND EXISTS {{ MATCH (c:Class) WHERE c.repo_id = n.repo_id "
    "AND c.file = n.file AND c.owner = $owner AND c.kind = 'companion' AND n.owner = $owner + '.' + c.name }}"
)


def _owner_split(name: str) -> tuple[str, str] | None:
    """`A.save` (`Outer.Inner.run`, `A::save`, `Outer::Inner::run`) as
    (owner, member), the owner dotted: a symbol named with the declarations
    it is in (see devgraph/indexer/common.py `member_owner`); None for a name
    with neither separator."""
    separator = "::" if "::" in name else "."
    owner, found, member = name.rpartition(separator)
    return (owner.replace("::", "."), member) if found and owner and member else None


def _suggestions_cypher(labels: tuple[str, ...]) -> str:
    body = _label_branches(
        labels,
        "NOT n:Repository AND (toLower(n.name) CONTAINS toLower($name) "
        "OR toLower(n.path) CONTAINS toLower($name))",
    )
    return (
        f"CALL () {{\n{body}\n}}\n"
        "RETURN labels(n)[0] AS label, n.name AS name, coalesce(n.file, n.path) AS file\n"
        f"ORDER BY name, file LIMIT {_DESCRIBE_MAX_SUGGESTIONS}"
    )


def _did_you_mean(suggestions: list[dict[str, Any]], lead: str = "Did you mean") -> str:
    """` Did you mean: label='...', name='...'; ...?`, or "" with no suggestions."""
    if not suggestions:
        return ""
    shown = "; ".join(", ".join(f"{k}={_echo(v)}" for k, v in _node_ref(s).items()) for s in suggestions)
    return f" {lead}: {shown}?"


def _not_found(
    engine: GraphEngine, repo_id: str, what: str, name: str, labels: tuple[str, ...], cross_repo: bool = False
) -> ToolError:
    """A ToolError saying no `what` is named `name`, with up to five of this
    repository's nodes of `labels` whose name contains it."""
    suggestions = _query(engine, _suggestions_cypher(labels), {"repo_id": repo_id, "name": name})
    if cross_repo:
        where, lead = "any repository", f"Similar names in repository {_echo(repo_id)}"
    else:
        where, lead = f"repository {_echo(repo_id)}", "Did you mean"
    return ToolError(
        f"no {what} named {_echo(name)} in {where}.{_did_you_mean(suggestions, lead)} "
        "search_component searches by partial name."
    )


def _named_nodes(labels: tuple[str, ...], param: str, cross_repo: bool, var: str = "n") -> str:
    """`CALL () { ... }` returning `var`: every node of `labels` whose name is
    `$param`, in the repository (or in any of `cross_repo_ids`), one branch per
    label so each can seek its `(repo_id, name)` index, never scanning every
    node in the database. Labels are validated identifiers."""
    scope = _cross_scope(var) if cross_repo else f"{var}.repo_id = $repo_id"
    body = "\n  UNION\n".join(
        f"  MATCH ({var}:`{label}`) WHERE {var}.name = ${param} AND {scope} RETURN {var}" for label in labels
    )
    return f"CALL () {{\n{body}\n}}\n"


def component_labels(extra_labels: tuple[str, ...] = ()) -> tuple[str, ...]:
    """The labels a component name is looked up under: every built-in one but
    Repository (`schema.NAMED_LABELS`), then the valid ones of `extra_labels`
    (a repository's declared labels)."""
    extra = (label for label in extra_labels if LABEL_PATTERN.fullmatch(label) and label not in schema.NAMED_LABELS)
    return schema.NAMED_LABELS + tuple(dict.fromkeys(extra))


def _exists(engine: GraphEngine, repo_id: str, name: str, labels: tuple[str, ...], cross_repo: bool = False) -> bool:
    """Whether a node of `labels` is named `name` in the repository (any repository when cross_repo)."""
    params = {"name": name} if cross_repo else {"name": name, "repo_id": repo_id}
    return bool(_query(engine, _named_nodes(labels, "name", cross_repo) + "RETURN 1 LIMIT 1", params))


#: Most files a row's `target_files` lists; past it the row adds `target_file_count`.
_MAX_TARGET_FILES = 10

_TARGETS_RETURN = (
    "WITH DISTINCT labels(n)[0] AS label, n.file AS file, n.repo_id AS repo_id "
    "RETURN label, file, repo_id ORDER BY repo_id, file, label"
)


def _normalize_file(file: str | None, root: Path | None = None) -> str | None:
    """A repo-relative path as the graph stores it: `/` separators, no leading
    `./`, and, given the repository's `root`, an absolute path inside it made
    relative to it (case-insensitively on Windows)."""
    if file is None:
        return None
    if root is not None and os.path.isabs(file):
        prefix = os.path.normcase(os.path.normpath(str(root))) + os.sep
        if os.path.normcase(os.path.normpath(file)).startswith(prefix):
            file = os.path.normpath(file)[len(prefix):]
    file = file.replace("\\", "/")
    while file.startswith("./"):
        file = file[2:]
    return file


#: How long the lookup under undeclared labels may take before it gives up.
_UNDECLARED_LOOKUP_TIMEOUT_S = 3


def _undeclared_targets(engine: GraphEngine, params: dict[str, Any], cross_repo: bool) -> list[dict]:
    """`_TARGETS_RETURN` rows for the nodes named `$name` under a label in use that
    is no built-in one, one label scan per label (never every node), within
    `_UNDECLARED_LOOKUP_TIMEOUT_S`; [] when there are none or it ran out of time."""
    from neo4j.exceptions import Neo4jError

    in_use = _query(engine, "CALL db.labels() YIELD label RETURN label", {})
    labels = [
        r["label"] for r in in_use
        if r["label"] not in schema.NAMED_LABELS and r["label"] != "Repository" and LABEL_PATTERN.fullmatch(r["label"])
    ]
    if not labels:
        return []
    cypher = _named_nodes(tuple(labels), "name", cross_repo) + _TARGETS_RETURN
    try:
        rows, _ = engine.run_read_cypher(
            cypher, _with_cross_ids(cypher, params), timeout_s=_UNDECLARED_LOOKUP_TIMEOUT_S, max_rows=BUILTIN_MAX_ROWS
        )
    except Neo4jError as exc:
        if "TransactionTimedOut" in str(getattr(exc, "code", None) or ""):
            return []
        raise
    return rows


def _owned_target(
    engine: GraphEngine, repo_id: str, name: str, owner: str | None, cross_repo: bool, labels: tuple[str, ...]
) -> tuple[str, str | None]:
    """The (name, owner) a caller means: `owner` as given; else, when no node
    is named `name` but it is a member named with its declarations (`A.run`,
    `Outer::Inner.run`, `_owner_split`) and such a member exists, that
    member and owner; else `name` with no owner."""
    if owner is not None or _exists(engine, repo_id, name, labels, cross_repo):
        return name, owner
    owned = _owner_split(name)
    if owned is None:
        return name, None
    split_owner, member = owned
    params = {"name": member, "owner": split_owner, **({} if cross_repo else {"repo_id": repo_id})}
    cypher = _named_nodes(labels, "name", cross_repo) + f"WITH n WHERE {_OWNER_MATCH} RETURN 1 LIMIT 1"
    return (member, split_owner) if _query(engine, cypher, params) else (name, None)


#: A node of `$owner` (`n.owner`, "" at the top level; only a node with a
#: file has one), or any node when `$owner` is null.
_OWNER_MATCH = "($owner IS NULL OR (n.file IS NOT NULL AND coalesce(n.owner, '') = $owner))"


def _resolve_targets(
    engine: GraphEngine,
    repo_id: str,
    name: str,
    cross_repo: bool,
    file: str | None,
    labels: tuple[str, ...],
    describe: bool = True,
    owner: str | None = None,
) -> tuple[dict[str, Any] | None, tuple[str, ...], int]:
    """Look up the nodes named `name`. Returns `ambiguous_targets` (an envelope of
    their `{label, file}`, `repo_id` too when cross_repo, a file-less one with
    `file` None) when they lie in more than one file and no `file` narrows them,
    else None; the labels to look the name up under; and how many files they lie in.

    The lookup seeks `labels`. When it finds nothing it falls back to the labels
    in use that are no built-in one (`_undeclared_targets`), so a node whose label
    the repository does not declare (any more) is still found, and its label is
    added to the returned ones. `describe` False skips building the envelope.
    `owner` keeps only the nodes of that owner (`_OWNER_MATCH`). A route's
    handler stub that folds into a real Function (`_fold_target`,
    `_shadowed_stub`) is left out, as search and describe_node leave it out.

    Raises:
        ToolError: no node has that name (of `owner`), or none of them is in `file`.
    """
    params = {"name": name, "owner": owner, **({} if cross_repo else {"repo_id": repo_id})}
    # A route's handler stub that stands for real Functions is no target of its own.
    hidden = f"{_fold_target('n')}WITH n, folded WHERE folded IS NULL AND NOT {_shadowed_stub('n')} "
    rows = _query(
        engine, _named_nodes(labels, "name", cross_repo) + f"WITH n WHERE {_OWNER_MATCH} " + hidden + _TARGETS_RETURN,
        params,
    )
    if not rows and owner is not None:
        where = "any repository" if cross_repo else f"repository {_echo(repo_id)}"
        raise ToolError(
            f"no component named {_echo(name)} with owner {_echo(owner)} in {where}; "
            "describe_node or get_source without owner lists the candidates with theirs."
        )
    if not rows:
        rows = _undeclared_targets(engine, params, cross_repo)
        if not rows:
            raise _not_found(engine, repo_id, "component", name, labels, cross_repo)
        found = (row["label"] for row in rows if LABEL_PATTERN.fullmatch(row["label"]))
        labels = tuple(dict.fromkeys((*labels, *found)))
    files = sorted({row["file"] for row in rows if row["file"] is not None})
    if file is not None:
        if file not in files:
            shown = ", ".join(_echo(f) for f in files[:_DESCRIBE_MAX_CANDIDATES]) or "no file"
            raise ToolError(f"no component named {_echo(name)} in file {_echo(file)}; it is defined in: {shown}")
        return None, labels, 1
    if len(files) < 2 or not describe:
        return None, labels, len(files)
    refs = [
        {"label": row["label"], "file": row["file"], **({"repo_id": row["repo_id"]} if cross_repo else {})}
        for row in rows
    ]
    ambiguous = {
        "count": len(refs),
        "results": [_sanitize_row(ref) for ref in refs[:_DESCRIBE_MAX_CANDIDATES]],
        "truncated": len(refs) > _DESCRIBE_MAX_CANDIDATES,
    }
    return ambiguous, labels, len(files)


def _with_target_files(row: dict[str, Any], files: Any) -> dict[str, Any]:
    """`row` plus `target_files`: the sorted distinct non-null `files`, at most
    `_MAX_TARGET_FILES`, and `target_file_count` when there are more."""
    distinct = sorted({f for f in files if f is not None})
    labelled = {**row, "target_files": distinct[:_MAX_TARGET_FILES]}
    if len(distinct) > _MAX_TARGET_FILES:
        labelled["target_file_count"] = len(distinct)
    return labelled


def _node_ref(row: dict[str, Any]) -> dict[str, Any]:
    """`{label, name, file, owner}` from raw values, so it re-describes exactly
    this node: `file` when it has one, and then `owner` too (the declarations
    it is in, "" at the top level), which tells it from a same-named
    member of another declaration of its file."""
    ref = {"label": row["label"], "name": row["name"]}
    if row.get("file") is not None:
        ref["file"] = row["file"]
    if row.get("owner") is not None:
        ref["owner"] = row["owner"]
    return ref


def _unresolved_marked(ref: dict[str, Any]) -> dict[str, Any]:
    """A candidate ref, with `unresolved_handler: true` when it is a route's
    handler stub left visible because it folds into no single real Function
    (`_fold_target`, `_shadowed_stub`): a file-less Function."""
    return {**ref, "unresolved_handler": True} if ref["label"] == "Function" and ref.get("file") is None else ref


def _visible_properties(props: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Properties minus bookkeeping, sanitised for display and capped; and whether any were dropped."""
    from devgraph.mcp.tool_plane import _sanitize_deep

    keys = sorted(k for k in props if k not in _DESCRIBE_HIDDEN and not k.startswith("insight_"))
    shown = {}
    for key in keys[:_DESCRIBE_MAX_PROPERTIES]:
        value = props[key]
        if isinstance(value, list):
            value = value[:_DESCRIBE_MAX_LIST_ITEMS]
        shown[key] = _sanitize_deep(value)
    return shown, len(keys) > _DESCRIBE_MAX_PROPERTIES


def _read(engine: GraphEngine, cypher: str, params: dict[str, Any], max_rows: int) -> tuple[list[dict], bool]:
    """A read-only, time- and row-bounded read; a Neo4j error becomes its code, never its message."""
    from neo4j.exceptions import Neo4jError

    try:
        return engine.run_read_cypher(cypher, _with_cross_ids(cypher, params), timeout_s=DEFAULT_TIMEOUT_S, max_rows=max_rows)
    except Neo4jError as exc:
        unavailable = neo4j_unavailable(exc, getattr(engine, "uri", "the configured URI"))
        if unavailable is not None:  # a refused login is a Neo4jError too
            raise unavailable from exc
        code = str(getattr(exc, "code", None) or "unknown")
        if "TransactionTimedOut" in code:
            raise ToolError(
                f"describe_node timed out after {DEFAULT_TIMEOUT_S}s; "
                "narrow it with label, file, relationship_types or neighbor_labels"
            ) from exc
        raise ToolError(f"describe_node failed: {code}") from exc


def describe_node(
    engine: GraphEngine,
    repo_id: str,
    name: str,
    label: str | None = None,
    file: str | None = None,
    direction: str = "both",
    relationship_types: list[str] | None = None,
    neighbor_labels: list[str] | None = None,
    max_per_type: int = 10,
    declared_labels: tuple[str, ...] = (),
    owner: str | None = None,
) -> dict[str, Any]:
    """One node's properties and its relationships, grouped by type, one hop out.

    Args:
        engine: GraphEngine instance
        repo_id: Repository to look in
        name: The node's exact name; a file, folder or docs node may also be
            named by its repo-relative path; a method or nested class by its
            name qualified with its owner (`A.save`), when no node has that
            exact name (a Kotlin companion's member by its class's,
            `Repo.create` for `Repo.Companion.create`)
        label: Only nodes of this label
        file: Only nodes whose file, path or source_file is this
        direction: "both", "out" or "in"
        relationship_types: Only these relationship types (at most 20)
        neighbor_labels: Only neighbours with one of these labels (at most 20)
        max_per_type: Neighbours listed per relationship type, clamped to 1..50
        declared_labels: The repository's schema-declared labels to search as
            well; anything that isn't a valid label identifier is ignored
        owner: Only the Class or Function declared in this owner (the dotted
            classes around it, as a ref's `owner` gives it); "" for a top-level one

    Returns:
        `{"status": "found", "node", "outgoing", "incoming", "groups_truncated"}`,
        each group a `{count, results, truncated}` envelope of `{label, name, file}`
        refs (and `owner` for a method or nested class); or `{"status":
        "ambiguous", "count", "candidates", "truncated"}`.
        No match raises ToolError with suggestions. `Repository` nodes are never
        matched or listed. A route's handler stub that folds into a real
        Function (`_fold_target`: the one Function its endpoint's handler
        reference resolved to, or else the one of its name in its route file) is not
        matched, is listed as that Function among neighbours, and has its
        relationships folded into that Function's groups; a stub several
        routes share, each of which resolved to its own Function
        (`_shadowed_stub`), is neither matched nor listed; any other stub
        stays visible (an unresolved handler, file null).
    """
    file = _normalize_file(file)
    if not name.strip():
        raise ToolError("name is empty; pass a node's exact name, or search_component to find one")
    _at_least(1, max_per_type=max_per_type)
    if direction not in _DESCRIBE_DIRECTIONS:
        raise ToolError(f"direction must be 'both', 'out' or 'in', not {_echo(direction)}")
    types = _validated_identifiers(relationship_types, RELATIONSHIP_TYPE_PATTERN, "relationship type")
    neighbour_labels = _validated_identifiers(neighbor_labels, LABEL_PATTERN, "neighbor label")
    declared = tuple(dict.fromkeys(d for d in declared_labels if LABEL_PATTERN.fullmatch(d)))
    if label is not None:
        if not LABEL_PATTERN.fullmatch(label):
            raise ToolError(f"invalid label {_echo(label)}")
        labels: tuple[str, ...] = (label,)
        # A built-in label carries no path; any other label may be a provider's.
        path_labels = frozenset(labels) if label not in schema.BUILTIN_LABELS or label in declared else frozenset()
    else:
        labels = _DESCRIBE_BUILTIN_LABELS + tuple(d for d in declared if d not in _DESCRIBE_BUILTIN_LABELS)
        path_labels = frozenset(declared)
    cap = max(1, min(_DESCRIBE_MAX_PER_TYPE, int(max_per_type)))

    params = {"repo_id": repo_id, "name": name, "file": file}
    owned = (owner, name) if owner is not None else None
    rows = []
    if owned is None:
        rows, _more = _read(engine, _lookup_cypher(labels, path_labels), params, _DESCRIBE_MAX_CANDIDATES + 1)
        owned = _owner_split(name) if not rows else None
    if owned is not None and any(each in _OWNED_LABELS for each in labels):
        params = {**params, "owner": owned[0], "member": owned[1]}
        rows, _more = _read(engine, _owned_lookup_cypher(labels), params, _DESCRIBE_MAX_CANDIDATES + 1)
        if not rows:
            # `Repo.create` written as Kotlin calls it: the member of Repo's companion.
            rows, _more = _read(engine, _owned_lookup_cypher(labels, True), params, _DESCRIBE_MAX_CANDIDATES + 1)

    if not rows:
        suggestions, _ = _read(
            engine, _suggestions_cypher(labels), {"repo_id": repo_id, "name": name}, _DESCRIBE_MAX_SUGGESTIONS
        )
        filters = "".join(
            f", {key}={_echo(value)}" for key, value in (("label", label), ("file", file), ("owner", owner))
            if value is not None
        )
        message = f"no node named {_echo(name)}{filters} in repository {_echo(repo_id)}.{_did_you_mean(suggestions)}"
        raise ToolError(f"{message} {_DESCRIBE_HINT}.")

    if len(rows) > 1:
        candidates = sorted(
            (_unresolved_marked(_node_ref(row)) for row in rows),
            key=lambda r: (r["label"], r["name"], r.get("file") or "", r.get("owner") or ""),
        )
        return {
            "status": "ambiguous",
            "count": len(rows),
            "candidates": candidates[:_DESCRIBE_MAX_CANDIDATES],
            "truncated": len(rows) > _DESCRIBE_MAX_CANDIDATES,
        }

    (row,) = rows
    properties, properties_truncated = _visible_properties(row["properties"])
    node = {**_unresolved_marked(_node_ref(row)), "properties": properties, "properties_truncated": properties_truncated}
    ids = [row["id"]]
    if row["label"] in ("Function", "Class") and row.get("file"):
        stubs, _ = _read(engine, _FOLDED_STUBS, {"id": row["id"]}, _DESCRIBE_MAX_CANDIDATES)
        ids += [stub["id"] for stub in stubs]
    groups, groups_truncated = _read(
        engine,
        _DESCRIBE_GROUPS,
        {"ids": ids, "repo_id": repo_id, "direction": direction, "types": types,
         "labels": neighbour_labels, "cap": cap},
        _DESCRIBE_MAX_GROUPS,
    )
    outgoing: dict[str, Any] = {}
    incoming: dict[str, Any] = {}
    for group in groups:
        refs = [_node_ref(ref) for ref in group["refs"]]
        target = outgoing if group["dir"] == "out" else incoming
        target[group["rel"]] = {"count": group["total"], "results": refs, "truncated": group["total"] > len(refs)}
    return {
        "status": "found",
        "node": node,
        "outgoing": dict(sorted(outgoing.items())),
        "incoming": dict(sorted(incoming.items())),
        "groups_truncated": groups_truncated,
    }


#: Most results god_nodes returns, whatever max_results asks for.
_GOD_NODES_MAX = 500


def god_nodes(
    engine: GraphEngine,
    repo_id: str,
    cross_repo: bool = False,
    max_results: int = 10,
    declared_labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the most-connected nodes in the graph — the core abstractions
    a new agent should look at first to orient itself in an unfamiliar repo.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID to search within (unless cross_repo=True)
        cross_repo: If True, search across all repos
        max_results: Maximum number of results to return, clamped to 1..500
        declared_labels: Schema-declared labels to rank as well (with
            cross_repo, every registered repository's); anything that isn't a
            valid label identifier is ignored

    Returns:
        Dict with count (nodes ranked), results, and truncated flag. Each result
        has name, labels, repo_id, and degree (number of direct relationships).
        `Repository` nodes are not ranked.
    """
    _at_least(1, max_results=max_results)
    declared = tuple(d for d in dict.fromkeys(declared_labels) if LABEL_PATTERN.fullmatch(d))
    labels = _DESCRIBE_BUILTIN_LABELS + tuple(d for d in declared if d not in _DESCRIBE_BUILTIN_LABELS)
    repo_filter = _cross_scope("n") if cross_repo else "n.repo_id = $repo_id"
    # One branch per label, so each is a label scan rather than a scan of every
    # node; UNION drops a node reached through two of its labels.
    nodes = "\n  UNION\n".join(f"  MATCH (n:`{label}`) WHERE {repo_filter} RETURN n" for label in labels)
    limit = max(1, min(_GOD_NODES_MAX, int(max_results)))
    # Ranked and counted in one scan.
    cypher = f"""
    CALL () {{
{nodes}
    }}
    WITH n, COUNT {{ (n)--() }} AS degree
    ORDER BY degree DESC, n.name
    WITH collect({{name: n.name, labels: labels(n), repo_id: n.repo_id, degree: degree}}) AS ranked
    RETURN size(ranked) AS total, ranked[0..$limit] AS top
    """
    params: dict[str, Any] = {"limit": limit} if cross_repo else {"repo_id": repo_id, "limit": limit}
    (row,) = _query(engine, cypher, params)
    envelope = _envelope(row["top"], limit)
    envelope["count"] = row["total"]
    envelope["truncated"] = row["total"] > len(envelope["results"])
    return envelope


def all_declared_node_labels(registry: RepoRegistry | None) -> tuple[str, ...]:
    """Every registered repository's declared node labels, each once, for a cross-repository query."""
    if registry is None:
        return ()
    return tuple(
        dict.fromkeys(label for repo in registry.list_repos() for label in declared_node_labels(registry, repo.repo_id))
    )


# Dependency-edge types find_dependency_cycles is allowed to traverse. A
# strict subset of schema.RELATIONSHIP_TYPES: containment (CONTAINS, RUNS),
# provenance (MODIFIES, RESOLVES, REFERENCES) and intent (SATISFIES,
# DOCUMENTED_BY, DECIDED_BY, SUPERSEDES, MENTIONS) edges are not dependencies
# and a "cycle" over them means nothing. This is an allow-list, not a filter:
# it is the only thing besides a clamped integer ever interpolated into the
# cycle query's Cypher, so an unlisted value can never reach the graph.
_CYCLE_RELATIONSHIPS: tuple[str, ...] = ("CALLS", "DEPENDS_ON", "EXTENDS", "IMPORTS", "USES")
# Cycle length in edges. The floor of 2 excludes single-edge self-loops; the
# ceiling bounds a variable-length expansion that is exponential in practice.
_CYCLE_MIN_LENGTH = 2
_CYCLE_MAX_LENGTH = 8
# Raw paths pulled back before deduplication. One cycle of length L is found
# L times (once per rotation), and a dense component yields far more paths
# than distinct cycles, so this caps the expansion rather than the answer.
_CYCLE_RAW_PATH_LIMIT = 500


def _cycle_node_key(node: dict[str, Any]) -> tuple[str, str, str, tuple[str, ...]]:
    """Total, null-safe ordering key identifying one node inside a cycle.

    Null-safe because it is used to rotate and sort: `file` is absent on
    Module/Endpoint nodes and `repo_id`/`name` can be missing on a partially
    indexed node, and a None would make the tuple uncomparable at that
    position. Labels are sorted because Neo4j's `labels()` ordering is not
    guaranteed stable across calls, and an unstable key would make the
    canonical rotation (and therefore deduplication) non-deterministic.
    """
    return (
        node.get("repo_id") or "",
        node.get("name") or "",
        node.get("file") or "",
        tuple(sorted(node.get("labels") or [])),
    )


def find_dependency_cycles(
    engine: GraphEngine,
    repo_id: str,
    relationship: str = "IMPORTS",
    max_length: int = 5,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Find circular dependency chains over one already-indexed relationship type.

    Plain read-only Cypher over the existing graph — no GDS, no projection, no
    writes. A cycle is reported once regardless of which of its nodes the
    traversal started from: every rotation of the same node ring collapses to
    a single canonical result. Two cycles over the same nodes in opposite
    directions are genuinely different dependency chains and are both reported.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID to search within (unless cross_repo=True)
        relationship: Dependency edge type to traverse; case-insensitive, one of
            CALLS, DEPENDS_ON, EXTENDS, IMPORTS, USES. Most IMPORTS edges join
            two Modules, but a Kotlin import ends at the Class or Function it
            names (no edge leaves those), so a Kotlin file's imports close no
            IMPORTS cycle.
        max_length: Longest cycle to look for, in edges; clamped to 2..8
        cross_repo: If True, allow cycles that cross repository boundaries
        max_results: Maximum number of cycles to return in the envelope

    Returns:
        Dict with count, results, and truncated flag. Each result is
        {length, nodes}, where `nodes` lists the cycle's members in traversal
        order starting from its canonical member (each with name, labels,
        repo_id, file) and the closing repeat of the first node is dropped.
        Acyclic data returns an empty envelope.

        `count` is a lower bound rather than an exact total whenever
        `truncated` is True: the underlying traversal stops at
        `_CYCLE_RAW_PATH_LIMIT` raw paths, so cycles beyond that clip were
        never seen and cannot be counted. `truncated` is therefore True when
        that clip is hit even if fewer than `max_results` cycles came back.

    Raises:
        ToolError: if `relationship` is not one of the supported types. This
            fails loudly rather than returning an empty envelope, because an
            empty envelope from a cycle search reads as "no cycles here" — a
            false clean bill of health on a typo'd argument.
    """
    _at_least(_CYCLE_MIN_LENGTH, max_length=max_length)
    _at_least(1, max_results=max_results)
    validated = str(relationship).strip().upper()
    if validated not in _CYCLE_RELATIONSHIPS:
        raise ToolError(
            f"unsupported relationship {_echo(relationship)} for cycle detection; supported types are: "
            + ", ".join(_CYCLE_RELATIONSHIPS)
        )

    length = max(_CYCLE_MIN_LENGTH, min(_CYCLE_MAX_LENGTH, int(max_length)))
    limit = max(1, int(max_results))

    # `start.repo_id` is repeated here rather than left to the ALL(...)
    # predicate below: ALL(nodes(path)) can only be evaluated once a whole
    # path exists, so without this the planner would expand from every node in
    # the database and discard the out-of-repo paths afterwards.
    start_scope = _cross_scope("start", "AND") if cross_repo else "AND start.repo_id = $repo_id"
    path_scope = (
        f"WHERE ALL(n IN nodes(path) WHERE {_cross_scope('n')})" if cross_repo
        else "WHERE ALL(n IN nodes(path) WHERE n.repo_id = $repo_id)"
    )
    # Lossless prefilter: every node on a cycle of this type necessarily has
    # both an outgoing and an incoming edge of it, so this can only remove
    # nodes that could not have started a cycle — it narrows the scan without
    # narrowing the answer.
    # Only `validated` (an allow-list member) and `length` (a clamped int) are
    # interpolated; every caller-supplied value stays in $repo_id. Node fields
    # are projected explicitly rather than returning whole nodes, so an
    # arbitrary indexed property can never ride out past the sanitizer below.
    cypher = f"""
    MATCH (start)
    WHERE (start)-[:{validated}]->() AND ()-[:{validated}]->(start)
    {start_scope}
    MATCH path = (start)-[:{validated}*{_CYCLE_MIN_LENGTH}..{length}]->(start)
    {path_scope}
    RETURN [n IN nodes(path) | {{name: n.name, labels: labels(n),
                                 repo_id: n.repo_id, file: n.file}}] as nodes
    ORDER BY [n IN nodes(path) | elementId(n)],
             [r IN relationships(path) | elementId(r)]
    LIMIT {_CYCLE_RAW_PATH_LIMIT}
    """
    params = {} if cross_repo else {"repo_id": repo_id}

    results = _query(engine, cypher, params)

    cycles: dict[tuple, dict[str, Any]] = {}
    for row in results:
        raw_nodes = row.get("nodes") or []
        # A closed path repeats its start node at the end; drop that repeat.
        # Fewer than three entries means the ring collapses to a single node
        # (a self-loop), which is not a dependency cycle between components.
        if len(raw_nodes) < 3:
            continue
        nodes = raw_nodes[:-1]
        keys = [_cycle_node_key(n) for n in nodes]
        # Neo4j's variable-length expansion uses trail semantics: it forbids
        # repeating a *relationship*, not a *node*. A path that revisits a node
        # (a figure-eight, or a parallel self-loop traversed twice) is not a
        # simple cycle, and it has no unique smallest member to rotate to.
        if len(set(keys)) != len(keys):
            continue
        offset = keys.index(min(keys))
        canonical = tuple(keys[offset:] + keys[:offset])
        if canonical in cycles:
            continue
        rotated = nodes[offset:] + nodes[:offset]
        cycles[canonical] = {
            "length": len(rotated),
            # _envelope's sanitizer only reaches an item's own string values,
            # never a nested list of dicts, so the nodes are sanitized here.
            "nodes": [_sanitize_row(n) for n in rotated],
        }

    rows = [cycles[key] for key in sorted(cycles)]
    rows.sort(key=lambda row: row["length"])

    envelope = _envelope(rows, limit)
    if len(results) >= _CYCLE_RAW_PATH_LIMIT:
        # The raw expansion was clipped, so there may be cycles neither
        # `count` nor `results` reflects. _envelope compares against
        # max_results alone and cannot see that.
        envelope["truncated"] = True
    return envelope


_INSIGHTS_NOT_COMPUTED = (
    "graph insights have not been computed for this repository yet; the DevGraph agent "
    "computes them after indexing, or run `devgraph insights <repo_id>`"
)
_MAX_MEMBERS_PER_COMMUNITY = 20
# Rows pulled for key_nodes before the envelope trims to max_results.
_KEY_NODES_LIMIT = 50


def find_communities(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    max_results: int = 10,
    members_per_community: int = 5,
) -> dict[str, Any]:
    """Return the repository's communities (Louvain over dependency and
    containment edges), largest first.

    Each result is {community, label, size, top_members}; `top_members`
    (highest PageRank first, each {name, labels, file, pagerank}) is filled
    for the communities inside max_results. `count` covers the largest 50
    communities the repository stores.

    Raises:
        ToolError: unknown repo_id, or insights never computed for it.
    """
    _registered(registry, repo_id)
    _at_least(1, max_results=max_results, members_per_community=members_per_community)
    summary = read_insights(engine, repo_id)
    if summary is None:
        raise ToolError(_INSIGHTS_NOT_COMPUTED)
    communities = summary["communities"]
    shown = [c["community"] for c in communities[:max(0, max_results)]]
    k = max(1, min(members_per_community, _MAX_MEMBERS_PER_COMMUNITY))
    with _timeouts_as_tool_errors():
        members = community_members(engine, repo_id, shown, k, timeout_s=BUILTIN_TIMEOUT_S) if shown else {}
    # Members are nested dicts, which _envelope's per-row sanitizing doesn't
    # reach, so they are sanitized here.
    rows = [
        {**c, "top_members": [_sanitize_row(m) for m in members.get(c["community"], [])]}
        if c["community"] in shown
        else c
        for c in communities
    ]
    return _envelope(rows, max_results)


def key_nodes(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    metric: str = "pagerank",
    max_results: int = 10,
) -> dict[str, Any]:
    """Rank the repository's entities by PageRank over dependency edges (core
    abstractions) or betweenness (bridges between subsystems).

    Returns {count, results, truncated} of {name, labels, file, score,
    community}. `metric` is "pagerank" or "betweenness"; the property it
    selects comes from an allow-list, never from the argument itself.

    CALLS edges are resolved by name, so widely used generic method names
    (such as get or close) can rank high.

    Raises:
        ToolError: unknown repo_id or metric, or insights never computed.
    """
    _registered(registry, repo_id)
    _at_least(1, max_results=max_results)
    metric_key = metric.strip().lower() if isinstance(metric, str) else ""
    if metric_key not in INSIGHT_METRICS:
        raise ToolError(f"metric must be {' or '.join(INSIGHT_METRICS)}, not {_echo(metric)}")
    if read_insights(engine, repo_id) is None:
        raise ToolError(_INSIGHTS_NOT_COMPUTED)
    with _timeouts_as_tool_errors():
        rows = top_nodes(engine, repo_id, metric_key, _KEY_NODES_LIMIT, timeout_s=BUILTIN_TIMEOUT_S)
    return _envelope(rows, max_results)


def trace_request_flow(
    engine: GraphEngine,
    repo_id: str,
    start_endpoint: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Trace the request flow from an endpoint through services, datastores, and queues.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID (or cross-repo search if cross_repo=True)
        start_endpoint: The endpoint's name, `"<METHOD> <path>"` (`"GET /users/<id>"`),
            or a bare path (`"/users/<id>"`), which starts from that path's endpoint
            for every method. A route on a router whose mount its file doesn't show
            is its file's own endpoint, `"<METHOD> ?<path> (<file>)"`
            (`"GET ?/:id (routes/users.js)"`), reached by that full name only.
            A Django route, stored without its path's leading "/", is reached
            with it too (`"* /orders/"` or `"/orders/"` for `* orders/`)
        cross_repo: If True, cross repository boundaries

    Returns:
        Dict with `components` (the start endpoints and every node within five
        hops, each {name, labels, repo_id, file, owner}: an endpoint's file is
        its route file, and file is null and owner left out for any other node
        with no file) and `edges` ({type, from, to} per
        relationship, `from`/`to` naming its components). A route's handler
        stub that folds into a real Function is listed as that Function, its
        edges with it, and the flow goes on from the Function's calls (four
        more hops): the one Function (or Class) of its name the traced endpoint
        IMPLEMENTS too, when a name several routes' handlers share makes the
        stub theirs together, else `_fold_target`'s; a stub that folds into
        none stays listed as it is.

    Raises:
        ToolError: no endpoint has that name or path.
    """
    repo_filter = _cross_scope("start", "AND") if cross_repo else "AND start.repo_id = $repo_id"
    node = (
        # An Endpoint has no file of its own: its route file (`source`). A
        # handler stub's `source` is a route file too, not its own: it stays null.
        "{id: elementId(n), name: n.name, labels: labels(n), repo_id: n.repo_id, "
        "file: coalesce(n.file, CASE WHEN n:Endpoint THEN n.source END), owner: "
        + _OWNER_OF.format(v="n") + "}"
    )
    edge = "{id: elementId(r), type: type(r), start: elementId(startNode(r)), end: elementId(endNode(r))}"
    cypher = f"""
    MATCH (start:Endpoint)
    WHERE (start.name IN [$endpoint, $django]
           OR ($bare AND (start.name ENDS WITH ' ' + $endpoint OR start.name ENDS WITH ' ' + $django)))
    {repo_filter}
    WITH collect(start) AS starts
    CALL (starts) {{
        UNWIND starts AS s
        MATCH (s)-[rels*1..5]->(node)
        UNWIND rels AS r
        RETURN collect(DISTINCT node) AS reached, collect(DISTINCT r) AS edges
    }}
    CALL (reached, starts) {{
        UNWIND reached AS raw
        {_fold_target("raw")}
        CALL (raw, starts) {{
            UNWIND starts AS s
            MATCH (s)-[:IMPLEMENTS]->(raw)
            WHERE raw:Function AND coalesce(raw.file, '') = ''
            MATCH (s)-[:IMPLEMENTS]->(twin)
            WHERE (twin:Function OR twin:Class) AND twin.repo_id = raw.repo_id AND twin.name = raw.name
              AND twin.file <> ''
            RETURN collect(DISTINCT twin) AS own
        }}
        WITH raw, CASE WHEN size(own) = 1 THEN own[0] ELSE folded END AS folded WHERE folded IS NOT NULL
        RETURN collect({{stub: elementId(raw), target: elementId(folded)}}) AS folds, collect(DISTINCT folded) AS targets
    }}
    CALL (targets) {{
        UNWIND targets AS t
        MATCH (t)-[rels*1..4]->(node)
        UNWIND rels AS r
        RETURN collect(DISTINCT node) AS further, collect(DISTINCT r) AS further_edges
    }}
    RETURN [n IN starts | {node}] AS starts, [n IN reached + targets + further | {node}] AS nodes,
        folds, [r IN edges + further_edges | {edge}] AS edges
    """
    bare = not any(ch.isspace() for ch in start_endpoint.strip())
    # A Django pattern is stored without its leading "/" (`* orders/`), which
    # explain_architecture and list_endpoints show (`* /orders/`): either matches.
    method, _, path = ("", "", start_endpoint.strip()) if bare else start_endpoint.strip().partition(" ")
    django = f"{method} {path[1:]}".lstrip() if path.startswith("/") else None
    params = {"endpoint": start_endpoint, "bare": bare, "django": django}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not results or not results[0]["starts"]:
        raise _not_found(engine, repo_id, "endpoint", start_endpoint, ("Endpoint",), cross_repo)
    (row,) = results
    fold = {f["stub"]: f["target"] for f in row["folds"]}
    nodes: dict[str, dict[str, Any]] = {}
    for n in row["starts"] + row["nodes"]:
        if n["id"] not in fold:
            nodes.setdefault(n["id"], _located({k: n[k] for k in ("name", "labels", "repo_id", "file", "owner")}))
    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for e in row["edges"]:
        start, end = fold.get(e["start"], e["start"]), fold.get(e["end"], e["end"])
        if start in nodes and end in nodes:
            edges.setdefault((e["type"], start, end), {"type": e["type"], "from": nodes[start]["name"], "to": nodes[end]["name"]})
    return {"components": list(nodes.values()), "edges": list(edges.values())}


def get_service_dependencies(
    engine: GraphEngine,
    repo_id: str,
    service_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Get all dependencies (services, datastores, queues) for a given service.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        service_name: Name of the service to analyze
        cross_repo: If True, include cross-repo dependencies

    Returns:
        Dict with direct_dependencies, transitive_dependencies, and potential_impacts
    """
    repo_filter = _cross_scope("s", "WHERE") if cross_repo else "WHERE s.repo_id = $repo_id"
    cypher = f"""
    MATCH (s:Service {{name: $service_name}})
    {repo_filter}
    OPTIONAL MATCH (s)-[:USES|DEPENDS_ON|RUNS]->(dep)
    OPTIONAL MATCH (s)-[:CALLS]->(called:Service)
    RETURN s.name as service,
           [d IN COLLECT(DISTINCT {{name: dep.name, type: labels(dep)[0], repo_id: dep.repo_id}}) WHERE d.name IS NOT NULL] as dependencies,
           [c IN COLLECT(DISTINCT {{name: called.name, repo_id: called.repo_id}}) WHERE c.name IS NOT NULL] as calls
    """
    params = {"service_name": service_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not results:
        raise _not_found(engine, repo_id, "service", service_name, ("Service",), cross_repo)
    return results[0]


def find_callers(
    engine: GraphEngine,
    repo_id: str,
    target_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
    scope_to_class: str | None = None,
    modified_within_commits: int | None = None,
    resolved_only: bool = False,
    file: str | None = None,
    extra_labels: tuple[str, ...] = (),
    owner: str | None = None,
    include_tests: bool = True,
) -> dict[str, Any]:
    """Find all functions, services, or endpoints that call a given target.

    A CALLS edge carries a `confidence`, in every source language:
    "resolved" (the callee was found through the caller's own scope, imports
    or types, in a file it names), "package" (in a file the caller's
    imports reach less exactly: under an imported package or a re-exporting
    module, a class file found by suffix in another module, a C# class file
    `/T.cs`, a C++ header's same-stem source) or "name" (a method on a
    receiver nothing types, linked to every Function of that name). An
    edge written before index format 4 (Python), 6 (JS/TS), 7 (Go, Java,
    Kotlin) or 8 (Rust, C#, C++) carries none until the automatic rescan.
    Each caller is returned once, with its best confidence, and
    resolved callers come first. When a method-body call's enclosing class
    is known at index time, the edge carries a `caller_class` property
    recording it — pass scope_to_class to narrow results to callers made
    from within a specific class's own methods (opt-in; omitted, behavior is
    unchanged/repo-wide).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        target_name: Name of the function/service/endpoint being called
        cross_repo: If True, find callers across repos
        max_results: Maximum number of results to return in the envelope
        scope_to_class: If given, only return callers whose call to
            target_name was made from within this class's own method bodies
        modified_within_commits: If given, only return targets touched within
            the last N commits repo-wide (by `last_modified_at`, staged by git-history
            recency indexing). A target never touched by that staging has no
            `last_modified_at` property and is excluded, never silently included.
            If fewer than N commits exist repo-wide, no cutoff applies and this
            filter is a no-op.
        resolved_only: If True, only return callers whose edge confidence is
            "resolved" or "package" (dropping bare-name matches and every
            edge without a confidence)
        file: Repo-relative path of the target's defining file, to pick one of
            several same-named targets
        extra_labels: The repository's declared labels, looked up besides the built-in ones
        owner: The declarations the target is in ("" for a top-level one), to
            pick a method of one class (`describe_node`'s `owner`); a target
            named with them (`A.run`, `A::run`) does the same when no node has
            that exact name
        include_tests: If False, leave out callers in test code (`is_test_path`
            of their file); either way they come after every other caller

    Returns:
        Dict with count, results, truncated flag and test_count (the callers in
        test code, listed or not) containing callers with their
        types, repo_id, file, owner and start_line (when the caller has them: a
        Module has neither) and confidence (same-named callers are separate
        rows, told apart by file and owner); count 0 for a known target nothing calls. When targets
        of that name lie in several files and `file` is not given, each row adds
        `target_files`, the files of the targets it calls (at most 10, with
        `target_file_count` past that; a file-less target is never listed), and
        the dict adds `ambiguous_targets`, a {count, results, truncated} envelope of their
        {label, file} (file None for a file-less one; repo_id too when cross_repo).
        `routed_by` is a {count, results, truncated} envelope of the Endpoints
        that IMPLEMENTS the target (a route is no caller), each as
        list_endpoints lists it, ordered by name, with `target_files` as a
        caller row has them.

    Raises:
        ToolError: no node has that name, or none in `file`.
    """
    _at_least(1, max_results=max_results, modified_within_commits=modified_within_commits)
    cutoff = None
    if modified_within_commits is not None:
        cutoff = _resolve_recency_cutoff(engine, repo_id, modified_within_commits)
    file = _normalize_file(file)
    labels = component_labels(extra_labels)
    target_name, owner = _owned_target(engine, repo_id, target_name, owner, cross_repo, labels)
    ambiguous, labels, _ = _resolve_targets(engine, repo_id, target_name, cross_repo, file, labels, owner=owner)
    target_files = ", collect(DISTINCT target.file) AS target_files" if ambiguous else ""

    class_filter = "AND rel.caller_class = $scope_to_class" if scope_to_class else ""
    recency_filter = "AND target.last_modified_at >= $cutoff" if cutoff is not None else ""
    resolved_filter = "AND rel.confidence IN ['resolved', 'package']" if resolved_only else ""
    callers = f"""
    {_named_nodes(labels, "target_name", cross_repo, "target")}
    MATCH (caller)-[rel:CALLS]->(target)
    WHERE true
    {class_filter}
    {recency_filter}
    {resolved_filter}
    AND ($file IS NULL OR target.file = $file)
    AND {_OWNER_MATCH.replace("n.", "target.")}
    """
    is_test = "coalesce(caller.file, caller.source_file, '') =~ $test_path"
    cypher = f"""
    {callers}
    WITH caller, min(CASE rel.confidence WHEN 'resolved' THEN 0 WHEN 'package' THEN 1 WHEN 'name' THEN 3
                     ELSE 2 END) AS rank{target_files}
    WITH *, {is_test} AS is_test
    WHERE $include_tests OR NOT is_test
    RETURN caller.name as name, labels(caller) as type, caller.repo_id as repo_id,
           coalesce(caller.file, caller.source_file) as file,
           {_OWNER_OF.format(v="caller")} as owner, caller.start_line as start_line,
           CASE rank WHEN 0 THEN 'resolved' WHEN 1 THEN 'package' WHEN 3 THEN 'name' END as confidence{target_files and ", target_files"}
    ORDER BY is_test, rank, name, file
    LIMIT {LIST_ROW_LIMIT + 1}
    """
    params = {
        "target_name": target_name, "file": file, "owner": owner, "test_path": TEST_PATH_CYPHER,
        "include_tests": include_tests,
    }
    if not cross_repo:
        params["repo_id"] = repo_id
    if scope_to_class:
        params["scope_to_class"] = scope_to_class
    if cutoff is not None:
        params["cutoff"] = cutoff

    # Counted apart from the capped rows, so it is exact however many there are.
    tests = _query(engine, f"{callers} WITH DISTINCT caller WHERE {is_test} RETURN count(caller) AS tests", params)
    # A route reaches its handler by IMPLEMENTS, not CALLS: listed apart.
    routes = f"""
    {_named_nodes(labels, "target_name", cross_repo, "target")}
    MATCH (e:Endpoint)-[:IMPLEMENTS]->(target)
    WHERE true
    {recency_filter}
    AND ($file IS NULL OR target.file = $file)
    AND {_OWNER_MATCH.replace("n.", "target.")}
    WITH collect({{e: e, file: target.file}}) AS pairs
    {_capped_endpoints("pairs")}
    RETURN endpoint_count, endpoints
    """
    routed_by = next(iter(_query(engine, routes, {**params, "max_results": max_results})), None)
    results, more = _list_query(engine, cypher, params)
    results = [_located(row) for row in results]
    if ambiguous:
        results = [_with_target_files(row, row["target_files"]) for row in results]
    envelope = _capped_envelope(results, more, max_results)
    envelope["test_count"] = tests[0]["tests"] if tests else 0
    envelope["routed_by"] = _endpoints_envelope(
        routed_by or {"endpoint_count": 0, "endpoints": []}, max_results, tagged=ambiguous is not None
    )
    if ambiguous:
        envelope["ambiguous_targets"] = ambiguous
    return envelope


def find_related_files(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
    file: str | None = None,
    extra_labels: tuple[str, ...] = (),
    owner: str | None = None,
    include_tests: bool = True,
) -> dict[str, Any]:
    """Find all files related to a component (via CONTAINS, IMPORTS, CALLS relationships).

    Every CALLS edge counts whatever its `confidence` (see find_callers).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component
        cross_repo: If True, include cross-repo relationships
        max_results: Maximum number of results per list to return in the envelope
        file: Repo-relative path of the component's defining file, to pick one of
            several same-named components
        extra_labels: The repository's declared labels, looked up besides the built-in ones
        owner: The declarations the component is in, as in find_callers (`A.run` too)
        include_tests: If False, leave out related modules and components in
            test code (devgraph/test_paths.py, as in find_callers); either way
            they come after the rest of each list

    Returns:
        Dict with containing_modules, imported_modules, and related_components,
        each wrapped as {count, results, truncated, test_count} envelope
        objects; test_count counts the list's entries in test code, listed or not.
        imported_modules are the names of what `component_name` IMPORTS: a
        Module's path, or, for a Kotlin import, the Class or Function it names.
        When components of that name lie in several files and `file` is not
        given, each row is `{name, target_files}` (the files of the components it
        is related to, capped as in find_callers) instead of a bare name, and the
        dict adds `ambiguous_targets`, a {count, results, truncated} envelope of their
        {label, file} (file None for a file-less one; repo_id too when cross_repo)

    Raises:
        ToolError: no node has that name, or none in `file`.
    """
    _at_least(1, max_results=max_results)
    file = _normalize_file(file)
    labels = component_labels(extra_labels)
    component_name, owner = _owned_target(engine, repo_id, component_name, owner, cross_repo, labels)
    ambiguous, labels, _ = _resolve_targets(engine, repo_id, component_name, cross_repo, file, labels, owner=owner)
    cypher = f"""
    {_named_nodes(labels, "component_name", cross_repo)}
    WITH n WHERE ($file IS NULL OR n.file = $file) AND {_OWNER_MATCH}
    OPTIONAL MATCH (m:Module)-[:CONTAINS*]->(n)
    OPTIONAL MATCH (n)-[:IMPORTS]->(imported)
    OPTIONAL MATCH (n)-[:CALLS]->(related)
    WITH CASE WHEN $split THEN n.file END AS target_file, m, imported, related
    RETURN target_file,
        COLLECT(DISTINCT m.name) as containing_modules,
        COLLECT(DISTINCT CASE WHEN imported IS NOT NULL THEN {{name: imported.name,
            path: coalesce(imported.file, imported.source_file, imported.name)}} END) as imported_modules,
        COLLECT(DISTINCT CASE WHEN related IS NOT NULL THEN {{name: related.name,
            path: coalesce(related.file, related.source_file)}} END) as related_components
    ORDER BY target_file
    """
    params = {"component_name": component_name, "file": file, "owner": owner, "split": bool(ambiguous)}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    keys = ("containing_modules", "imported_modules", "related_components")
    answer: dict[str, Any] = {}
    for key in keys:
        # One entry per name (a Module's name is its path), with the target
        # files it was found for, in the order the rows list them.
        by_name: dict[str, dict[str, Any]] = {}
        for row in results:
            for item in row[key]:
                name, path = (item, item) if key == "containing_modules" else (item["name"], item["path"])
                entry = by_name.setdefault(name, {"name": name, "path": None, "files": []})
                entry["path"] = entry["path"] or path
                entry["files"].append(row["target_file"])
        shown, test_count = _tests_last(list(by_name.values()), include_tests, "path")
        rows = [_with_target_files({"name": e["name"]}, e["files"]) if ambiguous else e["name"] for e in shown]
        answer[key] = {**_envelope(rows, max_results), "test_count": test_count}
    if ambiguous:
        answer["ambiguous_targets"] = ambiguous
    return answer


def summarise_repository(
    engine: GraphEngine,
    repo_id: str,
) -> dict[str, Any]:
    """Get a high-level summary of a repository's architecture.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID to summarize

    Returns:
        Summary with the repository's node counts by type: services, modules,
        classes, functions, endpoints, databases, vector stores, queues and caches
    """
    # Each count runs as its own uncorrelated subquery (CALL {...}) rather
    # than chaining OPTIONAL MATCHes in one query. Chained OPTIONAL MATCHes
    # on independent label patterns force Neo4j to compute their cartesian
    # product before COUNT(DISTINCT ...) collapses it back down — on a real
    # repo with hundreds of Function/Class nodes that product explodes
    # combinatorially and the query effectively hangs. Subqueries keep each
    # count's cardinality independent.
    count_cypher = """
    CALL () { MATCH (s:Service {repo_id: $repo_id}) RETURN COUNT(s) as service_count }
    CALL () { MATCH (m:Module {repo_id: $repo_id}) RETURN COUNT(m) as module_count }
    CALL () { MATCH (c:Class {repo_id: $repo_id}) RETURN COUNT(c) as class_count }
    CALL () { MATCH (f:Function {repo_id: $repo_id}) RETURN COUNT(f) as function_count }
    CALL () { MATCH (e:Endpoint {repo_id: $repo_id}) RETURN COUNT(e) as endpoint_count }
    CALL () { MATCH (d:Database {repo_id: $repo_id}) RETURN COUNT(d) as database_count }
    CALL () { MATCH (v:VectorStore {repo_id: $repo_id}) RETURN COUNT(v) as vectorstore_count }
    CALL () { MATCH (q:Queue {repo_id: $repo_id}) RETURN COUNT(q) as queue_count }
    CALL () { MATCH (k:Cache {repo_id: $repo_id}) RETURN COUNT(k) as cache_count }
    RETURN
        $repo_id as repo_name,
        service_count, module_count, class_count, function_count,
        endpoint_count, database_count, vectorstore_count, queue_count, cache_count
    """
    results = _query(engine, count_cypher, {"repo_id": repo_id})
    if results:
        return results[0]
    return {
        "repo_name": repo_id,
        "service_count": 0,
        "module_count": 0,
        "class_count": 0,
        "function_count": 0,
        "endpoint_count": 0,
        "database_count": 0,
        "vectorstore_count": 0,
        "queue_count": 0,
        "cache_count": 0,
    }


_COMPARE_LAST_INDEX = "impacted_callers come from the last index of the working tree, not from either ref"
_COMPARE_STATUSES = ("added", "removed", "modified", "renamed")
_COMPARE_LISTS = ("added", "removed", "changed")
_COMPARE_CALLERS_CYPHER = """
UNWIND $targets AS t
CALL (t) {
  MATCH (n:Function {repo_id: $repo_id, name: t.name, file: t.file}) WHERE n.owner IS NULL OR n.owner = t.owner
  RETURN n
  UNION
  MATCH (n:Class {repo_id: $repo_id, name: t.name, file: t.file}) WHERE n.owner IS NULL OR n.owner = t.owner
  RETURN n
}
MATCH (caller)-[:CALLS]->(n)
WHERE caller.repo_id = $repo_id
RETURN DISTINCT caller.name AS caller, labels(caller)[0] AS caller_type,
       caller.file AS caller_file,
       CASE WHEN caller.file IS NOT NULL THEN coalesce(caller.owner, '') END AS caller_owner,
       n.name AS calls, n.file AS calls_file, coalesce(n.owner, '') AS calls_owner
ORDER BY caller_file, caller_owner, caller, calls_file, calls_owner, calls
"""


def _compare_file(change: Any) -> dict[str, Any]:
    """One C1 `<file>` entry, every string sanitised."""
    from devgraph.mcp.tool_plane import _sanitize_deep

    entry: dict[str, Any] = {"path": change.path, "status": change.status}
    if change.old_path is not None:
        entry["old_path"] = change.old_path
    entry["language"] = change.language
    entry["symbols"] = change.symbols
    if change.symbols is None:
        entry["symbols_skipped"] = change.symbols_skipped
    if change.symbols_truncated:
        entry["symbols_truncated"] = True
    return _sanitize_deep(entry)


def _compare_callers(
    engine: GraphEngine, repo_id: str, targets: list[dict[str, str]], max_callers: int
) -> tuple[dict[str, Any] | None, list[str]]:
    """C8: the graph's callers of `targets`, as an envelope plus notices. Best-effort:
    a graph failure gives `None` and a notice, never an error."""
    from neo4j.exceptions import DriverError, Neo4jError

    if not targets:
        return _envelope([], max_callers), []
    try:
        rows, more = engine.run_read_cypher(
            _COMPARE_CALLERS_CYPHER,
            {"repo_id": repo_id, "targets": targets},
            timeout_s=DEFAULT_TIMEOUT_S,
            max_rows=max_callers + 1,
        )
    except (Neo4jError, DriverError) as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        return None, [f"impacted callers unavailable: {code}"]
    callers = {
        "count": len(rows),
        "results": [_sanitize_row(r) for r in rows[:max_callers]],
        "truncated": more or len(rows) > max_callers,
    }
    return callers, [_COMPARE_LAST_INDEX]


def compare_branches(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    branch_a: str,
    branch_b: str,
) -> dict[str, Any]:
    """What changed on `branch_b` (head) since it diverged from `branch_a` (base), like
    `git diff branch_a...branch_b`: files, per-file symbols added/removed/changed, and
    the graph's callers of the changed and removed symbols.

    Reads git objects in memory and never fetches; the graph is only read. See
    docs/superpowers/specs/2026-10-08-compare-branches-design.md (C1, C7, C8).

    Raises:
        ToolError: an unknown repo_id, a repository without its own .git, a bad or
            unknown ref, no common history, or git failing (C7).
    """
    from devgraph.indexer.git_history import compare as git_compare

    record = _registered(registry, repo_id)
    try:
        with git_compare.open_comparison(record.path, repo_id, branch_a, branch_b) as comparison:
            detailed = git_compare.symbol_detail(comparison)
    except git_compare.CompareError as exc:
        raise ToolError(str(exc)) from exc

    max_files = git_compare._COMPARE_MAX_FILES
    symbol_counts = {name: 0 for name in _COMPARE_LISTS}
    targets: list[dict[str, str]] = []
    for change in detailed:
        if change.symbols is None:
            continue
        for name in _COMPARE_LISTS:
            symbol_counts[name] += len(change.symbols[name])
        for name in ("changed", "removed"):
            for entry in change.symbols[name]:
                target = {"name": entry["name"], "file": change.path, "owner": entry.get("owner", "")}
                if target not in targets:
                    targets.append(target)
    callers, notices = _compare_callers(engine, repo_id, targets, git_compare._COMPARE_MAX_CALLERS)
    reasons = comparison.truncated_reasons
    return {
        "base": {"ref": _sanitize_value(branch_a), "commit": comparison.base_commit.hexsha},
        "head": {"ref": _sanitize_value(branch_b), "commit": comparison.head_commit.hexsha},
        "merge_base": comparison.merge_base.hexsha,
        "counts": {status: sum(c.status == status for c in comparison.changes) for status in _COMPARE_STATUSES},
        "files": {
            "count": len(comparison.changes),
            "results": [_compare_file(c) for c in detailed],
            "truncated": len(comparison.changes) > max_files,
        },
        "symbol_counts": symbol_counts,
        "impacted_callers": callers,
        "truncated": bool(reasons),
        "truncated_reasons": list(reasons),
        "notices": notices,
    }


def impact_analysis(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
    file: str | None = None,
    extra_labels: tuple[str, ...] = (),
    owner: str | None = None,
    include_tests: bool = True,
) -> dict[str, Any]:
    """Analyze the impact of changing a component on the rest of the system.

    Every CALLS edge counts as a dependency whatever its `confidence` (see
    find_callers), so a Python method called on an untyped receiver keeps
    its by-name dependents: impact errs toward listing too much.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component to analyze
        cross_repo: If True, include cross-repo impacts
        max_results: Maximum number of results per dependents list to return in the envelope
        file: Repo-relative path of the component's defining file, to pick one of
            several same-named components
        extra_labels: The repository's declared labels, looked up besides the built-in ones
        owner: The declarations the component is in, as in find_callers (`A.run` too)
        include_tests: If False, leave out dependents in test code (as in
            find_callers); either way they come after every other dependent

    Returns:
        Dict with direct_dependents and transitive_dependents ({name, type, file,
        owner}, owner left out and file null when the dependent has none) wrapped as {count, results, truncated}
        envelopes, plus risk_level (computed from true untruncated count). Transitive
        dependents are 2 to `IMPACT_MAX_DEPTH` hops away; a node is listed at its
        nearest hop only, so a direct dependent is never also transitive. When
        components of that name lie in several files and `file` is not given, the
        dict adds `ambiguous_targets`, a {count, results, truncated} envelope of their
        {label, file} (file None for a file-less one; repo_id too when cross_repo). Up to `IMPACT_MAX_SPLIT` files, each file's are traced
        apart: a dependent is listed at its nearest hop to any of them, with
        `target_files` (capped as in find_callers): for a direct dependent the
        files of the targets it depends on directly, for a transitive one those of
        every target that reaches it within `IMPACT_MAX_DEPTH` hops; risk_level is
        the riskiest target's. Past that, one expansion covers them all, as for a
        single target, with a notice to pass `file`. Another same-named target can
        itself be listed as a dependent. Each list is ordered by name, file and
        owner, dependents in test code last, and its envelope adds
        `test_count`, its dependents in test code (listed or not); risk_level
        counts them too. `endpoints` is a {count, results, truncated} envelope of
        the Endpoints that IMPLEMENTS the component or any dependent (a route
        reaches its handler that way, so it is never a dependent; risk_level
        leaves them out), each as list_endpoints lists it, ordered by name;
        when each file's targets are traced apart, each also has the
        `target_files` of the targets it reaches.

    Raises:
        ToolError: no node has that name, or none in `file`.
    """
    _at_least(1, max_results=max_results)
    file = _normalize_file(file)

    def dependents_envelope(rows: list[dict[str, Any]]) -> dict[str, Any]:
        rows = sorted(rows, key=lambda d: (d["name"] or "", d.get("file") or "", d.get("owner") or "", d["type"] or ""))
        shown, test_count = _tests_last(rows, include_tests)
        return {**_envelope(shown, max_results), "test_count": test_count}

    labels = component_labels(extra_labels)
    component_name, owner = _owned_target(engine, repo_id, component_name, owner, cross_repo, labels)
    ambiguous, labels, files = _resolve_targets(
        engine, repo_id, component_name, cross_repo, file, labels, owner=owner
    )
    # Each file's targets are traced apart only for a few files: the expansion
    # runs once per file, and a name like `get` is defined in hundreds.
    split = ambiguous is not None and files <= IMPACT_MAX_SPLIT
    cypher = f"""
    {_named_nodes(labels, "component_name", cross_repo)}
    WITH n WHERE ($file IS NULL OR n.file = $file) AND {_OWNER_MATCH}
    WITH CASE WHEN $split THEN n.file END AS target_file, collect(n) AS l0
    CALL (l0) {{
    {_impact_expansion(cross_repo, with_ids=split)}
    }}
    WITH collect({{target_file: target_file, direct_dependents: direct_dependents,
                  transitive_dependents: transitive_dependents, direct_count: direct_count}}) AS rows,
         collect([e IN endpoint_nodes | {{e: e, file: target_file}}]) AS nested
    WITH rows, reduce(acc = [], each IN nested | acc + each) AS pairs
    {_capped_endpoints("pairs")}
    RETURN rows, endpoint_count, endpoints
    """
    params = {
        "component_name": component_name, "file": file, "owner": owner, "split": split, "max_results": max_results,
    }
    if not cross_repo:
        params["repo_id"] = repo_id

    (answer_row,) = _query(engine, cypher, params)
    results = sorted(answer_row["rows"], key=lambda r: r["target_file"] or "")
    if not split:
        row = results[0]
        answer = {
            "direct_dependents": dependents_envelope([_located(d) for d in row["direct_dependents"]]),
            "transitive_dependents": dependents_envelope([_located(d) for d in row["transitive_dependents"]]),
            "endpoints": _endpoints_envelope(answer_row, max_results),
            "risk_level": _risk_level(row["direct_count"]),
        }
        if ambiguous:
            answer["ambiguous_targets"] = ambiguous
            answer["notices"] = [
                f"{_echo(component_name)} is defined in {files} files; their dependents are listed together "
                "and risk_level is theirs together. Pass file to analyse one."
            ]
        return answer
    # A dependent is listed once, at its nearest hop to any target (as the union of
    # the targets would list it), with the files of every target it depends on.
    direct_ids = {d["id"] for row in results for d in row["direct_dependents"]}
    dependents: dict[str, dict[str, Any]] = {}
    for row in results:
        for key in ("direct_dependents", "transitive_dependents"):
            for d in row[key]:
                if key == "transitive_dependents" and d["id"] in direct_ids:
                    continue
                located = _located({k: v for k, v in d.items() if k != "id"})
                entry = dependents.setdefault(d["id"], {"key": key, "row": located, "files": []})
                entry["files"].append(row["target_file"])
    lists = {
        key: [_with_target_files(d["row"], d["files"]) for d in dependents.values() if d["key"] == key]
        for key in ("direct_dependents", "transitive_dependents")
    }
    return {
        **{key: dependents_envelope(rows) for key, rows in lists.items()},
        "endpoints": _endpoints_envelope(answer_row, max_results, tagged=True),
        "risk_level": _risk_level(max(row["direct_count"] for row in results)),
        "ambiguous_targets": ambiguous,
    }


def _risk_level(direct_count: int) -> str:
    return "high" if direct_count > 10 else "medium" if direct_count > 3 else "low"


def _endpoints_envelope(row: dict[str, Any], max_results: int, tagged: bool = False) -> dict[str, Any]:
    """A {count, results, truncated} envelope of the Endpoints `_capped_endpoints`
    read into `row`, each as list_endpoints lists it, ordered by repository and
    name; `tagged` adds each one's `target_files` (`_with_target_files`), as
    the dependents or callers of several same-named targets carry them."""
    shown = sorted(row["endpoints"], key=lambda e: (e["repo_id"], e["name"]))
    rows = [_endpoint_row(e) for e in shown]
    if tagged:
        rows = [_with_target_files(r, e["target_files"]) for r, e in zip(rows, shown, strict=True)]
    envelope = _envelope(rows, max_results)
    envelope["count"] = row["endpoint_count"]
    envelope["truncated"] = row["endpoint_count"] > len(envelope["results"])
    return envelope


_DIFF_TARGETS_CYPHER = """
CALL () {
  UNWIND $targets AS t
  CALL (t) {
    MATCH (n:Function {repo_id: $repo_id, name: t.name, file: t.file}) WHERE n.owner IS NULL OR n.owner = t.owner
    RETURN n
    UNION
    MATCH (n:Class {repo_id: $repo_id, name: t.name, file: t.file}) WHERE n.owner IS NULL OR n.owner = t.owner
    RETURN n
  }
  RETURN n
  UNION
  UNWIND $files AS f
  CALL (f) {
    MATCH (n:Function {repo_id: $repo_id, file: f}) RETURN n
    UNION
    MATCH (n:Class {repo_id: $repo_id, file: f}) RETURN n
  }
  RETURN n
}
WITH collect(DISTINCT n) AS l0
"""


def _diff_symbol(entry: dict[str, Any], path: str) -> dict[str, Any]:
    return _sanitize_row({
        "name": entry["name"], "kind": entry["kind"], "container": entry["container"], "file": path,
        **({"owner": entry["owner"]} if "owner" in entry else {}),
    })


def impact_analysis_for_diff(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    base_ref: str,
    head_ref: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Analyze the combined impact of the symbols changed between two git refs.

    Compares `head_ref` with its merge base with `base_ref` (like
    `git diff base_ref...head_ref`, what a pull request shows) the way
    compare_branches does: git objects read in memory, never fetched, under its
    ref rules and caps. Functions and classes changed or removed are traced to
    their dependents with impact_analysis's bounded hop expansion; added ones
    are listed apart, since nothing depends on new code yet. Every indexed
    function and class of a changed code file counts as changed, with a notice,
    when its symbols weren't diffed: it is past the 200-file detail cap, its
    symbols couldn't be read (too large, a cap, a parse error, a blob missing
    from a partial clone), or it was renamed (both paths). A changed or removed
    symbol that matches no indexed node (by name and file) is named in a
    notice: its dependents are unknown, for example when the index already
    reflects the head.

    Args:
        engine: GraphEngine instance
        registry: RepoRegistry, used to resolve repo_id to its registered root path
        repo_id: Repository ID
        base_ref: Git ref (branch/tag/sha) the change is based on; must resolve locally
        head_ref: Git ref (branch/tag/sha) with the change; must resolve locally
        cross_repo: If True, include cross-repo impacts
        max_results: Maximum number of results per dependents list to return in the envelope

    Returns:
        Dict with changed_files; added_symbols, changed_symbols and
        removed_symbols ({name, kind, container, file}); changed_components (the
        names traced); direct_dependents and transitive_dependents ({count,
        results, truncated} envelopes); endpoints (as impact_analysis gives
        them); risk_level; truncated and
        truncated_reasons (compare_branches' caps); and notices. Dependents come
        from the last index of the working tree, not from either ref.

    Raises:
        ToolError: an unknown repo_id, a bad or unknown ref, no common history,
            or git failing.
    """
    from devgraph.indexer.git_history import compare as git_compare
    from devgraph.indexer.symbols import language_for

    record = _registered(registry, repo_id)
    _at_least(1, max_results=max_results)
    notices: list[str] = []
    try:
        with git_compare.open_comparison(
            record.path, repo_id, base_ref, head_ref,
            arg_names=("base_ref", "head_ref"), tool="impact_analysis_for_diff",
        ) as comparison:
            detail_failed = False
            try:
                detailed = git_compare.symbol_detail(comparison)
            except git_compare.CompareError as exc:
                detailed, detail_failed = [], True
                notices.append(f"{exc}; every indexed symbol in the changed files counts as changed")
    except git_compare.CompareError as exc:
        raise ToolError(str(exc)) from exc

    changes = comparison.changes
    changed_files = list(dict.fromkeys(p for c in changes for p in (c.old_path, c.path) if p is not None))
    symbols: dict[str, list[dict[str, Any]]] = {"added": [], "changed": [], "removed": []}
    targets: list[dict[str, str]] = []
    # Files every indexed symbol of which counts as changed, by why.
    fallback: dict[str, list[str]] = {"unread": [], "uncapped": [], "renamed": []}
    detailed_paths = {c.path for c in detailed}
    for change in changes:
        if change.kind != "blob" or change.status == "added":
            pass
        elif language_for(change.path) is None and language_for(change.old_path or change.path) is None:
            continue  # no functions or classes to trace
        elif change.status == "renamed":
            fallback["renamed"] += [change.old_path, change.path]
            continue
        elif change.path not in detailed_paths:
            fallback["unread" if detail_failed else "uncapped"].append(change.path)
            continue
        elif change.symbols is None and change.symbols_skipped in ("too_large", "limit", "parse_error"):
            fallback["unread"].append(change.path)
            continue
        if change.symbols is None:
            continue
        for name in symbols:
            symbols[name] += [_diff_symbol(entry, change.path) for entry in change.symbols[name]]
        for name in ("changed", "removed"):
            for entry in change.symbols[name]:
                target = {"name": entry["name"], "file": change.path, "owner": entry.get("owner", "")}
                if target not in targets:
                    targets.append(target)
    if fallback["unread"] and not detail_failed:
        notices.append(
            f"symbols of {len(fallback['unread'])} changed file(s) could not be read; "
            "every indexed symbol in them counts as changed"
        )
    if fallback["uncapped"]:
        notices.append(
            f"{len(fallback['uncapped'])} changed file(s) past the {git_compare._COMPARE_MAX_FILES}-file detail cap; "
            "every indexed symbol in them counts as changed"
        )
    if fallback["renamed"]:
        notices.append(
            f"{len(fallback['renamed']) // 2} renamed file(s); every indexed symbol under the old or new path "
            "counts as changed, since whatever imports the old path may break"
        )
    files = list(dict.fromkeys(f for paths in fallback.values() for f in paths))

    row = {
        "direct_dependents": [], "transitive_dependents": [], "direct_count": 0, "matched": [],
        "endpoint_count": 0, "endpoints": [],
    }
    if targets or files:
        cypher = (
            f"CALL () {{\n{_DIFF_TARGETS_CYPHER}{_impact_expansion(cross_repo)}\n}}\n"
            "WITH *, [e IN endpoint_nodes | {e: e, file: null}] AS pairs\n"
            f"{_capped_endpoints('pairs')}"
            "RETURN direct_dependents, transitive_dependents, direct_count, matched, endpoint_count, endpoints"
        )
        params = {"repo_id": repo_id, "targets": targets, "files": files, "max_results": max_results}
        rows = _query(engine, cypher, params)
        if rows:
            row = rows[0]
    found = {(m["name"], m["file"], m["owner"]) for m in row["matched"]}
    missing = [t for t in targets if (t["name"], t["file"], t["owner"]) not in found]
    if missing:
        shown = ", ".join(f"{t['name']} ({t['file']})" for t in missing[:_DESCRIBE_MAX_SUGGESTIONS])
        more = f" and {len(missing) - _DESCRIBE_MAX_SUGGESTIONS} more" if len(missing) > _DESCRIBE_MAX_SUGGESTIONS else ""
        notices.append(
            f"{len(missing)} changed or removed symbol(s) match no indexed node, so their dependents are unknown "
            f"(the index may already be at the head, or not yet cover them): {_sanitize_value(shown)}{more}"
        )
    reasons = comparison.truncated_reasons
    return {
        "changed_files": changed_files,
        "added_symbols": symbols["added"],
        "changed_symbols": symbols["changed"],
        "removed_symbols": symbols["removed"],
        "changed_components": sorted({m["name"] for m in row["matched"] if m["name"] is not None}),
        "direct_dependents": _envelope([_located(d) for d in row["direct_dependents"]], max_results),
        "transitive_dependents": _envelope([_located(d) for d in row["transitive_dependents"]], max_results),
        "endpoints": _endpoints_envelope(row, max_results),
        "risk_level": _risk_level(row["direct_count"]),
        "truncated": bool(reasons),
        "truncated_reasons": list(reasons),
        "notices": notices,
    }


def explain_architecture(
    engine: GraphEngine,
    repo_id: str,
    max_results: int = 15,
) -> dict[str, Any]:
    """Generate a high-level architectural explanation of the repository.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        max_results: Maximum number of results per list to return in the envelope

    Returns:
        Dict with services_and_datastores ({service, uses}) and endpoints
        ({endpoints, calls}, only services some endpoint calls), each a
        {count, results, truncated} envelope; past `LIST_ROW_LIMIT` services
        both are truncated, with a notice. Each endpoint is named with a
        leading "/" on its path, as list_endpoints gives its `path` (a Django
        pattern's added: `* /orders/`); trace_request_flow takes that name
    """
    from devgraph.mcp.tool_plane import _sanitize_deep

    _at_least(1, max_results=max_results)
    cypher = f"""
    MATCH (s:Service {{repo_id: $repo_id}})
    OPTIONAL MATCH (s)-[:USES]->(ds:{"|".join(schema.DATASTORE_LABELS)})
    WITH s, COLLECT(DISTINCT ds.name) as datastore_names
    OPTIONAL MATCH (ep:Endpoint {{repo_id: $repo_id}})-[:CALLS]->(s)
    WITH s, datastore_names, COLLECT(DISTINCT ep.name) as endpoint_names
    RETURN s.name as service, datastore_names, endpoint_names
    ORDER BY service
    LIMIT {LIST_ROW_LIMIT + 1}
    """
    rows, more = _list_query(engine, cypher, {"repo_id": repo_id})
    for row in rows:
        row["endpoint_names"] = [_slashed_endpoint_name(name) for name in row["endpoint_names"]]
    services: list[dict[str, Any]] = []
    endpoints: list[dict[str, Any]] = []
    seen_services: set[tuple[Any, ...]] = set()
    seen_endpoints: set[tuple[Any, ...]] = set()
    for row in rows:  # two same-named services with the same lists are one entry, as before
        service = _sanitize_deep({"service": row["service"], "uses": row["datastore_names"]})
        key = (service["service"], tuple(service["uses"]))
        if key not in seen_services:
            seen_services.add(key)
            services.append(service)
        called = _sanitize_deep({"endpoints": row["endpoint_names"], "calls": row["service"]})
        key = (called["calls"], tuple(called["endpoints"]))
        if called["endpoints"] and key not in seen_endpoints:
            seen_endpoints.add(key)
            endpoints.append(called)
    return {
        "services_and_datastores": _capped_envelope(services, more, max_results),
        "endpoints": _capped_envelope(endpoints, more, max_results),
    }


def list_services(
    engine: GraphEngine,
    repo_id: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """List all services in a repository (or across repos if cross_repo=True).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID. Filters when cross_repo is False; when True it
            only decides ordering (this repo's services first)
        cross_repo: If True, list services from all repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing services with their properties
    """
    _at_least(1, max_results=max_results)
    repo_filter = _cross_scope("s", "WHERE") if cross_repo else "WHERE s.repo_id = $repo_id"
    # `truncated` is applied to the ordered list, so ordering decides what
    # survives max_results. Sorting by repo_id alone meant a cross-repo call
    # could drop the caller's own repo entirely -- with enough registered
    # repos sorting ahead of it alphabetically, "list services across repos"
    # returned none of the services belonging to the repo that asked. The
    # caller's repo comes first now; everything else keeps the old order.
    cypher = f"""
    MATCH (s:Service)
    {repo_filter}
    RETURN s.name as name, s.repo_id as repo_id, s.description as description
    ORDER BY (s.repo_id = $repo_id) DESC, s.repo_id, s.name
    """
    params = {"repo_id": repo_id}

    results = _query(engine, cypher, params)
    return _envelope(results, max_results)


#: The `framework` an Endpoint carries, by the extractors that write them
#: (indexer/apis/extractor.py, and the add_route callers of indexer/apis/routes.py).
ENDPOINT_FRAMEWORKS = ("ASP.NET Core", "Django", "Express", "FastAPI", "Flask", "Go", "NestJS", "Spring")
#: A method filter: an HTTP method token (`GET`, `PROPFIND`), or "*".
_ENDPOINT_METHOD = re.compile(r"[A-Z][A-Z-]*|\*")


def _slashed_endpoint_name(name: str) -> str:
    """An Endpoint's name with a leading "/" on its path, as list_endpoints
    gives its `path`: a Django pattern is stored without one (`* orders/` is
    `* /orders/`). A route under an unknown mount (`"GET ?/:id (file)"`) is
    left as it is."""
    method, _, rest = name.partition(" ")
    return name if rest.startswith(("/", "?")) else f"{method} /{rest}"


def _endpoint_row(row: dict[str, Any]) -> dict[str, Any]:
    """A list_endpoints result: an Endpoint's name split into its method and
    path, `mounted` False for a route under a prefix its file doesn't know
    (`"<METHOD> ?<path> (<file>)"`, whose path is the route's own). Its
    handlers are the Functions (and a Django class-based view's Class) it
    IMPLEMENTS that have a file; one that resolved to none is its file-less
    stub, with file and owner None."""
    method, _, rest = row["name"].partition(" ")
    mounted = not rest.startswith("?")
    path = rest if mounted else rest[1:].rsplit(" (", 1)[0]
    path = path if path.startswith("/") else "/" + path  # a Django pattern has no leading "/"
    handlers = [h for h in row["handlers"] if h["file"]] or [
        {"label": h["label"], "name": h["name"], "file": None, "owner": None} for h in row["handlers"]
    ]
    return {
        "name": row["name"], "method": method, "path": path, "mounted": mounted, "framework": row["framework"],
        "files": row["files"],
        "handlers": sorted(handlers, key=lambda h: (h["file"] or "", h["owner"] or "", h["name"])),
        "services": sorted(row["services"]), "repo_id": row["repo_id"],
    }


def list_endpoints(
    engine: GraphEngine,
    repo_id: str,
    method: str | None = None,
    path_prefix: str | None = None,
    framework: str | None = None,
    service: str | None = None,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """List the repository's API endpoints (or every repository's, with cross_repo).

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID. Filters when cross_repo is False; when True it
            only decides ordering (this repo's endpoints first)
        method: Only this HTTP method token (any case: GET, PROPFIND...; "*" is a
            route for every method)
        path_prefix: Only paths at or below this prefix, by whole segments
            ("/api" matches "/api" and "/api/users", not "/apis"); a route
            whose mount prefix is unknown (`mounted` False) never matches
        framework: Only this framework (any case), one of ENDPOINT_FRAMEWORKS
        service: Only endpoints that CALLS this compose service
        cross_repo: If True, list endpoints from every repository
        max_results: Maximum number of results to return in the envelope

    Returns:
        {count, results, truncated}, each result {name, method, path (always with a
        leading "/", a Django pattern's added), mounted,
        framework, files (the files routing it), handlers ({label, name, file, owner}:
        a Function, or a Django class-based view's Class and its HTTP method handlers),
        services (the compose services it CALLS), repo_id}, ordered by name;
        past `LIST_ROW_LIMIT` matches it is truncated, with a notice

    Raises:
        ToolError: an unknown method or framework, or no service of that name.
    """
    _at_least(1, max_results=max_results)
    if method is not None:
        method = method.upper()
        if not _ENDPOINT_METHOD.fullmatch(method):
            raise ToolError(f"method must be an HTTP method like GET or PROPFIND, or *, not {_echo(method)}")
    if framework is not None:
        known = {each.lower(): each for each in ENDPOINT_FRAMEWORKS}
        if framework.lower() not in known:
            raise ToolError(f"framework must be one of {', '.join(ENDPOINT_FRAMEWORKS)}, not {_echo(framework)}")
        framework = known[framework.lower()]
    if service is not None and not _exists(engine, repo_id, service, ("Service",), cross_repo):
        raise _not_found(engine, repo_id, "service", service, ("Service",), cross_repo)
    prefix = None if path_prefix is None else "/" + path_prefix.strip("/")
    if cross_repo:
        match = f"MATCH (e:Endpoint) WHERE e.name IS NOT NULL {_cross_scope('e', 'AND')}"
    else:
        match = (
            "MATCH (e:Endpoint) USING INDEX SEEK e:Endpoint(repo_id, name) "
            "WHERE e.repo_id = $repo_id AND e.name IS NOT NULL"
        )
    # A Django pattern has no leading "/": `path` adds one, so a prefix matches it too.
    cypher = f"""
    {match}
    WITH e, split(e.name, ' ')[0] AS method, substring(e.name, size(split(e.name, ' ')[0]) + 1) AS rest
    WITH e, method, rest, CASE WHEN rest STARTS WITH '/' THEN rest ELSE '/' + rest END AS path
    WHERE ($method IS NULL OR method = $method)
      AND ($framework IS NULL OR e.framework = $framework)
      AND ($prefix IS NULL OR (NOT rest STARTS WITH '?'
           AND ($prefix = '/' OR path = $prefix OR path STARTS WITH $prefix + '/')))
      AND ($service IS NULL OR EXISTS {{ MATCH (e)-[:CALLS]->(s:Service) WHERE s.name = $service }})
    {_ENDPOINT_DETAILS}
    RETURN e.name AS name, e.framework AS framework, coalesce(e.sources, [e.source]) AS files,
           handlers, services, e.repo_id AS repo_id
    ORDER BY (e.repo_id = $repo_id) DESC, e.repo_id, e.name
    LIMIT {LIST_ROW_LIMIT + 1}
    """
    params = {"repo_id": repo_id, "method": method, "framework": framework, "prefix": prefix, "service": service}
    rows, more = _list_query(engine, cypher, params)
    return _capped_envelope([_endpoint_row(row) for row in rows], more, max_results)


def explain_decision(
    engine: GraphEngine,
    repo_id: str,
    decision_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Explain a design decision: its rationale, what it documents, and history.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        decision_name: Name (id) of the DesignDecision note
        cross_repo: If True, search across repos

    Returns:
        Dict with the decision's properties, what it documents, what it
        supersedes, and any ArchitectureNote it's backed by.
    """
    repo_filter = _cross_scope("d", "WHERE") if cross_repo else "WHERE d.repo_id = $repo_id"
    cypher = f"""
    MATCH (d:DesignDecision {{name: $decision_name}})
    {repo_filter}
    OPTIONAL MATCH (doc)-[:DOCUMENTED_BY]->(d)
    OPTIONAL MATCH (d)-[:SUPERSEDES]->(prior:DesignDecision)
    OPTIONAL MATCH (d)-[:DECIDED_BY]->(note:ArchitectureNote)
    RETURN d.name as name, d.title as title, d.body as body,
           COLLECT(DISTINCT doc.name) as documents,
           COLLECT(DISTINCT prior.name) as supersedes,
           COLLECT(DISTINCT note.name) as backed_by
    """
    params = {"decision_name": decision_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not results:
        raise _not_found(engine, repo_id, "design decision", decision_name, ("DesignDecision",), cross_repo)
    return results[0]


def find_requirements_for(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
    extra_labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Find requirements a component (Module/Service/etc.) satisfies.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope
        extra_labels: The repository's declared labels, looked up besides the built-in ones

    Returns:
        Dict with count, results, and truncated flag containing the Requirement
        notes ({name, title, body}) satisfied by this component

    Raises:
        ToolError: no node has that name.
    """
    _at_least(1, max_results=max_results)
    _, labels, _ = _resolve_targets(
        engine, repo_id, component_name, cross_repo, None, component_labels(extra_labels), describe=False
    )
    cypher = f"""
    {_named_nodes(labels, "component_name", cross_repo)}
    MATCH (n)-[:SATISFIES]->(r:Requirement)
    RETURN r.name as name, r.title as title, r.body as body
    ORDER BY r.name
    LIMIT {LIST_ROW_LIMIT + 1}
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results, more = _list_query(engine, cypher, params)
    return _capped_envelope(results, more, max_results)


def trace_design_rationale(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    extra_labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Trace the design rationale (decisions, notes, requirements) behind a component.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component (Module/Service/Class/etc.)
        cross_repo: If True, search across repos
        extra_labels: The repository's declared labels, looked up besides the built-in ones

    Returns:
        Dict grouping every Requirement/DesignDecision/ArchitectureNote linked
        to this component via SATISFIES or DOCUMENTED_BY.

    Raises:
        ToolError: no node has that name.
    """
    _, labels, _ = _resolve_targets(
        engine, repo_id, component_name, cross_repo, None, component_labels(extra_labels), describe=False
    )
    cypher = f"""
    {_named_nodes(labels, "component_name", cross_repo)}
    OPTIONAL MATCH (n)-[:SATISFIES]->(req:Requirement)
    OPTIONAL MATCH (n)-[:DOCUMENTED_BY]->(doc)
    RETURN n.name as component,
           [r IN COLLECT(DISTINCT {{name: req.name, title: req.title}}) WHERE r.name IS NOT NULL] as requirements,
           [d IN COLLECT(DISTINCT {{name: doc.name, title: doc.title, type: labels(doc)[0]}})
            WHERE d.name IS NOT NULL] as notes
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    return _sanitize_row(results[0])


def blame_component(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """Find commits that modified a component's file, most recent first.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to look up commit history for
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing the commits
        (sha, message, author, authored_date) that modified this component,
        most recent first; count 0 for a file no recorded commit touched.
        Past `LIST_ROW_LIMIT` commits it is truncated, with a notice.

    Raises:
        ToolError: no file (Module) has that path.
    """
    repo_filter = _cross_scope("c", "AND") if cross_repo else "AND c.repo_id = $repo_id"
    cypher = f"""
    MATCH (c:Commit)-[:MODIFIES]->(m:Module {{name: $component_name}})
    WHERE true
    {repo_filter}
    RETURN c.name as sha, c.message as message, c.author as author,
           c.authored_date as authored_date
    ORDER BY c.authored_date DESC
    LIMIT {LIST_ROW_LIMIT + 1}
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    _at_least(1, max_results=max_results)
    results, more = _list_query(engine, cypher, params)
    if not results and not _exists(engine, repo_id, component_name, ("Module",), cross_repo):
        raise _not_found(engine, repo_id, "file", component_name, ("Module",), cross_repo)
    return _capped_envelope(results, more, max_results)


def _off_source_file_check(engine: GraphEngine, repo_id: str, path: str, cross_repo: bool) -> None:
    """With a repository's source off: the not-found error for a path no Module has,
    unless Neo4j can't be reached, when the source-off notice comes first."""
    try:
        exists = _exists(engine, repo_id, path, ("Module",), cross_repo)
    except Exception as exc:
        if neo4j_unavailable(exc, getattr(engine, "uri", "")) is not None:
            return
        raise
    if not exists:
        raise _not_found(engine, repo_id, "file", path, ("Module",), cross_repo)


def find_related_prs(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
    registry: RepoRegistry | None = None,
) -> dict[str, Any]:
    """Find pull requests related to a component via commits that resolved issues touching it.

    Reads the graph's PullRequest nodes. When the repository's PR ingestion is
    off (pr_source_enabled=False) the envelope is empty, with a `notice` saying
    how to enable it; nothing reaches the network.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to find related PRs for
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope
        registry: RepoRegistry, used to check whether the repository's source is on

    Returns:
        Dict with count, results, and truncated flag containing PullRequests linked
        (via RESOLVES on an Issue referenced by a commit that touched this component)
        to the component.

    Raises:
        ToolError: no file (Module) has that path.
    """
    _at_least(1, max_results=max_results)
    component_name = _normalize_file(component_name)
    if _source_off(registry, repo_id, "pr_source_enabled"):
        _off_source_file_check(engine, repo_id, component_name, cross_repo)
        return _source_off_notice(max_results, "PR", "pr-source", repo_id)

    repo_filter = _cross_scope("m", "AND") if cross_repo else "AND m.repo_id = $repo_id"
    cypher = f"""
    MATCH (m:Module {{name: $component_name}})
    WHERE true
    {repo_filter}
    MATCH (c:Commit)-[:MODIFIES]->(m)
    OPTIONAL MATCH (c)-[:REFERENCES]->(i:Issue)<-[:RESOLVES]-(pr:PullRequest)
    WITH DISTINCT pr
    WHERE pr IS NOT NULL
    RETURN pr.name as number, pr.title as title, pr.state as state, pr.url as url
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not results and not _exists(engine, repo_id, component_name, ("Module",), cross_repo):
        raise _not_found(engine, repo_id, "file", component_name, ("Module",), cross_repo)
    return _envelope(results, max_results)


def issue_history_for(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    max_results: int = 15,
    registry: RepoRegistry | None = None,
) -> dict[str, Any]:
    """Find issues referenced by commits that touched a component.

    Reads the graph's Issue nodes. When the repository's issue ingestion is off
    (issue_source_enabled=False) the envelope is empty, with a `notice` saying
    how to enable it; nothing reaches the network.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to find issue history for
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope
        registry: RepoRegistry, used to check whether the repository's source is on

    Returns:
        Dict with count, results, and truncated flag containing Issues referenced
        by commits that modified this component.

    Raises:
        ToolError: no file (Module) has that path.
    """
    _at_least(1, max_results=max_results)
    component_name = _normalize_file(component_name)
    if _source_off(registry, repo_id, "issue_source_enabled"):
        _off_source_file_check(engine, repo_id, component_name, cross_repo)
        return _source_off_notice(max_results, "Issue", "issue-source", repo_id)

    repo_filter = _cross_scope("m", "AND") if cross_repo else "AND m.repo_id = $repo_id"
    cypher = f"""
    MATCH (m:Module {{name: $component_name}})
    WHERE true
    {repo_filter}
    MATCH (c:Commit)-[:MODIFIES]->(m)
    OPTIONAL MATCH (c)-[:REFERENCES]->(i:Issue)
    WITH DISTINCT i
    WHERE i IS NOT NULL
    RETURN i.name as number, i.title as title, i.state as state, i.url as url
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not results and not _exists(engine, repo_id, component_name, ("Module",), cross_repo):
        raise _not_found(engine, repo_id, "file", component_name, ("Module",), cross_repo)
    return _envelope(results, max_results)


def get_source(
    engine: GraphEngine,
    registry: RepoRegistry,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
    file: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Fetch a Function or Class's actual source text by reading its last-indexed line range.

    Reads live from disk using the graph's last-indexed start_line/end_line —
    a stale index could return the wrong lines; rescan first if freshness
    matters, same caveat as every other tool here.

    Args:
        engine: GraphEngine instance
        registry: RepoRegistry, used to resolve repo_id to its registered root path
        repo_id: Repository ID
        component_name: Name of the Function or Class to fetch source for; a
            method may be named with the declarations it is in, `A.save`
            (`Outer.Inner.run`, `A::save`), when no node has that exact name;
            a Kotlin companion's member by its class's (`Repo.create` for
            `Repo.Companion.create`, and `owner="Repo"` likewise)
        cross_repo: If True, search across repos (the file is still read from
            whichever repo actually owns the matched node, via its own registry entry)
        file: Repo-relative path of the defining file, to pick one of several
            same-named Functions or Classes
        owner: The declarations the Function or Class is in, as a candidate's
            `owner` gives it ("" for a top-level one), to pick one of several
            same-named ones in one file

    Returns:
        Dict with name, label, file, start_line, end_line, source, and
        docstring_full (when present); source None when the file can't be
        read. When several nodes match, `source` is None and the dict adds
        `status: "ambiguous"`, `count`, `truncated` and `candidates`
        ({label, name, file, owner}; repo_id too when cross_repo): pass one's
        `file` and `owner`. The result names its `owner` too. A route's
        handler stub that folds into a real Function (`_fold_target`) is left
        out, so it never makes its handler ambiguous; any other stub is a
        candidate with file None.
        A file is decoded as the indexer decodes it (`source_text`): a
        Python coding line, else UTF-8, else cp1252 or Latin-1; when an
        undeclared file is not valid UTF-8, or a file isn't valid in its
        declared codec, the dict adds a `notice` saying how it was decoded.

    Raises:
        ToolError: no Function or Class has that name (of `owner`, in `file`),
            with similar names. An absolute `file` inside the repository is
            read as its repo-relative path.
    """
    session_repo = None if cross_repo else registry.get(repo_id)
    file = _normalize_file(file, session_repo.path if session_repo is not None else None)
    repo_filter = _cross_scope("n", "AND") if cross_repo else "AND n.repo_id = $repo_id"
    def lookup(match: str) -> list[dict[str, Any]]:
        return _query(engine, f"""
    MATCH (n)
    WHERE (n:Function OR n:Class) AND {match}
    {repo_filter}
    AND ($file IS NULL OR n.file = $file)
    {_fold_target("n")}WITH n, folded WHERE folded IS NULL AND NOT {_shadowed_stub("n")}
    RETURN n.name as name, labels(n) as labels, n.repo_id as repo_id,
           n.file as file, {_OWNER_OF.format(v="n")} as owner, n.start_line as start_line, n.end_line as end_line,
           n.docstring_full as docstring_full
    ORDER BY n.repo_id, n.file, n.start_line
    LIMIT {_DESCRIBE_MAX_CANDIDATES + 1}
    """, params)

    params: dict[str, Any] = {"component_name": component_name, "file": file}
    if not cross_repo:
        params["repo_id"] = repo_id

    if owner is not None:
        params["owner"] = owner
        results = lookup("n.name = $component_name AND coalesce(n.owner, '') = $owner AND n.file IS NOT NULL")
        if not results:
            # `owner="Repo"` for a member of Repo's Kotlin companion.
            results = lookup(f"n.name = $component_name AND {_COMPANION_MEMBER.format()}")
    else:
        results = lookup("n.name = $component_name")
    if not results and owner is None and (owned := _owner_split(component_name)) is not None:
        params["owner"], params["member"] = owned
        results = lookup("n.name = $member AND n.owner = $owner")
        if not results:
            # `Repo.create` written as Kotlin calls it: the member of Repo's companion.
            results = lookup(f"n.name = $member AND {_COMPANION_MEMBER.format()}")
    empty = {
        "name": component_name, "label": None, "file": None,
        "start_line": None, "end_line": None, "source": None, "docstring_full": None,
    }
    if not results:
        where = f" in file {_echo(file)}" if file is not None else ""
        whose = f" with owner {_echo(owner)}" if owner is not None else ""
        error = _not_found(engine, repo_id, "Function or Class", component_name, ("Function", "Class"), cross_repo)
        if where or whose:
            raise ToolError(f"no Function or Class named {_echo(component_name)}{whose}{where}. {error}")
        raise error

    def label_of(row: dict[str, Any]) -> str:
        return next((label for label in row["labels"] if label in ("Function", "Class")), row["labels"][0])

    if len(results) > 1:
        candidates = [
            {
                "label": label_of(r), "name": r["name"], "file": r["file"],
                **({"owner": r["owner"]} if r["owner"] is not None else {}),
                **({"repo_id": r["repo_id"]} if cross_repo else {}),
            }
            for r in results[:_DESCRIBE_MAX_CANDIDATES]
        ]
        candidates = [_unresolved_marked(c) for c in candidates]
        return {
            **empty,
            "status": "ambiguous",
            "count": len(results),
            "candidates": [_sanitize_row(c) for c in candidates],
            "truncated": len(results) > _DESCRIBE_MAX_CANDIDATES,
        }

    row = results[0]
    node_repo_id = row["repo_id"]
    file_rel_path = row["file"]
    start_line = row["start_line"]
    end_line = row["end_line"]
    if not file_rel_path or start_line is None or end_line is None:
        return empty

    repo = registry.get(node_repo_id)
    if repo is None:
        return empty

    file_path = (repo.path / file_rel_path).resolve()
    if not is_within(file_path, repo.path):
        return empty  # never read outside the registered repo root

    # O_NOFOLLOW refuses a final component swapped for a symlink after the
    # check above; platforms without it fall back to a plain open.
    try:
        fd = os.open(file_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with open(fd, "rb") as handle:
            data = handle.read()
    except OSError:
        return empty
    notice = None
    # Decoded as the indexer decodes it, so the line range lines up.
    python = is_python_path(file_rel_path)
    text, encoding = decode_source_as(data, python=python)
    declared = declared_encoding(data) if python else None
    if declared is not None and encoding != declared:
        notice = f"the file declares {declared} but is not valid {declared}; decoded as {encoding}, as the indexer reads it"
    elif declared is None and encoding != "utf-8":
        notice = f"the file is not valid UTF-8; decoded as {encoding}, as the indexer reads it"
    lines = text.splitlines()

    source_text = "\n".join(lines[start_line - 1 : end_line])

    result = {
        "name": row["name"],
        "label": label_of(row),
        "file": file_rel_path,
        "start_line": start_line,
        "end_line": end_line,
        "source": source_text,
        "docstring_full": row.get("docstring_full"),
    }
    if row["owner"] is not None:
        result["owner"] = row["owner"]
    if notice is not None:
        result["notice"] = notice
    return result


def find_mentions(
    engine: GraphEngine,
    repo_id: str,
    name: str,
    label: str | None = None,
    direction: str = "mentioned_by",
    cross_repo: bool = False,
    max_results: int = 15,
    registry: RepoRegistry | None = None,
) -> dict[str, Any]:
    """Find Document nodes that mention an entity, or what a Document mentions.

    When the repository's mentions indexing is off (mentions_enabled=False) the
    envelope is empty, with a `notice` saying how to enable it.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        name: Entity name (for direction="mentioned_by") or Document repo-relative path (for direction="mentions")
        label: Optional node label filter for the target entity (validates against BUILTIN_LABELS)
        direction: "mentioned_by" (default) to find Documents mentioning the entity, or
                   "mentions" to find what a Document mentions
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope
        registry: RepoRegistry, used to check whether the repository's mentions indexing is on

    Returns:
        Dict with count, results, and truncated flag containing nodes with name, type (labels), and repo_id
    """
    _at_least(1, max_results=max_results)
    if _source_off(registry, repo_id, "mentions_enabled"):
        return _source_off_notice(max_results, "Mentions", "mentions", repo_id)
    # Validate and reject unrecognized labels
    if label is not None and label not in schema.BUILTIN_LABELS:
        return _envelope([], max_results)

    # Build label filter using node label check (not parameterized label in node pattern)
    label_filter = "AND $label IN labels(target)" if label else ""

    if direction == "mentions":
        # Document mentions target: (d:Document {name: $name})-[:MENTIONS]->(target)
        repo_filter = _cross_scope("d", "AND") if cross_repo else "AND d.repo_id = $repo_id"
        cypher = f"""
        MATCH (d:Document {{name: $name}})-[:MENTIONS]->(target)
        WHERE true
        {repo_filter}
        {label_filter}
        RETURN target.name as name, labels(target) as type, target.repo_id as repo_id
        ORDER BY target.name
        LIMIT {LIST_ROW_LIMIT + 1}
        """
    else:
        # Mentioned by: (d:Document)-[:MENTIONS]->(target {name: $name})
        repo_filter = _cross_scope("target", "AND") if cross_repo else "AND target.repo_id = $repo_id"
        cypher = f"""
        MATCH (d:Document)-[:MENTIONS]->(target {{name: $name}})
        WHERE true
        {repo_filter}
        {label_filter}
        RETURN d.name as name, labels(d) as type, d.repo_id as repo_id
        ORDER BY d.name
        LIMIT {LIST_ROW_LIMIT + 1}
        """

    params = {"name": name}
    if label is not None:
        params["label"] = label
    if not cross_repo:
        params["repo_id"] = repo_id

    results, more = _list_query(engine, cypher, params)
    return _capped_envelope(results, more, max_results)


def list_recent_changes(
    engine: GraphEngine,
    repo_id: str,
    within_commits: int,
    entity_type: str | None = None,
    cross_repo: bool = False,
    max_results: int = 15,
) -> dict[str, Any]:
    """List entities touched within the last N commits repo-wide, most-recently-modified first.

    Entities are staged with a `last_modified_at` property by git-history
    recency indexing; an entity never touched by that staging has no such
    property and is excluded here, never silently included. If fewer than
    within_commits commits exist repo-wide, there is no cutoff to resolve —
    the whole repo history fits inside the requested window, so every entity
    that was ever staged with a `last_modified_at` is returned (still ordered
    most-recent-first), rather than nothing.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        within_commits: Only include entities modified within this many most-recent commits
        entity_type: Optional node label filter; a label not in BUILTIN_LABELS
            (NODE_LABELS and Cache) is a ToolError
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing entities with
        name, type (labels), repo_id, and last_modified_at, ordered most-recently-modified first.
    """
    _at_least(1, within_commits=within_commits, max_results=max_results)
    if entity_type is not None and entity_type not in schema.BUILTIN_LABELS:
        raise ToolError(f"unknown entity_type {_echo(entity_type)}; use one of: {', '.join(schema.BUILTIN_LABELS)}")

    cutoff = _resolve_recency_cutoff(engine, repo_id, within_commits)
    recency_filter = "AND n.last_modified_at >= $cutoff" if cutoff is not None else "AND n.last_modified_at IS NOT NULL"

    scope = _cross_scope("n") if cross_repo else "n.repo_id = $repo_id"
    # One branch per label, each seeking its `(repo_id, name)` index (recency is
    # staged by name, so a staged node always has one).
    branches = "\n  UNION\n".join(
        f"  MATCH (n:`{label}`) WHERE {scope} AND n.name IS NOT NULL {recency_filter} RETURN n"
        for label in ((entity_type,) if entity_type else schema.NAMED_LABELS)
    )
    cypher = f"""
    CALL () {{
    {branches}
    }}
    RETURN n.name as name, labels(n) as type, n.repo_id as repo_id,
           n.last_modified_at as last_modified_at
    ORDER BY n.last_modified_at DESC
    LIMIT {LIST_ROW_LIMIT + 1}
    """
    params = {}
    if cutoff is not None:
        params["cutoff"] = cutoff
    if not cross_repo:
        params["repo_id"] = repo_id

    results, more = _list_query(engine, cypher, params)
    return _capped_envelope(results, more, max_results)


def run_cypher(
    engine: GraphEngine,
    query: str,
    parameters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Advanced escape hatch for direct Cypher queries.

    This should be gated behind devgraph.config.get_settings().enable_run_cypher
    and is NOT registered as a standard MCP tool by default.

    Args:
        engine: GraphEngine instance
        query: Cypher query string
        parameters: Optional parameters dict

    Returns:
        Raw query results
    """
    return engine.run_cypher(query, parameters or {})
