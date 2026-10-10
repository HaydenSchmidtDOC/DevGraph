# Call-graph ground-truth fixtures

One small, realistic project per language (`ts`, `go`, `java`, `kotlin`,
`csharp`, `rust`, `cpp`), each with an `expected.json` that says which calls
and imports the language itself binds. The truth was written from each
language's own name-resolution rules (compiler, linker, runtime), not from
DevGraph's resolver, so it can be used to measure that resolver.

DevGraph keys Function and Class nodes by (name, file) and Module nodes by
repo-relative path, so the truth is at file granularity. Every path in
`expected.json` is relative to the language directory (the fixture's own
repository root).

## expected.json

```json
{
  "language": "go",
  "description": "...",
  "calls":   [{"caller": "Get", "caller_file": "cache/cache.go",
               "callee": "Get", "callee_file": "internal/store/memory.go",
               "note": "struct-field-receiver", "not_files": ["cache/cache.go"]}],
  "imports": [{"from_file": "cache/cache.go", "to_file": "internal/store/keys.go",
               "kind": "package"}],
  "no_edge": [{"caller": "routes", "caller_file": "main.go", "callee": "Get"}],
  "symbols": [{"name": "Stopwatch", "file": "src/main/kotlin/util/Clock.kt", "kind": "class"}],
  "notes":   {"struct-field-receiver": "why this case is tricky ..."}
}
```

- `calls`: every call site inside a repo function whose target is a repo
  function, listed once per (caller, caller_file, callee, callee_file).
  Names are bare (`Get`, not `Cache.Get`), as DevGraph names methods. The list
  is complete: an edge that is not in it is a false positive.
  - `ambiguous: true`: the language does not fix one file (Go build tags, a
    C++ function defined per platform, Java interface dispatch, C# `dynamic`).
    Every plausible target file gets its own row with the flag; an edge to any
    of them is correct, and none of them is required for recall.
  - `not_files` (optional): files that also define a function of that name and
    must not receive the edge. Informative; the completeness of `calls`
    already makes such an edge a false positive.
- `imports`: in-repo file-to-file imports. Each language's `notes` has an
  `imports-format` (or equivalent) entry saying what counts: Go lists every
  non-test file of the imported package, Java/Kotlin wildcards list every file
  of the package, C# lists the files of the in-repo types a file references,
  Rust lists `mod` declarations and `use` paths, C++ lists resolved quoted
  `#include`s. `kind` is informative; `ambiguous` as above.
- `no_edge`: calls that must not link to any repo function at all (standard
  library, third-party packages, macros, built-ins that share a name with a
  repo function, including the caller itself).
- `symbols` (Kotlin only): declarations the extractor must produce, for the
  adjacent one-line class bodies the grammar mis-parses.
- `notes`: one explanation per tricky case; `note` fields refer to its keys.

Not listed anywhere: constructor invocations (`new T()`, `T()` in Kotlin,
struct literals), JSX element usage, function values passed without being
called, calls inside anonymous lambdas/closures, and module-level code outside
any function.

## Using a fixture

Scan a fixture as its own repository: copy the language directory to a
temporary folder and register that. Registering it in place would also pick
up this repository's git history.

The `.gitignore` here ignores every language directory so DevGraph's scan of
this repository leaves them out; the files are tracked anyway, so add new ones
with `git add -f`. `tests/indexer/test_callgraph_fixtures.py` checks that every
`expected.json` is well formed and names files and functions that exist.
