# compare_branches Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** `compare_branches(repo_id, branch_a, branch_b)` stops being a stub. It compares `branch_b` with its merge base with `branch_a`, as `git diff branch_a...branch_b` does. It lists the files added, removed, renamed and modified, and for each code file in the eight language families the functions and classes added, removed and changed. It also names the graph's callers of what changed. Everything is read from git objects in memory, bounded, and never written to the graph. The MCP signature is unchanged.

**Spec:** `docs/superpowers/specs/2026-10-08-compare-branches-design.md`. Every task implements the decisions it names (C1–C9).

**Working directory:** this worktree, branch `epic1/compare-branches` (from `epic1/graph-accuracy`). Run `uv sync --extra dev` once, then run `uv run` from inside the worktree, because the editable install otherwise imports another checkout (check `devgraph.__file__`). None of these tests need Neo4j; the graph is a stub engine. Never touch `~/.devgraph`.

## Global Constraints

- **Read-only (C1).**
  - Nothing writes to the graph, the working tree, the staging area or `.git`.
  - Both sides are read from git objects (`commit.tree`, `Blob.size`, `Blob.data_stream`), with CRLF normalised to LF before extraction (C3). The checked-out files are never read, whatever branch is checked out.
- **Git process surface (C4).**
  - The only `git` subcommands a comparison may run are `cat-file` (GitPython's object database) and `merge-base`.
  - `merge-base` gets the two resolved `hexsha`s, never caller strings. It gets `kill_after_timeout=max(remaining, 1)` only when `sys.platform != "win32"`.
  - No `git diff`, `log` or `rev-parse`, and no `subprocess` call of our own.
- **Ref validation before resolution (C4).** The spec's rules apply, and they run before `git.Repo` is even opened. An invalid ref never reaches GitPython.
- **No `search_parent_directories`.** `git.Repo(record.path)` opens the registered root exactly. The `Repo` is closed in a `finally`.
- **Caps are module constants (C5).** They live in `devgraph/indexer/git_history/compare.py` with the spec's names and values. `_COMPARE_MAX_FILE_BYTES` is `MAX_CONFIG_BYTES` imported from `devgraph.paths`, not a new number. The deadline uses an injectable `clock`, and no test sleeps.
- **Errors are plain text (C7).**
  - `compare.py` raises `CompareError(ValueError)` with the spec's exact messages.
  - `tools.compare_branches` turns it, and an unknown `repo_id`, into `ToolError`.
  - No other exception escapes for a caller-fixable case. `repo.commit()` alone is wrapped in `except Exception`. The whole comparison is wrapped in a final `except (GitCommandError, OSError)` that becomes the generic git-failure message.
  - Caller values are echoed through `tools._echo` in `tools.py`; `compare.py` has its own 100-character echo with the same behaviour.
- **One routing table (C6).** `devgraph/indexer/symbols.py` maps `dispatch._CODE_ROUTES` values to extractors. It adds no suffix list of its own.
- **Signatures.**
  - The MCP tool stays `compare_branches(repo_id: str, branch_a: str, branch_b: str)`.
  - `tools.compare_branches(engine, registry, repo_id, branch_a, branch_b)` gains `registry`, as `impact_analysis_for_diff` has.
  - No new config knob, and no new dependency.
- **Sanitised output.** Every string from git or source (paths, symbol names, refs) passes `_sanitize_value`, through `_envelope` or directly, before it leaves `tools.py`.
- **Test repos are real.** Every comparison test builds a temporary git repository with `main` and a second branch, using `git` via `subprocess` in the test, as `tests/mcp/test_tools_impact_diff.py` does. Each test repository has `-c user.email=dev@example.com -c user.name="Dev Example" -c commit.gpgsign=false`, and `init -b main`. No real names or paths.
- **Observable reads.** Tests that assert a blob was or was not read patch `git.db.GitCmdObjectDB.stream` and `GitCmdObjectDB.info` at class level. GitPython's `Blob.data_stream` and `Blob.size` go through those, while a patch on one `Blob` instance or on `repo.odb` taken from a different `Repo` would miss them. `RefComparison` also exposes `repo`, so a test can assert against the very object database the comparison used.
- **Windows.** The symlink and chmod tests are `@pytest.mark.skipif(sys.platform == "win32", ...)`. The deadline test for `merge-base`'s `kill_after_timeout` is POSIX-only too.
- **TDD.** Each task starts with failing tests, then the implementation, then `uv run pytest -q` (the full suite).
- **Commits.** Plain imperative messages, with no `Co-Authored-By` trailer and no AI attribution. Never stage `uv.lock`, real names or personal paths.

## Review Focus

Each item names the test that proves it.

1. **Three dots, not two.**
   - A commit on `main` after the branch point must not appear: not as a removed file, not as a reverted symbol.
   - A branch that is already merged must give an empty result, not an error.

   Tests: Task 1 `test_changes_on_base_after_branch_point_are_not_reported` and `test_merged_head_is_empty`.
2. **Process surface.** Every `Git.execute` during a full comparison, including the symbol pass, is recorded. The set of subcommands must be a subset of `{"cat-file", "merge-base"}`, and `merge-base`'s argv must be exactly two 40-hex SHAs after the subcommand. Tests: Task 1 `test_only_cat_file_and_merge_base_run`; Task 2 reruns it with symbols.
3. **Hostile refs never reach git, odd refs never crash.** `-h`, `--output=x`, `a..b`, `HEAD:secret.txt`, `../../x`, `""`, a 300-character ref, `ma in`, `a\x07b`, `x*` and every `@{...}` form (`HEAD@{1}`, `@{-9}`, `@{upstream}`, `main@{yesterday}`, ...) each give a `CompareError` naming the rule. A patched `git.Repo` that fails the test if it is constructed proves nothing was opened. Valid-looking refs that GitPython cannot resolve (`HEAD^{tree}`, `HEAD~99`, an unknown short SHA) and `logs/HEAD` (the all-zero SHA) each give the unknown-ref `CompareError`, never another exception type. Tests: Task 1 `test_invalid_refs_are_rejected_before_git` and `test_unresolvable_refs_are_unknown`.
4. **One test per language family.**
   - The eight families are `py`, `js` (one test covers `.ts`), `cs`, `cpp`, `java`, `rs`, `kt` and `go`.
   - Each builds a two-branch repository, then asserts the exact `added`, `removed` and `changed` symbol keys, plus one moved-but-identical symbol that is not reported.
   - A guard asserts that the parametrized family list equals `set(_CODE_ROUTES.values())`.

   Test: Task 2 `test_symbols_per_language_family[...]`.
5. **Callers degrade, the answer survives.** With a stub engine raising `ServiceUnavailable`, the tool still returns files and symbols, `impacted_callers is None`, and the notice is present. Test: Task 3 `test_graph_down_keeps_the_git_answer`.

### Task 1: Ref resolution, merge base and the file list (C1, C2, C4, C5 walk caps, C7)

**Files:**
- `devgraph/indexer/git_history/compare.py` (new):
  - **`CompareError(ValueError)`**.
  - **`validate_ref(arg_name, ref)`**. It applies the C4 rules in this order: empty or too long, leading `-`, leading `/`, `..`, `:`, whitespace or control character, `*?[\`. There is no allow-list charset, so `fix#123` and non-ASCII names pass. It raises `CompareError` with the C7 "is not a valid ref" message.
  - **`FileChange` dataclass**: `path`, `status`, `old_path=None`, `base_blob`/`head_blob` (GitPython `Blob` or `None`), `kind` (`"blob"`, `"symlink"` or `"submodule"`).
  - **`RefComparison` dataclass**: `repo` (the open `git.Repo`, for tests and Task 2), `base_ref`, `head_ref`, `base_commit`, `head_commit`, `merge_base`, `changes: list[FileChange]` (sorted by path, uncapped by `_COMPARE_MAX_FILES`, capped by `_COMPARE_MAX_DIFF_ENTRIES`), `truncated_reasons: list[str]`.
  - **`open_comparison(repo_path, repo_id, base_ref, head_ref, *, clock=time.monotonic)`**. A context manager that yields a `RefComparison` and closes the `Repo` on exit. The blobs stay readable for Task 2 while it is open. It:
    - validates both refs;
    - opens the repository, mapping `NoSuchPathError` and `InvalidGitRepositoryError` to the C7 message;
    - resolves each ref with `repo.commit(ref)` inside `except Exception` (that call only), mapping any exception to the unknown-ref message. It then rejects the all-zero `hexsha`, and a commit whose `.tree` can't be read, with the same message. The shallow sentence is added when `Path(repo.common_dir, "shallow")` exists;
    - calls `repo.merge_base(base.hexsha, head.hexsha, **timeout_kw)`, with `timeout_kw = {"kill_after_timeout": max(remaining, 1)}` on POSIX and `{}` on win32. It maps:
      - `[]` to "share no history", with the shallow sentence;
      - a `GitCommandError` whose `stderr` (stripped, decoded) starts with `Timeout:` to the timeout message;
      - any other `GitCommandError`, including `GitCommandNotFound`, to the generic git-failure message;
    - walks the trees (C2), checking the deadline before each tree read;
    - pairs exact renames;
    - wraps everything from `git.Repo(...)` to the end of the walk (and, through the context manager, Task 2's reads) in a final `except (GitCommandError, OSError)`, mapped to the generic git-failure message. `CompareError` itself passes through untouched.
  - **The caps** `_COMPARE_MAX_DIFF_ENTRIES`, `_COMPARE_MAX_FILES`, `_COMPARE_DEADLINE_S`, `_COMPARE_MAX_FILE_BYTES`, `_COMPARE_MAX_TOTAL_BYTES`, `_COMPARE_MAX_SYMBOLS_PER_LIST` and `_COMPARE_MAX_SYMBOLS`, all defined here. Task 2 uses the last four.
- `tests/indexer/git_compare_helpers.py` (new), a helper module like `docs_live_helpers.py`:
  - **`git(repo, *args)`**: `subprocess.run(["git", "-c", "user.email=dev@example.com", "-c", "user.name=Dev Example", "-c", "commit.gpgsign=false", *args], cwd=repo, check=True, capture_output=True, text=True)`, returning stdout stripped.
  - **`commit_files(repo, files: dict[str, str | bytes | None], message)`**. It writes each file, deleting it when the value is `None`, then runs `add -A` and `commit`, and returns the SHA.
  - **`two_branch_repo(tmp_path, base, branch, main_after=None, branch_name="feature")`**. It:
    - runs `init -b main` and commits `base`;
    - runs `checkout -b <branch_name>` and commits `branch`;
    - when `main_after` is given, checks out `main` and commits it;
    - checks out `main` at the end, so the working tree is never the head side.

    It returns the repository path.
  - **`record_git_commands(monkeypatch)`**. It wraps `git.cmd.Git.execute` and appends each `command` list to a returned list.
  - **`record_object_reads(monkeypatch)`**. It wraps `git.db.GitCmdObjectDB.stream` and `GitCmdObjectDB.info` at class level and returns two lists of the hex SHAs passed to each.
- `tests/indexer/test_git_compare.py` (new).

- [ ] Write failing tests:
  - **`test_added_removed_modified_and_nested_paths`.**
    - `base` has `a.py`, `pkg/b.py`, `pkg/sub/c.txt` and `d.md`.
    - `branch` modifies `pkg/b.py`, removes `d.md`, and adds `pkg/sub/new.go` and `e/f/g.rs` (a new directory).
    - The changes are exactly `[("d.md","removed"), ("e/f/g.rs","added"), ("pkg/b.py","modified"), ("pkg/sub/new.go","added")]`, sorted by path.
    - `merge_base` is `main`'s commit.
  - **`test_changes_on_base_after_branch_point_are_not_reported`.** `main_after` edits `a.py` and adds `only_main.py`. Neither appears, and the branch's own change does.
  - **`test_merged_head_is_empty`.** Comparing `branch_a="feature", branch_b="main"`, after `main` has merged `feature` (`git merge --no-ff`), gives no changes. `branch_a == branch_b` also gives no changes, and no error.
  - **`test_refs_of_every_shape_resolve`.** These all resolve: a tag, a full SHA, a 7-character SHA, `feature~1`, `HEAD`, `refs/heads/feature`, and a branch named `fix#123`.
  - **`test_exact_rename_and_edited_rename`.**
    - `x/old.py` is moved unchanged to `y/new.py`. That gives one `renamed` entry, with `old_path="x/old.py"`.
    - `m.py` is moved and edited to `n.py`. That gives `removed` `m.py` and `added` `n.py`.
    - Two empty files deleted and two others added stay `removed`/`added`, unpaired.
  - **`test_tree_blob_swap`.** `thing` is a file on `main` and a directory `thing/inner.py` on the branch. That gives `thing` `removed` and `thing/inner.py` `added`.
  - **`test_mode_only_change_is_not_reported`** (skipped on win32). A `chmod +x` on an unchanged file (`git update-index --chmod=+x`) is not reported.
  - **`test_symlink_and_submodule_are_listed_not_opened`** (skipped on win32).
    - The branch adds a symlink `link -> a.py`, giving `kind="symlink"`.
    - It adds a gitlink, via `git update-index --add --cacheinfo 160000,<some sha>,vendor/sub`, giving `kind="submodule"`.
    - Neither `record_object_reads` list contains the gitlink's SHA, and the symlink's blob is never `stream`ed.
  - **`test_only_cat_file_and_merge_base_run`.** Over a full comparison, the recorded subcommands are a subset of `{"cat-file", "merge-base"}`. The `merge-base` command is `[..., "merge-base", <40hex>, <40hex>]`.
  - **`test_invalid_refs_are_rejected_before_git`.**
    - It is parametrized over `"-h"`, `"--output=x"`, `"a..b"`, `"HEAD:secret.txt"`, `"../../x"`, `""`, `"a"*300`, `"ma in"`, `"a\tb"`, `"a\x07b"`, `"x*"`, `"x?"`, `"x[1]"`, `"a\\b"` and `"/abs"`.
    - With `git.Repo` monkeypatched in `compare` to `pytest.fail`, each raises `CompareError` whose message names `branch_a` and the rule.
  - **`test_unknown_ref`.** The message is exactly the C7 text, and it has no shallow sentence.
  - **`test_unresolvable_refs_are_unknown`.**
    - It is parametrized over `HEAD^{tree}` (`ValueError`), `HEAD~99`, `deadbeef` and `refs/heads/nosuch`. The `@{...}` forms are rejected by validation instead (`test_invalid_refs_are_rejected_before_git`).
    - Each raises `CompareError` with the unknown-ref message, never another type.
  - **`test_all_zero_sha_is_unknown`.** `logs/HEAD` resolves through the reflog file to `0000…0`. That gives the unknown-ref message, and `merge-base` is never run (per `record_git_commands`).
  - **`test_merge_base_failures`.**
    - `Git.execute` is monkeypatched to raise `GitCommandError(["git", "merge-base"], -9, stderr="Timeout: the command ... was killed")` for `merge-base` only. That gives the timeout message.
    - With `stderr="fatal: something"`, it gives the generic git-failure message.
  - **`test_git_missing`.** `Git.execute` is monkeypatched to raise `GitCommandNotFound("git", "not found")` for every command. That gives the generic git-failure `CompareError`, with no other exception type.
  - **`test_cat_file_failure`.** `GitCmdObjectDB.stream` is patched to raise `OSError` during the walk. That gives the generic git-failure message, and the `Repo` is still closed (spy on `Repo.close`).
  - **`test_not_a_git_repository`.** A plain `tmp_path` directory, and a missing path, both give the C7 text.
  - **`test_ref_naming_a_tree`.** A lightweight tag pointing at a tree (`git tag treetag <tree sha>`) gives the unknown-ref message.
  - **`test_shallow_clone_without_base`.** `git clone --depth 1 --branch feature file://<src> <dst>`, then `branch_a="main"` gives the unknown-ref message plus `this is a shallow clone, so older commits may be missing: git fetch --unshallow`.
  - **`test_shallow_clone_without_common_history`.**
    - Run `git clone --depth 1 --no-single-branch file://<src> <dst>`, where `src` has diverging `main` and `feature`.
    - Both refs resolve as `origin/main` and `origin/feature`.
    - The error is "share no history" plus the shallow sentence.
  - **`test_unrelated_histories`.** An orphan branch (`checkout --orphan`) in a normal repository gives "share no history" with no shallow sentence.
  - **`test_diff_entry_cap_and_deadline`.**
    - With `_COMPARE_MAX_DIFF_ENTRIES` monkeypatched to 3 and 5 added files, the result has 3 changes and `"diff_entries" in truncated_reasons`.
    - With a `clock` that jumps past the deadline after the first call, the walk stops and gives `"deadline"`.
    - With a `clock` that leaves 0.2 s, `merge-base` receives `kill_after_timeout=1` (POSIX only; asserted through `record_git_commands`' kwargs).
- [ ] Implement `compare.py` (C1, C2, C4, C5 walk, C7).
- [ ] `uv run pytest -q`. Commit "Compare two local refs from their merge base".

### Task 2: In-memory symbols for every language family (C3, C5 per-file caps, C6)

**Files:**
- `devgraph/indexer/symbols.py` (new):
  - **`EXTRACTORS: dict[str, Callable[[str, str], ExtractionResult]]`**. It is keyed by every `_CODE_ROUTES` value and calls `extract_*_file(source, path, "")` (`extract_go_file(..., module_path=None)`).
  - **`Symbol` dataclass**: `kind`, `name`, `container`, `ordinal`, `start_line`, `end_line`, `body`.
  - **`language_for(path) -> str | None`**. It routes by `PurePosixPath(path).suffix` through `_CODE_ROUTES`.
  - **`decode_source(data: bytes) -> str`**: `data.decode("utf-8", errors="replace").replace("\r\n", "\n")`.
  - **`extract_symbols(path, text) -> list[Symbol]`**. It keeps the `Function`/`Class` nodes that have both lines.
    - The body comes from `text.split("\n")[start-1:end]`, never `splitlines()`.
    - `container` is the innermost `Class` whose range encloses the symbol's (`<=` both ends) and that is not the symbol itself (identity, not equality of lines). Innermost means the latest `start_line`, then the earliest `end_line`.
    - `ordinal` is the position by `(kind, container, name)` in `(start_line, end_line)` order.
  - **`diff_symbols(old, new) -> (added, removed, changed)`**. Each list is sorted by `(start_line, kind, name)`. A `changed` entry carries both sides' lines (`start_line`/`end_line` from the head side, `old_start_line`/`old_end_line` from the base side).
- `devgraph/indexer/git_history/compare.py`:
  - **`symbol_detail(comparison, *, clock)`**. It fills, per change, `language`, `symbols` (or `None`) and `symbols_skipped`, applying C5's per-file, byte and symbol caps and the deadline.
  - It orders the changes code-language files first (path order), then the rest (path order), and details at most `_COMPARE_MAX_FILES` in that order. An `unsupported_language` file takes a slot only after every code file has one. The files past the cap are counted in `counts` and `files.count` but not listed. `symbol_counts` and the caller targets are built from the detailed files only.
  - It reads each blob's size from `Blob.size` before reading its data.
  - The binary check (a NUL byte in the first 8,000 bytes) and the `parse_error` catch are per side.
  - It adds the `files`, `bytes`, `symbols` and `deadline` reasons to `truncated_reasons`, keeping the spec's order with no duplicates.
- `tests/indexer/test_symbols.py` (new) and `tests/indexer/test_git_compare.py` (more cases).

- [ ] Write failing tests:
  - **`test_every_code_route_has_an_extractor`.** `set(EXTRACTORS) == set(_CODE_ROUTES.values())`.
  - **`test_symbols_per_language_family`**, the Review Focus item 4 test.
    - It is parametrized with one case per family. Each case is `(route, base_files, branch_files, expected)`, where `expected` is `{"added": {...}, "removed": {...}, "changed": {...}}` of `(kind, container, name)` triples.
    - Every case:
      - adds a function;
      - removes a function;
      - changes one function's body;
      - moves one function within the file unchanged (not reported);
      - has a method inside a class, where the language allows it lexically, so `container` is asserted.
    - **`py`:** `app.py`, with two classes that both define `__init__`, where only `B.__init__` changes. It asserts `("Function","B","__init__")` changed and `A`'s not.
    - **`js`:** `web/util.ts`. A `.ts` path proves the TypeScript grammar.
    - **`cs`:** `Svc.cs`, with a class and an overloaded method. Only the second overload changes, so it asserts the ordinal pairing.
    - **`cpp`:** `geo.cpp`, with an in-class method plus a free function.
    - **`java`:** `src/Main.java`.
    - **`rs`:** `lib.rs`, with an `impl` method and a free fn.
    - **`kt`:** `App.kt`.
    - **`go`:** `main.go`, with two types each having a `String()` method, where only the second changes. It asserts that ordinal `1` is changed and ordinal `0` is not.
    - Each case runs through `two_branch_repo`, then `open_comparison`, then `symbol_detail`, and asserts:
      - the file's `language == route`;
      - its symbol triples equal `expected`;
      - removed symbols carry base-side line numbers.
    - A guard asserts that the parametrized routes equal `set(_CODE_ROUTES.values())`.
  - **`test_body_slicing_ignores_form_feeds`.** A Python file with a `\f` line (a form feed, legal Python whitespace) before `def b()` gives `b`'s exact body, and an unchanged `b` is not reported when only a function above the form feed changes. A `splitlines()` slice would shift the body by one line and fail this.
  - **`test_crlf_only_change_is_not_changed`.** `main` commits `app.py` with LF endings, and the branch rewrites the same text with CRLF. The file is `modified` (its blob differs), but its `symbols` lists are all empty.
  - **`test_changed_entry_has_old_lines`.** A function moved down five lines and edited is `changed`, with `old_start_line`/`old_end_line` from the base and `start_line`/`end_line` from the head.
  - **`test_container_is_not_self`.** A one-line class `class A: pass` and a one-line method sharing the class's lines get containers `None` and `"A"`; the class never contains itself.
  - **`test_class_with_changed_method_is_changed`.** Python: the class and the method both appear in `changed`.
  - **`test_whole_file_added_and_removed`.** An added `.py` lists all its symbols as `added`, and a removed one lists all as `removed`.
  - **`test_unsupported_and_special_files`.** These are listed with `symbols is None`:
    - `README.md` and `notes.c`: `unsupported_language`;
    - a symlink: `symlink`;
    - a gitlink: `submodule`;
    - an exact rename of a `.py` file: `symbols == {"added": [], "removed": [], "changed": []}`, and the blob's SHA is not in `record_object_reads`' `stream` list.
  - **`test_too_large_and_binary`.**
    - A 1 MiB + 1 byte `big.py` gives `too_large`. Its SHA appears in the `info` list (the size check) but not in the `stream` list.
    - A `.py` containing `b"\x00"` gives `binary`.
  - **`test_parse_error_is_per_file`.** An extractor monkeypatched to raise, for `.go` only, gives `parse_error` on the Go file, while a `.py` beside it still has symbols.
  - **`test_symbol_caps`.**
    - With `_COMPARE_MAX_SYMBOLS_PER_LIST` patched to 2, a file adding 5 functions lists 2 and is marked `symbols_truncated`.
    - With `_COMPARE_MAX_SYMBOLS` patched to 3 across two files, the second file gets `limit`.
    - Both give the `symbols` reason.
  - **`test_byte_budget_and_deadline`.**
    - With `_COMPARE_MAX_TOTAL_BYTES` patched small, later files get `limit` and the `bytes` reason.
    - With a jumping clock, the result gives `limit` and `deadline`.
  - **`test_file_cap`.** With `_COMPARE_MAX_FILES` patched to 2 and 4 changed files, only 2 are detailed and the `files` reason is set.
  - **`test_code_files_get_the_slots_first`.**
    - `_COMPARE_MAX_FILES` is patched to 2. The branch changes `a.md`, `b.json`, `c.py` and `d.go`.
    - The detailed files are `c.py` then `d.go`, and the two others are counted (`files.count == 4`) but not listed.
    - With `_COMPARE_MAX_FILES` at 3, `a.md` takes the third slot.
  - **Rerun `test_only_cat_file_and_merge_base_run`** with `symbol_detail` included.
- [ ] Implement `symbols.py` and `symbol_detail` (C3, C5, C6).
- [ ] `uv run pytest -q`. Commit "Extract and diff symbols at both refs in memory".

### Task 3: The MCP tool, callers, catalog and docs (C1 response, C7, C8, C9)

**Files:**
- `devgraph/mcp/tools.py`:
  - **`compare_branches(engine, registry, repo_id, branch_a, branch_b)`** replaces the stub. It:
    - raises `ToolError` for an unknown `repo_id` (C7);
    - runs `open_comparison` + `symbol_detail` inside `try`/`except CompareError as exc: raise ToolError(str(exc)) from exc`;
    - builds the C1 response. `files` is built directly as `{"count": len(comparison.changes), "results": <the detailed entries>, "truncated": len(comparison.changes) > _COMPARE_MAX_FILES}`, not through `_envelope`: only the first `_COMPARE_MAX_FILES` changes were detailed. `impacted_callers` comes from `rows, more = engine.run_read_cypher(...)`, as `{"count": len(rows), "results": <sanitised rows[:_COMPARE_MAX_CALLERS]>, "truncated": more or len(rows) > _COMPARE_MAX_CALLERS}`. The tuple is unpacked, never treated as the row list;
    - runs the C8 query through `engine.run_read_cypher(..., timeout_s=DEFAULT_TIMEOUT_S, max_rows=_COMPARE_MAX_CALLERS + 1)`, catching `Neo4jError` and `DriverError`;
    - sanitises every string with `_sanitize_value`, including the nested symbol entries, which `_sanitize_row` alone does not reach.
  - **`_COMPARE_CALLERS_CYPHER`**, the spec's query, verbatim.
- `devgraph/mcp/server.py`: the `compare_branches` tool passes `registry`, and its docstring is the C9 description. The signature is unchanged.
- `devgraph/mcp/catalog.py`: the C9 entry.
- `tests/mcp/test_compare_branches.py` (new). It uses the stub `Engine` pattern of `tests/mcp/test_describe_node.py` (a `run_read_cypher` recorder returning canned rows or raising), a real `RepoRegistry(tmp_path / "r.db")` with the test repository registered, and `two_branch_repo`.
- `tests/mcp/test_repo_default.py`: add `"compare_branches"` to the `REPO_ARG_INDEX` set that takes `repo_id` after engine and registry.
- `tests/mcp/test_tools.py`: delete `TestCompareBranches` and the `compare_branches` import.
- `DEVGRAPH-CLIENT.md`, `README.md`, `PROJECT_STATUS.md`: per C9.

- [ ] Write failing tests:
  - **`test_response_shape`.** On a Python two-branch repository, the keys are exactly the C1 keys. `base`/`head`/`merge_base` carry the SHAs, `counts` and `symbol_counts` add up, and `files` is a `{count, results, truncated}` envelope.
  - **`test_errors_are_tool_errors`.** Each of these raises `ToolError` with the C7 text, and none raises another exception type:
    - an unknown `repo_id`;
    - a registered non-git directory;
    - `branch_a="-h"`;
    - an unknown ref;
    - the shallow case (reuse the Task 1 setup).
  - **`test_callers_query_targets_changed_and_removed_only`.**
    - The stub engine records params. `targets` holds exactly the changed and removed `{name, file}` pairs, and no added ones.
    - `repo_id` is a parameter, and the query text equals `_COMPARE_CALLERS_CYPHER`.
    - `max_rows == 26` and `timeout_s == DEFAULT_TIMEOUT_S`.
    - The canned `(rows, False)` come back as the `impacted_callers` envelope, and the "last index" notice is present.
  - **`test_no_targets_no_query`.** A comparison with only added symbols makes no engine call, and `impacted_callers` is the empty envelope.
  - **`test_callers_capped`.** The stub returns `(26 rows, True)`, as the real `run_read_cypher` does at `max_rows=26`. That gives `truncated is True`, 25 results, and `count == 26`. A second case returns `(3 rows, False)` and gives `truncated is False`, `count == 3`.
  - **`test_callers_cover_detailed_files_only`.** With `_COMPARE_MAX_FILES` patched to 1 and changed functions in two `.py` files, `targets` holds only the first file's symbols.
  - **`test_graph_down_keeps_the_git_answer`.**
    - The engine raises `ServiceUnavailable`.
    - Files and symbols are present, and `impacted_callers is None`.
    - The notice is `impacted callers unavailable: ServiceUnavailable`.
    - The same holds for a `Neo4jError` with code `Neo.ClientError.Transaction.TransactionTimedOut`, which yields that code.
  - **`test_hostile_strings_are_sanitised`.** A file named with a control character (`"evil\x07.py"`, committed via `git update-index --add --cacheinfo`), and a 600-character function name, come back stripped and capped at 500.
  - **`test_mcp_call_defaults_repo_and_keeps_signature`.**
    - Build the server as `test_repo_default.py` does, with the session repository set to the test repository.
    - Call `compare_branches` with `branch_a`/`branch_b` only. The result is not an error, has `repo_id` and the default notice, and keeps the "last index" notice.
    - The listed input schema's properties are exactly `repo_id`, `branch_a` and `branch_b`.
  - **`test_catalog_entry`.** The `compare_branches` catalog entry equals the C9 dict, and no catalog note contains "stub".
- [ ] Implement the tool, wiring, catalog and test edits (C1, C7, C8, C9).
- [ ] Update the docs (C9):
  - DEVGRAPH-CLIENT.md: the §4 row, the paragraph comparing it with `impact_analysis_for_diff`, and the "What this gets you" list.
  - README: replace the stub limitation with the similarity-rename and last-index limitation.
  - PROJECT_STATUS: the shipped entry, and `compare_branches` in the MCP tools line.
  - Check: `grep -rn "compare_branches" README.md DEVGRAPH-CLIENT.md PROJECT_STATUS.md devgraph/` shows no "stub", and `grep -rn "Phase 3" devgraph/mcp/server.py` shows no compare_branches line.
- [ ] `uv run pytest -q`. Commit "Make compare_branches compare refs, symbols and callers".
