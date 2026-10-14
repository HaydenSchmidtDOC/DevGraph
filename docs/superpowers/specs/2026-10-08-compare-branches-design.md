# `compare_branches`: what changed between two local refs — design

Upstream epic: HaydenSchmidtDOC/DevGraph#1 ("indexed → queryable through MCP
tools"). Builds on the session repository default
(`2026-10-08-mcp-repo-default-design.md`) and reuses the conventions of
`describe_node` (`2026-10-08-describe-node-design.md`).

## Problem

`compare_branches(repo_id, branch_a, branch_b)` is registered, but it is a stub.
It returns `{"added_in_b": [], "removed_in_b": [], "changed": [], "note": "Git
history integration planned for Phase 3"}` whatever it is given
(`devgraph/mcp/tools.py`). The catalog (`"note": "stub until git metadata is
fully wired"`), the README's limitations and DEVGRAPH-CLIENT.md all warn about
it. So an assistant asked "what changed between my branch and main?" has to
read `git diff` output itself, or call `impact_analysis_for_diff`. That tool
lists changed file names and the graph components in them, but it never says
which functions or classes were added, removed or edited.

## Goal

- Given two local refs, list the files added, removed, renamed and modified on
  the head side, and for each code file the symbols (functions, classes)
  added, removed and changed, in every language DevGraph extracts.
- Optionally, name the graph's callers of the changed and removed symbols.
- Read git objects in memory and never write the graph. The working tree,
  the index (staging area) and the graph stay untouched, whatever is checked
  out.
- Bounded in files, bytes, symbols and time. No new config knob, no new
  dependency, and the MCP signature is unchanged.

## Decisions

| # | Decision |
| --- | --- |
| C1 | **Merge-base semantics, like `git diff branch_a...branch_b`.** `branch_a` is the base and `branch_b` the head. The result is what changed on the head side since it diverged from the base. |
| C2 | **Files compared by object id; renames are exact-content only.** Statuses are `added`, `removed`, `modified` and `renamed`. |
| C3 | **Symbols compared by kind plus name.** The enclosing class and an ordinal only tell apart same-named symbols. "Changed" means the symbol's source text differs. |
| C4 | **Git safety.** Refs are validated, then resolved by GitPython against the repository's own refs and object database. Objects are read with GitPython. The one deliberate subprocess is `git merge-base`, on resolved SHAs. No hooks, textconv or filters run. |
| C5 | **Cost bounds.** Caps on changed paths walked, files detailed, file size, total bytes parsed and symbols listed, plus a wall-clock deadline. Each cap sets `truncated` and names its reason. |
| C6 | **Languages.** Every source extractor that `index_paths` routes to runs on bytes in memory. Any other file is listed with no symbol detail and a reason. |
| C7 | **Errors are `ToolError`s in plain text.** This covers an unknown repo, no git, a bad or unknown ref, and no common history (with a shallow-clone hint). |
| C8 | **Callers come from the graph, best-effort.** One bounded read-only query. If the graph is down, the git answer still returns, with a notice. |
| C9 | **Docs and catalog.** The stub flags go, the README limitation goes, DEVGRAPH-CLIENT.md gets a real table row, and PROJECT_STATUS gets an entry. |

### C1: what `branch_a` and `branch_b` mean

- **The signature is kept:** `compare_branches(repo_id, branch_a, branch_b)`. The
  session default for `repo_id` applies unchanged. The test suite's
  `MIN_ARGS` entry already exists.
- **`branch_a` is the base and `branch_b` the head.** The tool resolves both
  to commits, computes their merge base `M`, and compares `M`'s tree with
  `branch_b`'s tree. That is what `git diff branch_a...branch_b` shows, and
  what a pull request from `branch_b` into `branch_a` shows. Work that landed
  on `branch_a` after the branch point is not reported as "removed on the
  branch".
- **Any ref works, not only branch names:** a branch, tag, remote-tracking
  branch, SHA, `HEAD`, or a suffix like `~2` or `^`. Reflog, upstream and date
  forms (`@{...}`) are rejected (C4). All of them must already exist locally,
  and DevGraph never fetches (like `impact_analysis_for_diff`).
- **Same commit, or head already merged into base.** If `M` equals
  `branch_b`'s commit, the result has no files. That is not an error, and the
  response's `merge_base` and `head.commit` show why.
- **Response:**

  ```json
  {
    "base": {"ref": "main", "commit": "<sha>"},
    "head": {"ref": "feature/login", "commit": "<sha>"},
    "merge_base": "<sha>",
    "counts": {"added": 3, "removed": 1, "modified": 7, "renamed": 1},
    "files": {"count": 12, "results": [<file>, ...], "truncated": false},
    "symbol_counts": {"added": 9, "removed": 2, "changed": 5},
    "impacted_callers": {"count": 4, "results": [<caller>, ...], "truncated": false},
    "truncated": false,
    "truncated_reasons": [],
    "notices": ["impacted_callers come from the last index of the working tree, not from either ref"]
  }
  ```

  - `counts` covers every changed path walked. `files` lists at most 200
    files: code-language files first, then other files while slots remain
    (C5).
  - `symbol_counts` and `impacted_callers` cover only the files that were
    detailed (listed with `symbols` not `null`), never the files past the
    cap.
  - `<file>` is `{"path", "status", "language", "symbols"}`, plus:
    - `old_path`, for a rename only;
    - `symbols_skipped`, when `symbols` is `null` (C6);
    - `symbols_truncated: true`, when a per-file list was capped.
  - `symbols` is `{"added": [...], "removed": [...], "changed": [...]}`. Each
    entry is `{"kind", "name", "container", "start_line", "end_line"}`.
    - Lines are on the head side for `added` and `changed`, and on the base
      side (the merge base) for `removed`.
    - A `changed` entry also carries `old_start_line` and `old_end_line`, its
      lines on the base side.
    - `container` is the enclosing class's name, or `null`.
  - `<caller>` is `{"caller", "caller_type", "caller_file", "calls", "calls_file"}`.
- **The old stub keys go.** `added_in_b`, `removed_in_b`, `changed` and `note`
  only ever held empty lists and a placeholder, so nothing can depend on
  them.

**Not chosen:**

- Two-dot (`branch_a..branch_b`, a straight tree-to-tree comparison). It
  reports every commit that landed on main after the branch point as a change
  "on the branch". That is the classic wrong answer to "what did my branch
  change?". (`impact_analysis_for_diff` used two dots when this was written;
  it now reuses `compare.py` and compares from the merge base, three dots.)
- New parameters (`max_files`, `include_callers`, ...). The brief keeps the
  signature, and the caps are constants like `describe_node`'s.

### C2: the file list

- **The tree walk is in Python, over GitPython `Tree` objects.** It starts at
  the two root trees and compares entries by name:
  - an entry present on one side only is `added` (head only) or `removed`
    (base only); a whole subtree contributes every blob under it;
  - for an entry present on both sides:
    - equal object ids: skipped without being read, which is what keeps the
      walk proportional to the change, not to the repository;
    - both trees: walked into;
    - both blobs: `modified`;
    - a tree on one side and a blob on the other: the blob side is `removed`
      or `added`, and the tree side contributes its blobs.
- **Mode-only changes are not reported.** The object id is unchanged, so the
  symbols are too.
- **Submodules** (gitlink entries, mode `160000`) are listed with their status
  and `symbols_skipped: "submodule"`. They are never opened: a nested
  repository is out of bounds.
- **Symlinks** (mode `120000`) are listed with `symbols_skipped: "symlink"`.
  The link text is never followed.
- **Renames: exact content only.**
  - A `removed` path and an `added` path with the same blob id become one
    `renamed` entry, with `path` the new path and `old_path` the old one.
  - When several paths share an id, the pairing is in sorted path order, and
    the leftovers stay `removed`/`added`.
  - The empty blob is never paired.
  - A file renamed and also edited stays `removed` + `added`. Detecting
    similarity-based renames would need `git diff -M` (a subprocess, plus
    rename-limit tuning) or an in-Python similarity scorer. The symbol lists
    of the two entries still show what moved.
- **Paths are sorted** by their full repo-relative path (forward slashes).
- **Listing order puts code first.** `files.results` holds the code-language
  files (C6) in path order, then the other files in path order, up to
  `_COMPARE_MAX_FILES` in all. On a large diff, generated assets, lock files
  and docs therefore never crowd out the source files. Every file still counts
  in `counts` and `files.count`.

### C3: symbols and "changed"

- **Extraction.** For each code file and each side that has the file:
  - decode the blob as UTF-8 with `errors="replace"` and normalise `\r\n` to
    `\n`, matching what `_index_single_path` gets from `read_text`
    (universal newlines);
  - run the language's `extract_*_file(source, path, repo_id="")`;
  - keep the `Function` and `Class` nodes that carry `start_line` and
    `end_line`, and drop the `Module` node and every relationship.

  Nothing is written.
- **The body** of a symbol is the text of lines `start_line..end_line` of the
  normalised source, inclusive. Lines are split on `"\n"` only, never with
  `str.splitlines()`: tree-sitter counts rows by `\n` alone, while
  `splitlines()` also breaks on form feeds, `\x1c`–`\x1e`, `\x85`, `\u2028`
  and similar characters, which would shift every later body.
- **The comparison is exact after normalisation.** A whitespace change inside a
  symbol is a change. A CRLF-only edit (a file converted between `\r\n` and
  `\n` line endings) is not, because both sides normalise to the same text.
  That matches what the indexer sees: it reads files with universal newlines.
  A lone `\r` (classic Mac line endings) is left as it is, which is the one
  place this differs from `read_text`, which would turn it into `\n`. Such
  files are rare enough that the simpler rule wins.
- **Identity: kind plus name, then two tie-breakers.** The key is
  `(kind, container, name, ordinal)`.
  - `kind` is the label, `Function` or `Class`.
  - `container` is the name of the innermost `Class` node in the same file
    whose line range encloses the symbol's (`start <= s.start` and
    `s.end <= end`) and that is not the symbol itself, or `None`. Two
    distinct nodes can share exact lines (a one-line class around a one-line
    method), so identity, not strict containment, is the test. The innermost
    is the enclosing class with the latest `start_line`, then the earliest
    `end_line`. Equal line ranges cannot show nesting (`class A {} class B {}`
    on one line), so among them the one the extractor lists first is the
    outer one, and a class is never the container of its own container.
    - The search is a sort plus a stack of open classes, O(n log n). A file
      with more than `_COMPARE_MAX_FILE_SYMBOLS` (5,000) functions and classes
      gets `symbols_skipped: "limit"` before any per-symbol work (C5).
    - This tells apart `A.__init__` and `B.__init__`, and two `Run` methods in
      Java/C#/Kotlin classes.
    - Go methods are written outside their type, so their container is
      `None`. Rust methods sit inside an `impl` block, but the `impl` is not a
      `Class` node, so theirs is `None` too.
  - `ordinal` is the symbol's position among symbols with the same
    `(kind, container, name)`, in source order. This tells apart Go methods
    on two types that share a name, and overloads (Java, C#, C++, Kotlin).
  - So a pure name-plus-kind comparison is exact whenever names are unique in
    a file, which is the common case. The tie-breakers only matter for
    duplicates.
  - **Pairing within a duplicate group.** Among symbols sharing
    `(kind, container, name)`, those with identical bodies on both sides pair
    first, so overloads that are only reordered report nothing. The rest pair
    by ordinal (source order), and the leftovers are `added` or `removed`.
- **Classification:**
  - a key only on the head side is `added`;
  - a key only on the base side is `removed`;
  - a key on both sides with different bodies is `changed`;
  - a key on both sides with the same body is not reported, even if it moved
    within the file.
- **A class whose method changed is itself `changed`,** because its body text
  differs. That is accurate and cheap. The method's own entry says what
  changed inside it.
- **Known imprecision, accepted:**
  - when an overload is both edited and another inserted before it, the
    by-ordinal fallback can pair the wrong two, so it can read as one overload
    `changed` and one `added` with their lines swapped;
  - a Python decorator line sits outside `start_line` (the extractors start at
    the `def`), so a decorator-only edit is not seen;
  - a moved and edited Go method on another type with the same name may pair
    with the wrong type's method;
  - Rust methods on two types with the same name (`impl A { fn new }` and
    `impl B { fn new }`) are told apart only by ordinal, the same way as Go;
  - a nested function (a function inside a function) gets the enclosing
    class as its container, not the enclosing function, so it can share a key
    with a same-named method of that class and be told apart only by ordinal.

  - extractor blind spots, inherited from the indexer: TypeScript method
    decorators and Rust `#[attr]` lines sit outside the symbol's lines, so an
    edit to them alone is not seen; C++ templates, nested C++ classes, Kotlin
    companion objects and TypeScript interfaces and enums produce no symbols;
    a Kotlin class whose body is on one line fails to parse (`parse_error`).

  The response always carries line numbers, so the assistant can check.

### C4: git safety

- **Refs are validated before resolution.** `branch_a` and `branch_b` must each
  be 1–256 characters, and:
  - must not start with `-` (option-like) or `/`;
  - must not contain `..` (a range, or a path escape attempt);
  - must not contain `:` (`<rev>:<path>` names a blob, not a commit);
  - must not contain whitespace or a control character (Unicode category
    `C*`);
  - must not contain `*`, `?`, `[` or `\` (globs and Windows separators);
  - must not contain `@{` (reflog, upstream and date forms such as
    `HEAD@{1}`, `@{-1}`, `@{upstream}` and `main@{yesterday}`). GitPython's
    handling of these may not match git's, so such a ref could silently
    compare the wrong commit. The rule is checked before the repository is
    opened, and the caller passes a branch name, tag or SHA instead;
  - must not contain `{/` (the `^{/regex}` commit search). GitPython runs it
    as `git rev-parse --verify <sha>^{/pattern}` with no timeout, a
    history-wide search that is neither `cat-file` nor `merge-base` and is
    not bounded by the deadline.

  Everything else is allowed, so `fix#123`, `feature/ünïcode` and `v1.2+build`
  pass. A rejected ref is a `ToolError` naming the argument and the rule.
  Validation only screens out option-like and path-like input. Whether a ref
  exists is decided by resolution, below.
- **Resolution stays inside the repository.**
  - `git.Repo(record.path)` opens the registered root. As in
    `impact_analysis_for_diff`, there is no `search_parent_directories`, so a
    registered folder that is not itself a repository root is "not a git
    repository" (C7) rather than silently reading a parent repository.
  - `repo.commit(ref)` resolves the ref by GitPython's own `rev_parse`. That
    reads only `refs/…`, `packed-refs` and the object database of this
    repository, and peels a tag to its commit.
  - **Only `repo.commit(ref)` sits in `except Exception`,** mapped to the
    unknown-ref message (C7). GitPython 3.2.0's `rev_parse` raises a zoo of
    types for refs that pass validation: `BadName`, `BadObject` and
    `ValueError` (`HEAD^{tree}` and anything that peels to a non-commit), and
    for the `@{...}` forms that validation now rejects also
    `NotImplementedError` and `IndexError`. Listing them would miss the next
    one. The broad catch covers that one call only, so a bug elsewhere is
    never mislabelled as a bad ref. The one carve-out: a `CommandError` or
    `OSError` raised there (a `cat-file` that cannot start, a missing `git`)
    is re-raised to the final catch-all below, since git failed rather than
    the ref.
  - **A resolved commit is then checked:**
    - the all-zero SHA is rejected (`logs/HEAD` resolves to it through the
      reflog file, and `merge-base` would fail on it);
    - the commit's `.tree` must be readable.

    Either failure is the unknown-ref message.
  - **`@{...}` forms never reach GitPython.** A probe showed GitPython 3.2.0
    raises on `@{upstream}` and the date forms, and resolves numeric reflog
    forms (`HEAD@{1}`, `@{-1}`) its own way, which may not match git's. Rather
    than risk comparing the wrong commit, validation rejects every ref
    containing `@{` (above).
  - A probe confirmed that a `../../file` ref does not resolve in GitPython
    3.2.0. The `..` rule above keeps that true whatever GitPython does in
    future.
- **Objects are read through GitPython:** `commit.tree`, `Tree` iteration,
  `Blob.size` and `Blob.data_stream`.
  - GitPython's default object database (`GitCmdObjectDB`) serves these
    through its persistent `git cat-file --batch` and `--batch-check`
    processes, which read raw objects. `cat-file` without `--textconv` or
    `--filters` runs no textconv driver, no clean/smudge filter and no hook.
  - Every SHA and path goes over stdin, never argv, so nothing can be read as
    an option.
  - **No lazy fetch.** In a partial clone (`--filter=blob:none`, or treeless
    `--filter=tree:0`), git fetches a missing object from the promisor remote
    on demand: network access, possibly a credential prompt, and no deadline.
    The `Repo`'s git environment gets `GIT_NO_LAZY_FETCH=1` before its first
    command, so a missing object is reported as missing instead and becomes
    the "git object missing" message (C7). The variable exists from git 2.44.
    Older git ignores it, so there a partial clone can still lazy-fetch during
    a comparison; that is accepted rather than probing the git version.
  - This is the same object access `impact_analysis_for_diff`,
    `GitHistoryExtractor` and `blame` already use.
- **`merge_base` is the one deliberate subprocess.**
  - GitPython's `Repo.merge_base` shells out to `git merge-base` (checked in
    3.2.0: `self.git.merge_base(*rev)`), and there is no object-only
    equivalent in GitPython.
  - An in-Python ancestor walk was considered and rejected:
    - it is slow on long histories (one object read per commit, with no
      commit-graph);
    - it gets criss-cross merges wrong unless it reimplements git's
      `paint_down_to_common`.
  - `git merge-base` is plumbing: no hooks, no filters, no pager (no TTY), no
    textconv.
  - It is called with the two resolved 40-hex SHAs (`commit.hexsha`), never
    with the caller's strings, so its argv can't carry an option.
  - On POSIX it gets `kill_after_timeout = max(remaining, 1)`: the time left
    on the deadline (C5), floored at one second so a nearly spent deadline
    never passes zero or a negative timeout. GitPython raises on Windows if
    `kill_after_timeout` is passed, so it is omitted there, and **only POSIX
    has a merge-base timeout**. `merge-base` on two local SHAs is normally
    fast, but on Windows nothing bounds it.
  - **Its failures are mapped by stderr.** GitPython reports a kill as a
    `GitCommandError` whose stderr starts with `Timeout:` (GitPython stores it
    wrapped as `stderr: '...'`, which is stripped first). That gives the
    timeout message. Any other `GitCommandError`, and `GitCommandNotFound`,
    gives the generic git-failure message (C7).
- **No other `git` subcommand runs:** no `diff` (unlike
  `impact_analysis_for_diff`'s `git diff --name-only`), no `log`, no
  `rev-parse`. A test records every `Git.execute` call during a comparison
  and asserts that the subcommands are a subset of `{"cat-file",
  "merge-base"}`.
- **Hardened-git attacks** (a malicious `.git/config`, `core.fsmonitor`,
  hostile packfiles) are out of scope by Hayden's ruling. C4 avoids extra
  exposure but does not defend against a repository the user registered on
  purpose.
- **The `Repo` is closed in a `finally`,** which ends its `cat-file`
  processes, as `impact_analysis_for_diff` does.
- **A final catch-all.** The whole comparison, from opening the repository to
  the last blob read, sits inside a last `except (CommandError, OSError)`
  that becomes the generic git-failure `CompareError` (C7). `CommandError` is
  the common base of `GitCommandError` and `GitCommandNotFound` (in GitPython
  3.2.0 the latter is not a `GitCommandError`). This covers a `cat-file` that cannot start or dies:
  a missing `git` binary, a `safe.directory` refusal on a repository owned by
  another user, or a killed process. The more specific mappings above run
  first.

### C5: cost bounds

The caps are module constants in `devgraph/indexer/git_history/compare.py`,
named like `describe_node`'s:

| Constant | Value | When hit |
| --- | --- | --- |
| `_COMPARE_MAX_DIFF_ENTRIES` | 10,000 changed paths walked | The walk stops. `counts` and `files.count` are lower bounds. Reason `diff_entries`. |
| `_COMPARE_MAX_FILES` | 200 files in `files.results` | `files.truncated`. Reason `files`. |
| `_COMPARE_MAX_FILE_BYTES` | `MAX_CONFIG_BYTES` (1 MiB, `devgraph/paths.py`, the existing read cap) | That file gets `symbols: null`, `symbols_skipped: "too_large"`. Its size comes from `Blob.size` (`--batch-check`), so it is never read. Not a truncation. |
| `_COMPARE_MAX_TOTAL_BYTES` | 16 MiB of blob data parsed in one call | Later files get `symbols_skipped: "limit"`. Reason `bytes`. |
| `_COMPARE_MAX_SYMBOLS_PER_LIST` | 50 per file per list | The list is capped and the file is marked `symbols_truncated`. Reason `symbols`. |
| `_COMPARE_MAX_SYMBOLS` | 1,000 symbol entries in the whole response | Later files get `symbols_skipped: "limit"`. Reason `symbols`. |
| `_COMPARE_MAX_FILE_SYMBOLS` | 5,000 functions and classes on one side of one file | That file gets `symbols_skipped: "limit"`, checked right after the extractor and before any per-symbol work, so one generated file cannot hold the call. Not sticky: later files are still detailed. Reason `symbols`. |
| `_COMPARE_MAX_CALLERS` | 25 callers | `impacted_callers.truncated`. `count` is 26, a lower bound (as in `describe_node`). |
| `_COMPARE_DEADLINE_S` | 20 s from the start of the call | Checked before each tree read and each file parse. The walk stops, or later files get `symbols_skipped: "limit"`. Reason `deadline`. Also bounds `merge-base` (C4). |

- **`truncated`** is true when any reason was hit. `truncated_reasons` lists
  the reasons in the order above, without duplicates.
- **`symbol_counts`** counts the entries actually listed, so a capped list
  counts as its cap. Like `impacted_callers`, it covers only the detailed
  files.
- **Code files get the slots first** (C2). An `unsupported_language` file costs
  no bytes or symbols, but it would cost a listed slot, so it is listed only
  after every code file and only while slots remain. Symlinks and submodules
  have `language: null` and are listed among the other files.
- **`limit` is sticky.** Once the byte budget, the symbol budget or the
  deadline is hit, every later file that would need parsing gets
  `symbols_skipped: "limit"`, even one small enough to fit. The file whose
  lists would overrun `_COMPARE_MAX_SYMBOLS` keeps the entries that fit and is
  marked `symbols_truncated`.
- **Binary files.** A NUL byte in the first 8,000 bytes of either side makes
  the file `symbols_skipped: "binary"`, which is git's own heuristic. Its
  bytes still count toward the total.
- **The deadline is wall clock** (`time.monotonic`). The comparison function
  takes a `clock` argument so tests can make it expire deterministically.
  `symbol_detail` takes the clock and the deadline from the comparison, so
  the two can never disagree.
- **The graph read** uses `engine.run_read_cypher` with `DEFAULT_TIMEOUT_S`
  (10 s) and `max_rows=_COMPARE_MAX_CALLERS + 1`, outside the 20 s git
  deadline.

### C6: languages

- **One routing table.** `devgraph/indexer/symbols.py` maps each value of
  `dispatch._CODE_ROUTES` (the suffix-to-route table `_routes` uses) to its
  extractor:
  - `py` → `extract_python_file`;
  - `js` → `extract_js_file` (`.js`, `.jsx`, `.ts`, `.tsx`; the extractor picks
    the grammar from the path's suffix);
  - `cs` → `extract_csharp_file`;
  - `cpp` → `extract_cpp_file`;
  - `java` → `extract_java_file`;
  - `rs` → `extract_rust_file`;
  - `kt` → `extract_kotlin_file`;
  - `go` → `extract_go_file(..., module_path=None)`. The module path only
    affects `IMPORTS` targets, which are dropped.

  A test asserts that every route in `_CODE_ROUTES` has an extractor, so a
  new language that is indexed but not compared fails loudly.
- **`language`** in each file entry is that route code (`"py"`, `"js"`, ...),
  or `null`.
- **Every other file** (Markdown, YAML, JSON, Dockerfiles, `.c`, images, ...)
  is listed with its status, `language: null`, `symbols: null` and
  `symbols_skipped: "unsupported_language"`.
  - Docs, compose and Containerfile extractors are deliberately not run.
    Their nodes are not symbols, and their identity rules differ.
- **An extractor exception** on one side gives that file
  `symbols_skipped: "parse_error"` and the comparison goes on, as
  `index_paths` does per file.
- **An exact rename** has identical content, so it gets empty symbol lists
  without being parsed. This holds even when the rename changes the language
  (`a.txt` to `a.py`): such a file reports no symbols, though every symbol is
  new to code.
- **A symlink replaced by a file** is a `modified` blob whose base side is
  link text. That side counts as no symbols, so the file's symbols are all
  `added`. A file replaced by a symlink is listed with `symbols_skipped:
  "symlink"`.

### C7: errors

Every failure the caller can fix is a `ToolError`. The SDK passes its text to
the client, where a `ValueError` would be hidden. Caller values are echoed
cut to 100 characters and quoted, as `tools._echo` does. The messages:

| Case | Message |
| --- | --- |
| `repo_id` not registered (explicit) | `no such repo_id: '<id>'; run devgraph list to see registered repositories` |
| Registered root missing, or not a git repository root (`NoSuchPathError`, `InvalidGitRepositoryError`) | `repository '<id>' is not a git repository at its registered root; compare_branches needs the repository's own .git` |
| Ref fails validation | `branch_a '<ref>' is not a valid ref: <rule>`. The rule is one of "it starts with '-'", "it starts with '/'", "it contains '..'", "it contains ':'", "it contains whitespace or a control character", "it contains one of * ? [ \", "it is empty or longer than 256 characters", "reflog and upstream forms like @{...} aren't supported; pass a branch name, tag or commit SHA", "regex forms like ^{/...} aren't supported; pass a branch name, tag or commit SHA". |
| `repo.commit(ref)` raises anything; the result is the all-zero SHA; or its tree can't be read | `branch_b '<ref>' is not a branch, tag or commit in repository '<id>'; refs must exist locally (DevGraph never fetches)`. If the repository is shallow (a `shallow` file in `repo.common_dir`), add `; this is a shallow clone, so older commits may be missing: git fetch --unshallow`. This also covers a ref naming a tree or blob, which GitPython reports as a `ValueError` when it peels to a commit. |
| No merge base (`merge_base` returns `[]`) | `'<a>' and '<b>' share no history in repository '<id>'`, plus the same shallow-clone sentence when the repository is shallow. |
| `merge-base` killed by the deadline (`GitCommandError` whose stderr starts with `Timeout:`) | `compare_branches timed out finding the merge base of '<a>' and '<b>'` |
| An object missing mid-walk or while reading a blob for symbols (`BadName`, `BadObject`, `ValueError` from `cat-file`, as in a partial clone) | `git object missing while comparing '<a>' and '<b>'; the clone may be partial or shallow` |
| Any other `GitCommandError` (including `GitCommandNotFound`) or `OSError`, from `merge-base` or anywhere in the comparison | `git failed while comparing '<a>' and '<b>' in repository '<id>': <exception class name>`. This covers a missing `git` binary or a `safe.directory` refusal. The exception's own text (which may hold paths) is logged, not returned. |

- **Errors are raised by the git module itself.** It raises its own
  `CompareError(ValueError)` with the message, and `tools.compare_branches`
  re-raises it as `ToolError(str(exc))`. The module stays free of the MCP SDK,
  like the other indexer modules.
- **A same-commit or already-merged comparison is not an error** (C1).

### C8: impacted callers

- **Targets:** every `changed` or `removed` symbol listed (not `added` ones:
  nothing indexed can call them yet), as `{"name", "file"}` pairs. `file` is
  the file's path.
- **One read-only query,** a per-label union so that each branch uses the
  `(repo_id, name, file)` key:

  ```cypher
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
  ```

  - Every value is a parameter, and nothing is interpolated.
  - It runs through `engine.run_read_cypher` (read access, `DEFAULT_TIMEOUT_S`,
    `max_rows=26`), which returns `(rows, more)`. `impacted_callers` is
    `{"count": len(rows), "results": rows[:25], "truncated": more or len(rows) > 25}`,
    with strings sanitised.
  - The scoped `CALL (t) { ... }` form needs Neo4j 5.23 or later, the same as
    the `CALL () { ... }` that `summarise_repository` and `describe_node`
    already use. This adds no new version requirement.
  - With no targets, no query runs and `impacted_callers` is the empty
    envelope.
- **The graph is the working tree's last index, not either ref.** The notice
  `impacted_callers come from the last index of the working tree, not from
  either ref` is added whenever the query runs. A symbol that exists only on
  `branch_a` finds no node and therefore no callers. That is correct for
  "what of mine calls something this branch removed", as long as the user is
  on the base branch. The notice tells the assistant to read it as a hint.
- **The graph failing does not fail the tool.** Any `neo4j.exceptions.Neo4jError`
  or `DriverError` (Neo4j down, a timeout) gives `impacted_callers: null` and
  the notice `impacted callers unavailable: <code>`. `<code>` is the error's
  `code`, or its class name. The git answer is the point of the tool.
- **Repository scope only:** no `cross_repo`, which keeps the signature.

### C9: docs and catalog

- **Catalog** (`devgraph/mcp/catalog.py`):

  ```python
  {"name": "compare_branches",
   "identifier_kind": "two local git refs (branch_a = base, branch_b = head; compared from their merge base, like git diff a...b)",
   "envelope": False,
   "phase": 3,
   "note": "the response is not an envelope; files and impacted_callers inside it are {count, results, truncated}"}
  ```
- **Server** (`devgraph/mcp/server.py`). The tool's docstring, which is its MCP
  description, says:
  - the C1 semantics;
  - local refs only, never fetches;
  - per-file symbol detail for the eight language families;
  - the caps and `truncated`;
  - that `impacted_callers` reflect the last index;
  - that only POSIX has a merge-base timeout.

  `tools.compare_branches` gains `registry` after `engine`, as
  `impact_analysis_for_diff` has, and the server passes it.
- **DEVGRAPH-CLIENT.md:**
  - the §4 row becomes "What changed between my branch and main?" →
    `compare_branches(branch_a="main", branch_b="<branch>")`: files, per-file
    symbols added, removed and changed, and the graph callers of what changed;
  - a short paragraph next to the table on `compare_branches` versus
    `impact_analysis_for_diff`;
  - the tool joins the "What this gets you" list.
- **README:** the "`compare_branches` ... remains a stub" limitation is replaced
  by one line on what it does not do: similarity renames, and callers from
  the last index, not the refs.
- **PROJECT_STATUS:** a shipped entry under Summary, in the style of the
  `describe_node` entry. The built-in count stays 25.
- **The old stub test** `tests/mcp/test_tools.py::TestCompareBranches` (it
  asserted `"Phase 3" in result["note"]`) is deleted, and the new tests
  replace it.

## Non-goals

- Similarity-based rename detection, line-level diffs and hunks: `git diff` and
  `get_source` already show text.
- Comparing the working tree or the staging area, or uncommitted changes.
- Fetching, or comparing remote refs that are not already local.
- Indexing either ref into the graph, or graph-level (edge) differences
  between branches. That would need per-ref graphs.
- Changing `impact_analysis_for_diff` (then a two-dot range, a `git diff`
  subprocess and an `error` key). That came later as a separate change
  (audit 2, item 8): it now reuses `compare.py`, compares three-dot from the
  merge base, and raises a ToolError instead of the `error` key.
- Opening submodules or nested repositories.
- A merge-base timeout on Windows. GitPython cannot kill the process there
  (C4), so only the tree walk and parsing are bounded.
