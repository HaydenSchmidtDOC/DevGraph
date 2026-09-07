---
name: adding-a-language
description: Use when adding a new source-language extractor to DevGraph (e.g. after Python, JS/TS, C#, C++, Java, Rust, Go — the next one: Ruby, PHP, Kotlin, Swift, ...). Walks through the worktree + parallel-subagent process used to add multiple languages at once in Implementation Plan #8, and the single-language version of the same loop for adding just one more later.
---

> **Status: finalized against Implementation Plan #8's six-language batch
> (JS/TS, C#, C++, Java, Rust, Go) — all six shipped and merged to master.
> The "Lessons" section at the bottom reflects what actually happened, not
> predictions.**

# Adding a language to DevGraph

DevGraph's source extractors are all Tree-sitter-based, share one dataclass
module (`devgraph/indexer/common.py`), and plug into `devgraph/indexer/dispatch.py`
purely by file extension. Every extractor — regardless of language — produces
the same shape: `Module`/`Class`/`Function` nodes and `CONTAINS`/`CALLS`/
`IMPORTS`/`EXTENDS` relationships, upserted via the same `GraphEngine`. Adding
a language means writing one new extractor module that fits this shape, not
touching the graph schema, the engine, or any MCP tool.

This skill covers two paths:

- **Single language**, added on its own (the common case going forward).
- **Multiple languages at once**, worktree + parallel-subagent driven (how
  Implementation Plan #8 added six in one pass — use this shape again if a
  future batch does the same).

Read `Blueprints/Implementation Plan #8.md` for the concrete worked example
this skill is distilled from — it has the full per-language decision table
(grammar packages, import-resolution approach, known limitations) for the six
languages already shipped; don't re-derive that reasoning from scratch for a
language already covered there.

## Prerequisites, always

1. **Shared dataclasses must exist.** `GraphNode`/`GraphRelationship`/
   `ExtractionResult` live in `devgraph/indexer/common.py`. Every extractor
   imports them from there — never redefine them per-language. This landed
   as Implementation Plan #8's Open Item 1; if it's ever missing, do it
   first, before any language work, since every subagent's prompt in the
   batch case below assumes it already exists on the branch they fork from.
2. **Pick the grammar package.** Tree-sitter has an official or
   community-maintained Python binding for nearly every mainstream language
   (`tree-sitter-<lang>` on PyPI). Confirm one exists and is reasonably
   maintained before committing to the language.
3. **Decide the import-resolution ceiling up front.** Every language's
   import/include/use system maps to DevGraph's `IMPORTS` edges differently,
   and some (C++'s `#include`, without a real build system) cannot resolve
   reliably at all. Decide explicitly whether this language gets full
   import-graph parity or "structural parity only" (nodes/CALLS/EXTENDS
   solid, IMPORTS best-effort/documented-weak) — same distinction
   Implementation Plan #8 made for C++ vs. the other five. Write the
   decision down before implementation starts; don't let it become an
   accidental discovery mid-build.

## The generalized extractor loop (per language, single or batched)

1. Add the grammar dependency to `pyproject.toml`, pinned the same way as
   `tree-sitter-python` (`>=X.Y,<X.Y+1`), then `pip install` it into the
   working venv.
2. Create `devgraph/indexer/<lang>/extractor.py` (+ `__init__.py`).
   Import `GraphNode`/`GraphRelationship`/`ExtractionResult` from
   `devgraph.indexer.common`. Implement `_make_parser()`,
   `extract_<lang>_file(source_code, file_path, repo_id) -> ExtractionResult`,
   and an `index_file()` wrapper matching the Python extractor's signature.
3. Node extraction: Module (the file itself), the language's closest
   equivalent to Class (may not be a literal `class` keyword — Rust's
   struct/enum/trait and Go's struct are judgment calls; document the
   mapping explicitly in a module docstring when it's not 1:1), Function
   (free functions and/or methods, whatever the language has), `CONTAINS`
   edges, and the language's doc-comment convention extracted into
   `description`/`docstring_full` properties.
4. `CALLS` extraction: name-based, not type-resolved — same philosophy as
   Python's `_callee_simple_name`/`_extract_call_targets`. `foo()` → `'foo'`,
   `obj.foo()`/`obj->foo()`/`this.foo()` → `'foo'` (the member/attribute
   name only). Don't descend into nested function/class scopes while
   walking a body for calls — attribute those to the nested scope instead.
   This deliberately over-links same-named methods across types rather than
   under-linking; that's a documented, intentional trade-off, not a gap to
   close later.
5. `IMPORTS` resolution, per the ceiling decided in Prerequisites #3: emit a
   same-repo file-path *guess* for the language's dominant intra-repo
   import style. Guessing wrong is safe and expected — `upsert_relationship`
   only MATCH-links real existing nodes, so an unresolved guess simply never
   materializes an edge, it doesn't corrupt the graph. Don't build resolution
   for cross-package/workspace/multi-module cases in v1 unless the language
   makes that the dominant real-world case.
6. Wire into `devgraph/indexer/dispatch.py`: extend the extension-routing
   branch to call the new extractor for this language's file extensions,
   reusing the same `replace_file_nodes`/two-pass upsert pattern the `.py`
   branch already uses. Add any dependency/build-noise directories (like
   `node_modules`, `vendor`, `target`, `bin`/`obj`) to `IGNORED_DIR_NAMES`.
   Do not change the existing Python branch's behavior.
7. Golden-repo test: shallow-clone (`git clone --depth 1`) one small-to-medium
   real open-source repo in the language, run the extractor over a
   representative sample (10-20 files covering the class-equivalent,
   functions, imports, and some cross-file calls), and hand-verify a
   sample of the resulting edges against the actual source. This is the
   real parity check — "doesn't crash" is not sufficient.
8. Unit tests: `tests/indexer/test_<lang>_extractor.py`, mirroring
   `tests/indexer/test_python_extractor.py`'s structure. Pure-function
   tests against `extract_<lang>_file()` directly — no live Neo4j needed.
9. Update `PROJECT_STATUS.md` and the relevant Blueprint doc.
10. Ship as its own commit/PR — don't bundle multiple languages into one
    change unless doing the batched/parallel path below, and even then,
    merge them into master one at a time (see below), not as one giant
    merge commit.

### Definition of done

- Module/Class-equivalent/Function/CONTAINS extraction present and
  structurally sound.
- CALLS edges present and name-resolved.
- IMPORTS edges resolve for the dominant intra-repo import style, scoped
  per the Prerequisites #3 decision (full or structural-only).
- Golden-repo test passing with a hand-verified sample.
- Full existing test suite (`pytest`) still green after merge to master —
  no regression to any other language's extraction.

## Single-language path

Do the loop above directly, in the main checkout or a single throwaway
branch. No worktree ceremony needed — that machinery exists to let multiple
languages be built *concurrently without colliding*, which doesn't apply
when there's only one in flight.

## Batched/parallel path (multiple languages at once)

Use this when adding several languages in one push, as Implementation Plan
#8 did. The goal is to let each language's implementation happen
concurrently (they don't depend on each other) while keeping one architect
session in control of integration, without that session's own context
filling up with six languages' worth of code.

### 1. Land the prerequisite on master first

Do the `common.py` shared-dataclasses refactor (Prerequisites #1) as a
normal direct edit — small and foundational enough that it's not worth a
subagent round-trip — and get it merged/committed to master *before*
creating any worktrees. Every language's worktree forks from this commit,
so getting it in first means no language's subagent has to deal with an
extra merge later just to pick it up.

### 2. Create one git worktree per language, all forked from that commit

```
git worktree add "../<repo>-worktrees/<lang>" -b "lang/<lang>" master
```

Do this from the actual language repo, not any wrapping/parent repo. If the
target repo is nested inside another git repo (a monorepo-of-repos setup,
common in a life-hub-style tree), **do not** rely on an agent-launching
tool's own built-in worktree isolation flag for this — it will worktree the
*outer* repo, not the nested target, and every write inside the nested repo
will be refused. Create the worktrees yourself with plain `git worktree add`
against the target repo directly, then point each subagent at the resulting
absolute path in its prompt instead.

### 3. Dispatch one subagent per language, in parallel, each pinned to its own worktree path

Each subagent's prompt needs, explicitly:

- The absolute worktree path, with a hard instruction not to touch any
  other path — especially not the main checkout or sibling worktrees,
  since other subagents are running concurrently against them right now.
- A pointer to read the Implementation Plan doc first, and which row/language
  in it they own.
- Its own venv setup (`python -m venv .venv`, editable install, plus that
  language's grammar package) — worktrees don't inherit the main checkout's
  `.venv` (it's untracked), so each needs its own.
- An instruction to use DevGraph's own MCP tools
  (`mcp__devgraph__get_source`/`search_component`, `repo_id` for this repo)
  to pull *only the specific reference functions it needs* from an existing
  extractor, instead of reading that whole file — this is the main token
  saver for the batch. Fall back to a plain `Read` (once, not repeatedly)
  if those tools aren't available in its session.
- The full generalized loop above, steps 1-9 (not 10 — no merging; the
  subagent commits to its own branch and stops there).
- An explicit instruction to run *only its own new test file*, not the full
  suite — worktrees don't have their own Neo4j, and other test files that
  need a live DB will fail for reasons that have nothing to do with the new
  extractor. Full-suite regression happens once, centrally, during
  integration (step 5 below).
- A request for a **concise structured report back**, not a diff walkthrough:
  what was built, unit test pass/fail count, golden-repo spot-check findings
  (repo used, files sampled, any bad edges and how handled), deviations from
  the brief and why, and known limitations.

Launch all of them in the same batch, in the background. The architect
session does not read their code as they go — only their final reports.

### 4. Stay out of the implementation while subagents run

The point of this path is that the orchestrating session's context stays
small: it holds the plan, not six languages' worth of extractor code. Resist
the urge to `Read` into a worktree to "check progress" — that's exactly the
token cost this path exists to avoid. Wait for each subagent's completion
report.

### 5. Integrate sequentially, in the plan's priority order, not in whatever order they finish

Even though the subagents ran concurrently, merge their branches into
master **one at a time**, in the priority order the plan specified, not
completion order:

```
git merge lang/<next-in-priority>
```

Every language's subagent touched the same few shared files
(`pyproject.toml`'s dependency list, `dispatch.py`'s routing/
`IGNORED_DIR_NAMES`) independently — that's an expected, not accidental,
merge conflict at integration time, not a subagent mistake. Resolve each
one by hand (both languages' entries usually just need to coexist side by
side), then run the full test suite before merging the next language, so a
break is always attributable to exactly one merge.

After each merge: run the full existing suite. A failure that reproduces
identically on the pre-merge master (check with `git stash`/`git stash pop`
around a targeted rerun) is pre-existing and not this integration's
problem — don't chase it here. A failure that's new is this merge's
responsibility to fix before moving to the next language.

### 6. Clean up worktrees once merged

```
git worktree remove "../<repo>-worktrees/<lang>"
git branch -d "lang/<lang>"
```

### 7. Update docs and finalize this skill

`PROJECT_STATUS.md`, the Blueprint doc, and — if this skill file still has
its draft banner at the top — replace it with real findings from the batch
that just shipped (see below).

## Lessons from Implementation Plan #8

Ran exactly as this skill describes: `common.py` refactor landed directly
first (commit `45bbe10`), six worktrees created off it, six subagents
dispatched in parallel, all six finished and reported back, then merged
into master one at a time in priority order (JS/TS, C#, C++, Java, Rust,
Go). Full regression went from 351 to 479 passing tests (each language's
unit-test count: JS/TS 27, C# 19, C++ 21, Java 22, Rust 21, Go 18 — sums
exactly). One pre-existing failure (`TestListServices::
test_list_services_cross_repo`, a Neo4j test-fixture issue) was present
before this batch and after — confirmed with `git stash`/rerun, not
this work's problem.

**What actually went wrong, and what to expect next time:**

- **The cross-repo worktree trap is real and will bite immediately.**
  DevGraph lives nested inside a larger life-hub repo. The very first
  attempt used the Agent tool's own `isolation: "worktree"` flag, which
  worktreed the *outer* repo instead of DevGraph itself, and every write
  the subagent tried was refused. This is now documented as a hard rule in
  the "Batched/parallel path" section above (step 2) — create worktrees
  yourself with plain `git worktree add` against the actual target repo,
  never the agent-launcher's own isolation flag, whenever the repo could be
  nested inside another one. Lost a full subagent round-trip discovering
  this; don't repeat it.

- **Every single language needed a grammar-version pin below "latest."**
  Not a one-off — all five of C#, C++, Java, Rust, and Go's `tree-sitter-*`
  packages had a "latest" release using a newer compiled-grammar ABI (15)
  than this repo's `tree-sitter` core package supports (`>=0.23,<0.24`,
  ABI 13-14). Each subagent had to discover this itself via a `ValueError:
  Incompatible Language version` at runtime and step down a minor version.
  **For the next language: check the grammar package's ABI compatibility
  against the pinned `tree-sitter` core version *before* writing extraction
  code**, not after — this is now a known, expected step, not a surprise.
  If a future language's grammar has no ABI-13/14-compatible release at
  all, that's a real blocker worth raising before starting, since it would
  mean either bumping the shared `tree-sitter` core pin (affecting every
  existing extractor) or the language can't be added yet.

- **Every merge conflicted in exactly the same two files, in a
  fully mechanical way.** `pyproject.toml` (new dependency line) and
  `dispatch.py` (extension-routing branch, `IGNORED_DIR_NAMES` entries, the
  parallel `_files`/`_extractions` bookkeeping, and its two-pass re-upsert
  loop) conflicted on every single merge after the first. Every conflict
  was "both sides' additions coexist side by side" — never a real logical
  clash. Two extractors (C++, Go) also independently added their own
  `.md`-cleanup-shaped branch to `remove_paths` with bodies identical to
  an already-merged language's branch (e.g. Rust's and Go's `remove_paths`
  bodies were byte-identical to Python's) — worth folding those into one
  shared conditional at integration time rather than keeping N near-duplicate
  `elif` branches, which is what happened here.

- **Subagents caught real bugs themselves, via the golden-repo step.** Three
  of six found and fixed a genuine extraction bug during their own
  golden-repo spot-check, not after: C#'s generic-method-call name
  stripping, JS/TS's `require('../..')` path resolution, and C++'s
  DLL-export-macro-prefixed class recovery (`class YAML_CPP_API Name :
  public Base` completely defeats a non-preprocessing grammar without
  special-casing). This validates that step 7 (golden-repo test) isn't
  ceremony — it's where real bugs actually surfaced, more than the unit
  tests did. Keep it mandatory, don't compress it out under time pressure.

- **Token cost stayed flat per language, as intended.** The orchestrating
  session never read any extractor's source code — only each subagent's
  final structured report and the plan/table it started from. Integration
  (merge conflict resolution) was the only point the orchestrator touched
  actual extractor code, and even there only the two shared files, never
  the language-specific extractor bodies themselves.
