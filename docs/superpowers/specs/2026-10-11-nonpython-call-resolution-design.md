# Call and import resolution beyond Python (audit-2 items 4, 7, 11, 12)

## Goal

`CALLS` and `IMPORTS` edges out of JS/TS, Go, Java, Kotlin, C#, Rust and C++
files point at the files the code names, as Python's already do
(docs/superpowers/specs/2026-10-10-python-call-resolution-design.md), instead
of every same-named function in the repository. The graph-accuracy invariant
holds throughout: incremental indexing equals a fresh `full_scan`, edges and
edge properties included (`tests/indexer/test_graph_accuracy_fuzz.py`).

Kotlin also loses declarations today (item 11): tree-sitter-kotlin 1.1.0, the
latest release, turns everything after a member that closes on the same line
(`class A { fun a() {} }`) into one error node.

The work lands in three slices, each with its own `INDEX_FORMAT` bump: A (the
shared core, JS/TS and the Kotlin parse fix, format 6), B (Go, Java, Kotlin,
format 7) and C (Rust, C#, C++, format 8). This spec covers what has landed;
the later slices' rules are summarised at the end.

## Principle

An edge is a pure function of the writer file's text, of the resolver
configuration that applies to it (below), and of whether its target exists.
The extractor turns names into candidate *pins*; a row whose pin matches no
node writes nothing; a removed target goes with its node's `DETACH DELETE`;
an added one is relinked from the writer's `name_refs`. Tiers, confidence,
collapsing and exclusivity are Python's: the first tier that resolves a call
site wins, every call site of one caller to one callee name collapses to one
set of rows, and a bare-name row is dropped when any pinned row exists.

Function and Class nodes are keyed by (name, file), so a file is the finest
target there is: every tier only picks files.

## Resolver configuration

Some names cannot be resolved from a file's text alone: a TypeScript path
alias (`@/lib/text`) and a Go import path (`example.com/app/store`) mean
nothing without `tsconfig.json` or `go.mod`. These files are *resolver
configuration*, declared inputs of extraction in the way a `.gitignore` is an
input of the walk (`devgraph/indexer/resolver_config.py`):

- **What is read.** For a JS/TS file, the nearest `tsconfig.json` (else
  `jsconfig.json`) above it, parsed as JSONC (comments, trailing commas and a
  byte order mark allowed; a malformed file gives no aliases), with its
  `extends` chain and the configs its `references` name followed inside the
  repository (a package name or an absolute path is outside it; a chain that
  extends itself is malformed). `paths` and
  `baseUrl` resolve against the config that defines them; `paths` win over
  `baseUrl`. A config under a path the walk ignores (a `.gitignore`, a
  `node_modules`) is treated as absent.
- **Fingerprint.** Per language, a hash of the relevant content of every
  configuration file the walk finds (for TS: `baseUrl`, `paths`, `extends`,
  `references`, with every config those reach, whatever its name; for Go:
  each go.mod's `module` line and each go.work; a compiler flag or a
  `require` bump never counts), stored on the Repository node
  (`resolver_config`) with the list of paths it looked at, found or not.
- **Triggers.** A live batch (`RepoSync.on_changes`) that changes or deletes a
  path on that list, a directory holding one, or a path named like a
  configuration file recomputes the fingerprint; `catch_up` recomputes it after
  its walk. When it differs, every graph file of that language is re-indexed in
  one more batch, and the new fingerprint is stored only once that batch
  succeeds (never after a batch cut short by shutdown). `full_scan` takes it
  before reading any file and stores it at the end
  (`dispatch.sync_resolver_config`).
- **Go today.** The Go extractor still reads only the root go.mod's module
  line until slice B; that line is in the fingerprint, so editing it now
  re-indexes the Go files, which it did not before.

Fresh is then extract(text, configuration), and an incremental run re-extracts
every dependent when the configuration changes.

## Engine: path pins

A row's `to_file` is a *pin*. Python used two (a file, and a recursive package
directory ending in `/`); the other languages need three more. One function,
`pin_matches(pin, path)` in `devgraph/indexer/common.py`, defines them, and the
Cypher in `engine._end_match` mirrors it (a randomized test checks that the two
agree):

| Pin | Matches | Confidence | Used by |
|---|---|---|---|
| `a/b.ts` | that file | resolved | all |
| `a/b/.` | files directly in `a/b/` (`.` alone: the repository root's files) | resolved | Go packages, Kotlin same package |
| `/a/b/C.java` | files whose `/`-prefixed path ends with it | package | Java/Kotlin/C# class files, C++ includes |
| `/a/b/.` | files directly in any directory ending `/a/b/` | package | Kotlin top-level functions, Java wildcards |
| `a/b/` | files anywhere under `a/b/` | package | Python packages, JS barrels, Rust modules |

- No repository-relative path starts with `/` or ends with `/.`, so a pin can
  never be mistaken for a file. A pin may end with `!suffix` (Go's
  `!_test.go`): the matched file must not end with the suffix. The kind is
  read after the suffix is stripped.
- **Confidence is the pin kind's**, so it never depends on which other pins a
  row was written with. Precedence runs down the table: each row's pin leaves
  out every file a higher-precedence pin of the same call (the same caller and
  callee name) matches. `exclude` carries those *pins*, not paths, and the
  match is `NOT any(p IN exclude WHERE pin_matches(p, file))`; a recursive
  prefix also leaves out the writer's own file, as before. Relink rebuilds
  `exclude` from the entry's pins in the same way.
- **Module targets** (`IMPORTS`) have no `file`. Every Module carries `dir` and
  `basename`, derived from its path in the engine's node writer, with
  `(repo_id, dir)` and `(repo_id, basename)` lookup indexes. A directory pin to
  a Module seeks `dir`; a suffix pin seeks `basename` and keeps the paths ending
  with it. The row's `to_name` is then that seek key (the directory or the
  basename), and the Module's own path is what relink pins.
- **A file's imports collapse like one call.** Every `IMPORTS` edge of a file
  ends at a Module, so its imports by path (unpinned, no confidence, as
  before) and its Module pins form one set (`calls.import_rows`): a pin leaves
  out what a path import or a stronger pin matches, and the file itself.
  Relink rebuilds the set from the file's `IMPORTS` entries.
- **`no_self`** on a row adds `a <> b`: a call `x.m()` on an untyped receiver
  inside a method `m` never links the method to itself. A collapsed row has it
  only when every one of its call sites had it.

`name_refs` keeps its format (pins are strings). A pinned entry no longer
stores one confidence for all its pins: its confidence field is `pin` when its
edges carry one, and relink gives each pin its kind's (`common.best_pin`
picks the strongest pin matching the added file, as the writer's `exclude`
did). Relink also looks for Module entries by an added Module's `dir` and
`basename`.

Until the resolvers write them, the graph-accuracy fuzz writes path pins of
every kind from `# pin` comments in its Python files, and the relink
benchmark runs with pinned entries too.

## Shared extractor pieces (`devgraph/indexer/calls.py`)

`call_rows` (moved from the Python extractor) turns one caller's call sites to
one callee name into rows, with `no_self` and the precedence above.
`STOP_METHODS[lang]` are the container, string and runtime methods that link
nothing on an untyped receiver; `STOP_TYPES[lang]` the library types whose
static calls link nothing.

## JS/TS (`devgraph/indexer/jsts/extractor.py`)

Specifier resolution: a relative specifier names its existing candidate files
(the extensions and `index` files the extractor already tried; a `.js`
specifier also names the `.ts`/`.tsx` source), plus the recursive prefix of
its directory form, so a barrel re-export reaches the defining file at package
confidence (never the repository root as a prefix). An aliased specifier
resolves through the file's tsconfig (`paths` first, then `baseUrl`) to the
same candidates. Any other bare specifier (`react`, `lodash`) is external: no
`IMPORTS` edge (the old `node_modules/<name>` guess never matched, the walk
skipping `node_modules`), and a call through its binding links nothing.

Tiers, first match wins:

1. A function, class or `const` arrow defined in the file (nested included):
   this file.
2. A name bound by a named or default import, `export … from`, or
   `const {a} = require(…)`: the specifier's candidates and prefix.
3. `this.m()`: the enclosing class, else its in-file bases, else an imported
   base's candidates.
4. `ns.f()` on a namespace import or a `require` binding; a receiver typed by
   an annotation (`x: T`, `T | null`; not `T[]`), a constructor parameter
   property (`constructor(private repo: Repo)` types `this.repo`), `x = new
   T()`, or `T.f()` on a class: T's file (in this file, or its import's).
5. Anything else: a bare-name row, unless the method is in `STOP_METHODS.js`
   or the receiver is a literal or a library type (`STOP_TYPES.js`: `JSON`,
   `Object`, `console`, ...); `no_self` on a member call. A bare `f()` nothing
   defines or imports links nothing, except in a classic script (no `import`,
   `export` or `require`), whose top-level declarations are globals (browser
   globals such as `fetch` excepted).

A parameter shadows an import of its name, as in Python. Calls inside an
anonymous function (a callback, an IIFE) are still not attributed to anyone.

## Kotlin parse recovery (`devgraph/indexer/kotlin/extractor.py`)

1. When the tree has errors, reparse a same-length patch that turns ` }`
   after a member on the same line into `;}`, and keep whichever tree has fewer
   bytes under error nodes, then fewer invented tokens (the original on a
   tie: the blind patch can break a string template). An invented member
   terminator is hidden: it shows only as an error flag on the class body
   with no erroneous child, and counts as one.
2. Each remaining top-level error node is split at column-0 declaration starts
   (a declaration keyword, a modifier such as `data`, `private`, `sealed`, or an
   annotation); each piece is parsed alone over its own byte range
   (tree-sitter's `included_ranges`), so offsets stay the same and the cost
   follows the piece, patched as in 1. when that helps, and its top-level
   declarations are visited. At most 5,000 pieces and four times the file's
   bytes are parsed this way; the rest stays unrecovered. (Parsing each piece
   blanked at full file length was quadratic: 18 s for a 39 KB file with an
   unclosed brace near the top, now 0.13 s.)
3. Text is always read from the original bytes.

## Later slices

- **Go:** an import resolves by the longest in-repo module path over every
  `go.mod` (and `go.work`, and in-repo `replace` targets) to an anchored
  directory pin with `!_test.go`; calls by package, receiver type and struct
  field type.
- **Java/Kotlin:** a source root from the package declaration; an import gives
  an anchored class file and a package-confidence suffix pin (multi-module
  builds, vendored duplicates); wildcards anchored and suffix directory pins.
- **C#:** a referenced type `T` gives the suffix pin `/T.cs`, so C# calls
  resolve at package confidence.
- **Rust:** crate roots (`src/`, `tests/`, `benches/`, `examples/`,
  `src/bin/*.rs`), `crate::`/`self::`/`super::` paths, `mod foo;` in `src/a.rs`
  naming `src/a/foo.rs`.
- **C++:** a quoted `#include` gives the relative file and a suffix pin, plus
  same-stem sources.

## Measurements

`scripts/measure_call_graph.py --fixture <lang>` copies a ground-truth fixture
(`tests/fixtures/callgraph/<lang>`, written from each language's own rules by
someone other than the resolver's author) to a temporary folder, scans it into
a scratch repository and compares the graph with its `expected.json`. A
`resolved` or `package` edge is *linked*; a package edge to the right file is a
correct link, its confidence reported apart. `tests/scripts/test_measure_fixtures.py`
asserts the targets, a strict xfail for each language not landed yet.

- **M1** multi-target `CALLS` (one caller, one callee name, several targets) by
  confidence. Target: resolved under 2 % of resolved edges.
- **M2** precision of linked `CALLS` against the truth (resolved and all
  edges reported too). Target: at least 98 %, and no resolved edge between two
  directories without an `IMPORTS` between their files.
- **M3** `IMPORTS` against the truth. Targets: precision 98 %, recall 95 %.
- **M4** `CALLS` recall at any confidence (linked and resolved recall
  reported). Target: 95 %, and no stoplisted name in the PageRank top five
  unless the truth calls it there.
- **M5** every named site: each `calls` row with a `note` present (and none of
  its `not_files`), each `no_edge` absent; for Kotlin, every expected symbol
  extracted.
- **M6** cost: the fixture's `full_scan` and DevGraph's within +15 % of
  BEFORE, the largest `name_refs` under 1 MiB, and the relink benchmark
  (`test_name_ref_relink_benchmark`) green.

BEFORE, measured on this branch before any extractor change (sample lists
left out; every edge carries no confidence, so nothing is linked yet):

```json
{
 "ts": {
  "M1": {"calls_by_confidence": {"None": 32}, "multi_target_by_confidence": {"None": 8}, "resolved_multi_share": null},
  "M2": {"precision_resolved": null, "precision_linked": null, "precision_all": 0.6875, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 14, "truth_imports": 24, "precision": 1.0, "recall": 0.5833},
  "M4": {"recall_any": 0.9565, "recall_linked": 0.0, "recall_resolved": 0.0, "top5": [["check", "src/api/http.ts"], ["get", "src/api/http.ts"], ["src/types.ts", "src/types.ts"], ["src/api/http.ts", "src/api/http.ts"], ["src/lib/storage.ts", "src/lib/storage.ts"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 32, "sites": 41},
  "M6": {"full_scan_s": 2.3, "largest_name_refs_module": "src/pages/Profile.tsx", "largest_name_refs_bytes": 1506, "total_name_refs_bytes": 11346},
  "failures": ["M1 resolved multi-target share < 2 %", "M2 linked precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M5 every named site"]
 },
 "go": {
  "M1": {"calls_by_confidence": {"None": 43}, "multi_target_by_confidence": {"None": 24}, "resolved_multi_share": null},
  "M2": {"precision_resolved": null, "precision_linked": null, "precision_all": 0.7209, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 2, "truth_imports": 14, "precision": 1.0, "recall": 0.1429},
  "M4": {"recall_any": 1.0, "recall_linked": 0.0, "recall_resolved": 0.0, "top5": [["normalize", "internal/store/keys.go"], ["Get", "internal/store/memory.go"], ["cache/cache.go", "cache/cache.go"], ["abs", "money/round.go"], ["header", "tools/internal/codegen/render.go"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 26, "sites": 33},
  "M6": {"full_scan_s": 1.37, "largest_name_refs_module": "main.go", "largest_name_refs_bytes": 1037, "total_name_refs_bytes": 3736},
  "failures": ["M1 resolved multi-target share < 2 %", "M2 linked precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M5 every named site"]
 },
 "java": {
  "M1": {"calls_by_confidence": {"None": 34}, "multi_target_by_confidence": {"None": 17}, "resolved_multi_share": null},
  "M2": {"precision_resolved": null, "precision_linked": null, "precision_all": 0.7059, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 2, "truth_imports": 13, "precision": 1.0, "recall": 0.1538},
  "M4": {"recall_any": 1.0, "recall_linked": 0.0, "recall_resolved": 0.0, "top5": [["getName", "core/src/main/java/com/acme/core/model/Item.java"], ["add", "core/src/main/java/com/acme/core/repo/AuditedRepo.java"], ["validate", "core/src/main/java/com/acme/core/repo/Repo.java"], ["add", "core/src/main/java/com/acme/core/repo/Repo.java"], ["Notifier", "core/src/main/java/com/acme/core/notify/Notifier.java"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 24, "sites": 30},
  "M6": {"full_scan_s": 1.43, "largest_name_refs_module": "app/src/main/java/com/acme/app/OrderService.java", "largest_name_refs_bytes": 2240, "total_name_refs_bytes": 7690},
  "failures": ["M1 resolved multi-target share < 2 %", "M2 linked precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M5 every named site"]
 },
 "kotlin": {
  "M1": {"calls_by_confidence": {"None": 19}, "multi_target_by_confidence": {}, "resolved_multi_share": null},
  "M2": {"precision_resolved": null, "precision_linked": null, "precision_all": 0.8947, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 0, "truth_imports": 13, "precision": null, "recall": 0.0},
  "M4": {"recall_any": 0.85, "recall_linked": 0.0, "recall_resolved": 0.0, "top5": [["slugify", "src/main/kotlin/util/Text.kt"], ["truncate", "src/main/kotlin/util/Text.kt"], ["create", "src/main/kotlin/data/NoteRepository.kt"], ["add", "src/main/kotlin/tags/Tags.kt"], ["line", "src/main/kotlin/export/MarkdownExporter.kt"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 29, "sites": 33, "symbols_found": 5, "symbols": 13},
  "M6": {"full_scan_s": 0.81, "largest_name_refs_module": "src/main/kotlin/Main.kt", "largest_name_refs_bytes": 1531, "total_name_refs_bytes": 6356},
  "failures": ["M1 resolved multi-target share < 2 %", "M2 linked precision >= 98 %", "M3 IMPORTS precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M4 CALLS recall >= 95 %", "M5 every named site", "M5 every expected symbol"]
 },
 "csharp": {
  "M1": {"calls_by_confidence": {"None": 25}, "multi_target_by_confidence": {"None": 6}, "resolved_multi_share": null},
  "M2": {"precision_resolved": null, "precision_linked": null, "precision_all": 0.92, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 0, "truth_imports": 17, "precision": null, "recall": 0.0},
  "M4": {"recall_any": 1.0, "recall_linked": 0.0, "recall_resolved": 0.0, "top5": [["Title", "src/Shop.Core/Utils/Formatting.cs"], ["Display", "src/Shop.Core/Legacy/User.cs"], ["Display", "src/Shop.Core/Models/User.cs"], ["Count", "src/Shop.Core/Services/UserService.cs"], ["Register", "src/Shop.Core/Services/UserService.cs"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 29, "sites": 31},
  "M6": {"full_scan_s": 1.97, "largest_name_refs_module": "src/Shop.App/Program.cs", "largest_name_refs_bytes": 1164, "total_name_refs_bytes": 4504},
  "failures": ["M1 resolved multi-target share < 2 %", "M2 linked precision >= 98 %", "M3 IMPORTS precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M5 every named site"]
 },
 "rust": {
  "M1": {"calls_by_confidence": {"None": 29}, "multi_target_by_confidence": {"None": 18}, "resolved_multi_share": null},
  "M2": {"precision_resolved": null, "precision_linked": null, "precision_all": 0.5862, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 8, "truth_imports": 19, "precision": 1.0, "recall": 0.4211},
  "M4": {"recall_any": 0.85, "recall_linked": 0.0, "recall_resolved": 0.0, "top5": [["normalize", "crates/core/src/util/text.rs"], ["new", "crates/core/src/models/order.rs"], ["new", "crates/core/src/net/client.rs"], ["new", "crates/core/src/models/user.rs"], ["crates/core/src/net/client.rs", "crates/core/src/net/client.rs"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 23, "sites": 34},
  "M6": {"full_scan_s": 1.01, "largest_name_refs_module": "crates/core/src/models/user.rs", "largest_name_refs_bytes": 614, "total_name_refs_bytes": 4223},
  "failures": ["M1 resolved multi-target share < 2 %", "M2 linked precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M4 CALLS recall >= 95 %", "M5 every named site"]
 },
 "cpp": {
  "M1": {"calls_by_confidence": {"None": 25}, "multi_target_by_confidence": {"None": 10}, "resolved_multi_share": null},
  "M2": {"precision_resolved": null, "precision_linked": null, "precision_all": 0.76, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 5, "truth_imports": 17, "precision": 1.0, "recall": 0.2941},
  "M4": {"recall_any": 1.0, "recall_linked": 0.0, "recall_resolved": 0.0, "top5": [["src/report.h", "src/report.h"], ["log_info", "src/net/log.h"], ["log_info", "include/util/log.h"], ["find", "src/inventory.cpp"], ["src/net/conn.h", "src/net/conn.h"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 23, "sites": 29},
  "M6": {"full_scan_s": 0.84, "largest_name_refs_module": "src/main.cpp", "largest_name_refs_bytes": 717, "total_name_refs_bytes": 3464},
  "failures": ["M1 resolved multi-target share < 2 %", "M2 linked precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M5 every named site"]
 }
}
```

### Results after slice A

| | Target | BEFORE | AFTER | |
|---|---|---|---|---|
| ts M1 resolved multi-target share | < 2 % | no resolved edge (32 bare, 8 multi-target) | 0 % (20 resolved, 2 package, 1 name) | met |
| ts M2 linked precision | ≥ 98 %, 0 cross-directory without import | none linked; 69 % of all edges right | 100 % (resolved, linked and all); 0 | met |
| ts M3 `IMPORTS` precision / recall | ≥ 98 % / 95 % | 100 % / 58 % | 100 % / 100 % (24/24) | met |
| ts M4 `CALLS` recall | ≥ 95 % | 96 % (all bare) | 100 % (linked 96 %, resolved 87 %) | met |
| ts M5 named sites | all | 32/41 | 41/41 | met |
| Kotlin symbols extracted | all | 5/13 | 13/13 (named sites 29/33 to 31/33; calls resolve in slice B) | met |
| Python on DevGraph | unchanged but self-loops | name 4,087, self-loops 62 | name 4,072, self-loops 47; every other figure equal | met |
| M6 DevGraph `full_scan` | ≤ +15 % | 54.4 s | 58.2 s (+7 %) | met |
| M6 ts fixture `full_scan` | ≤ +15 % | 1.12-1.18 s | 1.18-1.30 s (+5-10 %) | met |
| M6 largest `name_refs` | < 1 MiB | 231,682 B (DevGraph) | 229,842 B (DevGraph); 4,273 B (ts) | met |
| M6 relink benchmark | green | green | green, by name and with path pins (about 56k and 59k db hits) | met |

- **Timing.** The `full_scan` figures are medians of alternating runs of the
  BEFORE and AFTER code over the same tree, in one process each (DevGraph:
  three each; ts: two series of seven, the first run of a series left out),
  on a machine other test runs were sharing, so single runs vary by up to a
  third. A first AFTER build spent its extra time packing parameters (every
  target carried `pin`, `exclude` maps and `no_self`, +23 %); a target now
  carries a key only where its group's Cypher reads it.
- **Python.** `no_self` removed 15 bare `name` self-loops (`x.save()` inside
  `save`); M1-M5 are otherwise identical (resolved 8,555, package 40, `IMPORTS`
  recall and precision 100 %, 21/21 sites, `find_callers` recall unchanged).
- **The ts no-edge sites** (`getItem` calling `localStorage.getItem`, lodash's
  `_.capitalize`, `JSON.parse`, React hooks) are now absent through the
  stoplists, external bindings and `no_self`; the classic scripts'
  `initLegacy -> trackEvent` stays a `name` edge, by design.

AFTER (ts in full; Kotlin's parse coverage, its other measures waiting for
slice B; `full_scan_s` is one cold run of the script, the table has the
paired timing):

```json
{
 "ts": {
  "M1": {"calls_by_confidence": {"name": 1, "package": 2, "resolved": 20}, "multi_target_by_confidence": {}, "resolved_multi_share": 0.0},
  "M2": {"precision_resolved": 1.0, "precision_linked": 1.0, "precision_all": 1.0, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 24, "truth_imports": 24, "precision": 1.0, "recall": 1.0},
  "M4": {"recall_any": 1.0, "recall_linked": 0.9565, "recall_resolved": 0.8696, "top5": [["check", "src/api/http.ts"], ["src/types.ts", "src/types.ts"], ["src/api/http.ts", "src/api/http.ts"], ["src/lib/storage.ts", "src/lib/storage.ts"], ["get", "src/api/http.ts"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 41, "sites": 41},
  "M6": {"full_scan_s": 3.93, "largest_name_refs_module": "src/pages/Settings.tsx", "largest_name_refs_bytes": 4273, "total_name_refs_bytes": 20721},
  "failures": []
 },
 "kotlin": {
  "M5": {"sites_ok": 31, "sites": 33, "symbols_found": 13, "symbols": 13, "symbols_missing": []}
 }
}
```

