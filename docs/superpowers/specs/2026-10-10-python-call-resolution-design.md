# Python call and import resolution (audit B7, B8)

## Goal

Python `CALLS` and `IMPORTS` edges point at the files the code actually
names, instead of every same-named function in the repository (B7) and a
dotted-path guess that missed packages, `src/` layouts and re-exports (B8),
without breaking the graph-accuracy invariant: incremental indexing equals a
fresh `full_scan`, edges and edge properties included
(`tests/indexer/test_graph_accuracy_fuzz.py`).

## Principle

Every Python edge is a pure function of its writer file's text and of
whether a target node exists. The extractor turns names into *candidate*
target files and emits a pinned row per candidate; only the candidates that
exist become edges, the rule `IMPORTS` already followed. No edge reads another
file's content or any repository-level state (no `pyproject.toml` roots, no
filesystem check in the extractor). A removed target goes with the
`DETACH DELETE` of its node, and an added one is relinked from the writer's
`name_refs`, so the same edges come out whatever order files are indexed in.

## Imports (`devgraph/indexer/python/resolve.py`)

- **Roots.** An absolute import is tried under every ancestor directory of the
  importer: `import a.b` in `x/y/f.py` names `a/b`, `x/a/b` and `x/y/a/b`.
  That covers a flat layout, a `src/` layout and a monorepo's nested projects.
  Configured roots were rejected: they are repository state, and a change to
  them would need a forced rescan.
- **Candidates.** A module path `p` names `p.py` and `p/__init__.py`. `from P
  import n` names P's candidates and the submodule `P/n`'s (n may be either).
  A relative import resolves against the importer's directory: `from . import
  n` names the package's own `__init__.py` (never `p.py`) and `n`'s candidates.
- **Edges.** `IMPORTS` goes to every candidate, one row each, deduplicated;
  the bare dotted name (`'os'`, `'a.b'`), which never matched a Module, is no
  longer emitted, and neither is a package's import of itself. Function-local
  imports count file-wide.
- **Bindings.** The same walk records, per file: receivers that are modules
  (`a.b`, an `as` alias), from-imported names (the module, the name as a
  submodule, and the name as defined, so `from p import f as h; h()` calls
  `f`), and star-imported modules.
- **Prefixes.** A module's package directory `p/` (never the repository root)
  is a *prefix pin*: a name re-exported from `p/__init__.py` can be defined in
  any file under it (`from devgraph.config import get_settings` reaches
  `devgraph/config/settings.py`). The prefix is emitted for every module
  candidate, decided from text alone, so namespace packages work too.

## Calls (the extractor; first matching tier wins per call site)

1. **Scope.** `f()` where `f` is a def in this file's module level or in an
   enclosing function: pinned to this file.
2. **Imported name.** `f()` bound by `from P import f`: P's files, plus P's
   prefix. A bare name nothing defines or imports falls to a star import's
   module when the file has one (Python builtins, a fixed list, excepted;
   chained star imports are not followed), and otherwise links nothing.
3. **`self`/`cls`/`super()`.** The enclosing class defines the method: this
   file. Otherwise its bases: an in-file base is walked recursively, an
   imported base (or `module.Base`) gives that module's files and prefix and
   is not followed further. A class pre-pass collects every class's methods
   and bases.
4. **Module attribute or typed receiver.** `mod.f()` / `a.b.f()` on an imported
   module, `Name.f()` on a from-imported name or an in-file class, and a
   variable typed by a parameter annotation (`X`, `X | None`, `Optional[X]`,
   quoted; `Any` and `object` type nothing), by `x = X(...)` (a capitalised
   callable), or by being a parameter named after a pytest fixture defined in
   the same file (its return annotation, or the `X(...)` it returns or
   yields); also `X().f()`. An in-file class is walked like `self` (tier 3),
   so `class Fake(GraphEngine): pass` reaches `GraphEngine`'s module. A
   parameter shadows an import of its name, and `import a.b` also binds `a`.
5. **Unknown receiver.** `obj.m()` keeps one bare-name row, except on a literal
   receiver (`"".join`, `{}.get`) or when `m` is a dict/list/str/set/bytes/io
   method (`get`, `items`, `join`, `append`, `read`, `close`, ...).

Rows carry `confidence`: `resolved` for a file pin, `package` for a prefix
pin (whose row also carries `exact`: the caller's file pins and its own file,
which the prefix leaves out), `name` for a bare row. Every call site of one
caller node to one callee name collapses into one set of rows: the bare row is
dropped when any resolved or package row exists (so a resolved and a bare edge
never merge into one relationship), and `caller_class` is the least non-null
enclosing class of those call sites. No two rows can then meet in one edge
with different properties, whatever the write order.

## Engine, `name_refs` and relink

- **Prefix pins.** A `to_file` ending in `/` matches every node of the name
  whose `file` starts with it and is not in the row's `exact` list. `_pin`
  tests the trailing `/` before `""` (the file-less handler stub), and the
  prefix has its own group key. The match seeks the `(repo_id, name)` index.
- **Rows per source.** Within a group, the edges out of one source with the
  same properties and origin share one row whose `targets` lists them, so the
  source is matched once per callee set rather than once per candidate. A
  resolved call names up to nine candidates; without this a DevGraph full
  scan cost about 40 % more than before, with it about 10 %.
- **`name_refs`.** An edge is recorded when another file can add its target:
  no `to_file`, or one that is neither `""` nor the writer's own file (which
  now includes the cross-file and prefix pins), or the existing
  unpinned-source rule. Each entry is `rel_type, from_label, from_name,
  from_file, to_label, to_name, caller_class, pins, confidence`: one entry per
  source and target name, its pins listed once (joined by `\x1e`, the
  file-less pin as `\x1d`), so the candidate list is never repeated per field.
  An empty pin field is unpinned. Fields 2 and 5, which `find_name_refs`
  filters on, have not moved, and a seven-field entry written before this
  change parses as unpinned with no confidence.
- **Relink.** An unpinned entry relinks as before. A pinned entry relinks an
  added node at one of its files (with the entry's confidence), or a node in a
  file under one of its prefixes that is neither one of its files nor the
  writer's own (`package`). Every relinked edge gets back its `caller_class`
  and `confidence`.
- **No Python reverse dependents.** Since an importer's edges come from its
  own text, a change to an imported file needs no re-index of the importer:
  `_expand_with_reverse_dependents` widens only Java batches now, and editing
  `devgraph/config/__init__.py` re-indexes one file instead of every importer.

## Consumers

- Insights (`_LOAD_INSIGHT_EDGES_CYPHER`) leave out `CALLS` of confidence
  `name` or `package`; a `CALLS` edge with no confidence (another language,
  or a graph not yet upgraded) stays in. A `Class` node ranks only through
  `EXTENDS`/`IMPLEMENTS` (no other dependency edge type points at a class), so
  a class nothing subclasses, such as `GraphEngine`, never ranks.
- `find_callers` returns each caller once with its best `confidence`
  (`resolved`, `package`, none, `name`), most certain first, and takes
  `resolved_only` (keep `resolved` and `package`). `impact_analysis` and
  `find_related_files` count every `CALLS` edge, whatever its confidence.

## Upgrade

`INDEX_FORMAT` is 4. The existing upgrade path (the schema rescan scheduler,
`catch_up`, `devgraph status`) runs a `full_scan`; each file's replace
unclaims its legacy bare `CALLS` (no `origins` counts as written by the
source's file) and writes the resolved edges and the new `name_refs`. A
watcher batch before the upgrade still parses the seven-field entries.

## Measurements

`scripts/measure_call_graph.py <repo_id> --scan --cleanup` indexes a tree
into a scratch repository and prints M1-M6; BEFORE ran on this branch's base
(8b31073), AFTER on the finished change, both over the same DevGraph
worktree.

- **M1** multi-target `CALLS` (edges from one caller to a name with more than
  one target), by confidence. Target: resolved under 2 % of resolved edges;
  total under 25 % of the 23,159 baseline.
- **M2** cross-file `CALLS` with no `IMPORTS` from the caller's Module to the
  callee's file or to the `__init__.py` of a package holding it. Target 0 for
  `resolved`; `name` is reported separately. A namespace package (no
  `__init__.py`) cannot satisfy the second form, so `package` edges into one
  would count; DevGraph has none.
- **M3** `IMPORTS` against an `ast` ground truth resolved with
  `importlib.machinery.PathFinder.find_spec` under the same ancestor roots
  (independent of `resolve.py`). Targets: recall at least 99 %, precision at
  least 98 %; importers of `devgraph/config/__init__.py` and
  `devgraph/agent/lifecycle.py` equal to a grep.
- **M4** PageRank top ten: no Function under `tests/`, no stoplisted name, and
  at least three of `GraphEngine`, `run_cypher`, `get_settings`,
  `upsert_relationships`, `devgraph/graph/engine.py`.
- **M5** 21 fixed DevGraph call sites (same-file, nested def, from-import,
  alias, module attribute, re-export, `self`, annotated receiver, untyped
  receiver, stoplisted untyped receiver), each the exact `(callee, file)` with
  the expected confidence; and callers of `upsert_nodes`, `get_settings`,
  `index_paths`, `run_cypher`, `full_scan` against an `ast` scan of call
  sites, at least 95 % each.
- **M6** cost: `full_scan` within +25 % of BEFORE, the incremental save of
  `devgraph/config/__init__.py`, the largest Module `name_refs` under 1 MiB,
  and the relink benchmark (`test_name_ref_relink_benchmark`) green.

### Results

| | Target | BEFORE | AFTER | |
|---|---|---|---|---|
| M1 multi-target CALLS | resolved < 2 %; total < 25 % of 23,159 | 26,028 (all bare) | 3,791 (resolved 14 = 0.17 %, name 3,510, other languages 267): 16 % | met |
| M2 resolved cross-file without import | 0 | 22,534 (all bare) | 0 (name 2,744 and other languages 263 reported apart) | met |
| M3 IMPORTS vs ground truth | recall ≥ 99 %, precision ≥ 98 % | recall 72 %, precision 100 % | recall 100 %, precision 100 %, none resolving under two roots | met |
| M3 importers of `config/__init__.py`, `lifecycle.py` | ≥ 33; = grep | 0; 0 | 31 (= grep in this tree); 5 (= grep) | see below |
| M4 PageRank top ten | no `tests/`, no stoplisted, ≥ 3 wanted | 7 under `tests/`, 2 stoplisted, 1 wanted | 0, 0, 2 wanted (`run_cypher`, `get_settings`) | partly, see below |
| M5 call-site sample | 21/21 | 0/21 (no confidence; the 3 alias sites absent) | 21/21 | met |
| M5 `find_callers` vs `ast` | ≥ 95 % each | 98-100 % | 98-100 % (also `upsert_relationships` 26/26, `list_repos` 45/45) | met |
| M6 `full_scan` | ≤ +25 % | 42.8 s | 50.9 s (+19 %; +7-13 % in alternating back-to-back runs) | met |
| M6 save of `config/__init__.py` | | 0.43 s | 0.16 s, one file re-indexed | |
| M6 largest `name_refs` | < 1 MiB | 89 KB | 224 KB (`devgraph/cli/main.py`) | met |

- **M3 importers of `devgraph/config/__init__.py`.** The graph links all 31
  files that import `devgraph.config` in this tree, exactly what a grep and
  the ground truth find; the design's 33 was counted on another checkout.
  Importing `devgraph.config.settings` runs the package's `__init__.py` too,
  but an import targets only the module it names, as the ground truth does.
- **M4.** Two of the five wanted nodes reach the top ten. `GraphEngine` is a
  class nothing subclasses, so it never ranks (only `EXTENDS`/`IMPLEMENTS`
  rank a class); `devgraph/graph/engine.py` ranks only through Module-to-Module
  `IMPORTS` (64th); `upsert_relationships` keeps 12 of its 25 callers through
  untyped `engine` parameters (each extractor's `index_file`), which are
  `name` edges and left out of insights (185th). Reaching it would take type
  inference beyond this design.
- The same-file pytest fixture rule (tier 4) came from M1: before it, test
  functions' untyped `engine` parameters left 7,059 `name` multi-target edges
  (32 % of the baseline), 3,276 of them to `run_cypher`.

BEFORE:

```json
{
 "M1": {
  "calls_by_confidence": {
   "None": 32062
  },
  "multi_target_by_confidence": {
   "None": 26028
  },
  "multi_target_total": 26028,
  "resolved_multi_share": null,
  "total_vs_baseline": 1.1239
 },
 "M2": {
  "cross_file_without_import_by_confidence": {
   "None": 22534
  }
 },
 "M3": {
  "graph_imports": 575,
  "truth_imports": 799,
  "recall": 0.7196,
  "precision": 1.0,
  "importers_of_config_init": 0,
  "importers_of_lifecycle": 0,
  "truth_importers_of_lifecycle": 5,
  "modules_resolving_under_several_roots": 0
 },
 "M4": {
  "top10": [
   [
    "Function",
    "join",
    "tests/watcher/test_watcher_reconcile.py"
   ],
   [
    "Function",
    "split",
    "tests/config/test_list_edit.py"
   ],
   [
    "Function",
    "run",
    "tests/mcp/test_compare_branches.py"
   ],
   [
    "Function",
    "run",
    "tests/cli/test_config_schema_cli.py"
   ],
   [
    "Function",
    "run",
    "tests/graph/test_engine_close.py"
   ],
   [
    "Function",
    "run",
    "devgraph/watcher/manager.py"
   ],
   [
    "Function",
    "get_settings",
    "devgraph/config/settings.py"
   ],
   [
    "Function",
    "home",
    null
   ],
   [
    "Function",
    "resolve",
    "tests/agent/test_schema_rescan.py"
   ],
   [
    "Function",
    "resolve",
    "tests/watcher/test_watcher_reconcile.py"
   ]
  ],
  "functions_under_tests": [
   "join",
   "split",
   "run",
   "run",
   "run",
   "resolve",
   "resolve"
  ],
  "stoplisted": [
   "join",
   "split"
  ],
  "wanted_present": [
   "get_settings"
  ]
 },
 "M5": {
  "sample_ok": 0,
  "sample_size": 21,
  "sample": [
   "MISS same-file: devgraph/indexer/dispatch.py:full_scan -> devgraph/indexer/dispatch.py:index_paths (None)",
   "MISS same-file: devgraph/indexer/dispatch.py:catch_up -> devgraph/indexer/dispatch.py:prune_stale_files (None)",
   "MISS nested def: devgraph/indexer/dispatch.py:index_paths -> devgraph/indexer/dispatch.py:index_one (None)",
   "MISS nested def: devgraph/indexer/dispatch.py:index_one -> devgraph/indexer/dispatch.py:_index_single_path (None)",
   "MISS from-import: devgraph/indexer/dispatch.py:_index_single_path -> devgraph/indexer/python/extractor.py:extract_python_file (None)",
   "MISS from-import: devgraph/indexer/dispatch.py:full_scan -> devgraph/indexer/walk.py:check_repo_root (None)",
   "MISS alias: devgraph/cli/main.py:doctor -> devgraph/config/schema_findings.py:project_schema_findings (absent)",
   "MISS alias: devgraph/cli/main.py:_set_project_config -> devgraph/config/edits.py:project_config_notes (absent)",
   "MISS alias: devgraph/indexer/dispatch.py:index_paths -> devgraph/indexer/docs/extractor.py:index_file (absent)",
   "MISS module attribute: devgraph/cli/main.py:tray_start -> devgraph/agent/lifecycle.py:start_tray_if_not_running (None)",
   "MISS module attribute: devgraph/cli/main.py:tray_stop -> devgraph/agent/lifecycle.py:read_tray_pid (None)",
   "MISS re-export: devgraph/agent/lifecycle.py:tray_pid_path -> devgraph/config/settings.py:get_settings (None)",
   "MISS re-export: devgraph/indexer/dispatch.py:index_paths -> devgraph/config/settings.py:get_settings (None)",
   "MISS self: devgraph/registry/store.py:add_repo -> devgraph/registry/store.py:_touch_change_marker (None)",
   "MISS self: devgraph/registry/store.py:set_docs_path -> devgraph/registry/store.py:get (None)",
   "MISS annotated receiver: devgraph/indexer/dispatch.py:_relink_name_refs -> devgraph/graph/engine.py:find_name_refs (None)",
   "MISS annotated receiver: devgraph/indexer/dispatch.py:_relink_name_refs -> devgraph/graph/engine.py:upsert_relationships (None)",
   "MISS annotated receiver: devgraph/indexer/dispatch.py:full_scan -> devgraph/graph/engine.py:set_index_format (None)",
   "MISS untyped receiver: devgraph/indexer/python/extractor.py:index_file -> devgraph/graph/engine.py:upsert_nodes (None)",
   "MISS untyped receiver: devgraph/indexer/jsts/extractor.py:index_file -> devgraph/graph/engine.py:upsert_relationships (None)",
   "MISS stoplisted untyped receiver: devgraph/indexer/pr_issues/extractor.py:fetch -> devgraph/registry/store.py:get (None)"
  ],
  "find_callers_recall": {
   "upsert_nodes": {
    "sites": 37,
    "found": 37,
    "recall": 1.0
   },
   "get_settings": {
    "sites": 51,
    "found": 51,
    "recall": 1.0
   },
   "index_paths": {
    "sites": 115,
    "found": 115,
    "recall": 1.0
   },
   "run_cypher": {
    "sites": 184,
    "found": 183,
    "recall": 0.9946
   },
   "full_scan": {
    "sites": 56,
    "found": 55,
    "recall": 0.9821
   }
  }
 },
 "M6": {
  "full_scan_s": 42.78,
  "config_init_save_files": 1,
  "config_init_save_s": 0.43,
  "largest_name_refs_module": "tests/dashboard/test_config_routes.py",
  "largest_name_refs_bytes": 89191,
  "total_name_refs_bytes": 2146988
 }
}
```

AFTER:

```json
{
 "M1": {
  "calls_by_confidence": {
   "resolved": 8175,
   "None": 327,
   "name": 3806,
   "package": 39
  },
  "multi_target_by_confidence": {
   "None": 267,
   "name": 3510,
   "resolved": 14
  },
  "multi_target_total": 3791,
  "resolved_multi_share": 0.0017,
  "total_vs_baseline": 0.1637
 },
 "M2": {
  "cross_file_without_import_by_confidence": {
   "None": 263,
   "name": 2744
  }
 },
 "M3": {
  "graph_imports": 810,
  "truth_imports": 810,
  "recall": 1.0,
  "precision": 1.0,
  "importers_of_config_init": 31,
  "importers_of_lifecycle": 5,
  "truth_importers_of_lifecycle": 5,
  "modules_resolving_under_several_roots": 0
 },
 "M4": {
  "top10": [
   [
    "Function",
    "_retry_transient",
    "devgraph/graph/engine.py"
   ],
   [
    "Function",
    "run_cypher",
    "devgraph/graph/engine.py"
   ],
   [
    "Function",
    "get_settings",
    "devgraph/config/settings.py"
   ],
   [
    "Function",
    "bounded_safe_load",
    "devgraph/config/yaml_bound.py"
   ],
   [
    "Function",
    "add_repo",
    "devgraph/registry/store.py"
   ],
   [
    "Function",
    "build_server",
    "devgraph/mcp/server.py"
   ],
   [
    "Function",
    "_check_expanded_size",
    "devgraph/config/yaml_bound.py"
   ],
   [
    "Function",
    "extract_python_file",
    "devgraph/indexer/python/extractor.py"
   ],
   [
    "Function",
    "load_project_schema",
    "devgraph/config/project_schema.py"
   ],
   [
    "Function",
    "read_bounded",
    "devgraph/paths.py"
   ]
  ],
  "functions_under_tests": [],
  "stoplisted": [],
  "wanted_present": [
   "get_settings",
   "run_cypher"
  ]
 },
 "M5": {
  "sample_ok": 21,
  "sample_size": 21,
  "sample": [
   "ok same-file: devgraph/indexer/dispatch.py:full_scan -> devgraph/indexer/dispatch.py:index_paths (resolved)",
   "ok same-file: devgraph/indexer/dispatch.py:catch_up -> devgraph/indexer/dispatch.py:prune_stale_files (resolved)",
   "ok nested def: devgraph/indexer/dispatch.py:index_paths -> devgraph/indexer/dispatch.py:index_one (resolved)",
   "ok nested def: devgraph/indexer/dispatch.py:index_one -> devgraph/indexer/dispatch.py:_index_single_path (resolved)",
   "ok from-import: devgraph/indexer/dispatch.py:_index_single_path -> devgraph/indexer/python/extractor.py:extract_python_file (resolved)",
   "ok from-import: devgraph/indexer/dispatch.py:full_scan -> devgraph/indexer/walk.py:check_repo_root (resolved)",
   "ok alias: devgraph/cli/main.py:doctor -> devgraph/config/schema_findings.py:project_schema_findings (resolved)",
   "ok alias: devgraph/cli/main.py:_set_project_config -> devgraph/config/edits.py:project_config_notes (resolved)",
   "ok alias: devgraph/indexer/dispatch.py:index_paths -> devgraph/indexer/docs/extractor.py:index_file (resolved)",
   "ok module attribute: devgraph/cli/main.py:tray_start -> devgraph/agent/lifecycle.py:start_tray_if_not_running (resolved)",
   "ok module attribute: devgraph/cli/main.py:tray_stop -> devgraph/agent/lifecycle.py:read_tray_pid (resolved)",
   "ok re-export: devgraph/agent/lifecycle.py:tray_pid_path -> devgraph/config/settings.py:get_settings (package)",
   "ok re-export: devgraph/indexer/dispatch.py:index_paths -> devgraph/config/settings.py:get_settings (package)",
   "ok self: devgraph/registry/store.py:add_repo -> devgraph/registry/store.py:_touch_change_marker (resolved)",
   "ok self: devgraph/registry/store.py:set_docs_path -> devgraph/registry/store.py:get (resolved)",
   "ok annotated receiver: devgraph/indexer/dispatch.py:_relink_name_refs -> devgraph/graph/engine.py:find_name_refs (resolved)",
   "ok annotated receiver: devgraph/indexer/dispatch.py:_relink_name_refs -> devgraph/graph/engine.py:upsert_relationships (resolved)",
   "ok annotated receiver: devgraph/indexer/dispatch.py:full_scan -> devgraph/graph/engine.py:set_index_format (resolved)",
   "ok untyped receiver: devgraph/indexer/python/extractor.py:index_file -> devgraph/graph/engine.py:upsert_nodes (name)",
   "ok untyped receiver: devgraph/indexer/jsts/extractor.py:index_file -> devgraph/graph/engine.py:upsert_relationships (name)",
   "ok stoplisted untyped receiver: devgraph/indexer/pr_issues/extractor.py:fetch -> devgraph/registry/store.py:get (absent)"
  ],
  "find_callers_recall": {
   "upsert_nodes": {
    "sites": 38,
    "found": 38,
    "recall": 1.0
   },
   "upsert_relationships": {
    "sites": 26,
    "found": 26,
    "recall": 1.0
   },
   "get_settings": {
    "sites": 51,
    "found": 51,
    "recall": 1.0
   },
   "index_paths": {
    "sites": 119,
    "found": 119,
    "recall": 1.0
   },
   "run_cypher": {
    "sites": 189,
    "found": 188,
    "recall": 0.9947
   },
   "full_scan": {
    "sites": 60,
    "found": 59,
    "recall": 0.9833
   },
   "list_repos": {
    "sites": 45,
    "found": 45,
    "recall": 1.0
   }
  }
 },
 "M6": {
  "full_scan_s": 50.85,
  "config_init_save_files": 1,
  "config_init_save_s": 0.16,
  "largest_name_refs_module": "devgraph/cli/main.py",
  "largest_name_refs_bytes": 224445,
  "total_name_refs_bytes": 5251492
 }
}
```

## Recall losses and risks

- Cross-file inheritance deeper than one imported base, dynamic dispatch, and
  untyped receivers (a fixture from `conftest.py`, an attribute such as
  `self.engine`) calling a stoplisted name are not linked.
- Exclusivity: when one caller resolves a name anywhere, its bare row for
  that name goes, even if the resolved target is outside the repository
  (`subprocess.run()` beside `runner.run()` in one function leaves no edge to
  the repository's `run`). Kept for determinism; no DevGraph site hits it
  (searched; nor is there an in-repo cross-file inherited `self.m()`), so it is covered by a unit test rather than an M5 site.
- Constructor calls `Foo()` stay unlinked (`CALLS` targets `Function`).
- An ancestor root can link a stdlib-named module next to the importer
  (`json.py`); M3 found none on DevGraph.
- Rows per resolved call grow up to nine candidates; M6 holds the cost.
- Other languages change only through shared engine code: their pins are the
  writer's own file or `""`, so `name_refs` excludes them as before, and they
  carry no confidence, so insights keep them. Java keeps its reverse-dependent
  re-index.
