---
name: devgraph-build-component
description: Build one DevGraph Phase-1 component (extractor, watcher, MCP tool, CLI command) against the established conventions in devgraph/graph and devgraph/registry, and self-verify it before reporting done. Use when implementing a specific, scoped piece of the DevGraph build-out described in Blueprints/Implementation Plan #1.md.
---

# DevGraph component build

You are implementing ONE scoped component of DevGraph, a local-first developer
knowledge graph platform. Follow these steps in order.

## 1. Ground yourself

- Read `Blueprints/Design Brief #1.md` and `Blueprints/Implementation Plan #1.md`
  in full before writing code — do not rely on a summary.
- Read `CLAUDE.md` and `AGENTS.md` for hard constraints.
- Read the existing `devgraph/graph/schema.py` and `devgraph/graph/engine.py` —
  these define the node labels, relationship types, and idempotent-MERGE
  convention every extractor/tool must follow. Do not invent new node labels
  or relationship types; import `NODE_LABELS`/`RELATIONSHIP_TYPES` from
  `devgraph.graph.schema` if you need to validate against them.
- Read `devgraph/registry/store.py` — the registry is the ONLY source of
  truth for which repo paths may be touched. Never accept a raw filesystem
  path from a tool/CLI argument and act on it directly; resolve it through
  `RepoRegistry` first.

## 2. Hard constraints (non-negotiable, from the Design Brief)

- No path outside the registry may ever be watched, read, or indexed.
- Every node/edge you write carries `repo_id`. Every read filters by it.
  Cross-repo results require an explicit opt-in flag — never the default.
- No telemetry, no cloud calls, no outbound network requests.
- Writes must be idempotent (`MERGE` keyed on `(repo_id, name)` or
  `(repo_id, path)`), so re-running indexing never duplicates nodes.
- The MCP layer exposes high-level tools, not raw Cypher, as the primary
  interface. `run_cypher` is an advanced opt-in escape hatch only.

## 3. Implement

Write the component in its designated `devgraph/<subpackage>/` location per
the layout in the Implementation Plan. Keep it focused — implement only the
scoped component you were asked for for, not adjacent ones.

## 4. Self-verify before reporting done

- Write or run a smoke test that exercises the component against the live
  local Neo4j (`bolt://127.0.0.1:7687`, user `neo4j`, password
  `devgraph-local-dev` — DevGraph's own isolated container, already running)
  or against a temp SQLite/fixture repo as appropriate. Do not just eyeball
  the code — actually run it.
- If your component writes to the graph, verify idempotency: run the write
  twice and confirm no duplicate nodes result.
- If your component touches the registry or filesystem, verify it rejects a
  path that was never registered.
- Clean up any test data you wrote to the shared dev Neo4j instance
  (delete-by-repo_id) so you don't pollute it for other components running
  in parallel — use a distinctive throwaway `repo_id` like
  `_smoketest_<component>` for this.

## 5. Report back

Summarize: what you built (file paths), what you verified and how (exact
commands/output), and anything you deliberately deferred or flagged as an
open question. Keep it tight — the orchestrating session needs to review
your diff, not read a narrative.
