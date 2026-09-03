# DevGraph — Implementation Plan #6: Mentions (free-text doc-to-entity links)

Build-ready plan for detecting when a Markdown file mentions a known graph
entity by name in prose (not just the existing strict front-matter `links:`
field), and exposing that as a queryable `MENTIONS` relationship.

## Context

Today, `devgraph/indexer/docs/extractor.py` only links a Markdown file to
code when that file carries YAML front-matter with an explicit `type` and
`links: [...]` list — one file, one `Requirement`/`DesignDecision`/
`ArchitectureNote` node, links named explicitly by the author. Every other
Markdown file in a repo (READMEs, CLAUDE.md, plain notes) is invisible to
the graph.

The gap: an LLM reading a design doc that says "see `find_callers()` in
`tools.py`" has no way to ask DevGraph "what docs talk about `find_callers`"
or "what does this doc talk about" — it has to fall back to grepping.

This plan adds a second, independent extraction pass over **every**
`.md`/`.markdown` file in a registered repo (not limited to a configured
`docs_path`), detecting syntactically-plausible references to existing
entities and materializing them as `MENTIONS` edges. It does not touch or
replace the existing front-matter extractor — both run over the same files
when applicable.

Decisions already made (do not re-litigate without a reason):

- **Syntax-gated matching, not plain-text search.** A name match only
  counts as a mention if the surrounding syntax looks code-like. Plain
  English prose containing a token that happens to match an entity name
  (`get`, `Config`, `run`) must never produce an edge. See the Detection
  rule below — this was chosen specifically to keep false positives near
  zero without needing NLP.
- **Opt-in per repo**, off by default, same pattern as PR/issue ingestion
  (`pr_source_enabled`/`issue_source_enabled` in the registry).
- **Ambiguous names (shared by 2+ entities) are configurable**, default to
  linking all matches, with a setting to instead drop the match entirely.
- **One new MCP tool**, not folded into existing tools — keeps existing
  tool output shapes stable.

---

## Architecture

```
dispatch.py: index_paths()
 ├─ (existing) docs_root-scoped .md -> index_doc_file()          [front-matter only]
 └─ (new) every .md/.markdown, if repo.mentions_enabled -> index_mentions_file()
              └─ devgraph/indexer/mentions/extractor.py
                   ├─ creates/updates one `Document` node (keyed by repo-relative path)
                   └─ creates `Document -[:MENTIONS]-> <entity>` edges
```

New package: `devgraph/indexer/mentions/`
- `extractor.py` — `MentionsExtractor` class + `index_file()` function,
  structured identically to `devgraph/indexer/docs/extractor.py` (dataclasses
  for the node/relationship/result shapes, an `extract_from_source(content,
  filename)` method, a module-level `index_file(engine, repo_id, file_path)`
  entry point called from dispatch).

### Data model additions (`devgraph/graph/schema.py`)

- Add `"Document"` to `NODE_LABELS` (repo-scoped, same uniqueness constraint
  as every other label — keyed on `(repo_id, name)` where `name` is the
  repo-relative file path, matching how `Module` is keyed).
- Add `"MENTIONS"` to `RELATIONSHIP_TYPES`.
- `Document` node properties: `{"source_file": <repo-relative path>, "title":
  <first H1 heading in the file, or the filename stem if none>}`.
- A `Document` node is created for **every** scanned `.md` file, including
  ones that also produce a `Requirement`/`DesignDecision`/`ArchitectureNote`
  node via the existing front-matter extractor — these are two independent
  representations of the same file and both persist. Do not attempt to
  merge or deduplicate them; that is out of scope for this pass.

### Detection rule

Implemented as pure regex/string scanning over the raw file text — no NLP,
no external dependency. For each known entity name in the repo (`Module`,
`Class`, `Function`, `Service`, `Endpoint`, `Database`, `VectorStore`,
`Queue`, `Requirement`, `DesignDecision`, `ArchitectureNote` — query these
via `engine.run_cypher` scoped to `repo_id`, once per file indexed), scan
for exact, case-sensitive, word-boundary occurrences of that name in the
file text. Each occurrence counts as a mention only if **at least one**
holds:

1. **Code span** — the occurrence falls inside inline code (`` `Name` ``)
   or a fenced code block (` ```...``` `). Parse fences/spans first (a
   simple line-oriented scan: track fenced-block open/close by counting
   ` ``` ` delimiters; within a non-fenced line, find `` `...` `` spans via
   regex) and check membership by character offset.
2. **Call syntax** — the occurrence is immediately followed by `(`, allowing
   optional whitespace: regex `\bName\s*\(`.
3. **Declaration syntax** — the occurrence is immediately preceded on the
   same line by one of a fixed keyword list: `class`, `struct`, `interface`,
   `def`, `function`, `void`, `int`, `string`, `bool`, `const`, `let`,
   `var` (regex `\b(class|struct|interface|def|function|void|int|string|
   bool|const|let|var)\s+Name\b`). This list is a plain Python tuple at the
   top of `extractor.py` — extending it later is a one-line change, not a
   redesign.

A name with zero occurrences meeting any rule produces no edge. Bare
appearance of the name elsewhere in prose (no code span, no `(`, no
declaration keyword) is never a mention.

### Collision handling

Query all entities repo-wide matching a given name (any label). If more
than one entity matches:
- Default (`all`): create a `MENTIONS` edge to every matching entity.
- `skip` mode: create no edge for that name in that file.

Controlled by a new setting (see below), not per-repo — this is a global
behavior toggle, matching how other cross-cutting toggles
(`enable_run_cypher`, `allow_cross_repo`) are already global in
`devgraph/config/`.

### Settings addition (`devgraph/config/`)

Add to the existing `Settings` model:
- `mentions_ambiguous_mode: str = "all"`  (accepted values: `"all"`,
  `"skip"`; validate in the extractor, raise/log and fall back to `"all"`
  on an unrecognized value rather than crashing indexing)

### Registry addition (`devgraph/registry/store.py`)

Mirror `pr_source_enabled` exactly:
- New column `mentions_enabled INTEGER NOT NULL DEFAULT 0`, added both to
  the `CREATE TABLE` schema and to the existing migration list (the
  `("column_name", "ALTER TABLE ...")` tuples pattern already used for
  `pr_source_enabled`/`issue_source_enabled`).
- `RepoRecord.mentions_enabled: bool = False` field.
- `RepoRegistry.set_mentions_enabled(repo_id: str, enabled: bool) -> None`,
  delegating to the existing `_set_flag(repo_id, "mentions_enabled", value)`
  helper — same one-liner as `set_pr_source_enabled`.
- Include `mentions_enabled` in whatever row-construction/`SELECT` list
  currently includes `pr_source_enabled`/`issue_source_enabled` (the same
  places referenced at `devgraph/registry/store.py:241` and the columns
  around it).

### Dispatch wiring (`devgraph/indexer/dispatch.py`)

- Import `index_file as index_mentions_file` from
  `devgraph.indexer.mentions.extractor`.
- `index_paths` currently takes `docs_path: str | None = None`; add a
  `mentions_enabled: bool = False` parameter.
- Add a branch alongside the existing docs branch (near line 107): if
  `mentions_enabled` and `resolved.suffix in (".md", ".markdown")`
  (unconditional on `docs_root`, unlike the existing branch), call
  `index_mentions_file(engine, repo_id, resolved)`. This must run
  **independently** of the existing docs branch — a file inside `docs_root`
  with front-matter should hit both branches, not one or the other.
- `full_scan` and every CLI/watcher/tray call site that currently passes
  `docs_path=...` into `index_paths`/`full_scan` must also pass
  `mentions_enabled=registry.get(repo_id).mentions_enabled` (or the
  equivalent already-in-scope `record`/`repo` object at that call site).
  Grep for `docs_path=` to find every call site that needs the matching
  `mentions_enabled=` argument added.
- Deletion: no new code needed. `GraphEngine.delete_nodes_by_source_file`
  already matches on the `source_file`/`file`/`source` provenance property
  label-agnostically — since `Document.source_file` is set, removing a
  tracked `.md` file already cleans up its `Document` node and outgoing
  `MENTIONS` edges via the existing delete path.
- Known limitation, inherited from the existing docs extractor and
  acceptable for this pass: re-indexing an **edited** (not deleted) file
  only upserts — it does not remove `MENTIONS` edges for names that were
  removed from the file's text. `index_doc_file` has this same gap today.
  Do not attempt to fix this as part of this plan; it would require
  `replace_file_nodes`-style delete-then-upsert semantics as a separate,
  explicitly-scoped follow-up.

## Item 1 — `Document`/`MENTIONS` schema + extractor

**Files:** modify `devgraph/graph/schema.py`; new
`devgraph/indexer/mentions/__init__.py`, `devgraph/indexer/mentions/extractor.py`.

- Add `Document` and `MENTIONS` per the Data model section above.
- `MentionsExtractor.__init__(self, repo_id: str)`.
- `MentionsExtractor.extract_from_source(self, content: str, filename: str,
  known_entities: list[tuple[str, str]]) -> ExtractionResult`: takes
  `known_entities` as `(name, label)` pairs (already fetched by the caller —
  the extractor itself does no graph I/O, matching `DocsExtractor`'s
  separation) and returns a `Document` node plus zero or more `MENTIONS`
  relationships, applying the code-span/call-syntax/declaration-syntax rule
  and the configured ambiguous-name mode (pass `ambiguous_mode: str = "all"`
  as a constructor or method argument).
- Module-level `index_file(engine, repo_id, file_path, ambiguous_mode="all")`:
  reads the file, queries `engine.run_cypher` for all entity names/labels in
  `repo_id` (one query: `MATCH (n {repo_id: $repo_id}) WHERE n.name IS NOT
  NULL RETURN DISTINCT n.name as name, labels(n)[0] as label`), runs
  extraction, and upserts via `engine.upsert_nodes`/`upsert_relationships`
  (same calling convention as `devgraph/indexer/docs/extractor.py`'s
  `index_file`).

## Item 2 — Registry + settings + dispatch wiring

**Files:** modify `devgraph/registry/store.py`, `devgraph/config/` settings
module, `devgraph/indexer/dispatch.py`, and every call site passing
`docs_path=` into `index_paths`/`full_scan` (CLI `add`/`rescan`, watcher,
tray).

- Registry and settings changes per above.
- Dispatch changes per above.
- CLI: new `devgraph mentions <repo_id> <enable|disable>` command in
  `devgraph/cli/main.py`, following the existing `pr-source`/`issue-source`
  commands exactly — extend `_set_external_source_flag`'s `setter_flag`
  branching (or add a small parallel helper if extending that function's
  two-way `if/else` into a three-way chain reads worse) to call
  `registry.set_mentions_enabled`.

## Item 3 — MCP tool: `find_mentions`

**Files:** modify `devgraph/mcp/tools.py`, `devgraph/mcp/server.py`.

- `devgraph_tools.find_mentions(engine, repo_id, name, label=None,
  direction="mentioned_by", cross_repo=False, max_results=15) -> dict[str, Any]`,
  following `find_callers`'s exact shape (Cypher built from
  `direction`/`label`/`cross_repo` filters, `_envelope(results, max_results)`
  return):
  - `direction="mentioned_by"` (default): `MATCH (d:Document)-[:MENTIONS]->
    (target {name: $name})` — returns Documents that mention the entity
    named `name`. If `label` is given, add `AND target:{label}` (parameterize
    the label safely — do not string-interpolate user input directly into
    the label position; validate `label` against `schema.NODE_LABELS`
    first and reject/ignore an unrecognized value, mirroring how other
    tools already validate inputs against the schema module).
  - `direction="mentions"`: `MATCH (d:Document {name: $name})-[:MENTIONS]->
    (target)` — returns what a given Document (by repo-relative path)
    mentions.
  - Result rows: `{name, label, repo_id}` for the matched node on the
    non-anchor side, ordered by `name`.
- Register in `server.py`: add a `_TOOL_CATALOG` entry (`{"name":
  "find_mentions", "identifier_kind": "entity name (mentioned_by) or
  Document repo-relative path (mentions)", "envelope": True, "phase": 2}`)
  and a `@server.tool(annotations=_READ_ONLY)` wrapper following the
  `find_callers`/`find_related_files` pattern exactly — `"phase": 2`
  because this sits alongside the other docs/design-intent tools, not the git
  history ones).

## Item 4 — Tests

**Files:** new `tests/indexer/mentions/test_extractor.py`,
`tests/registry/test_mentions_flag.py` (or extend the existing
pr_source/issue_source flag test file if one already covers that pattern —
check `tests/registry/` first), `tests/mcp/test_find_mentions.py` (or
extend whatever existing file covers `find_callers`/`find_related_files`).

- Extractor unit tests, one per detection rule: code span match, fenced
  block match, call-syntax match (`Name(`), declaration-syntax match (each
  keyword in the list at least once), a negative case (bare prose mention,
  no edge), an ambiguous-name case under both `all` and `skip` modes.
- Dispatch integration test: a repo with `mentions_enabled=True` and a
  plain (non-front-matter) `.md` file mentioning a known `Function` produces
  a `Document` node and a `MENTIONS` edge after `index_paths`/`full_scan`;
  a repo with `mentions_enabled=False` (default) does not scan `.md` files
  outside `docs_root` at all.
- Registry test: `set_mentions_enabled` round-trips through `get()`, and a
  freshly `add_repo`'d record defaults to `False`.
- MCP tool test: both `direction` values return the expected envelope shape
  against a seeded test graph.

## Item 5 — Docs

**Files:** `README.md`, `PROJECT_STATUS.md`, `DEVGRAPH-CLIENT.md`.

- `README.md`: one paragraph under a "Mentions" or extend the existing
  docs-indexing description — what it detects, that it's opt-in, the
  enable command.
- `PROJECT_STATUS.md`: record as shipped, bump the MCP tool count from 18
  to 19, note the new `mentions_enabled` registry column and
  `mentions_ambiguous_mode` setting.
- `DEVGRAPH-CLIENT.md`: add `find_mentions` to whatever tool listing/table
  it already carries for the other 18 tools.

---

## Explicitly out of scope for this pass

- Removing stale `MENTIONS` edges on file edit (only on file delete) — see
  the Known limitation note above.
- Cross-repo mentions (a doc in repo A mentioning an entity in repo B) —
  entity lookup is always scoped to the mentioning file's own `repo_id`,
  same repo-isolation principle as the rest of DevGraph.
- Any dashboard UI for configuring `mentions_enabled` or
  `mentions_ambiguous_mode` — CLI/env-var only in this pass; a dashboard
  config panel is a separate, later plan.
- Function/class-level granularity beyond what the existing extractors
  already produce as entities — mentions only link to nodes that already
  exist in the graph, it does not discover new entities.
- Expanding the declaration-keyword list beyond the fixed starter set
  above (e.g. full per-language keyword coverage) — the list is
  intentionally small and extensible later if real repos show missed
  matches worth adding.
