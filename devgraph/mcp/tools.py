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

#: How many CALLS/USES/DEPENDS_ON hops `impact_analysis` and
#: `impact_analysis_for_diff` follow for transitive dependents. An unbounded
#: variable-length path hangs on a real repository's call graph, and a
#: dependent further away than this says little about a change's impact.
IMPACT_MAX_DEPTH = 4


def _impact_expansion(cross_repo: bool, with_ids: bool = False) -> str:
    """Cypher continuing from `WITH collect(n) AS l0` (the changed nodes): one
    CALL per hop collecting the distinct nodes with a CALLS/USES/DEPENDS_ON edge
    into the previous hop's nodes, never one seen at an earlier hop, then
    returning `direct_dependents` (hop 1), `transitive_dependents` (hops 2 to
    `IMPACT_MAX_DEPTH`), `direct_count` and `matched` (the changed nodes' {name, file}).
    `with_ids` adds each dependent's `id` (its elementId).

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
    transitive = " + ".join(f"l{k}" for k in range(2, IMPACT_MAX_DEPTH + 1))
    ref = "{name: d.name, type: labels(d)[0]" + (", id: elementId(d)}" if with_ids else "}")
    parts.append(
        f"RETURN [d IN l1 | {ref}] AS direct_dependents, "
        f"[d IN {transitive} | {ref}] AS transitive_dependents, "
        "size(l1) AS direct_count, [n IN l0 | {name: n.name, file: n.file}] AS matched"
    )
    return "\n".join(parts)


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
            f"Neo4j isn't reachable at {uri}; start it (for example `podman start devgraph-neo4j`), "
            "then retry (`devgraph doctor` checks the connection)"
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


# Labels search_component always covers. A repository's schema-declared
# labels are appended per call (see declared_node_labels).
_SEARCH_LABELS: tuple[str, ...] = ("Service", "Module", "Class", "Function", "Endpoint")


def search_component(
    engine: GraphEngine,
    repo_id: str,
    query: str,
    cross_repo: bool = False,
    max_results: int = 15,
    modified_within_commits: int | None = None,
    extra_labels: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Search for components (modules, services, classes, functions) by name or description.

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
        exact name match > name starts-with > name contains > description
        contains. At most `SEARCH_MAX_RESULTS` results are returned.
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
        repo_id=None if cross_repo else repo_id, cutoff=cutoff,
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

# Rank of a matching node: exact name, name starts with a term, name contains
# one, description only.
_SEARCH_TIER = (
    "CASE WHEN toLower(n.name) IN $terms THEN 0 "
    "WHEN any(t IN $terms WHERE toLower(n.name) STARTS WITH t) THEN 1 "
    "WHEN any(t IN $terms WHERE toLower(n.name) CONTAINS t) THEN 2 ELSE 3 END"
)
_SEARCH_FILE = "CASE WHEN coalesce(n.file, '') <> '' THEN n.file ELSE coalesce(n.path, n.source_file, n.source) END"
_SEARCH_ROW = (
    "{id: elementId(n), name: n.name, labels: labels(n), repo_id: n.repo_id, description: n.description, "
    f"file: {_SEARCH_FILE}}}"
)


def _name_variants(query: str, terms: list[str]) -> list[str]:
    """Spellings of each term an exact-case index seek tries: the term, Capitalised,
    UPPER, and the query's own words that lower-case to it."""
    variants = [v for t in terms for v in (t, t.capitalize(), t.upper())]
    variants += [w for w in [query.strip(), *re.findall(r"[A-Za-z0-9_]+", query)] if w.lower() in terms]
    return list(dict.fromkeys(variants))


def _search_branches(labels: tuple[str, ...], where: str, unwind: bool = False) -> str:
    """`CALL () { ... }` over one MATCH per label, so each can use its label's indexes.

    A file-scoped label's file-less node (a route's handler stub) is left out when
    a real node of that label shares its name."""
    branches = []
    for label in labels:
        stub = (
            f" AND NOT (coalesce(n.file, '') = '' AND EXISTS {{ MATCH (m:`{label}`) "
            "WHERE m.repo_id = n.repo_id AND m.name = n.name AND m.file <> '' })"
            if label in schema.FILE_SCOPED_LABELS
            else ""
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
) -> tuple[list[dict], int, bool]:
    """Nodes of `labels` whose name or description contains a (lower-case) term,
    best first: exact name, name prefix, name substring, description only.

    Names that start with one of `names` are found first through the
    `(repo_id, name)` indexes. Only when they don't fill `limit` does a substring
    scan fill the rest and count every match. Returns (rows, count,
    count_is_lower_bound): the count is a lower bound when the scan never ran.
    `repo_id` None searches every repository. Labels must be validated identifiers.
    """
    scope = "n.repo_id = $repo_id" if repo_id is not None else _cross_scope("n")
    if cutoff is not None:
        scope += " AND n.last_modified_at >= $cutoff"
    params: dict[str, Any] = {"repo_id": repo_id, "terms": terms, "names": names, "cutoff": cutoff}
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
    return _rank_search_results(found + row["rest"], terms), max(row["total"], len(found)), False


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


def _rank_search_results(results: list[dict], tokens: list[str]) -> list[dict]:
    """Sort search rows: exact name match first, then name starts-with a token,
    then name contains a token, then description-only matches last; by name and
    file within a tier (no file last), as `ranked_search`'s queries order them."""
    def tier(row: dict) -> int:
        name = (row.get("name") or "").lower()
        if name in tokens:
            return 0
        if any(name.startswith(t) for t in tokens):
            return 1
        if any(t in name for t in tokens):
            return 2
        return 3
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
_DESCRIBE_BUILTIN_LABELS = tuple(label for label in schema.NODE_LABELS if label != "Repository")
_DESCRIBE_HINT = (
    "search_component searches by partial name; a file, folder or docs node is named by its "
    "repo-relative path or its id"
)

_DESCRIBE_NEIGHBOUR = (
    "m.repo_id = $repo_id AND NOT m:Repository "
    "AND ($types IS NULL OR type(r) IN $types) "
    "AND ($labels IS NULL OR any(l IN labels(m) WHERE l IN $labels))"
)
_DESCRIBE_GROUPS = f"""
MATCH (n) WHERE elementId(n) = $id
CALL (n) {{
  MATCH (n)-[r]->(m)
  WHERE $direction IN ['both','out'] AND {_DESCRIBE_NEIGHBOUR}
  RETURN 'out' AS dir, type(r) AS rel, m
  UNION
  MATCH (n)<-[r]-(m)
  WHERE $direction IN ['both','in'] AND {_DESCRIBE_NEIGHBOUR}
  RETURN 'in' AS dir, type(r) AS rel, m
}}
WITH n, dir, rel, count(DISTINCT m) AS total
ORDER BY dir, rel
LIMIT {_DESCRIBE_MAX_GROUPS + 1}
CALL (n, dir, rel) {{
  CALL (n, dir, rel) {{
    MATCH (n)-[r:$(rel)]->(m) WHERE dir = 'out' RETURN m
    UNION
    MATCH (n)<-[r:$(rel)]-(m) WHERE dir = 'in' RETURN m
  }}
  WITH m WHERE m.repo_id = $repo_id AND NOT m:Repository
    AND ($labels IS NULL OR any(l IN labels(m) WHERE l IN $labels))
  WITH DISTINCT m
  ORDER BY m.name, coalesce(m.file, m.path)
  LIMIT $cap
  RETURN collect({{label: labels(m)[0], name: m.name, file: coalesce(m.file, m.path)}}) AS refs
}}
RETURN dir, rel, total, refs
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
    `path`, so only they get a path branch. Labels are validated identifiers.
    """
    tail = "AND NOT n:Repository AND ($file IS NULL OR $file IN [n.file, n.path, n.source_file])"
    body = "\n  UNION\n".join(
        f"  MATCH (n:`{label}`) WHERE n.repo_id = $repo_id AND n.{key} = $name {tail} RETURN n"
        for label in labels
        for key in (("name", "path") if label in declared else ("name",))
    )
    return (
        f"CALL () {{\n{body}\n}}\n"
        "RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name, "
        "coalesce(n.file, n.path) AS file, properties(n) AS properties\n"
        f"LIMIT {_DESCRIBE_MAX_CANDIDATES + 1}"
    )


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


def _named_nodes(labels: tuple[str, ...], param: str, cross_repo: bool) -> str:
    """`CALL () { ... }` returning `n`: every node of `labels` whose name is
    `$param`, in the repository (or in any of `cross_repo_ids`), one branch per
    label so each can seek its `(repo_id, name)` index. Labels are validated identifiers."""
    scope = _cross_scope("n") if cross_repo else "n.repo_id = $repo_id"
    body = "\n  UNION\n".join(
        f"  MATCH (n:`{label}`) WHERE n.name = ${param} AND {scope} RETURN n" for label in labels
    )
    return f"CALL () {{\n{body}\n}}\n"


def _exists(engine: GraphEngine, repo_id: str, name: str, labels: tuple[str, ...], cross_repo: bool = False) -> bool:
    """Whether a node of `labels` is named `name` in the repository (any repository when cross_repo)."""
    params = {"name": name} if cross_repo else {"name": name, "repo_id": repo_id}
    return bool(_query(engine, _named_nodes(labels, "name", cross_repo) + "RETURN 1 LIMIT 1", params))


def _resolve_targets(
    engine: GraphEngine, repo_id: str, name: str, cross_repo: bool, file: str | None
) -> list[dict[str, Any]] | None:
    """The `{label, file}` of every node named `name` when they lie in more than one
    file and no `file` narrows them (the caller then labels each row with its
    `target_files`), else None.

    Raises:
        ToolError: no node has that name, or none of them is in `file`.
    """
    params = {"name": name} if cross_repo else {"name": name, "repo_id": repo_id}
    rows = _query(
        engine,
        _named_nodes(_DESCRIBE_BUILTIN_LABELS, "name", cross_repo)
        + "WITH DISTINCT labels(n)[0] AS label, n.file AS file RETURN label, file ORDER BY file, label",
        params,
    )
    if not rows:
        raise _not_found(engine, repo_id, "component", name, _DESCRIBE_BUILTIN_LABELS, cross_repo)
    files = sorted({row["file"] for row in rows if row["file"] is not None})
    if file is not None:
        if file not in files:
            shown = ", ".join(_echo(f) for f in files[:_DESCRIBE_MAX_CANDIDATES]) or "no file"
            raise ToolError(f"no component named {_echo(name)} in file {_echo(file)}; it is defined in: {shown}")
        return None
    if len(files) < 2:
        return None
    return [_sanitize_row(row) for row in rows if row["file"] is not None][:_DESCRIBE_MAX_CANDIDATES]


def _node_ref(row: dict[str, Any]) -> dict[str, Any]:
    """`{label, name, file}` from raw values, so it re-describes exactly this node."""
    ref = {"label": row["label"], "name": row["name"]}
    if row.get("file") is not None:
        ref["file"] = row["file"]
    return ref


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
) -> dict[str, Any]:
    """One node's properties and its relationships, grouped by type, one hop out.

    Args:
        engine: GraphEngine instance
        repo_id: Repository to look in
        name: The node's exact name; a file, folder or docs node may also be
            named by its repo-relative path
        label: Only nodes of this label
        file: Only nodes whose file, path or source_file is this
        direction: "both", "out" or "in"
        relationship_types: Only these relationship types (at most 20)
        neighbor_labels: Only neighbours with one of these labels (at most 20)
        max_per_type: Neighbours listed per relationship type, clamped to 1..50
        declared_labels: The repository's schema-declared labels to search as
            well; anything that isn't a valid label identifier is ignored

    Returns:
        `{"status": "found", "node", "outgoing", "incoming", "groups_truncated"}`,
        each group a `{count, results, truncated}` envelope of `{label, name, file}`
        refs; or `{"status": "ambiguous", "count", "candidates", "truncated"}`.
        No match raises ToolError with suggestions. `Repository` nodes are never
        matched or listed.
    """
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
        path_labels = frozenset(labels) if label not in schema.NODE_LABELS or label in declared else frozenset()
    else:
        labels = _DESCRIBE_BUILTIN_LABELS + tuple(d for d in declared if d not in _DESCRIBE_BUILTIN_LABELS)
        path_labels = frozenset(declared)
    cap = max(1, min(_DESCRIBE_MAX_PER_TYPE, int(max_per_type)))

    params = {"repo_id": repo_id, "name": name, "file": file}
    rows, _more = _read(engine, _lookup_cypher(labels, path_labels), params, _DESCRIBE_MAX_CANDIDATES + 1)

    if not rows:
        suggestions, _ = _read(
            engine, _suggestions_cypher(labels), {"repo_id": repo_id, "name": name}, _DESCRIBE_MAX_SUGGESTIONS
        )
        filters = "".join(
            f", {key}={_echo(value)}" for key, value in (("label", label), ("file", file)) if value is not None
        )
        message = f"no node named {_echo(name)}{filters} in repository {_echo(repo_id)}.{_did_you_mean(suggestions)}"
        raise ToolError(f"{message} {_DESCRIBE_HINT}.")

    if len(rows) > 1:
        candidates = sorted(
            (_node_ref(row) for row in rows), key=lambda r: (r["label"], r["name"], r.get("file") or "")
        )
        return {
            "status": "ambiguous",
            "count": len(rows),
            "candidates": candidates[:_DESCRIBE_MAX_CANDIDATES],
            "truncated": len(rows) > _DESCRIBE_MAX_CANDIDATES,
        }

    (row,) = rows
    properties, properties_truncated = _visible_properties(row["properties"])
    node = {**_node_ref(row), "properties": properties, "properties_truncated": properties_truncated}
    groups, groups_truncated = _read(
        engine,
        _DESCRIBE_GROUPS,
        {"id": row["id"], "repo_id": repo_id, "direction": direction, "types": types,
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
            for every method
        cross_repo: If True, cross repository boundaries

    Returns:
        Dict with `components` (the start endpoints and every node within five
        hops, each {name, labels, repo_id}) and `edges` ({type} per relationship).

    Raises:
        ToolError: no endpoint has that name or path.
    """
    repo_filter = _cross_scope("start", "AND") if cross_repo else "AND start.repo_id = $repo_id"
    cypher = f"""
    MATCH (start:Endpoint)
    WHERE (start.name = $endpoint OR ($bare AND start.name ENDS WITH ' ' + $endpoint))
    {repo_filter}
    WITH collect(start) AS starts
    CALL (starts) {{
        UNWIND starts AS s
        MATCH (s)-[rels*1..5]->(node)
        UNWIND rels AS r
        RETURN collect(DISTINCT node) AS reached, collect(DISTINCT r) AS edges
    }}
    RETURN
        [n IN starts + [m IN reached WHERE NOT m IN starts] |
         {{name: n.name, labels: labels(n), repo_id: n.repo_id}}] AS components,
        [r IN edges | {{type: type(r)}}] AS edges
    """
    params = {"endpoint": start_endpoint, "bare": not any(ch.isspace() for ch in start_endpoint.strip())}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not results or not results[0]["components"]:
        raise _not_found(engine, repo_id, "endpoint", start_endpoint, ("Endpoint",), cross_repo)
    return results[0]


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

    Returns:
        Dict with count, results, and truncated flag containing callers with their
        types, repo_id, file and confidence (same-named callers in different files
        are separate rows); count 0 for a known target nothing calls. When targets
        of that name lie in several files and `file` is not given, each row adds
        `target_files`, the files of the targets it calls, and the dict adds
        `ambiguous_targets` ({label, file}, at most 20)

    Raises:
        ToolError: no node has that name, or none in `file`.
    """
    _at_least(1, max_results=max_results, modified_within_commits=modified_within_commits)
    cutoff = None
    if modified_within_commits is not None:
        cutoff = _resolve_recency_cutoff(engine, repo_id, modified_within_commits)
    ambiguous = _resolve_targets(engine, repo_id, target_name, cross_repo, file)
    target_files = ", collect(DISTINCT target.file) AS target_files" if ambiguous else ""

    repo_filter = _cross_scope("target", "AND") if cross_repo else "AND target.repo_id = $repo_id"
    class_filter = "AND rel.caller_class = $scope_to_class" if scope_to_class else ""
    recency_filter = "AND target.last_modified_at >= $cutoff" if cutoff is not None else ""
    resolved_filter = "AND rel.confidence IN ['resolved', 'package']" if resolved_only else ""
    cypher = f"""
    MATCH (caller)-[rel:CALLS]->(target)
    WHERE target.name = $target_name
    {repo_filter}
    {class_filter}
    {recency_filter}
    {resolved_filter}
    AND ($file IS NULL OR target.file = $file)
    WITH caller, min(CASE rel.confidence WHEN 'resolved' THEN 0 WHEN 'package' THEN 1 WHEN 'name' THEN 3
                     ELSE 2 END) AS rank{target_files}
    RETURN caller.name as name, labels(caller) as type, caller.repo_id as repo_id,
           coalesce(caller.file, caller.source_file) as file,
           CASE rank WHEN 0 THEN 'resolved' WHEN 1 THEN 'package' WHEN 3 THEN 'name' END as confidence{target_files and ", target_files"}
    ORDER BY rank, name, file
    LIMIT {LIST_ROW_LIMIT + 1}
    """
    params = {"target_name": target_name, "file": file}
    if not cross_repo:
        params["repo_id"] = repo_id
    if scope_to_class:
        params["scope_to_class"] = scope_to_class
    if cutoff is not None:
        params["cutoff"] = cutoff

    results, more = _list_query(engine, cypher, params)
    if ambiguous:
        results = [{**row, "target_files": sorted(row["target_files"])} for row in results]
    envelope = _capped_envelope(results, more, max_results)
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

    Returns:
        Dict with containing_modules, imported_modules, and related_components,
        each wrapped as {count, results, truncated} envelope objects.
        imported_modules are the names of what `component_name` IMPORTS: a
        Module's path, or, for a Kotlin import, the Class or Function it names.
        When components of that name lie in several files and `file` is not
        given, each row is `{name, target_files}` (the files of the components it
        is related to) instead of a bare name, and the dict adds
        `ambiguous_targets` ({label, file}, at most 20)

    Raises:
        ToolError: no node has that name, or none in `file`.
    """
    _at_least(1, max_results=max_results)
    ambiguous = _resolve_targets(engine, repo_id, component_name, cross_repo, file)
    repo_filter = _cross_scope("n", "WHERE") if cross_repo else "WHERE n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n {{name: $component_name}})
    {repo_filter}
    AND ($file IS NULL OR n.file = $file)
    OPTIONAL MATCH (m:Module)-[:CONTAINS*]->(n)
    OPTIONAL MATCH (n)-[:IMPORTS]->(imported)
    OPTIONAL MATCH (n)-[:CALLS]->(related)
    WITH CASE WHEN $split THEN n.file END AS target_file, m, imported, related
    RETURN target_file,
        COLLECT(DISTINCT m.name) as containing_modules,
        COLLECT(DISTINCT imported.name) as imported_modules,
        COLLECT(DISTINCT related.name) as related_components
    ORDER BY target_file
    """
    params = {"component_name": component_name, "file": file, "split": bool(ambiguous)}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    keys = ("containing_modules", "imported_modules", "related_components")
    if not ambiguous:
        return {key: _envelope(results[0][key], max_results) for key in keys}
    answer: dict[str, Any] = {}
    for key in keys:
        files: dict[str, list[str]] = {}
        for row in results:
            for name in row[key]:
                files.setdefault(name, []).append(row["target_file"])
        answer[key] = _envelope([{"name": name, "target_files": f} for name, f in files.items()], max_results)
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
        Summary with service count, module count, dependency graph stats
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
    RETURN
        $repo_id as repo_name,
        service_count, module_count, class_count, function_count,
        endpoint_count, database_count, vectorstore_count, queue_count
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
    }


_COMPARE_LAST_INDEX = "impacted_callers come from the last index of the working tree, not from either ref"
_COMPARE_STATUSES = ("added", "removed", "modified", "renamed")
_COMPARE_LISTS = ("added", "removed", "changed")
_COMPARE_CALLERS_CYPHER = """
UNWIND $targets AS t
CALL (t) {
  MATCH (n:Function {repo_id: $repo_id, name: t.name, file: t.file}) RETURN n
  UNION
  MATCH (n:Class {repo_id: $repo_id, name: t.name, file: t.file}) RETURN n
}
MATCH (caller)-[:CALLS]->(n)
WHERE caller.repo_id = $repo_id
RETURN DISTINCT caller.name AS caller, labels(caller)[0] AS caller_type,
       caller.file AS caller_file, n.name AS calls, n.file AS calls_file
ORDER BY caller_file, caller, calls_file, calls
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
                target = {"name": entry["name"], "file": change.path}
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

    Returns:
        Dict with direct_dependents and transitive_dependents wrapped as {count, results, truncated}
        envelopes, plus risk_level (computed from true untruncated count). Transitive
        dependents are 2 to `IMPACT_MAX_DEPTH` hops away; a node is listed at its
        nearest hop only, so a direct dependent is never also transitive. When
        components of that name lie in several files and `file` is not given, each
        file's are traced apart: every dependent adds `target_files`, the files of
        the targets it depends on, risk_level is the riskiest target's, and the dict
        adds `ambiguous_targets` ({label, file}, at most 20)

    Raises:
        ToolError: no node has that name, or none in `file`.
    """
    _at_least(1, max_results=max_results)
    ambiguous = _resolve_targets(engine, repo_id, component_name, cross_repo, file)
    repo_filter = _cross_scope("n", "WHERE") if cross_repo else "WHERE n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n {{name: $component_name}})
    {repo_filter}
    AND ($file IS NULL OR n.file = $file)
    WITH CASE WHEN $split THEN n.file END AS target_file, collect(n) AS l0
    CALL (l0) {{
    {_impact_expansion(cross_repo, with_ids=bool(ambiguous))}
    }}
    RETURN target_file, direct_dependents, transitive_dependents, direct_count
    ORDER BY target_file
    """
    params = {"component_name": component_name, "file": file, "split": bool(ambiguous)}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not ambiguous:
        row = results[0]
        return {
            "direct_dependents": _envelope(row["direct_dependents"], max_results),
            "transitive_dependents": _envelope(row["transitive_dependents"], max_results),
            "risk_level": _risk_level(row["direct_count"]),
        }
    # A dependent is listed once, at its nearest hop to any target (as the union of
    # the targets would list it), with the files of every target it depends on.
    direct_ids = {d["id"] for row in results for d in row["direct_dependents"]}
    dependents: dict[str, dict[str, Any]] = {}
    for row in results:
        for key in ("direct_dependents", "transitive_dependents"):
            for d in row[key]:
                if key == "transitive_dependents" and d["id"] in direct_ids:
                    continue
                entry = dependents.setdefault(d["id"], {"key": key, "name": d["name"], "type": d["type"], "target_files": []})
                entry["target_files"].append(row["target_file"])
    lists = {
        key: [{k: v for k, v in d.items() if k != "key"} for d in dependents.values() if d["key"] == key]
        for key in ("direct_dependents", "transitive_dependents")
    }
    return {
        **{key: _envelope(rows, max_results) for key, rows in lists.items()},
        "risk_level": _risk_level(max(row["direct_count"] for row in results)),
        "ambiguous_targets": ambiguous,
    }


def _risk_level(direct_count: int) -> str:
    return "high" if direct_count > 10 else "medium" if direct_count > 3 else "low"


_DIFF_TARGETS_CYPHER = """
CALL () {
  UNWIND $targets AS t
  CALL (t) {
    MATCH (n:Function {repo_id: $repo_id, name: t.name, file: t.file}) RETURN n
    UNION
    MATCH (n:Class {repo_id: $repo_id, name: t.name, file: t.file}) RETURN n
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
    return _sanitize_row(
        {"name": entry["name"], "kind": entry["kind"], "container": entry["container"], "file": path}
    )


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
        results, truncated} envelopes); risk_level; truncated and
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
                target = {"name": entry["name"], "file": change.path}
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

    row = {"direct_dependents": [], "transitive_dependents": [], "direct_count": 0, "matched": []}
    if targets or files:
        cypher = _DIFF_TARGETS_CYPHER + _impact_expansion(cross_repo)
        rows = _query(engine, cypher, {"repo_id": repo_id, "targets": targets, "files": files})
        if rows:
            row = rows[0]
    found = {(m["name"], m["file"]) for m in row["matched"]}
    missing = [t for t in targets if (t["name"], t["file"]) not in found]
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
        "direct_dependents": _envelope(row["direct_dependents"], max_results),
        "transitive_dependents": _envelope(row["transitive_dependents"], max_results),
        "risk_level": _risk_level(row["direct_count"]),
        "truncated": bool(reasons),
        "truncated_reasons": list(reasons),
        "notices": notices,
    }


def explain_architecture(
    engine: GraphEngine,
    repo_id: str,
) -> dict[str, Any]:
    """Generate a high-level architectural explanation of the repository.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID

    Returns:
        Dict with layers, key services, data flow overview
    """
    cypher = """
    MATCH (s:Service {repo_id: $repo_id})
    OPTIONAL MATCH (s)-[:USES]->(ds:Database|VectorStore|Queue)
    WITH s, COLLECT(DISTINCT ds.name) as datastore_names
    OPTIONAL MATCH (ep:Endpoint {repo_id: $repo_id})-[:CALLS]->(s)
    WITH s, datastore_names, COLLECT(DISTINCT ep.name) as endpoint_names
    RETURN
        COLLECT(DISTINCT {service: s.name, uses: datastore_names}) as services_and_datastores,
        COLLECT(DISTINCT {endpoints: endpoint_names, calls: s.name}) as endpoints
    LIMIT 1
    """
    results = _query(engine, cypher, {"repo_id": repo_id})
    if results:
        return {
            "services_and_datastores": results[0].get("services_and_datastores", []),
            "endpoints": results[0].get("endpoints", []),
        }
    return {"services_and_datastores": [], "endpoints": []}


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
) -> list[dict[str, Any]]:
    """Find requirements a component (Module/Service/etc.) satisfies.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component
        cross_repo: If True, search across repos

    Returns:
        List of Requirement notes satisfied by this component

    Raises:
        ToolError: no node has that name.
    """
    repo_filter = _cross_scope("n", "AND") if cross_repo else "AND n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n {{name: $component_name}})-[:SATISFIES]->(r:Requirement)
    WHERE true
    {repo_filter}
    RETURN r.name as name, r.title as title, r.body as body
    ORDER BY r.name
    """
    params = {"component_name": component_name}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    if not results and not _exists(engine, repo_id, component_name, _DESCRIBE_BUILTIN_LABELS, cross_repo):
        raise _not_found(engine, repo_id, "component", component_name, _DESCRIBE_BUILTIN_LABELS, cross_repo)
    return [_sanitize_row(r) for r in results]


def trace_design_rationale(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> dict[str, Any]:
    """Trace the design rationale (decisions, notes, requirements) behind a component.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the component (Module/Service/Class/etc.)
        cross_repo: If True, search across repos

    Returns:
        Dict grouping every Requirement/DesignDecision/ArchitectureNote linked
        to this component via SATISFIES or DOCUMENTED_BY.

    Raises:
        ToolError: no node has that name.
    """
    repo_filter = _cross_scope("n", "AND") if cross_repo else "AND n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n {{name: $component_name}})
    WHERE true
    {repo_filter}
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
    if not results:
        raise _not_found(engine, repo_id, "component", component_name, _DESCRIBE_BUILTIN_LABELS, cross_repo)
    return _sanitize_row(results[0])


def blame_component(
    engine: GraphEngine,
    repo_id: str,
    component_name: str,
    cross_repo: bool = False,
) -> list[dict[str, Any]]:
    """Find commits that modified a component's file, most recent first.

    Args:
        engine: GraphEngine instance
        repo_id: Repository ID
        component_name: Name of the Module (file) to look up commit history for
        cross_repo: If True, search across repos

    Returns:
        List of commits (sha, message, author, authored_date) that modified
        this component, ordered most-recent-first; empty for a file no
        recorded commit touched.

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

    results, _more = _list_query(engine, cypher, params)  # at most LIST_ROW_LIMIT commits
    if not results and not _exists(engine, repo_id, component_name, ("Module",), cross_repo):
        raise _not_found(engine, repo_id, "file", component_name, ("Module",), cross_repo)
    return [_sanitize_row(r) for r in results]


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
    if _source_off(registry, repo_id, "pr_source_enabled"):
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
    if _source_off(registry, repo_id, "issue_source_enabled"):
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
) -> dict[str, Any]:
    """Fetch a Function or Class's actual source text by reading its last-indexed line range.

    Reads live from disk using the graph's last-indexed start_line/end_line —
    a stale index could return the wrong lines; rescan first if freshness
    matters, same caveat as every other tool here.

    Args:
        engine: GraphEngine instance
        registry: RepoRegistry, used to resolve repo_id to its registered root path
        repo_id: Repository ID
        component_name: Name of the Function or Class to fetch source for
        cross_repo: If True, search across repos (the file is still read from
            whichever repo actually owns the matched node, via its own registry entry)
        file: Repo-relative path of the defining file, to pick one of several
            same-named Functions or Classes

    Returns:
        Dict with name, label, file, start_line, end_line, source, and
        docstring_full (when present). Empty/None fields if no match found.
        When several nodes match, `source` is None and the dict adds
        `status: "ambiguous"`, `count`, `truncated` and `candidates`
        ({label, name, file}; repo_id too when cross_repo): pass one's `file`.
        A file is decoded as the indexer decodes it (`source_text`): a
        Python coding line, else UTF-8, else cp1252 or Latin-1; when an
        undeclared file is not valid UTF-8, or a file isn't valid in its
        declared codec, the dict adds a `notice` saying how it was decoded.
    """
    repo_filter = _cross_scope("n", "AND") if cross_repo else "AND n.repo_id = $repo_id"
    cypher = f"""
    MATCH (n)
    WHERE (n:Function OR n:Class) AND n.name = $component_name
    {repo_filter}
    AND ($file IS NULL OR n.file = $file)
    RETURN n.name as name, labels(n) as labels, n.repo_id as repo_id,
           n.file as file, n.start_line as start_line, n.end_line as end_line,
           n.docstring_full as docstring_full
    ORDER BY n.repo_id, n.file, n.start_line
    LIMIT {_DESCRIBE_MAX_CANDIDATES + 1}
    """
    params: dict[str, Any] = {"component_name": component_name, "file": file}
    if not cross_repo:
        params["repo_id"] = repo_id

    results = _query(engine, cypher, params)
    empty = {
        "name": component_name, "label": None, "file": None,
        "start_line": None, "end_line": None, "source": None, "docstring_full": None,
    }
    if not results:
        return empty

    def label_of(row: dict[str, Any]) -> str:
        return next((label for label in row["labels"] if label in ("Function", "Class")), row["labels"][0])

    if len(results) > 1:
        candidates = [
            {"label": label_of(r), "name": r["name"], "file": r["file"], **({"repo_id": r["repo_id"]} if cross_repo else {})}
            for r in results[:_DESCRIBE_MAX_CANDIDATES]
        ]
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
        label: Optional node label filter for the target entity (validates against NODE_LABELS)
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
    if label is not None and label not in schema.NODE_LABELS:
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
        entity_type: Optional node label filter; a label not in NODE_LABELS is a ToolError
        cross_repo: If True, search across repos
        max_results: Maximum number of results to return in the envelope

    Returns:
        Dict with count, results, and truncated flag containing entities with
        name, type (labels), repo_id, and last_modified_at, ordered most-recently-modified first.
    """
    _at_least(1, within_commits=within_commits, max_results=max_results)
    if entity_type is not None and entity_type not in schema.NODE_LABELS:
        raise ToolError(f"unknown entity_type {_echo(entity_type)}; use one of: {', '.join(schema.NODE_LABELS)}")

    cutoff = _resolve_recency_cutoff(engine, repo_id, within_commits)
    recency_filter = "AND n.last_modified_at >= $cutoff" if cutoff is not None else "AND n.last_modified_at IS NOT NULL"

    repo_filter = _cross_scope("n", "AND") if cross_repo else "AND n.repo_id = $repo_id"
    label_filter = "AND $entity_type IN labels(n)" if entity_type else ""
    cypher = f"""
    MATCH (n)
    WHERE true
    {recency_filter}
    {repo_filter}
    {label_filter}
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
    if entity_type is not None:
        params["entity_type"] = entity_type

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
