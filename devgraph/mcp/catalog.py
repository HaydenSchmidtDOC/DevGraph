"""Machine-readable catalog of DevGraph's own MCP tools. Stdlib only, so
`doctor` and `config validate` can read it even when the `mcp` package is broken.

It backs the devgraph://tool-catalog resource. It lives apart from the
@server.tool() registrations in `devgraph.mcp.server`, whose tests assert the two
agree, so it still cannot silently drift from the actual tool surface.
"""

from __future__ import annotations

from typing import Any

TOOL_CATALOG: list[dict[str, Any]] = [
    {"name": "search_component", "identifier_kind": "name/description substring", "envelope": True, "phase": 1, "note": "exact name matches first, then prefixes, then substrings; each result has its file; count_is_lower_bound marks a count the substring scan never finished"},
    {"name": "list_recent_changes", "identifier_kind": "commit-count window (within_commits), optional entity_type label", "envelope": True, "phase": 3},
    {"name": "trace_request_flow", "identifier_kind": "endpoint name (\"GET /users/<id>\") or bare path (\"/users/<id>\", every method)", "envelope": False, "phase": 1},
    {"name": "get_service_dependencies", "identifier_kind": "service name", "envelope": False, "phase": 1},
    {"name": "find_callers", "identifier_kind": "function/class/service/endpoint name (not a file path)", "envelope": True, "phase": 1},
    {"name": "find_related_files", "identifier_kind": "function/class name (not a file path)", "envelope": True, "phase": 1},
    {"name": "summarise_repository", "identifier_kind": None, "envelope": False, "phase": 1},
    {"name": "compare_branches", "identifier_kind": "two local git refs (branch_a = base, branch_b = head; compared from their merge base, like git diff a...b)", "envelope": False, "phase": 3, "note": "the response is not an envelope; files and impacted_callers inside it are {count, results, truncated}"},
    {"name": "impact_analysis", "identifier_kind": "function/class name (not a file path)", "envelope": True, "phase": 1, "note": "transitive dependents stop at 4 hops"},
    {"name": "impact_analysis_for_diff", "identifier_kind": "two local git refs (base_ref, head_ref; compared from their merge base, like git diff base...head)", "envelope": True, "phase": 3, "note": "traces dependents of the changed and removed functions/classes only; added ones are listed apart; transitive dependents stop at 4 hops"},
    {"name": "explain_architecture", "identifier_kind": None, "envelope": False, "phase": 1},
    {"name": "list_services", "identifier_kind": None, "envelope": True, "phase": 1},
    {"name": "explain_decision", "identifier_kind": "DesignDecision name/id", "envelope": False, "phase": 2},
    {"name": "find_requirements_for", "identifier_kind": "component name", "envelope": False, "phase": 2},
    {"name": "trace_design_rationale", "identifier_kind": "component name", "envelope": False, "phase": 2},
    {"name": "find_mentions", "identifier_kind": "entity name (mentioned_by) or Document repo-relative path (mentions)", "envelope": True, "phase": 2, "note": "requires mentions indexing opt-in; when off, an empty envelope with a notice naming the enable command"},
    {"name": "blame_component", "identifier_kind": "file path (not a function name)", "envelope": False, "phase": 3},
    {"name": "find_related_prs", "identifier_kind": "file path (not a function name)", "envelope": True, "phase": 3, "note": "requires PR/issue ingestion opt-in; when off, an empty envelope with a notice naming the enable command"},
    {"name": "god_nodes", "identifier_kind": None, "envelope": True, "phase": 3},
    {"name": "find_dependency_cycles", "identifier_kind": "dependency relationship type (CALLS/DEPENDS_ON/EXTENDS/IMPORTS/USES), not a component name", "envelope": True, "phase": 3},
    {"name": "find_communities", "identifier_kind": None, "envelope": True, "phase": 3, "note": "requires computed graph insights (automatic after indexing, or `devgraph insights`)"},
    {"name": "key_nodes", "identifier_kind": "metric (pagerank/betweenness), not a component name", "envelope": True, "phase": 3, "note": "requires computed graph insights (automatic after indexing, or `devgraph insights`)"},
    {"name": "issue_history_for", "identifier_kind": "file path (not a function name)", "envelope": True, "phase": 3, "note": "requires PR/issue ingestion opt-in; when off, an empty envelope with a notice naming the enable command"},
    {"name": "get_source", "identifier_kind": "function/class name (not a file path), optional file to disambiguate", "envelope": False, "phase": 2, "note": "a name several nodes share returns status ambiguous with candidates ({label, name, file}) instead of source"},
    {"name": "describe_node", "identifier_kind": "node name (any label; a file, folder or docs node by repo-relative path or front-matter id), optional label and file to disambiguate", "envelope": False, "phase": 3, "note": "the response is not an envelope; each relationship group inside outgoing/incoming is {count, results, truncated}"},
    {"name": "run_cypher", "identifier_kind": "raw Cypher", "envelope": False, "phase": None, "note": "only registered when enable_run_cypher=true; prefer the purpose-built tools above"},
]


def builtin_tool_names() -> frozenset[str]:
    """Names of DevGraph's own MCP tools: a project tool may not take one over."""
    return frozenset(entry["name"] for entry in TOOL_CATALOG)


def scoped_tool_id(name: str, origin: str, repo_id: str | None = None) -> str:
    """The identity a tool carries in telemetry and on the Config page.

    Built-ins keep their bare name, a global tool is `gl_<name>`, a project tool
    `<repo_id>_<name>`. Not reversible (a repo id may itself be `gl`), so telemetry
    stores the origin beside it.
    """
    if origin == "global":
        return f"gl_{name}"
    if origin == "project":
        return f"{repo_id}_{name}"
    return name
