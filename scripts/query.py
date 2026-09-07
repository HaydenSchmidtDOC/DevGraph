"""Standalone CLI for calling DevGraph's MCP tool functions without an MCP
client/transport.

Exists for environments (e.g. an org policy disabling MCP client
integration in the editor) where devgraph.mcp.server can't be reached via
its stdio JSON-RPC transport, but the venv/Neo4j stack it depends on is
otherwise fully usable. This imports devgraph.mcp.tools' functions directly
and calls them the same way devgraph.mcp.server's @server.tool() closures
do (see that module) — no new query logic, just a different entry point.

Usage:
    <venv python> scripts/query.py <tool_name> --repo-id <id> [--args '<json>']
    <venv python> scripts/query.py --list-tools

Examples:
    query.py search_component --repo-id manual-translator --args '{"query": "translate_chunks"}'
    query.py find_callers --repo-id manual-translator --args '{"target_name": "process_chunk"}'
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys

from devgraph.config import get_settings
from devgraph.graph.engine import GraphEngine
from devgraph.mcp import tools as tools_module
from devgraph.registry.store import RepoRegistry

# Mirrors devgraph.mcp.server's tool registration list — run_cypher is
# deliberately excluded unless enable_run_cypher is set, same as the real
# MCP server's opt-in escape hatch.
_EXCLUDED = {"run_cypher"}


def _available_tools() -> dict[str, object]:
    settings = get_settings()
    tools = {
        name: fn
        for name, fn in vars(tools_module).items()
        if inspect.isfunction(fn) and not name.startswith("_") and name not in _EXCLUDED
    }
    if settings.enable_run_cypher:
        tools["run_cypher"] = tools_module.run_cypher
    return tools


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tool_name", nargs="?", help="name of the devgraph.mcp.tools function to call")
    ap.add_argument("--repo-id", default=None, help="repo_id as shown by 'devgraph list'")
    ap.add_argument("--args", default="{}", help="JSON object of extra keyword arguments for the tool")
    ap.add_argument("--list-tools", action="store_true", help="print available tool names and exit")
    args = ap.parse_args()

    available = _available_tools()

    if args.list_tools or not args.tool_name:
        for name in sorted(available):
            print(name)
        return 0

    if args.tool_name not in available:
        print(f"ERROR: unknown tool '{args.tool_name}'. Use --list-tools to see available tools.", file=sys.stderr)
        return 1

    try:
        extra_kwargs = json.loads(args.args)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --args is not valid JSON: {exc}", file=sys.stderr)
        return 1

    fn = available[args.tool_name]
    sig = inspect.signature(fn)

    settings = get_settings()
    engine = GraphEngine(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    engine.verify_connectivity()
    registry = RepoRegistry(settings.registry_db_path)

    kwargs: dict[str, object] = {}
    if "engine" in sig.parameters:
        kwargs["engine"] = engine
    if "registry" in sig.parameters:
        kwargs["registry"] = registry
    if "repo_id" in sig.parameters:
        if not args.repo_id:
            print("ERROR: this tool requires --repo-id.", file=sys.stderr)
            return 1
        kwargs["repo_id"] = args.repo_id
    kwargs.update(extra_kwargs)

    try:
        result = fn(**kwargs)
    except TypeError as exc:
        print(f"ERROR: bad arguments for '{args.tool_name}': {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
