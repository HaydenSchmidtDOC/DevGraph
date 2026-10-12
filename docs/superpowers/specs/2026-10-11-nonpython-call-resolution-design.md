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
and dropping the reverse-dependent fan-out, format 7) and C (Rust, C#, C++,
format 8). All three have landed.

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
alias (`@/lib/text`), a Go import path (`example.com/app/store`) and another
Rust crate's name (`shop_core::`) mean nothing without `tsconfig.json`,
`go.mod` or `Cargo.toml`. These files are *resolver
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
  each go.mod's `module` line and each go.work; for Rust: each Cargo.toml's
  library crate name and root file; a compiler flag, a `require` bump or a
  dependency or version change never counts), stored on the Repository node
  (`resolver_config`) with the list of paths it looked at, found or not.
- **Triggers.** A live batch (`RepoSync.on_changes`) that changes or deletes a
  path on that list, a directory holding one, or a path named like a
  configuration file recomputes the fingerprint; `catch_up` recomputes it after
  its walk. When it differs, every graph file of that language is re-indexed in
  one more batch, and the new fingerprint is stored only once that batch
  succeeds (never after a batch cut short by shutdown). `full_scan` takes it
  before reading any file and stores it at the end
  (`dispatch.sync_resolver_config`).
- **Go.** The Go fingerprint covers each go.mod's `module` line and the
  local `replace` directives (`=> ../shared`) of every go.mod and go.work. A
  batch extracts Go files under the go.mod and go.work paths its fingerprint
  found (the graph's, or the newer one `sync_resolver_config` re-indexes
  under), so no batch walks the repository for them.

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
| `a/b/.~` | the same | package | a method on a typed Go/Kotlin receiver |
| `/a/b/C.java` | files whose `/`-prefixed path ends with it | package | Java/Kotlin/C# class files, C++ includes |
| `/a/b/.` | files directly in any directory ending `/a/b/` | package | Kotlin top-level functions, Java wildcards |
| `a/b/` | files anywhere under `a/b/` | package | Python packages, JS barrels, Rust modules |

- No repository-relative path starts with `/` or ends with `/.`, so a pin can
  never be mistaken for a file. After its path a pin may carry flags, in this
  order (`graph/schema.py` `split_pin`): `~` (a directory at package
  confidence, the `loose_dir` kind), `?.go` or `?.kt,.java` (the matched file
  must end with one of these: a directory or suffix pin is otherwise blind to
  language, and `pkg/.` would also match the `.md` beside the Go files), `^`
  (no directory of the matched file's source root, the path before what the
  pin matches, is a vendored one such as `third_party` or `vendor`; `^t`
  nor is that root a conventional test root), `!_test.go` (the matched file must not end
  with it) and `@Shop:User,.:User` (the matched node's `mkeys`, its match
  keys, must hold one of these; each key holds a `:`, so a name starting
  with `@`, such as an npm scope, is no flag). The kind is read after the
  flags are stripped; a resolver never writes a path pin whose own path
  holds a flag character.
- **Match keys** (`@`, for C# namespaces; see the C# section) are written
  canonically, sorted and without repeats, by `schema.scoped_pin`, which
  drops the flag when there are more than 32: the site then links by its
  path alone. A Class or Function that carries `mkeys` always carries them,
  sorted, even when empty; a node without them (any other language, or a
  graph written before them) matches no `@` pin and every other one. The
  Cypher reads them as `coalesce(n.mkeys, [])`, and an `@` flag with no
  keys does not exist. A node keeps its identity, `(name, file)`, when its
  keys change, so the edges `@` pins gave it would go stale. A batch's
  pass-1 node writes leave the keys as they were; after pass 1,
  `engine.write_match_keys` writes every changed node's keys (removing them from one written
  without any), a few hundred nodes per transaction, before pass 2 tests
  them. A kept node whose keys match differently (no keys and none, `[]`,
  match alike) loses its incoming `CALLS` and `IMPORTS` edges (the types a
  resolver writes `@` pins on, whatever pin or bare name wrote them) in
  the same transaction and is marked `mkeys_stale`. Pass 2 writes the
  batch's edges again, and the relink writes other files' again under the
  new keys, as for an added node; then the mark goes. A batch that fails
  in between leaves the mark, so running it again (or any batch holding
  the node's file) retracts and relinks the node again: its new keys alone
  would no longer tell. Such a node is not added otherwise (no referrer,
  docs edge or mention depends on its keys), and its `MODIFIES` and
  `MENTIONS` edges stay. A batch file whose extraction fails loses its
  edges into such a node until it is indexed again, as it loses its other
  edges.
- **Confidence is the pin kind's**, so it never depends on which other pins a
  row was written with. Precedence runs down the table: each row's pin leaves
  out every file a higher-precedence pin of the same call (the same caller and
  callee name) matches. `exclude` carries those *pins*, not paths, and the
  match is `NOT any(p IN exclude WHERE pin_matches(p, file))`; a recursive
  prefix also leaves out the writer's own file, as before. Relink rebuilds
  `exclude` from the entry's pins in the same way.
- **Module targets** (`IMPORTS`) have no `file`. Every Module carries `dir`,
  `dir_name` (its directory's last name) and `basename`, derived from its
  path in the engine's node writer, each with a `(repo_id, ...)` lookup
  index. A directory pin to a Module seeks `dir`; a suffix-directory pin
  (a Java wildcard import reaching another module) seeks `dir_name`; a
  suffix pin seeks `basename`; each keeps the paths its pin matches. The
  row's `to_name` is then that seek key, and the Module's own path is what
  relink pins.
- **A file's imports collapse like one call.** Its `IMPORTS` edges to Modules,
  by path (unpinned, no confidence, as before) and by Module pin, form one
  set (`calls.import_rows`): a pin leaves out what a path import or a
  stronger pin matches, and the file itself. Relink rebuilds the set from the
  file's `IMPORTS` entries. (A Kotlin import of a class or function is an
  edge to that symbol, collapsed per name like a call.)
- **`no_self`** on a row adds `a <> b`: a call `x.m()` on an untyped receiver
  inside a method `m` never links the method to itself. A collapsed row has it
  only when every one of its call sites had it.

`name_refs` keeps its format (pins are strings), with one field more for
an entry whose pins carry match keys: they are stored once, after the
confidence, each such pin keeping only its `@`. One call's pins with
different keys (`a.Save()` and `b.Save()` on receivers of two types) are
entries of their own, one per key set, and relink takes the pinned
entries of one source and name together again, so each pin still leaves
out what a stronger one matches. A pinned entry no longer
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

## Go (`devgraph/indexer/go/extractor.py`)

Imports: every in-repository module path (each go.mod's `module`, each local
`replace` of a go.mod or go.work) names a directory; an import path resolves
by the longest one it is under, unless the directory it lands in has a go.mod
of its own below that module's (a nested module is not part of its parent).
The import is a directory pin to the package's non-test Go files
(`cache/.?.go!_test.go`, resolved); any other import is external, with no edge
(no more bare import-path targets, and no `_find_module_path`: the root
go.mod alone was read before). An unaliased import is used by its path's last
element, skipping a major version: any `vN` for an external path (`chi/v5` is
`chi`), only the module's own for an in-repository one (`example.com/shop/v2`
is `shop`, but `example.com/shop/v2/apis/apps/v1` is a directory, `package
v1`).

Tiers, first match wins:

1. `f()` declared in the file: this file.
2. `pkg.F()` on an import: its package's directory; nothing for an external
   package. A package clause can name a package otherwise than its directory
   (`internal/legacydb` declaring `package db`), and every import must be
   used, so an unknown qualifier (no import, type or name the file
   declares) may be an unaliased in-repo import whose path name the file
   never uses. Only when the unknown qualifiers and such imports are as many
   is it taken to be them (resolved); otherwise (a package-level `logger`
   of another file beside `db`) it reaches them at package confidence (`~`).
3. Any other `f()`: the file's package directory (and a dot-imported
   package's); a builtin (`len`, `append`) or a local function value links
   nothing.
4. `x.m()` on a typed `x`: the receiver, a parameter, `var x T`, `x := T{}`
   (or `&T{}`, `new(T)`), a field of a struct declared in the file (`c.s.Get()`
   with `s *store.Store`), what an in-file function returns, or what a
   package's function returned (`conn := db.Open()` makes `conn` some type of
   `db`). The method resolves to this file when its type and the method are
   declared here, else to the type's package directory at package confidence
   (`~`: methods are named bare, so another type of the package can have one
   of the name). A builtin or external type links nothing.
5. Anything else: a bare-name row that never ends at its caller, unless the
   method is in `STOP_METHODS["go"]`.

Function nodes are keyed by (name, file), with no receiver, so the
directory pin of a bare `f()` (tiers 2 and 3) also matches a method `T.f`
declared in that package, at resolved; the same holds for an unqualified
Kotlin `f()` through its package or an imported function's directory. A file
is the finest target there is (see Principle).

A pin to the file's own package leaves tests out from a non-test file
(`!_test.go`), keeps them from a test file of the package, and reaches only
the directory's test files from an external test package (`package x_test`,
`?_test.go`). Build-tag variants (`evict_linux.go`, `evict_other.go`) both
match: genuinely several targets.

## Java and Kotlin (`devgraph/indexer/java/`, `kotlin/extractor.py`, `jvm.py`)

**Source roots.** A file's directory ends with as much of its package as its
tree spells out: the longest tail of the package the directory ends with
gives the source root (`core/src/main/java`) and the package prefix the tree
leaves out (Kotlin's omitted root: `src/main/kotlin/data/Note.kt` declaring
`com.acme.notes.data` has root `src/main/kotlin`, `com.acme.notes` omitted).
A package then names its directory under the same root (*anchored*,
resolved) and, by path suffix, its directory under every other root (another
Maven or Gradle module, a test root: package confidence, S5). A suffix pin
from code outside a vendored directory carries `^` (no `third_party` or
`vendor` copy of the package: it is not on the classpath), from main code
`^t` (no conventional test root either: main never sees test sources). A
test root is a test source set (`src/test`, `src/androidTest`,
`src/testFixtures`, `src/integrationTest`) or a module's `test` directory
holding language source sets (`test/java`, `test/kotlin`;
`graph/schema.py` `TEST_ROOTS`), not any directory named `test`. Only the
source root, the path before what the suffix matches, is tested, so a
package named `vendor` or `test` is still found.

**Java.** `import a.b.C` (or a nested `a.b.Outer.Inner`, a static
`a.b.C.m`: the first capitalised segment picks the file) is an `IMPORTS` edge
to the anchored class file and a suffix pin `/a/b/C.java^t`; `a.b.*` is a
directory pin to the package's `.java` files under this root and a
suffix-directory pin elsewhere. The bare dotted target is gone. Calls, first
match wins:

1. Unqualified `m()`: the enclosing classes (innermost first) and their
   bases: this file when the class declared here has `m`, else a base's (an
   in-file base walked the same way, any other base's class file); then a
   static import's class file, then a static wildcard's.
2. `this.m()` and `super.m()`: the same, from the class or its bases.
3. A typed receiver: a field of the enclosing classes, a parameter, a local
   (`var x = new T()` included), a for-each, catch or resource variable,
   `new T().m()`, or `T.m()` on a class name. T's class file: this file when
   declared here (then its methods and in-file bases), an import's, else the
   own package's and each wildcard package's. A library type (`STOP_TYPES`,
   `ArrayList`, ...) links nothing, so `items.add()` on a `List<Item>` field
   is no edge, and no self-loop.
4. Anything else: a bare-name row off its caller, unless stoplisted.

A method T inherits from a base declared in another file is not found
through a receiver typed T (`repo.size()` on an `AuditedRepo` is Repo's
`size`): only AuditedRepo.java says what it extends, and reading it would
make the caller's edges depend on another file's text.

**Kotlin.** A class is not tied to a file named after it, and a top-level
function lives in any file of its package, so an import names a symbol, not
a file: `import a.b.C` (or `a.b.f`) is an `IMPORTS` edge to the Class `C`
(the Function `f`) declared in a `.kt` or `.java` file of the package's
directory, anchored and by suffix, which follows the symbol between files as
a call does (an edge to a Module would outlive the function moving out of
it). A wildcard is a directory and suffix-directory pin to the package's
files, as in Java. Calls, first match wins:

1. `f()` declared in the file: this file.
2. An imported function (or object member) `f`: its package's directory
   (`src/main/kotlin/util/.?.kt`, resolved) and by suffix (package); also as
   an extension call `x.f()`.
3. Any other `f()`: the enclosing classes' bases, the own package's
   directory (resolved; by suffix too, so a test reaches its package in main)
   and each wildcard package's.
4. `this.m()`, `super.m()` and a typed receiver as in Java (properties of the
   enclosing classes, primary-constructor `val`/`var`, parameters, locals
   typed by annotation or a constructor call, `T().m()`, `T.m()` on an object
   or companion): the class's own `.kt` file (resolved), any `.kt` file of its
   package (`~`, package confidence: `TagSet` lives in `Tags.kt`), and any
   `.kt` or `.java` file of the package's directories elsewhere (suffix
   directory pins, package confidence).
5. Anything else: a bare-name row off its caller, unless stoplisted.

## Rust (`devgraph/indexer/rust/extractor.py`)

**Crates and modules.** A configured library's root file (its `[lib] path`
included) is its crate's root, and the files under that directory are in
it; a file beside a package's Cargo.toml (`build.rs`) is a crate root of its
own. Otherwise a file's crate root is the nearest `src/` above it;
`src/bin/x.rs` and `src/bin/x/main.rs` are crate roots of their own, as is
every file directly in `tests/`, `benches/` or `examples/` (an integration
test is its own crate) and every `<dir>/<x>/main.rs` there; a file under
none of these is in a crate rooted at its own directory. The module path follows from the root (`src/a/b.rs` is
`a::b`, `src/a/mod.rs` is `a`, `lib.rs` and `main.rs` the root), and a
module `a::b` is `<root>/a/b.rs` or `<root>/a/b/mod.rs` (resolved), or this
file for an inline `mod x {}` of it. `crate::`, `self::` and `super::`
(repeatable, from inside an inline module too) walk the module tree. Another
crate is named by its package (the resolver configuration: each Cargo.toml's
`[lib]` name, else its `[package]` name with `-` as `_`, and its `[lib]`
path, else `src/lib.rs`): its files are pinned exactly, resolved when no
other package has the name, else at package confidence (a directory pin at
package confidence limited to the file's name, `a/src/.~?lib.rs`). A suffix
pin of the path would also match the same path inside another member: a
root package `app` beside `[workspace]` members had `app::run()` reach
`crates/core/src/lib.rs` too. A crate's own `tests/` reaches it the same
way. A path through another crate needs no `use`, so `shop_core::version()`
is a resolved edge between directories with no `IMPORTS` (the measure test
lists it). Without a
Cargo.toml naming it, a crate is external. `#[path]` attributes are not
read.

**`IMPORTS`.** `mod foo;` names the child module of the file's own module
(the 2018 rule: `mod user;` in `src/models.rs` is `src/models/user.rs`, which
was `src/user.rs` before); a `use` names the module of its item: `use
a::b::Type` is a::b, and a lowercase `use a::b::x` is the module a::b::x when
the file writes `x::`, the module a::b when it calls `x(`, else either. A
glob names its module. std, `::x` and any crate the configuration does not
name give no edge, and a `use` of an inline module of the file none.

**Calls**, first match wins:

1. `f()` declared in the file (nested functions included): this file.
2. A `use` binding or a path, `m::f()`, `crate::m::f()`, `super::super::m::f()`,
   `shop_core::f()`, `Type::f()`: the module's files, and the recursive
   prefix of its directory (package: an item re-exported from a submodule).
   `Type::f()` on a type declared in the file, and `Self::f()`, is this
   file. A path through std or an external crate links nothing, nor does a
   library type's (`Vec::new()`, `HashMap::new()`, `String::from()`:
   `STOP_TYPES["rust"]`), nor a bare `f()` nothing declares or imports (a
   glob import's modules excepted).
3. `self.m()` and a typed receiver: a parameter (`c: &Client`), `let x: T`,
   `let x = T::new()` (any associated function of a type) or `T { .. }`, a
   field of a struct declared in the file, the element of a `for` loop over
   a `Vec<T>`, slice or array (`for order in &self.orders`); `Box`, `Rc` and
   `Arc` are seen through. This file when the type and the method are
   declared here, else the type's module; a library type links nothing.
4. Anything else: a bare-name row off its caller, unless the method is in
   `STOP_METHODS["rust"]`.

The arguments of a macro invocation (`println!("{}", shop_core::version())`,
`assert_eq!(user.total(), 500)`) are reparsed as a call's arguments, so the
calls in them are found; the macro itself (`format!`) is no call. A method
a type gets from a trait implemented in another file is not found through
a receiver of that type: the call keeps a bare-name row when the type is
declared in the file, and links the type's module otherwise.

## C# (`devgraph/indexer/csharp/extractor.py`)

A C# file's text says which namespaces it uses, not which files hold them:
nothing ties a namespace to a folder, nor (beyond convention, SA1649) a
class to a file named after it. Where another file's type `T` lives is
therefore its class file by suffix, `/T.cs^` (no vendored copy), at package
confidence: two namespaces can each hold a `User.cs`, and only an index of
every file's namespace could tell them apart, which would make an edge
depend on the target file's text (a namespace edit keeps the target's nodes,
so edges into it would go stale). The design's partial-class rule (a
suffix-directory pin to the class's folder) is dropped; a method of
another part of a partial class keeps a bare-name row.

**Calls**, first match wins:

1. An unqualified `M()`, `this.M()`, `base.M()`: this file when an
   enclosing class declared here, innermost first, or an in-file base has
   M; else its other bases' class files; else a `using static` type's
   class file. A local function or a delegate variable links nothing.
2. `T.M()` on a type (an alias's target, the last name of a qualified
   `A.B.T.M()`) and a typed receiver (a parameter, a field or property of
   the enclosing classes, `T x`, `var x = new T()`, `new T().M()`, a
   `foreach` element of a `List<T>` or array): this file when T is declared
   here and has M; else `/T.cs^`, and for a receiver also
   `/TExtensions.cs^`, the conventional class of T's extension methods. A
   library type (`STOP_TYPES["csharp"]`: `Console`, `List`, `Math`, ...) or
   a predefined one (`string.IsNullOrEmpty`) links nothing.
3. Anything else, `dynamic` and `x?.M()` included: a bare-name row, off its
   caller for a member call, none for a stoplisted method.

**`IMPORTS`** are the classes a file references: an edge to the Class `T`
for each type it writes (a base, a field, property, parameter, local or
return type, `new T()`, a cast, `typeof`, a generic argument, a static
receiver, an alias's target, a `using static` type it calls through),
declared in any C# file (the suffix-directory pin `/.?.cs^`, package), not
in this file, and no library type, as Kotlin's imports name symbols. A
partial class's every part and a class in a file named otherwise are
reached; a `using` of a namespace, the bare dotted name and the old
`A/B.cs` folder guess are gone. An extension method's class, which the
caller never names, is not imported.

**Limits, and a follow-up.** Two classes of one name in different
namespaces both get every call and import of the name (the caller's
`using`s and enclosing namespace cannot pick one by path), and a call into a
class declared in a file not named after it (`UserMapper` in `Mappers.cs`)
links nothing. Both need a mechanism that knows each file's namespace and
classes, read from the project (`.csproj` files and the declarations of
every C# file) and kept current as the target files change, so that an edge
stays a function of the configuration and the target's existence; that is
a follow-up, not part of slice C.

## C++ (`devgraph/indexer/cpp/extractor.py`)

Without the build's `-I` flags a quoted include is read as the compiler's
search would most likely go. `#include "x/y.h"` names the file beside the
includer (`<dir>/x/y.h`, resolved; a `../` path only that) and `/x/y.h^`
anywhere else (package); a bare `"y.h"` names `<dir>/y.h` and only
`/include/y.h^` or `/src/y.h^` elsewhere, so `src/net/conn.cpp`'s
`"log.h"` is `src/net/log.h`, never `include/util/log.h`. These are its
`IMPORTS` (`#include <...>` none). A header's declarations are defined in
its same-stem source, so an include also reaches `y.cpp` and `y.cc` beside
the header it names (`<dir>/x/.~?y.cpp`) and under a `src/` root
(`/src/x/y.cpp^`), at package confidence (a convention, not what the
include names). The include-root rule holds for sources too: a bare
`"widget.h"` reaches no `widget.cpp` in another folder. `.cxx` (and `.c`,
which is not indexed) is no same-stem source, and a header outside the
includer's folder, `include/` and `src/` (a `-I` directory of another name)
is not resolved; fewer source extensions and include roots also keep the
rows per call down (M6).

**Calls**, first match wins:

1. A function defined in the file (a prototype is no Function node):
   this file.
2. `ns::f()` and `Cls::f()`: the includes whose path names the qualifier
   (`util::trim` reaches `util/strings.h` and `util/strings.cpp`), else
   every include. `std::` links nothing.
3. `x.m()` and `x->m()` on a typed receiver (a parameter, a local `T x;`,
   `T* x`, `auto x = new T`, a field of a class declared in the file):
   this file for a class declared here with m; else the include named after
   the type or its namespace (`Inventory` picks `inventory.h`,
   `net::Connection` picks `net/conn.h`); else the type's conventional files
   (`/inventory.h^`, `/inventory.cpp^`, ...) and every include. An STL type
   (`STOP_TYPES["cpp"]`, or anything in `std::`) links nothing.
4. Any other `f()`, and `this->m()`: every include (a file including nothing
   links nothing).
5. An untyped receiver: a bare-name row off its caller, unless the method
   is in `STOP_METHODS["cpp"]` (`items_.size()` inside `size()` is no
   self-loop).

A function defined once per platform (`now_ms` in `platform_posix.cpp` and
`platform_win.cpp`, declared in `platform.h`) is reached by neither: its
sources are not named after the header.

## No reverse dependents

With Go, Java and Kotlin resolved, every code edge is a function of its
writer's text, its resolver configuration and whether its target exists, so
`index_paths` no longer re-indexes a changed file's importers
(`_expand_with_reverse_dependents`, which followed Java's `IMPORTS` edges, is
gone) nor rescans a JVM file's directory for subtypes of a class it added
(`_same_package_subtype_referrers`: an unpinned `EXTENDS` is relinked from
`name_refs` like any by-name edge). A removed target goes with its node, an
added one is relinked, a configuration change re-indexes its language. The
graph-accuracy fuzz (13 seeds of 30 steps) moves a Java class between
modules, a function within a Go or Kotlin package, and adds and removes a
nested go.mod.

## Measurements

`scripts/measure_call_graph.py --fixture <lang>` copies a ground-truth fixture
(`tests/fixtures/callgraph/<lang>`, written from each language's own rules by
someone other than the resolver's author) to a temporary folder, scans it into
a scratch repository and compares the graph with its `expected.json`. A
`resolved` or `package` edge is *linked*; a package edge to the right file is a
correct link, its confidence reported apart. `tests/scripts/test_measure_fixtures.py`
asserts the targets, a strict xfail for each language not landed yet.

- **M1** multi-target `CALLS` (one caller, one callee name, several targets) by
  confidence. Target: resolved under 2 % of resolved edges, not counting a
  group whose every target the truth calls (Go's `store.New()` and
  `cache.New()` from one caller, a function declared once per build tag):
  that is no fan-out to a wrong file.
- **M2** precision of linked `CALLS` against the truth (resolved and all
  edges reported too). Target: at least 98 %, and no resolved edge between two
  directories without an `IMPORTS` between their files.
- **M3** `IMPORTS` against the truth, file to file (an edge to a Kotlin
  class or function counts as one to its file). Targets: precision 98 %,
  recall 95 %.
- **M4** `CALLS` recall at any confidence (linked and resolved recall
  reported). Target: 95 %, and no stoplisted name in the PageRank top five
  unless the truth calls it there.
- **M5** every named site: each `calls` row with a `note` present (and none of
  its `not_files`, unless the truth lists that file as a target of the same
  caller and name too), each `no_edge` absent; for Kotlin, every expected
  symbol extracted.
- **M6** cost: the fixture's `full_scan` and DevGraph's within +15 % of
  BEFORE, the largest `name_refs` under 1 MiB, the relink benchmark
  (`test_name_ref_relink_benchmark`) green, and the S10 fan-in
  (`--fan-in`: one re-save of a `main.go` calling `pK.New()`, `x.Get()` and
  `pK.Get()` in 30 of 400 packages that each declare them) costing what its
  own targets cost.

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

### Results after slice B

| | Target | BEFORE | AFTER | |
|---|---|---|---|---|
| go M1 resolved multi-target share | < 2 % | no resolved edge (43 bare, 24 multi-target) | 0 % (22 resolved, 9 package; its 6 multi-target edges all true: two `New`s, build tags) | met |
| go M2 linked precision | ≥ 98 %, 0 cross-directory without import | none linked; 72 % of all edges right | 100 %; 0 | met |
| go M3 `IMPORTS` precision / recall | ≥ 98 % / 95 % | 100 % / 14 % | 100 % / 100 % | met |
| go M4 `CALLS` recall | ≥ 95 % | 100 % (all bare) | 100 % (linked 100 %, resolved 69 %) | met |
| go M5 named sites | all | 26/33 | 33/33 | met |
| java M1 | < 2 % | no resolved edge (34 bare, 17 multi-target) | 0 % (12 resolved, 9 package) | met |
| java M2 | ≥ 98 %, 0 | none linked; 71 % | 100 %; 0 | met |
| java M3 | ≥ 98 % / 95 % | 100 % / 15 % | 100 % / 100 % (13/13, no vendored copy) | met |
| java M4 | ≥ 95 % | 100 % (all bare) | 95.2 % (linked 95.2 %, resolved 57 %) | met |
| java M5 | all | 24/30 | 29/30: `repo.size()` on an `AuditedRepo` field (Repo's, inherited in another file) is out of reach, see Java and Kotlin | disputed |
| kotlin M1 | < 2 % | no resolved edge (19 bare) | 0 % (16 resolved, 3 package, 1 name) | met |
| kotlin M2 | ≥ 98 %, 0 | none linked; 89 % | 100 %; 0 | met |
| kotlin M3 | ≥ 98 % / 95 % | none / 0 % | 100 % / 100 % (13/13) | met |
| kotlin M4 | ≥ 95 % | 85 % | 100 % (linked 95 %, resolved 80 %) | met |
| kotlin M5 | all | 29/33, symbols 5/13 | 33/33, symbols 13/13 | met |
| Python on DevGraph, ts fixture | unchanged | slice A | M1-M5 identical (old and new code over the same tree; ts 41/41 sites, `IMPORTS` 24/24) | met |
| M6 `full_scan` | ≤ +15 % | DevGraph 48.0 s; go 0.92 s, java 1.03 s, kotlin 0.92-1.09 s | DevGraph 52.6-53.0 s (+10 %); go 0.94 s, java 1.00 s, kotlin 1.03 s | met |
| M6 largest `name_refs` | < 1 MiB | 229,842 B (DevGraph) | 229,842 B (DevGraph); 3,134 B (java) | met |
| M6 S10 fan-in save | follows its targets | 3.6-4.7 s, 929k db hits (742k in one `Get` query) | 0.21-0.27 s, 5.9k db hits (1.3k at most per query) | met |

- **Timing.** Old and new code alternated over the same tree on one
  machine: DevGraph two pairs of `--scan` runs; each fixture the median of
  six in-process `full_scan`s after two warm-ups (kotlin's two series gave
  +13 % and -5 %).
- **Fan-in.** A directory, loose directory or prefix pin first sought every
  node of the name and then filtered by path, so a re-save's cost grew with
  how many packages declare `Get`. It now seeks the unique (repo_id, name,
  file) index as a prefix range (`b.file STARTS WITH` the pin's path),
  hinted like every other seek. `test_path_pins` bounds the db hits of 90
  such rows among 900 same-named functions (about 3.2k; 166k before).
- **Measure changes.** M1 leaves out a group whose every target the truth
  calls (Go's `store.New()` and `cache.New()` from one `main`, a function
  per build tag); M5 does not count a `not_files` entry the truth also lists
  for the same caller and name (the fixture's two `New` rows each name the
  other's file); M3 counts a Kotlin import's edge to a class or function as
  an import of its file. `test_measure_fixtures` lists the one disputed Java
  site.

AFTER (`full_scan_s` is one cold run of the script; the table has the paired
timing):

```json
{
 "go": {
  "M1": {"calls_by_confidence": {"package": 9, "resolved": 22}, "multi_target_by_confidence": {"resolved": 6}, "resolved_multi_share": 0.0},
  "M2": {"precision_resolved": 1.0, "precision_linked": 1.0, "precision_all": 1.0, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 18, "truth_imports": 14, "precision": 1.0, "recall": 1.0},
  "M4": {"recall_any": 1.0, "recall_linked": 1.0, "recall_resolved": 0.6897, "top5": [["New", "internal/store/memory.go"], ["normalize", "internal/store/keys.go"], ["abs", "money/round.go"], ["header", "tools/internal/codegen/render.go"], ["Open", "internal/legacydb/conn.go"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 33, "sites": 33},
  "M6": {"full_scan_s": 4.07, "largest_name_refs_module": "main.go", "largest_name_refs_bytes": 567, "total_name_refs_bytes": 2613},
  "failures": []
 },
 "java": {
  "M1": {"calls_by_confidence": {"package": 9, "resolved": 12}, "multi_target_by_confidence": {}, "resolved_multi_share": 0.0},
  "M2": {"precision_resolved": 1.0, "precision_linked": 1.0, "precision_all": 1.0, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 13, "truth_imports": 13, "precision": 1.0, "recall": 1.0},
  "M4": {"recall_any": 0.9524, "recall_linked": 0.9524, "recall_resolved": 0.5714, "top5": [["getName", "core/src/main/java/com/acme/core/model/Item.java"], ["core/src/main/java/com/acme/core/model/Item.java", "core/src/main/java/com/acme/core/model/Item.java"], ["validate", "core/src/main/java/com/acme/core/repo/Repo.java"], ["Notifier", "core/src/main/java/com/acme/core/notify/Notifier.java"], ["pages", "app/src/main/java/com/acme/app/OrderService.java"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 29, "sites": 30},
  "M6": {"full_scan_s": 2.46, "largest_name_refs_module": "app/src/main/java/com/acme/app/OrderService.java", "largest_name_refs_bytes": 3134, "total_name_refs_bytes": 8710},
  "failures": ["M5 every named site"]
 },
 "kotlin": {
  "M1": {"calls_by_confidence": {"name": 1, "package": 3, "resolved": 16}, "multi_target_by_confidence": {}, "resolved_multi_share": 0.0},
  "M2": {"precision_resolved": 1.0, "precision_linked": 1.0, "precision_all": 1.0, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 13, "truth_imports": 13, "precision": 1.0, "recall": 1.0},
  "M4": {"recall_any": 1.0, "recall_linked": 0.95, "recall_resolved": 0.8, "top5": [["now", "src/main/kotlin/util/Clock.kt"], ["truncate", "src/main/kotlin/util/Text.kt"], ["slugify", "src/main/kotlin/util/Text.kt"], ["Clock", "src/main/kotlin/util/Clock.kt"], ["line", "src/main/kotlin/export/MarkdownExporter.kt"]], "top5_stoplisted": []},
  "M5": {"sites_ok": 33, "sites": 33, "symbols_found": 13, "symbols": 13},
  "M6": {"full_scan_s": 1.72, "largest_name_refs_module": "src/main/kotlin/Main.kt", "largest_name_refs_bytes": 2087, "total_name_refs_bytes": 6684},
  "failures": []
 }
}

```

### Results after slice C

| | Target | BEFORE | AFTER | |
|---|---|---|---|---|
| rust M1 resolved multi-target share | < 2 % | no resolved edge (29 bare, 18 multi-target) | 0 % (10 resolved, 10 package) | met |
| rust M2 linked precision | ≥ 98 %, 0 cross-directory without import | none linked; 59 % of all edges right | 100 %; 0 | met |
| rust M3 `IMPORTS` precision / recall | ≥ 98 % / 95 % | 100 % / 42 % | 100 % / 100 % (19/19) | met |
| rust M4 `CALLS` recall | ≥ 95 % | 85 % (all bare) | 100 % (linked 100 %, resolved 50 %) | met |
| rust M5 named sites | all | 23/34 | 34/34 | met |
| csharp M1 | < 2 % | no resolved edge (25 bare, 6 multi-target) | 0 % (1 resolved, 18 package, 4 name) | met |
| csharp M2 | ≥ 98 %, 0 | none linked; 92 % | 89 % (2 wrong `User.cs`); 0 | short |
| csharp M3 | ≥ 98 % / 95 % | none / 0 % | 80 % / 94 % (16/17) | short |
| csharp M4 | ≥ 95 % | 100 % (all bare) | 90 % (linked 81 %) | short |
| csharp M5 | all | 29/31 | 27/31 | short |
| cpp M1 | < 2 % | no resolved edge (25 bare, 10 multi-target) | 0 % (3 resolved, 14 package) | met |
| cpp M2 | ≥ 98 %, 0 | none linked; 76 % | 100 %; 0 | met |
| cpp M3 | ≥ 98 % / 95 % | 100 % / 29 % | 100 % / 100 % (17/17) | met |
| cpp M4 | ≥ 95 % | 100 % (all bare) | 100 % (linked 100 %, resolved 18 %) | met |
| cpp M5 | all | 23/29 | 29/29 | met |
| Python on DevGraph; ts, go, java, kotlin fixtures | unchanged | slice B | M1-M5 identical (slice B and slice C code over the same tree) | met |
| M6 DevGraph `full_scan` | ≤ +15 % | 67.3 s | 70.8 s (+5 %) | met |
| M6 fixture `full_scan` | ≤ +15 % | rust 1.07 s, csharp 1.19 s, cpp 1.12 s | rust 1.21 s (+13 %), csharp 1.23 s (+3 %), cpp 1.21 s (+8 %) | met |
| M6 largest `name_refs` | < 1 MiB | 229,842 B (DevGraph) | 229,842 B (DevGraph); 1,441 B (cpp) | met |

- **C# falls short where a caller's text cannot reach.** The fixture's two
  `User.cs` (namespaces `Shop.Domain` and `Legacy.Accounts`, folders
  `Models/` and `Legacy/`) both match `/User.cs`, so `UserMapper.Describe`
  (`using Legacy.Accounts`) and `UserService.Register` (in `Shop.Domain`,
  whose enclosing namespace wins over its `using Legacy.Accounts`) each
  link both files' `Display` (two false linked edges, two wrong-file named
  sites) and four files import both classes. `UserMapper` lives in
  `Mappers.cs`, which `/UserMapper.cs` cannot find (two missed calls and
  named sites), and `user.Initials()`'s extension class is never named, so
  that import is missed. The design's namespace-segment narrowing (a pin to
  folders named after a `using`'s segments) would fix `Describe` and break
  `Register`, since `UserService.cs` has the same `using`; only an index
  of every file's namespace and classes, read at query time or kept by the
  engine, could tell them apart. `test_measure_fixtures` keeps C# a strict
  xfail with that reason.
- **Timing.** DevGraph's is one `--scan` each of the slice B and slice C
  code over the same tree, beside other test runs. The fixtures' are
  medians of alternating in-process runs (five scans after two warm-ups,
  three or six series each). A first C++ build wrote 8 pins per include (3
  include roots, 4 source extensions) and cost +19 %; `inc/` and `.cxx`/`.c`
  went (`.c` is not indexed at all).

AFTER (`full_scan_s` is one cold run of the script):

```json
{
 "rust": {
  "M1": {"calls_by_confidence": {"package": 10, "resolved": 10}, "multi_target_by_confidence": {"package": 4}, "resolved_multi_share": 0.0},
  "M2": {"precision_resolved": 1.0, "precision_linked": 1.0, "precision_all": 1.0, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 19, "truth_imports": 19, "precision": 1.0, "recall": 1.0},
  "M4": {"recall_any": 1.0, "recall_linked": 1.0, "recall_resolved": 0.5, "top5_stoplisted": []},
  "M5": {"sites_ok": 34, "sites": 34},
  "M6": {"full_scan_s": 2.92, "largest_name_refs_module": "crates/app/src/main.rs", "largest_name_refs_bytes": 1377, "total_name_refs_bytes": 5657},
  "failures": []
 },
 "csharp": {
  "M1": {"calls_by_confidence": {"name": 4, "package": 18, "resolved": 1}, "multi_target_by_confidence": {"name": 2, "package": 4}, "resolved_multi_share": 0.0},
  "M2": {"precision_resolved": 1.0, "precision_linked": 0.8947, "precision_all": 0.913, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 20, "truth_imports": 17, "precision": 0.8, "recall": 0.9412},
  "M4": {"recall_any": 0.9048, "recall_linked": 0.8095, "recall_resolved": 0.0476, "top5_stoplisted": []},
  "M5": {"sites_ok": 27, "sites": 31},
  "M6": {"full_scan_s": 2.23, "largest_name_refs_module": "src/Shop.App/Program.cs", "largest_name_refs_bytes": 1109, "total_name_refs_bytes": 3661},
  "failures": ["M2 linked precision >= 98 %", "M3 IMPORTS precision >= 98 %", "M3 IMPORTS recall >= 95 %", "M4 CALLS recall >= 95 %", "M5 every named site"]
 },
 "cpp": {
  "M1": {"calls_by_confidence": {"package": 14, "resolved": 3}, "multi_target_by_confidence": {}, "resolved_multi_share": 0.0},
  "M2": {"precision_resolved": 1.0, "precision_linked": 1.0, "precision_all": 1.0, "resolved_cross_dir_without_import": 0},
  "M3": {"graph_imports": 17, "truth_imports": 17, "precision": 1.0, "recall": 1.0},
  "M4": {"recall_any": 1.0, "recall_linked": 1.0, "recall_resolved": 0.1765, "top5_stoplisted": []},
  "M5": {"sites_ok": 29, "sites": 29},
  "M6": {"full_scan_s": 1.19, "largest_name_refs_module": "src/main.cpp", "largest_name_refs_bytes": 1441, "total_name_refs_bytes": 6788},
  "failures": []
 }
}
```

### Final results, every language

| Fixture | M1 resolved multi-target | M2 linked precision | M3 `IMPORTS` P / R | M4 recall (linked) | M5 sites | |
|---|---|---|---|---|---|---|
| ts | 0 % | 100 % | 100 % / 100 % | 100 % (96 %) | 41/41 | met |
| go | 0 % | 100 % | 100 % / 100 % | 100 % (100 %) | 33/33 | met |
| java | 0 % | 100 % | 100 % / 100 % | 95 % (95 %) | 29/30 | one disputed site |
| kotlin | 0 % | 100 % | 100 % / 100 % | 100 % (95 %) | 33/33, symbols 13/13 | met |
| rust | 0 % | 100 % | 100 % / 100 % | 100 % (100 %) | 34/34 | met |
| csharp | 0 % | 89 % | 80 % / 94 % | 90 % (81 %) | 27/31 | short (see above) |
| cpp | 0 % | 100 % | 100 % / 100 % | 100 % (100 %) | 29/29 | met |

Python on DevGraph is as after slice A (resolved precision, `IMPORTS` and
`find_callers` recall unchanged; 15 fewer self-loops than before `no_self`).

