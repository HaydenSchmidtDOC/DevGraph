# DevGraph — Implementation Plan #8: Multi-language source extraction (JS/TS, C#, C++, Java, Rust, Go)

**Status: shipped.** All six languages implemented, tested, golden-repo
verified, and merged to master (commits `92b1c39`..`726d026`). Full
regression suite: 479/480 passing (the 1 failure, `TestListServices::
test_list_services_cross_repo`, is a pre-existing Neo4j test-fixture issue
confirmed unrelated to this plan — reproduces identically on the pre-batch
commit). See `.claude/skills/adding-a-language/SKILL.md` for the
worktree+subagent process used, with real findings from this run.

Build-ready plan to extend DevGraph's source-code indexing beyond Python to
six more languages, one at a time, each shipped to parity with the existing
Python extractor before the next language starts.

## Context

DevGraph's only source-code extractor today is
`devgraph/indexer/python/extractor.py`, Tree-sitter-based by deliberate
choice — its own module docstring states Tree-sitter was picked over
stdlib `ast` specifically because it "generalizes to non-Python languages
later." `devgraph/indexer/dispatch.py` already routes files to extractors
purely by extension (`if resolved.suffix == ".py"`), and the graph schema
(`devgraph/graph/schema.py`) uses generic node labels (`Module`, `Class`,
`Function`, etc.) with no Python-specific shape. The architecture has been
waiting for this.

DevGraph has real external users pulling the project from GitHub (Hayden is
the sole contributor, but not the sole user), which changes the bar from
"good enough for my own repos" to "correct enough that a stranger's repo
produces a trustworthy graph." Decisions below reflect that.

Decisions already made (do not re-litigate without a reason):

- **Dynamic per-file-extension detection, not per-repo language lock-in.**
  Real repos are polyglot (this repo itself mixes Python, JS in
  `static/index.html`, and Dockerfiles) — one `Repository` node with
  mixed-language `Module` nodes matches how the graph already models
  containers/compose/docs alongside Python.
- **Priority order is fixed: JS/TS → C# → C++ → Java → Rust → Go.** Each
  language ships as its own complete PR (full node + CALLS + IMPORTS +
  EXTENDS extraction, not structure-only) before the next language starts.
  Real GitHub users see one solid new language roughly every 1-2 days
  instead of six half-finished ones landing at once.
- **Parity bar, not innovation bar.** Success for each language is defined
  as "extraction quality matches the Python extractor on a real, popular
  open-source repo in that language," verified by a golden-repo test with
  hand-checked sample edges — not new capabilities beyond what Python has.
- **Heuristic import resolution is acceptable and expected**, following the
  same pattern Python already uses (a guessed same-repo file-path target
  that simply never materializes an edge if wrong, since
  `upsert_relationship` only MATCH-links real existing nodes). C++ is the
  one language where this heuristic is weak enough (no build-system truth
  for `#include` resolution) that it should be documented as
  structural-parity-only, not import-graph parity — needs explicit sign-off
  before that language starts (see Open Items).

## The generalized loop (applies to every language, in order)

1. **Add the grammar dependency** to `pyproject.toml`, pinned the same way
   as `tree-sitter-python` (`>=X.Y,<X.Y+1`).
2. **Create `devgraph/indexer/<lang>/extractor.py`**, mirroring
   `python/extractor.py`'s shape: `GraphNode`/`GraphRelationship`/
   `ExtractionResult` (imported from a shared module — see Open Item 1),
   `extract_<lang>_file(source, path, repo_id) -> ExtractionResult`,
   `index_file()` wrapper.
3. **Node extraction**: Module (file), Class (+ base classes → EXTENDS),
   Function (+ CONTAINS), doc-comments where the language has an
   equivalent convention.
4. **CALLS extraction**: name-based callee resolution, same philosophy as
   Python's `_callee_simple_name` (over-link same-named calls rather than
   require full type resolution).
5. **IMPORTS resolution** — the hard, language-specific part (see table).
6. **Wire into dispatch**: extend `dispatch.py`'s extension routing; add
   any new noise directories to `IGNORED_DIR_NAMES`.
7. **Golden-repo test fixture**: one real, mid-size open-source repo in
   that language, indexed and hand-verified on a sample of CALLS/IMPORTS/
   EXTENDS edges — the actual parity check, not just "doesn't crash."
8. **Unit tests** mirroring `tests/indexer/test_python_extractor.py`'s
   structure, one test file per extractor.
9. **Update `PROJECT_STATUS.md`** and this Blueprint's status.
10. **Ship as its own PR/release** before starting the next language.

## Definition of Done (per language)

- Module/Class/Function/CONTAINS extraction matches Python's node shape and
  properties (`start_line`/`end_line`, `decorators` where applicable,
  docstring `description`/`docstring_full`).
- CALLS edges present and name-resolved.
- IMPORTS edges resolve for the language's dominant intra-repo import
  style (not every edge case — see per-language notes).
- Golden-repo test passes with hand-verified sample edges.
- No regression to existing Python indexing (`dispatch.py`/`GraphEngine`
  shared-path changes covered by existing 351-test suite).

## Per-language specifics

| # | Lang | Grammar pkg(s) | Extensions | New ignored dirs | Import resolution approach | Known limitation to document |
|---|---|---|---|---|---|---|
| 1 | JS/TS | `tree-sitter-javascript`, `tree-sitter-typescript` | `.js .jsx .ts .tsx` | `node_modules` | Relative (`./`, `../`) paths first; bare specifiers resolve against `package.json`/`tsconfig.json` `paths` if present, else best-effort `node_modules` guess (same non-materializing-guess pattern as Python's dotted-import guess) | No monorepo/workspace (`pnpm`/`yarn` workspaces) resolution v1 |
| 2 | C# | `tree-sitter-c-sharp` | `.cs` | `bin`, `obj` | `using` + namespace-to-folder convention guess (weaker than Java's — C# namespaces don't have to mirror folders) | Cross-project (`.csproj` reference) resolution out of scope v1 |
| 3 | C++ | `tree-sitter-cpp` | `.cpp .cc .cxx .h .hpp` | `build`, `cmake-build-*` | `#include "local.h"` → same-dir/relative guess only; `#include <system.h>` never resolved (no build-system truth available) | Structural parity only, not import-graph parity — flagged explicitly to users |
| 4 | Java | `tree-sitter-java` | `.java` | none new | `package`/folder-mirrors-package convention (cleanest of the six) | — |
| 5 | Rust | `tree-sitter-rust` | `.rs` | `target` | `mod`/`use` crate-relative, single-crate only v1 | Cargo workspaces (multi-crate) out of scope v1 |
| 6 | Go | `tree-sitter-go` | `.go` | `vendor` | import path = package dir convention | External (outside-repo) Go module deps never resolve, same as Python's third-party guess behavior |

## Estimates

JS/TS ~2 days · C# ~1.5 days · C++ ~2 days · Java ~1 day · Rust ~1 day ·
Go ~1 day → **~8.5 dev days total**, shipped incrementally.

## Testing strategy

Each language gets: unit tests (extractor logic, mirroring the existing
Python test file structure) + one golden-repo integration test (real repo,
hand-checked sample of edges) + a full regression run of the existing
351-test suite before merge, since `dispatch.py` is shared code.

## Execution strategy: using DevGraph's own MCP + subagents to keep this cheap

This plan spans ~8.5 dev-days of work across six languages. Run naively in
one long conversation, the context window fills with full-file reads,
repeated re-explanations of the extractor pattern, and six languages' worth
of code — expensive and error-prone (later languages get a worse-informed
model than earlier ones). Two things keep the token cost down:

**1. Dogfood DevGraph's own MCP tools against its own repo, instead of
`Read`ing whole files repeatedly.**

- Before starting each language, use `search_component` /
  `get_source` on the *previous* language's extractor to pull only the
  functions actually needed as a reference (e.g. `_extract_imports`,
  `_callee_simple_name`), not the whole ~700-line file via `Read`.
- Before editing `dispatch.py` or the shared dataclasses (Open Item 1), run
  `find_callers` / `impact_analysis` on the symbol being changed to see
  every call site in one query instead of grepping the repo file-by-file.
- After each language's `dispatch.py` edit, use `impact_analysis_for_diff`
  against the diff to confirm nothing outside the intended extension
  routing was touched, as a cheap pre-check before running the full
  pytest suite.
- This only works well once Open Item 1 (shared dataclasses module) lands
  and the repo is re-scanned — do that refactor first partly *because* it
  makes DevGraph's own graph a better reference for the extractors that
  follow.

**2. One subagent per language, not one long-running session.**

- The orchestrating session holds only this plan file and the per-language
  table — not any language's implementation detail.
- For each language, dispatch a fresh subagent (general-purpose, or a
  worktree-isolated one per `superpowers:using-git-worktrees`) briefed
  with: this plan file's path, that language's row from the table, and an
  instruction to use DevGraph's own MCP tools (per #1) rather than reading
  files wholesale for pattern reference. The subagent runs the full
  generalized loop (steps 1-10) for its one language and reports back a
  summary, not full diffs, to the orchestrating session.
- Starting a fresh subagent per language is a deliberate context reset:
  language N's subagent never carries languages 1..N-1's full
  implementation detail in its context, only the finished pattern
  (accessible via MCP if it needs a reference), so token cost per language
  stays roughly flat instead of growing across the six-language run.
- After each subagent finishes, apply `superpowers:finishing-a-development-branch`
  to decide the merge path before starting the next language's subagent —
  keeps the orchestrating session's own context small too (a merge
  decision, not a code review's worth of diff, is what re-enters it).

## Open Items

1. **Shared dataclasses or per-language duplicates?** — Resolved: extracted
   to `devgraph/indexer/common.py` before any language worktree was
   created (commit `45bbe10`). All six extractors import from it.
2. **CALLS/IMPORTS heuristic disclosure to users** — Resolved as a
   follow-up: `README.md` now states the heuristic nature of extraction up
   front, and `DEVGRAPH-CLIENT.md`'s existing "name-based, not type-resolved"
   section was extended to cover every language (not just Python) plus a
   dedicated import-resolution paragraph explaining the per-language guess
   pattern and calling out C++'s thin-by-design `IMPORTS` graph explicitly.
3. **C++ scope commitment** — Resolved: shipped as structural-parity-only,
   confirmed in the C++ extractor's own golden-repo check (thin IMPORTS
   graph on `yaml-cpp`, as expected) and documented in its module
   docstring.
