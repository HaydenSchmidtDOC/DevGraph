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
- **JS/TS workspace packages** (index format 17, audit 5 item 3). The
  repository root's `package.json` `workspaces` (an array, or Yarn's
  `{"packages": [...]}`) and `pnpm-workspace.yaml` `packages` are globs of
  package directories (`packages/*`, `apps/**`, a literal path; a `!` one
  excludes; a symlinked, ignored or gitignored directory is never
  entered); each matched directory whose `package.json` has a `name` is
  that package (`ResolverConfig.js_workspaces`; two directories may share
  a name and both count). A nested `package.json`'s own `workspaces`, and
  a `package.json` under no glob, declare nothing.
- **Fingerprint.** Per language, a hash of the relevant content of every
  configuration file the walk finds (for TS: `baseUrl`, `paths`, `extends`,
  `references`, with every config those reach, whatever its name, and the
  root `package.json`'s and `pnpm-workspace.yaml`'s workspace globs and
  each workspace package's `package.json` `name`, `exports`, `types`,
  `module` and `main`, only those present being inputs, since one created
  later is configuration by its name; for Go:
  each go.mod's `module` line and each go.work; for Rust: each Cargo.toml's
  library crate name and root file; for C#: see below; a compiler flag, a
  `require` bump or a dependency or version change never counts), stored on
  the Repository node (`resolver_config`) with the list of paths it looked
  at, found or not.
- **Triggers.** A live batch (`RepoSync.on_changes`) that changes or deletes a
  path on that list, a directory holding one, a path named like a
  configuration file, or a .cs file whose head now holds a `global using`
  recomputes the fingerprint; `catch_up` recomputes it after
  its walk. When it differs, every graph file of that language is re-indexed in
  one more batch, and the new fingerprint is stored only once that batch
  succeeds (never after a batch cut short by shutdown). `full_scan` takes it
  before reading any file and stores it at the end
  (`dispatch.sync_resolver_config`).
- **Python** (index format 16). Each `pyproject.toml`'s declared source
  roots apply to the Python files at or under its directory, and each
  directory named `src` holding a Python file beside a `pyproject.toml`,
  `setup.py` or `setup.cfg` to the files under its parent
  (`ResolverConfig.python_roots`; see the Roots bullet of
  2026-10-10-python-call-resolution-design.md). The inputs are the
  project files and those `src` directories, the digest each
  pyproject's roots, and a batch extracts Python under the inputs its
  fingerprint found.
- **Go.** The Go fingerprint covers each go.mod's `module` line and the
  local `replace` directives (`=> ../shared`) of every go.mod and go.work. A
  batch extracts Go files under the go.mod and go.work paths its fingerprint
  found (the graph's, or the newer one `sync_resolver_config` re-indexes
  under), so no batch walks the repository for them.
- **C#** (`csharp_projects`, `ResolverConfig.cs_project`). A .cs file
  belongs to the project of the nearest directory at or above it holding a
  `.csproj` (several there are one project, their items unioned); a file
  with none above it has no project and no configuration. A project's
  configuration is its `<Using>` items (`Include`, `Remove`, `Static`,
  `Alias`, as attributes or child elements), applied in order after the
  nearest Directory.Build.props and before the nearest
  Directory.Build.targets, and the `global using` directives in the heads of
  its own .cs files (not a nested project's), whatever the file is called.
  A project's references are the projects named by the
  `<ProjectReference Include>` paths of its `.csproj` files and of its
  nearest Directory.Build.props/.targets, each read relative to the
  project's own directory as MSBuild reads an imported file's relative
  paths (`\` as `/`, `%XX` escapes decoded), closed transitively
  (`CsProject.references`; a cycle ends where it comes back, and a path
  naming no `.csproj` of the repository, missing, ignored, absolute,
  drive-qualified or outside it, reaches nothing until one is there). On
  a case-insensitive platform (Windows, macOS: `gitignore.IGNORECASE`)
  paths are compared case-folded, so `..\core\core.csproj` names
  `Core/Core.csproj`. Nothing is evaluated: an `<Import>` is not
  followed, an item under a `Condition` counts as if the condition held,
  and a name or path holding `$(`, `@(`, `%(` or a wildcard (`*`, `?`) is
  left out (a `$(MSBuildThisFileDirectory)` reference too). `RootNamespace`
  is not read. `ImplicitUsings` is (index format 18): when the last
  `<ImplicitUsings>` of the nearest Directory.Build.props, the project
  file and the nearest Directory.Build.targets (MSBuild reads every
  property before any item) is `enable` or `true`, the global usings the
  project file's `Sdk` implies come before its `<Using>` items, so a
  `<Using Remove>` removes one: Microsoft.NET.Sdk's `System`,
  `System.Collections.Generic`, `System.IO`, `System.Linq`,
  `System.Net.Http`, `System.Threading` and `System.Threading.Tasks`
  (Microsoft.NET.Sdk.Razor's the same); Microsoft.NET.Sdk.BlazorWebAssembly
  adds `System.Net.Http.Json`,
  `Microsoft.AspNetCore.Components.WebAssembly.Hosting`,
  `Microsoft.Extensions.Configuration`, `.DependencyInjection` and
  `.Logging`; Microsoft.NET.Sdk.Worker adds `Microsoft.Extensions.Configuration`,
  `.DependencyInjection`, `.Hosting` and `.Logging`; Microsoft.NET.Sdk.Web
  those and `System.Net.Http.Json` and `Microsoft.AspNetCore.Builder`,
  `.Hosting`, `.Http` and `.Routing`. Another SDK adds none (WindowsDesktop's
  depend on `UseWPF`/`UseWindowsForms`, not read).
  A file's *head* is read from its start, at most its first 32 KiB, for as
  long as it holds only whitespace, comments, preprocessor lines (`#if`,
  `#region`, `#nullable`, ...), `extern alias` and using directives, global
  or not, `using unsafe P = int*;` included (the compiler allows a `global
  using` nowhere else); a directive
  under an `#if` counts whatever the condition, and anything else ends the
  head (an attribute, a namespace, a type, a top-level `using var`, a
  directive with a comment inside). The fingerprint's inputs are every
  `.csproj` and Directory.Build file and every .cs file of a project whose
  head holds a `global using`; the digest is each project file's `<Using>`
  items and each such file's `global using`s, so a package bump changes
  nothing; each `.csproj`'s `<ProjectReference>` paths count too, so adding
  or removing one re-indexes every C# file. A live batch reads a changed .cs file's head only when a stored
  `.csproj` is at or above it: a file of no project cannot change the
  digest, and the `.csproj` that would make it one is a change itself.
  Every catch-up and full scan reads every .cs file's head (about 45 ms for
  500 files, 5.5 MB; no cache). A batch extracts C# under the inputs its
  fingerprint found. A configuration change re-indexes every C# file. The
  C# resolver reads a file's project's `<Using>` items and `global using`s
  as usings of the file (see the C# section); the legacy
  `csharp/extractor.py` indexing entry point reads the repository's
  projects too, and `symbols.EXTRACTORS` needs none (it keeps nodes only,
  whose match keys come from the file's text). Each project and
  Directory.Build file's `Sdk` and `ImplicitUsings` are inputs of the
  digest too. The implicit usings are `System.*` and `Microsoft.*`
  namespaces, left out at a site unless its own namespace is under the
  same root (see the C# section), so they matter mostly in the
  framework's own repositories.

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
  path alone. The keys may come in tiers, nearest first, separated by `;`
  (`schema.tiered_pin`): a node is then linked only when no node of its
  name the pin's path matches holds a key of a nearer tier than its own
  (C# shadow tiers, see the C# section). A Class or Function that carries `mkeys` always carries them,
  sorted, even when empty; a node without them (any other language, or a
  graph written before them) matches no `@` pin and every other one. The
  Cypher reads them as `coalesce(n.mkeys, [])`, and an `@` flag with no
  keys does not exist. A node keeps its identity, `(name, file)`, when its
  keys change, so the edges `@` pins gave it would go stale. A batch's
  pass-1 node writes leave the keys as they were; after pass 1,
  `engine.write_match_keys` writes every changed node's keys (removing them from one written
  without any), a few hundred nodes per transaction, before pass 2 tests
  them. A kept node whose keys match differently (no keys and none, `[]`,
  match alike) loses its incoming `CALLS`, `IMPORTS` and (since index
  format 18, whose heir keys an owned route handler can match)
  `IMPLEMENTS` edges (the types a resolver writes `@` pins or owned ends
  on, whatever pin or bare name wrote them) in
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
same candidates. A bare specifier naming a workspace package (the longest
package name it equals or starts with followed by `/`;
`ResolverConfig.workspace_import`) resolves after `paths` and before
`baseUrl` (TypeScript tries `node_modules`, where the package is linked,
when a `baseUrl` lookup fails; not checking the disk, the package goes
first): the package itself to every target of its `exports` `"."` entry
(a string, or the conditions `types`, `import`, `module`, `default`,
`require`, `node`, then any other, nested, an array's in order), else to
its `types`, `module` and `main` and `src/index` (and `index` when it
declares none of them); a subpath (`@acme/utils/fmt`) to its exact
`exports` key's targets or the longest single-`*` pattern's, else to that
path in the package. A `null` target blocks the specifier: nothing links,
not even the directory. A target under `dist/`, `lib/` or `build/` is also
tried as the same path under `src/` (a `.d.ts` dropped), the common
TypeScript layout whose build output is not in the repository. Every
candidate is offered and only existing files link: the disk is never
checked, so the result depends on the configuration alone and a source file
created later is relinked like any target. Each names the same candidates
as a relative specifier (`.js` mapped to its source), and the binding is
also pinned to the package directory as a prefix, so a name the entry
point re-exports reaches its defining file at package confidence. A batch
takes the workspace packages from its fingerprint's inputs (the
`package.json` files besides the root's are exactly the workspace
packages'), so only a fingerprint globs the directories.
Any other bare specifier (`react`, `lodash`) is external: no
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

A typed receiver's or `T.f()`'s method in another file is T's member
there, its own or one it inherits from a class of that file (index format
18; heir keys, graph-accuracy spec O3 (c)), so `u: User; u.save()` no
longer links `Order.save` beside it. A parameter shadows an import of its name, as in Python. Calls inside an
anonymous function (a callback, an IIFE) are still not attributed to anyone.

**JSX** (index format 16, audit 5 item 4). An opening or self-closing JSX
element whose tag is a capitalised identifier (`<Button />`) or a member
expression (`<Ui.Button>`) renders that component: a call site of its
name, in `.jsx`, `.tsx` and `.js` files alike, resolved by the tiers above
(the file's own, an import with a barrel's prefix, a namespace member). A
tag nothing resolves (a parameter `<Slot />`, an external package's
`<Link />`, `<this.Foo />`) links nothing: no bare-name row. An intrinsic
tag (`<div>`, `<svg:rect>`) and a fragment are no calls. The ts fixture's two renders link (precision unchanged at
100 %), and a React fixture (`tests/fixtures/callgraph/react`) goes from
2 of 16 calls to 16 of 16, linked precision 100 %.

Since index format 18:

- An element inside an anonymous function (`items.map(i => <Row />)`, an
  inline render prop, a `useCallback` body, nested ones) renders from the
  named function holding it (`_call_functions`), unless its tag names a
  parameter of a callback holding it (`items.map(Comp => <Comp />)`).
  Calls there are attributed to no one (until index format 20, below). A function is named when
  it is a declarator's or a class field's value (`_named_function`); its
  elements are its own.
- A class component (`class Modal extends React.Component`) renders
  through the `render` it owns: a component tag is also a call of
  `render` owned by the class (`render_pins`), as `this.render()` in the
  class would be for a class of the file, and as a member of an imported
  class for an import (a named import's owner its name, a namespace
  member's its member, a default import's the default-exported class,
  heir keys reaching an inherited `render`); an owner-less pin (a
  `require`, a barrel's prefix) is left out, as it would link every
  `render` there. A function component of the file adds no such row; an
  imported one's links nothing, owning no `render`. A function's
  destructured parameters (not a default value's names) and its locals
  bound to anything but a function (`const Widget = pick()`) shadow an
  import or class of their name, for a tag and a call alike: a `const`
  or `let` only inside its block (a `for (const W of ...)` head and a
  `catch (W)` parameter inside theirs), a `var` throughout.
- A component made by a wrapper is a Function of the declarator's name:
  `memo(fn)`, `forwardRef(fn)` (`React.` ones, nested) of the wrapped
  function's body, and a styled component (`styled.div`...``,
  `styled(Button)`...``) with no body (`_function_value`).

The React fixture, extended with a list callback's and a render prop's
elements, a class component, and memo, forwardRef and styled components,
goes from 16 of 23 calls to 23 of 23, linked precision 100 %.

**Definitions beyond declarations** (index format 19, audit 7 item 6).
Only class and function declarations and `const x = <function>` were
Functions, so service objects, CommonJS handlers and functions declared in
a block had no node and their calls were attributed to no one (an
`export const` that defined no function even lost its module-level calls).

- **Object literals.** A *defined* object literal (`_object_path`: a
  declarator's value, `exports.x = {...}`, `module.exports = {...}`, an
  object-valued property of one, through `( )`, `as` and `satisfies`)
  makes a Function of each method (`async getUser() {}`, getters too) and
  each function-valued property (`deleteUser: async () => ...`, through a
  component wrapper), owned by the binding's name below the scope's own
  owner (`userService`, `userService.admin`, `outer.local` in a function
  `outer`; `module.exports`'s are top-level, owner "", or `exports`
  (`EXPORTS_OWNER`) when a top-level function of the file has the name,
  so `function create() {}` and `module.exports.create = function ...`
  are two nodes, never one), with `CONTAINS`
  from the scope. An object passed as an argument or returned defines
  nothing. Its other properties' calls are the scope's.
- **CommonJS exports.** At the module's top level, `exports.x = <function>`
  and `module.exports.x = <function>` are a top-level Function `x` (owned
  by `exports` when a top-level function of the file is named `x`).
- **Blocks.** A definition inside `if`/`else`, `try`/`catch`/`finally`, a
  loop, `switch` or a labelled statement (`_compound_statements`) is its
  scope's own, at the module and in a function, as a direct one is. Two
  definitions of one name in one scope are one node, the first
  (`common.merge_same_nodes`, as in Python).
- **Calls to them.** `userService.getUser()` on a module-level object
  binding of the file, and `exports.handler()`/`module.exports.handler()`,
  are pinned to the file and the member's owner, when the object (or the
  exports) define that member; any other member (a shorthand `{ helper }`)
  links as before. From another file the import binding's tiers apply:
  a named import's or a `require`'s files, any member of the name there
  (an import names no owner: an instance of a class bound to the same
  name owns its members through its class). `this.m()` in an object
  literal's method is a by-name call. `_named_function` treats exactly
  these values as Functions, so the walk of the enclosing scope leaves
  them out.

The fuzz's `op_ts_service` adds and removes such a service (`svc`, with a
nested object and a function declared in an `if`) in a TS file, which
others call as `svc.run()`.

**Calls inside callbacks** (index format 20, audit 8 item 2). A call inside
an anonymous function (`useEffect(() => fetchOrder(id))`, `onClick={() =>
save()}`, `.then(o => track(o))`, `xs.forEach(function (x) { ... })`, an
inline Express handler) was attributed to no one, unlike a Python lambda's,
a Go closure's or a Java, Kotlin, C#, Rust or C++ lambda's. `_call_functions`
now keeps them for the named function (or Module) holding the callback, as
it does the callback's JSX elements. A callback's parameters, destructured
ones included, shadow there like a block's locals (`_block_names`), so
`items.map(fn => fn())` links no function `fn`; a `const`/`let` in its body
shadows inside it. A `function` expression (not an arrow) binds its own
`this` (`_in_function_expression`), so `this.m()` there is not pinned to
the class. A named nested function (`_named_function`) keeps its own calls.
A method of an object literal `_object_path` does not define
(`configure({ onDone() { ... } })`, `export default { methods: {...} }`)
is walked the same way (`_anonymous_function`), binding its own `this`.
The call-graph fixtures list no call inside a callback, so their measures
are unchanged.

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
   of the name). An unqualified exported type the file doesn't declare,
   and what an unqualified function of another file returned, may be a
   dot-imported package's (`import . "pkg"`, index format 18): that
   package's directory at package confidence too. A builtin or external
   type links nothing.
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

**Companion objects** (index format 16, audit 5 item 5). A `companion
object` is a Class owned by its class; an anonymous one is named
`Companion`, Kotlin's own name for it (`Repo.Companion` in Kotlin and
Java code), so its members are owned by `Repo.Companion`, as a named
companion's are by `Repo.Factory`. They used to be dropped with the
anonymous companion. `Repo.create()` on a class declared in the file
reaches a member of its companion when the class itself has none of the
name (an owned pin to `Repo.Companion`), and so does a bare `create()`
inside Repo when neither Repo nor a base declared in the file has one (an
inherited member comes first, as in Kotlin); inside the companion a bare
call or `this.m()` reaches its own members, then its bases', and its
typed properties type their receivers; from another file it reaches
Repo's files like any member of Repo (tier 4). Companions are keyed by
their members' owner, so two classes' anonymous companions in one file
stay apart. A companion's Class node carries `kind: companion`, and
`get_source` and `describe_node` take `Repo.create` (or `owner="Repo"`)
for a member of Repo's companion, anonymous or named, when Repo has none
of the name.

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
   file. An item the module re-exports (below) also names the module
  defining it. A path through std or an external crate links nothing, nor does a
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

**Re-exports** (index format 16, audit 5 item 6). A `lib.rs` facade (`pub
use net::client::Client;`) is the usual shape of a Rust library, and its
callers name the item through the crate root (`shop_core::Client::new`),
which pinned only `lib.rs`. Each file's top-level `pub use` (any `pub`
visibility; `rust.extractor.reexports`) is resolver configuration: the
fingerprint's inputs are the Cargo.toml files and every `.rs` file holding
one, its digest each file's re-exports, so a function added beside them
changes nothing and a re-export added, removed or retargeted re-indexes
every Rust file (a live batch reads a changed `.rs` file for a `pub use`,
as a `.cs` file's head for a `global using`). A re-export's path is read in
its own file: a module it declares (`mod net;`) is `self::net`, a name a
top-level `use` binds is that use's path. An item `X` reached through
module M (a path, a `use` binding, a typed receiver's type, a glob import's
module) is pinned to M's files and, when a file of M re-exports `X` (`pub
use a::X;`, `pub use a::Y as X;`, or a glob `pub use a::*;` unless the
file also re-exports `X` by name, Rust's shadowing rule), to the module
it names too, and on through that module's own re-exports (at most nine
hops; a cycle ends): every module on the way stays a possible definer
(`a` may define `X` beside its own glob). What a glob reaches is linked
at package confidence: its module's files as suffix pins of their whole
paths (`/a/util.rs`, which `a/my_util.rs` does not match) and its
directory's recursive prefix
(`a/util/`, so anything under it of the name), each leaving out the
re-exporting module's own files, which stay resolved. An associated call
through a re-exported type (`shop_core::Client::new()`) names the type as
the owner of every module the re-exports reach, so `net/mod.rs`, which
only passes `Client` on, links its own `Pool::new` no more than
`client.rs` does; a method on a value of that type (`c.fetch()`) is not
narrowed by owner (a trait's default method is owned by the trait), so a
module on the way that defines a same-named function of another type
links it too. An item M itself
defines hides a glob's in Rust, and M's text is not the caller's: this
way a function added to M is no configuration change, and the glob's
module only ranks below it. A function re-exported under another name (`pub use
a::fetch as get;`) is called by its own name: `shop_core::get()` is a
`CALLS` row to `fetch`, as is `get()` after a private `use a::fetch as
get;` (whose row used to name `get`, which no function is). A `use` of a re-exported item also imports the
defining module's file. Since index format 18, an inline module's `pub
use` is read too: `reexports` records it as `[name, path, "a::b"]`, the
inline module's path in its file, its head read in that module (its own
`mod`s and `use`s), and a module `M::a::b` reads the re-exports a file of
`M` records for `a::b` (`_reexport_entries`), so `mod api { pub use
crate::net::fetch; }` in lib.rs makes `api::fetch()` there and
`shop_core::api::fetch()` elsewhere reach net.rs. A re-exported module
is followed as a path's module: walking a path (`_walk`), a lowercase
segment is its child module and, when the module re-exports a module of
the name by name (`pub use net::client;`), that module too (a glob is not
followed for a module: every path through its module would reach the
glob's whole tree), so `crate::client::fetch()`, `shop_core::client::fetch()`
and `use shop_core::client;` then `client::fetch()` reach
`net/client.rs`, which that `use` also imports.

The arguments of a macro invocation (`println!("{}", shop_core::version())`,
`assert_eq!(user.total(), 500)`) are reparsed as a call's arguments, so the
calls in them are found; the macro itself (`format!`) is no call. A method
a type gets from a trait implemented in another file is not found through
a receiver of that type: the call keeps a bare-name row when the type is
declared in the file, and links the type's module otherwise.

## C# (`devgraph/indexer/csharp/extractor.py`)

A C# file's text says which namespaces it uses, not which files hold them:
nothing ties a namespace to a folder, nor (beyond convention, SA1649) a
class to a file named after it. Each declaration therefore carries its
namespace as *match keys* (`mkeys`, see Engine: path pins), and a reference
to another file's type `T` is one pin to any C# file, `/.?.cs^`, limited
with `@` to the nodes holding a key the reference can mean, at package
confidence, and to the projects the writer's project sees (see Project
visibility). The keys come from the target's own text,
so a namespace edit keeps the target's nodes and only changes their keys:
the engine retracts and relinks the edges into them.

**Match keys** (target side). A class `Ns:Name` (`.` for the global
namespace; a nested class `Ns:Outer.Inner`); a method or constructor
`Ns:Owner`, and an extension method also `Ns:+T`, `T` the simple name of
its `this` parameter's type. `Ns` is the file's block namespaces joined
(`namespace A { namespace B.C` is `A.B.C`) or its file-scoped one. Every
part of a partial class has the same key (the compiler puts all parts in
one namespace). A file's same-named declarations (overloads, a method
name in two classes, one class name in two namespaces) are one node: its
keys are their union. `mkeys` is always written on a C# Class and Function,
sorted, even empty, and never derived from the configuration.

**Candidate namespaces per site** (the writer's text and its project): the
enclosing namespaces, innermost first, then their parents, then `.`; the
usings of the enclosing namespace blocks and of the compilation unit; the
project's `<Using>` items and the `global using`s of its files (resolver
configuration). A using inside a namespace block (or after a file-scoped
namespace) is also read relative to each enclosing namespace
(`namespace Contoso.Web { using Services; }`: `Services`,
`Contoso.Web.Services`, `Contoso.Services`), and its aliases are that
block's alone; `using global::A.B;` stays absolute. `System`, `Microsoft`
and the namespaces under them are left out, the framework's in a typical
repository and most of a typical file's usings, unless the site's own
namespace is under the same root (the framework's own repositories:
`Microsoft.AspNetCore.Http` using `Microsoft.AspNetCore.Routing` keeps it);
this applies to the project's `<Using>` items and `global using`s alike.
A type `T` written unqualified at the site is `Ns:T` for each
candidate, `Ns:Outer.T` for each enclosing class `Outer`, or an alias's
target alone. A dotted name (an alias's target, a `using static` type, a
qualified `A.B.T`) is keyed at every split, since a type can be nested:
`A.B:T`, `A:B.T`, `.:A.B.T`; a qualified name's splits also under each
enclosing namespace `P` (`P.A.B:T`, `P.A:B.T`), and `Ns:A.B.T` for each
candidate (`A` a type there); `global::A.B.T`, and a call `global::T.M()`,
only at the global namespace.
A verbatim identifier's `@` is left out of every key.

**Pins.** A site writes at most two `@` pins: its types' keys and, for a
typed receiver, their extension methods' `Ns:+T`, each capped at 32 keys
(`schema.MAX_SCOPE_KEYS`) on its own. A group over the cap falls back to
the class files the resolver pinned before match keys, `/T.cs^` and
`/TExtensions.cs^`, so a site never links worse than before; its
extension `IMPORTS` are then left out.

**Calls**, first match wins (each pin's keys the union of what the tier
found):

1. An unqualified `M()`, `this.M()`, `base.M()`: this file when an
   enclosing class declared here, innermost first, or an in-file base has
   M; else the keys of its out-of-file bases, of the class itself when it
   is partial (another part, in another file, has M), and, for an
   unqualified `M()`, of the `using static` types. A local function
   (declared anywhere in the method) or a delegate variable links nothing.
2. `T.M()` on a type (an alias's target, a qualified `A.B.T.M()`) and a
   typed receiver (a parameter, a field or property of the enclosing
   classes, `T x`, `var x = new T()`, `new T().M()`, a `foreach` element
   of a `List<T>` or array; a generic `Svc<int>.M()` by `Svc`): this file
   when T is declared here and has M
   (else, for a class declared here, tier 1's keys for it); else T's keys,
   and for a receiver also `Ns:+T` for each candidate namespace (the
   extension methods in scope). A library type (`STOP_TYPES["csharp"]`:
   `Console`, `List`, `Math`, ..., also as the leftmost qualifier such as
   `DateTime.UtcNow`, not `Contoso.Directory.UserStore`) or a predefined one (`string.IsNullOrEmpty`) links
   nothing.
3. Anything else, `dynamic` and `x?.M()` included: a bare-name row, off its
   caller for a member call, none for a stoplisted method.

**`IMPORTS`** are the classes a file references: an edge to the Class `T`
for each type it writes (a base, a field, property, parameter, local or
return type, `new T()`, a cast, `typeof`, a generic argument, a static
receiver, an alias's target, a `using static` type it calls through), under
the keys the reference can mean, not declared in this file, and no library
type. A partial class's every part and a class in a file named otherwise
are reached; a `using` of a namespace imports nothing. A typed receiver's
call `x.M()` also imports the Function `M` under the `Ns:+T` keys alone
(the file depends on an extension class it never names), unless the file
declares an `M` itself.

**Limits.** Two visible `Ns.T` (one compilation's error CS0433, or a
project's own type beside a referenced one, which C# prefers with warning
CS0436) both link. A key group over the cap links by class file
name, as before match keys, and is not tiered (see Shadow tiers).
An extension method is keyed by its `this`
parameter's type's simple name (`Ns:+T`), or by a type parameter's first
constraint naming a type (`this T x where T : class, IRepo`: `+IRepo`); a
receiver typed with a class implementing that interface (`Repo r;
r.Touch()`) looks for `+Repo` and misses it, and a type parameter
constrained by no type (`class`, `struct`, `new()`) keeps `+T`.

### Shadow tiers (index format 14)

C# finds a simple type name by tiers, and the first tier holding the name
hides every farther one: the types nested in the enclosing classes
(innermost first), then, for each enclosing namespace innermost first, the
namespace's own types and then the usings of the namespace declaration
for it in this file, and last the global namespace's types and then the
compilation unit's usings (with the project's `<Using>` items and `global
using`s, which act as the compilation unit's). `UserService` in
`Shop.Domain.Services` with `using Legacy.Accounts` means `Shop.Domain.User`
whenever that type exists, and `Legacy.Accounts.User` only when it does
not. Stage 1 wrote a site's candidate keys as one union, so it linked both.

**Encoding.** An `@` flag's keys are written in tiers, nearest first,
separated by `;` (`@Shop.Domain.Services:User;Shop.Domain:User;Shop:User;.:User;Legacy.Accounts:User`):
each key once, in its nearest tier, sorted within it, empty tiers
dropped, so a pin is canonical (`schema.tiered_pin`; `scoped_pin` is the
one-tier case and writes what Stage 1 wrote). The 32-key cap counts the
keys of every tier. `split_pin` gives `scope` (every key, sorted, as before)
and `ranks` (each key's tier; empty for one tier), and `pin_value` sends
`sr` beside `sc`.

**Matching.** A tiered pin links a node when the pin matches it as before
(its path and flags, and one of its keys) and no node of its label and
name that the pin's path and flags match (keys and the row's exclusions
aside) holds a key of a nearer tier than the node's own nearest
(`common.shadowed`; the engine's `NOT EXISTS` in `_end_match`, which the
randomized Python-vs-Cypher test covers). Equal nearest tiers link alike,
as an ambiguity in C# is an error the graph still shows. Rows with a tiered
pin are grouped apart (`_group_rels_by_triple`), so no other row's Cypher
tests it, and a node of the nearest tier a row reaches skips the
subquery.

The shadow is tested on the callee itself, not on the type: `user.Display()`
is shadowed by a `Display` keyed `Shop.Domain:User`, so a nearer `User`
without `Display` (whose `Display` an unread base declares) leaves the
farther `User`'s linked, as Stage 1 did. Testing the type would need a
second name per row and a relink keyed by it; the callee is what the
relink already looks up by.

**What is tiered.** One type lookup: an unqualified type name (a typed
receiver, a static `T.M()`, an `IMPORTS` of a referenced type, a class's
base), its keys in the tiers above; a qualified `A.B.T` the same way, by
the namespace each enclosing level reads `A` in (`N.A.B:T`, `N.A:B.T`,
`N:A.B.T` at level `N`, `U:A.B.T` for its usings `U`, the splits at the
global level); and a receiver's extension keys `Ns:+T`, one tier per
enclosing namespace with its usings (C# looks for extension methods per
namespace declaration, its own namespace and its usings together). A
using alias for the name (or for a qualified name's first part) belongs
to its declaration's level: after that level's namespace's own types, in
place of its using-namespace imports, its target split at each dot as one
tier, and the lookup ends there (C# resolves an alias before a using's
types, and nothing farther is reached). `global::A.B.T` and a `using
static` type are one tier. A
pin that unions several lookups (a class's several bases, a partial
class's own keys with a base's, several `using static` types) is one tier:
one type's nearer candidate must not hide another type's. A key group
over the cap falls back to its class file pins, un-tiered, as before.

**Keeping incremental equal to a fresh scan.** A tiered row's edges
depend on every node of its target's name, not only on the one it links,
so three things change:

- *Pass 1 writes no tiered row.* Between the batch's files the graph is
  half old: a node a later file adds, removes or rekeys would decide the
  shadow, and pass 2 only adds edges. Pass 2 writes them once every node
  and match key of the batch is final.
- *A tiered group is recorded whole.* A group is one source's rows to one
  name, the set `call_rows` collapses; its `name_refs` entries keep every
  pin, its own file's included (which `name_ref_properties` otherwise
  leaves out), so the group can be written again from the graph alone.
- *Changed names re-run their tiered groups.* For each label and name with
  a node the batch adds, removes (`previous_nodes - batch_nodes`) or rekeys,
  each tiered group outside the batch to that name one of whose pins holds
  one of those nodes' keys, old or new, is re-run after pass 2, 200 groups
  per transaction (`engine.rerun_edges`): a node holding none of a pin's
  keys neither links nor shadows under it, so the other groups cannot
  change, and a node without keys (any language but C#) touches none.
  Re-running a group means its edges (from its source, of
  its type, to that name, written by its file) lose that file from their
  `origins`, and its rows are written again exactly as the writer's. This
  covers both directions: a nearer node added retracts the farther edges,
  one removed or rekeyed away restores them, and a move between namespaces
  or files is both. `remove_paths` lists the deleted files' nodes first and
  re-runs their names' groups after deleting, only when a deleted file is
  a `.cs` file (no other language writes match keys). A tiered group never takes
  the additive relink (`best_pin` on the added node alone), which would
  link a shadowed node. Other groups keep it.

As with the additive relink, a batch that fails between pass 1 and the
re-run leaves the outside groups as they were until their files are
indexed again or a full scan runs (a rekeyed node, marked `mkeys_stale`,
is re-run by the next batch holding it).

**Cost.** Each matched node of a farther tier tests the name's nodes once
through the `(repo_id, name)` index; a nearest-tier match does not. The
nearer keys are computed once per matched node (`WITH`), and the test is
`size(nearer) = 0 OR NOT EXISTS {...}`, which Neo4j plans as a
select-or-anti-semi-apply seeking the index; a first cut wrapping the
`NOT EXISTS` in a `CASE` was evaluated as an expression scanning every
node of the label, and made the 10-project synthetic scan 5x slower. A
touched name re-runs those of its tiered callers outside the batch whose
keys it holds, a retraction and a write per group, where the additive
relink wrote one edge. A batch of another language costs nothing more:
its nodes have no keys, so it looks up no `name_refs` for them.

### Project visibility (index format 15)

Stage 2 of the namespace design (ruling N1): two projects of one
repository can declare the same namespace and type, and a site sees only
its own project's and those its project references, transitively.

**Node side.** A C# Class and Function carry `proj`, their project's
directory (`CsProject.dir`; "." for a project at the repository root, so
it is never ""), or "" for a file of no project. It comes from the
configuration, never the text, and is written beside the node's match
keys after pass 1 (`engine.write_match_keys`): a kept node whose `proj`
changes is retracted, stale-marked and relinked exactly as one whose
keys change, and the shadow tiers of its name re-run for its keys. Only a
`.csproj` added, deleted or moved changes a node's `proj`, and that is a
configuration change re-indexing every C# file anyway; a .cs file moving
between projects is a new path, so its nodes are added and removed.

**Row side.** Every path-pinned `CALLS` and `IMPORTS` row of a file in a
project carries `visible`, its project's and its references' `proj`s,
sorted (`GraphRelationship.visible`); other edge types carry none. The engine sends it once per row
(`vis`, beside `targets`), not per pin, and a path-pinned end of a Class
or Function keeps a node only when `row.vis IS NULL OR coalesce(n.proj,
'') = '' OR n.proj IN row.vis` (`_visible`; `common.visible_to` is its
Python mirror, which the randomized shadow test checks against the
Cypher). A file of no project writes no `visible` and sees every node; a
node of no project, or of another language, is seen by every row. A row
pinned to a file (the writer's own) or by bare name is not limited.
Like a tiered row, such a row waits for pass 2, when every node's `proj`
is final.

**Limits.** What MSBuild would make of a reference beyond its path is not
read: `PrivateAssets="all"` and `DisableTransitiveProjectReferences`
(which stop a reference flowing on to the referencing project's own
referrers) are ignored, so the closure can be wider than the compiler's;
a `<ProjectReference Remove>` or `Update` is not applied (only `Include`
items count); and a project's default compile globs are not applied, so
a .cs file belongs to its nearest `.csproj` even where a parent project
would also compile it (`EnableDefaultCompileItems`, explicit `<Compile>`
items and nested-project exclusions unread; ruling N2). A route's
`IMPLEMENTS` rows are not limited, only `CALLS` and `IMPORTS`, the edges
a changed `proj` retracts.

**Shadow tiers.** A nearer tier's node shadows only when it is visible to
the row: the `NOT EXISTS` of `_shadow_free` tests `shadow` with
`_visible` too, so an unreferenced project's nearer `User` neither links
nor hides the referenced farther one.

**Relink.** The visible set is stored once per file, as the Module's
`name_ref_visible` (`name_ref_properties`), not in each `name_refs`
entry, so entries are unchanged in size. `find_name_refs` returns it
with the entries; the additive relink tests an added or rekeyed node's
`proj` against it before linking through a path pin, and a tiered
group's re-run rows carry it as `visible`.

**Incremental equals fresh.** A `<ProjectReference>` added or removed,
and a project added or deleted, change the C# fingerprint, so every C#
file is re-indexed under the new references (and nodes under their new
`proj`). A .cs file moving between projects deletes its old nodes and
adds new ones, relinked from other files' `name_refs` through their
visible sets, and re-runs the tiered groups its keys meet. The fuzz
toggles a `<ProjectReference>` (one written with `\`, a cycle through
two projects) and moves a file declaring a type another project's file
declares too between projects.

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

## Audit 4 extractor fixes (index format 11)

Four constructs were dropped or misattributed before any call was resolved:

- **C++ templates.** A `template_declaration` is walked through to the
  class, struct or function it declares, at file scope, in a namespace and
  as a member template (the class table reads member templates too). An
  out-of-class member of a class template names the template
  (`template<class T> void Stack<T>::push()` belongs to `Stack`).
- **Java records** are types like classes, with their methods and a compact
  canonical constructor (`P { ... }`, a constructor named `P`).
- **TS/TSX class-field arrow functions.** tree-sitter-typescript names a
  field `name`, tree-sitter-javascript `property`; both are read.
- **Go methods on a pointer to a generic type** (`func (s *Stack[T]) Push`)
  belong to `Stack`, like a value receiver, so their `CONTAINS` has a real
  source and the method table resolves typed calls into them.

The same format keys each Class and Function by its owner within its file
(2026-10-08-graph-accuracy-design.md, owner identity, O3 for the full
list), and a resolver that knows the owner of a call's target names it: a
typed receiver's or `this`/`self` call to a class declared in the file
links that class's member (Python, JS/TS, Java, Kotlin, C#, Go, Rust; C++
resolves by function name alone and still links every same-named member of
the file); across files, an imported top-level function (a Python
from-import or module attribute, a JS/TS ES named import or namespace
member, a Go package's function) links the top-level one, never a method
of its name. A method of another file's Python, JS/TS or Java class names
that class (index format 18), which reaches its own member or the one it
inherits from a base of that file (heir keys: graph-accuracy spec, O3
(c)), a JS/TS default import's the default-exported class's; a call
through a `require` still links every member of the name there. C# narrows by
owner through its match keys already. The measurement fixtures are
unchanged by it (M1 to M5 equal; `name_refs` 3 to 8 % larger).

A C++ explicit specialisation is its template's class, a class nested in
a class body is extracted, and an out-of-class member is owned by its
qualifier's classes (less the file's namespaces).

## Routes beyond Python (index format 13)

Audit 4, item 9: `Endpoint` nodes came from Python alone, so
`trace_request_flow` and `explain_architecture` were empty for Express,
Spring, ASP.NET Core and Go services. Each extractor now finds its
framework's statically detectable routes and writes them through
`devgraph/indexer/apis/routes.py` `add_route`, in the shape a FastAPI route
has:

- An `Endpoint` named `"<METHOD> <path>"` (`*` for every method), with
  `framework` and `source`, claimed by the file (a shared node, as before:
  two files routing `GET /x` both claim it).
- A file-less handler stub `Function` (`type: handler`, `source`), claimed
  the same way, and `IMPLEMENTS` to it (`to_file` "").
- `IMPLEMENTS` to the handler. A handler declared in the route's file is
  pinned to that file and its owner (a Spring or ASP.NET action's class, a
  top-level JS function's ""), so the file writes both ends. A handler
  declared elsewhere is pinned where a call to it would go: `call_rows`
  with `rel_type="IMPLEMENTS"` over the pins the language's call resolver
  gives the name (an ES named import's candidate files and folder at owner
  "", a default import's or a typed value's member at its class's owner
  when the call resolver names one (index format 18), else any, a Go
  identifier's own file or package directory, a Go `pkg.H` the package's).
  Such an edge is pinned to another file, so `name_ref_properties` records
  it and G5 relinks it when a batch adds the handler; nothing else is new
  for incremental equals fresh. A name nothing resolves keeps only the stub.
  An inline handler (arrow function, lambda, function literal) has no node:
  its `Endpoint` implements nothing.

A route on a router or group whose mount its file doesn't show has an
unknown prefix (an Express router another file mounts is resolved since
index format 18: "Express routers mounted from another file", below). Its Endpoint is the file's own, named
`"<METHOD> ?<path> (<file>)"` (`routes.unmounted_endpoint`), so no two files'
`GET /` meet in one Endpoint that no request reaches as such, and
`trace_request_flow` never starts from such a hub (a bare `/` does not
match it; its full name does). Unknown means: an Express router (a
`Router()`, a Koa `new Router()`, a name-only `router`/`...Router`) the
file doesn't mount on its app; a Go receiver of a router package's group
type (`*gin.RouterGroup`, `gin.IRouter`, `*echo.Group`) or one nothing
types; an
ASP.NET builder that is no `WebApplication` (`IEndpointRouteBuilder`
parameters). A name-only `app`/`fastify`, an `express()`/`Fastify()` app,
a router package's root constructor or type (`gin.Default()`,
`*gin.Engine`, `http.NewServeMux()`, `chi.NewRouter()`, `mux.NewRouter()`,
a gorilla `*mux.Router` or `chi.Router` parameter or struct field,
`echo.New()`, `fiber.New()`) and a `WebApplication` are roots.

A receiver is followed to its nearest declaration before the call: in
the same function, then the enclosing ones, then the file's (Go and
ASP.NET; a same-named group in another function is another group, and in
Go one declared in a block or function literal that doesn't hold the
call is not it). A Go receiver declared from another (`v := s.router`)
is followed to it; an ASP.NET chained receiver
(`app.MapGroup("/x").MapGet(...)`) is read as its group. Only a router
package itself (or its major version, `chi/v5`) names routers, never one
of its subpackages (`fiber/v2/middleware/cache`). A dot import of one
(index format 18) is the qualifier "": its bare constructor (`r :=
NewRouter()`, unless the file declares the name) is a root router, its
bare router type (`m *Mux`) a root or mounted one as a qualified one, and
with a dot-imported net/http a bare `HandleFunc`/`Handle` (unless the
file declares it) registers on the DefaultServeMux. A verb-named call on another package
(`cache.Get("/k", load)`) is no route. An absolute ASP.NET action route
is one Endpoint whatever the class's `[Route]`s.

Forms read, all with literal paths only (a variable, constant or
concatenation in a path or prefix skips the route):

| Framework | Forms | Prefix |
|---|---|---|
| Express (and Koa-router, Fastify) | `r.get/post/put/delete/patch/options/head/all('/x', ..., h)`, `r.route('/x').get(h)`, Koa's named `r.get('name', '/x', h)`; `r` an app or router the file makes (`express()`, `Router()`, `new Router()`, `Fastify()`, held by a name or a class's `this.router`) or named `app`, `router`, `fastify`, `...Router`; the path starts with "/" and the last argument is the handler | `app.use('/api', r)` in the same file |
| Spring (Java, Kotlin) | methods of a `@RestController`/`@Controller` class with `@GetMapping`...`@PatchMapping` or `@RequestMapping(method = ...)` (none: `*`), `value`/`path`, arrays | the class's `@RequestMapping` |
| ASP.NET Core | actions of an `[ApiController]`, `[Route]` or `...Controller` class with `[HttpGet("x")]`... or `[Route("x")]` (`*`), every one of repeated attributes, `[controller]`/`[action]` filled in in any case (`Async` dropped), a template starting with "/" or "~/" absolute; `app.MapGet/MapPost/MapPut/MapDelete/MapPatch("/x", h)`, `h` a method group of the file | each of the class's `[Route]`s; a `MapGroup("/x")` declaration (or assignment) of the same scope |
| Go (a file importing `net/http` or gin, echo, chi, gorilla/mux, httprouter, fiber) | `Handle`/`HandleFunc` with a Go 1.22 pattern (`"GET /x"`, no method `*`, a host dropped, a subtree's trailing "/" kept), gorilla's chained `.Methods("GET", ...)`, gin/echo `GET`...`Any`, chi/fiber `Get`..., gin `Handle("GET", "/x", h)`, chi `Method("GET", "/x", h)`; `http.HandlerFunc(h)` is `h`; a verb-named call counts only on a router or with a handler that is a function of the file, an imported package's exported name or a typed value's method, and never with a string after the path (`client.Post("/x", "text/plain", b)`, `cache.Get("/k", v)` are no routes) | gin/echo `v := r.Group("/x")`, gorilla `r.PathPrefix("/x").Subrouter()`, chi `r.Route("/x", func(r chi.Router) {...})`, in the same function or an enclosing one |

Not read: Fastify's `route({...})` object form, echo's `Add`, chi
`Mount`, Koa's `app.use(router.routes())` as a mount, ASP.NET `MapMethods`
and conventional routing, Spring functional routes (`RouterFunction`),
Kotlin Ktor. Express, Koa and Fastify routes all carry `framework:
Express`. A compose change (a `build:` added, moved or dropped) re-owns
the route files outside its batch (below, "Compose changes re-own the files
outside the batch").

The dispatcher treats these files as it treats a Python file. Their replace
is a `service_api` one (`engine.SERVICE_API_SUFFIXES`, also checked on
deletion), so set (c) of the graph-accuracy design unclaims the Endpoint
edges they wrote. Their extraction's `Endpoint` nodes join the
owning-service pass (`_endpoint_service_relationships`), so a service's
Endpoints `CALLS` its compose `Service` as Python's do. Index format 13
makes an older graph rescan to gain them. The accuracy fuzz adds, removes
and changes Express and Spring routes and moves their handlers
(`op_route`, `op_route_change`, `op_route_move`), and
`tests/indexer/test_routes_live.py` checks `explain_architecture` and
`trace_request_flow` per framework.

**Cost.** A file's replace used to find the shared nodes it claimed by
reading every shared node of the repository (`_UNCLAIM_SOURCE_CYPHER`), and
a route file's set (c) every Endpoint's edges, so a full scan was quadratic
in the repository's Endpoints and stubs. A code file's Module now records
the shared nodes its replace claimed (`shared_claims`, hidden and reserved
like `claims`), and the next replace or delete seeks just those by name; a
code file with no Module yet claims nothing, and a file with no record (a
compose file, a Module an older index wrote) still takes the scan. The
record is read under the Module's write lock (a no-op `SET`), so two
overlapping writes of one file, from two processes, take turns: the second
unclaims what the first claimed. A record can still lose a claim (a version
without the lock, a write cut between claim and record); a full scan heals
it, rebuilding every code Module's record from the shared nodes' own
`sources` before it prunes deleted files or writes (`rebuild_claim_records`,
one read and a write of only the records that differ, so a scan that
changes nothing locks no Module), and a file's delete reads every claim,
not its record (deletes are rare).
Set (c)'s `USES` unclaim ran for Python files only, the only writers of
`USES` until index format 18 (datastore clients beyond Python, below); it
now runs for every `service_api` file. Python
route files gain the same. Measured on a synthetic repository of 2,000
files (400 each of TS, Java, Go, C# and Python, a quarter of the first four
with routes: 1,600 Endpoints), `full_scan` against the shared local Neo4j:

| | before (format 12) | routes, scan unclaim | routes, recorded claims |
|---|---|---|---|
| 2,000 files, no routes in them | 74-75 s | | 75-76 s |
| 2,000 files, 1,600 Endpoints | 74-75 s (none extracted) | 113-116 s | 89-93 s |
| 1,000 files, 800 Endpoints (the same files without routes: 41 s) | not run | 52 s | 46 s |

A repository with no routes costs the same. What remains is linear in the
routes (their Endpoint and stub claims, about 5.5 ms per label per route
file, and their edges).

### Django class-based views (index format 20)

A Django route to a class-based view (`views.OrderView.as_view()`) had
its `IMPLEMENTS` go to a `Function` named after the class, pinned to the
view's module at owner "": no node matches that, so the route implemented
only its file-less stub, which `_fold_target` could not fold and every
answer showed as an unresolved handler beside the `Class`
(`describe_node("OrderView")` was ambiguous between the two).

- **The Class.** A view written as `X.as_view(...)` is the top-level
  `Class` `X` (owner "") of the module it is imported from, pinned as a
  function view is (graph-accuracy spec R3): `views.X` with `from . import
  views` is `<dir>/views.py`'s or `views/__init__.py`'s, a from-imported
  `X` its module's (under the name it imports, an alias's too, which the
  stub now also takes). A bare name nothing imports is the route file's
  own top-level class when it defines one, pinned to that file; any other
  (a star import's) links every `Class` of its name, unpinned, and the
  stub.
- **Its HTTP method handlers.** The same pins also link the class's
  methods (owner the class) that serve the route: Django's
  `View.http_method_names` (`get`, `post`, `put`, `patch`, `delete`,
  `head`, `options`, `trace`) for a plain `as_view()`, or the actions a
  DRF ViewSet's literal `as_view({"get": "list", ...})` mapping names. A
  method links when the class defines it or inherits it from a class of
  its own file (an owned pin's heir keys, index format 18); one inherited
  from Django itself or from another file has no node of the class and
  links nothing. An unpinned bare-name view links no method: an unpinned
  owner would match every class's. The Class alone would leave
  `trace_request_flow` to reach the code through `CONTAINS`; the methods
  say which code serves a request and put it in `list_endpoints`.
- **Folding.** `_fold_target` folds a stub into the one Function or Class
  of its name that its Endpoint also implements, else the one Function or
  Class of its name in its route files. `describe_node` of the Class lists its stubs'
  relationships as its own (`_FOLDED_STUBS`), and `list_endpoints` gives
  each handler's `label`.
- **DRF.** `APIView.as_view()` is the same shape. A ViewSet registered on
  a router (`router.register("items", ItemViewSet)`, then
  `path("", include(router.urls))`) is not read: its URLs come from the
  router at run time, so it has no Endpoint.
- **Incremental equals fresh.** The pins are recorded in `name_refs` like
  every pinned route edge, so a batch adding the class or a method
  relinks it, and a route file whose stub's name a batch adds as a Class
  is re-read as for a Function (`_handler_stub_referrers`).
  `test_extract_coverage_live.py` renames the class, moves it into a
  package and back, deletes and restores its module and adds a method,
  comparing with a fresh scan each time.

## Datastore clients beyond Python (index format 18)

`Service -USES->` a datastore came from Python files alone, so
`get_service_dependencies` and `explain_architecture` listed no store for a
JS/TS, Java, Kotlin, C# or Go service. `datastores/extractor.py`
`client_datastores` now reads those languages' clients, by literal text only
(a URL, driver or namespace built at run time is not read). The text is the
file's code with its comments removed and its string literals masked
(`_masked`, a small lexer per family: `'`/`"`, `` ` `` in JS/TS and Go,
Java/Kotlin text blocks, C# verbatim strings), so a comment never counts
and a string counts only as an import specifier (an ES `import`/`export
... from`, not a type-only `import type`/`export type`; `require`/`import()`;
a Go import path) or, for a connection URL, a JDBC URL or a Go driver
name, as an argument passed straight to a call (`new Pool('postgres://...')`,
`DriverManager.getConnection("jdbc:...")`, `sql.Open("postgres", ...)`; a URL
assigned to a variable or field first is not read). A Spring Data
repository counts only in supertype position (after `extends`,
`implements` or `:`, maybe after other supertypes). A file under a
conventional test folder (`test/`, `tests/`, `__tests__/`, `src/test/`,
`*.Tests/`) names no store, its fixtures being no service's use:

| Language | Read |
|---|---|
| JS/TS | a module specifier (`import`, `from`, `require`, `import()`) of `pg`, `postgres`, `mysql`, `mysql2`, `mongodb`, `mongoose`, `sqlite3`, `better-sqlite3`, `neo4j-driver`, `@prisma/client`, `redis`, `ioredis`, `kafkajs`, `amqplib`, `@aws-sdk/client-s3`, `@aws-sdk/client-sqs` (or a subpath: `mysql2/promise`) |
| Java, Kotlin | an import of (or below) `org.postgresql`, `com.mongodb`, Spring Data's `mongodb`/`jpa`/`neo4j`/`redis`, `org.neo4j.driver`, `redis.clients.jedis` (Jedis), `io.lettuce.core` (Lettuce), `org.apache.kafka.clients`, `org.springframework.kafka`, `com.rabbitmq.client`, `org.springframework.amqp`, the AWS SDK's `s3`/`sqs`; a JDBC URL passed to a call (`"jdbc:postgresql:`, `mysql`, `mariadb`, `sqlserver`, `sqlite`, `oracle`, `h2`); a Spring Data repository supertype (`JpaRepository<`, `MongoRepository<`, `ReactiveMongoRepository<`, `Neo4jRepository<`, `CassandraRepository<`, `ElasticsearchRepository<`) |
| C# | a `using` of (or below) `Npgsql`, `Microsoft.Data.SqlClient`/`System.Data.SqlClient`, `Microsoft.Data.Sqlite`, `MySqlConnector`, `MySql.Data`, `MongoDB.Driver`, `StackExchange.Redis`, `Confluent.Kafka`, `RabbitMQ.Client`, `Amazon.S3`, `Amazon.SQS`; an EF Core provider call (`.UseNpgsql(`, `UseSqlServer`, `UseSqlite`, `UseMySql`, `UseCosmos`, `UseOracle`) and a `DbContext` subclass |
| Go | an import of (or below) `github.com/lib/pq`, `github.com/jackc/pgx`, `github.com/go-sql-driver/mysql`, `github.com/mattn/go-sqlite3`, `modernc.org/sqlite`, `go.mongodb.org/mongo-driver`, `github.com/redis/go-redis`, `github.com/go-redis/redis`, `github.com/IBM/sarama`, `github.com/Shopify/sarama`, `github.com/segmentio/kafka-go`, `github.com/rabbitmq/amqp091-go`, the AWS SDK v2's `s3`/`sqs`; `sql.Open("<driver>", ...)` with a literal driver (`postgres`, `pgx`, `mysql`, `sqlite3`, `sqlserver`) |
| all five | a connection URL passed to a call, at the start of its string literal: `postgres://`, `postgresql://`, `mysql://`, `mongodb://`, `mongodb+srv://`, `redis://`, `rediss://`, `amqp://`, `amqps://` |

A store's identity is a Python client's: its provider, under its type's
label (`Database`, `Cache`, `Queue`; `VectorStore` has no client here), so
every file and service using PostgreSQL meets one shared `PostgreSQL`
`Database`, whatever the language, and the URL's host, database name or
environment variable never splits it. An ORM whose store is configured
outside the code is named by the ORM (`Prisma`, `JPA`, `EF Core`), as a
Python LangChain vector store is `LangChain`; S3 is a `Database` named
`S3`, SQS a `Queue`. A file's node per provider takes the alphabetically
first of its clients of that provider as its `library`, and its `source`.

The dispatcher appends these nodes to the file's extraction
(`_datastore_nodes(..., language)`), so its replace claims them as a Python
file's (G3: re-claimed in place, a store's incoming edges kept). Their
`USES` from the owning compose `Service` are written in the owning-service
pass (`_datastore_service_relationships`, the edge a Python file's
`_owning_service_relationships` writes), with the file as origin; set (c)'s
`USES` unclaim now runs for every `service_api` file, on a replace and on a
delete. Nothing else is new for incremental equals fresh: a node and its
edge are functions of the file's text and the compose files. The fuzz
toggles clients' imports in Python, TS, Java, Kotlin, C# and Go files
(`op_datastore`); `tests/indexer/test_client_datastores_live.py` scans one
service per language. C++ and Rust clients are not read.

**Python from the syntax tree** (index format 20, audit 8 item 3). The
Python `DatastoreExtractor` was a regular expression over the whole file:
`import\s+(\S+)` matched by prefix (`redistricting` was Redis), and
`postgresql://`, `pymysql`, `MongoClient(` and the like anywhere in the
text, a comment, a docstring or the extractor's own tables, made a store,
and a test folder was not skipped (DevGraph's own repository had 13
stores, two real). It now walks the Tree-sitter Python tree: an
`import`/`from ... import` of a client module (`DATASTORE_LIBRARY_MAP`: the
module itself or one below it, `redis.asyncio`; never a relative import;
`pymysql` for MySQL), a known client call on a real call node
(`CLIENT_CALLS`: `psycopg2.connect(...)`, `redis.Redis(...)`), and a
connection URL that is a call's argument, positional or keyword
(`create_engine("postgresql://...")`, the schemes above, and for every
language SQLAlchemy's `dialect+driver` form, `postgresql+asyncpg://`); an
f-string counts by its literal start (`f"postgresql://{host}/db"`), an
implicit concatenation not at all. A file under a test folder (`TEST_DIR`) names
none, and its node per provider takes the alphabetically first library, as
the other languages' do. Both dispatcher call sites (`_datastore_nodes`,
`_owning_service_relationships`) go through it.
`tests/indexer/test_python_datastores_syntax.py` and
`test_python_datastores_live.py`.

## Express routers mounted from another file (index format 18)

`app.use('/api', usersRouter)`, with `usersRouter` imported from
`routes/users.js`, left that file's routes unmounted: `GET ?/:id
(routes/users.js)`. A route's full path now comes from both files, and
must still equal a fresh scan whichever of them changes, so neither file's
extraction reads the other: each records its half on its Module, and the
dispatcher joins them.

- **The mounting file** records `route_mounts`, one entry per mount of an
  imported router (`app.use('/p', x)`, the last argument, so middleware
  before it is skipped; `x` a default or named ES import, a `require` or
  its destructured name, or an inline `require('./r')`): its base, the
  prefix, the export name (`default` for a default import or a whole
  `require`) and the files the specifier can name
  (`_resolve_module_specifier`: every candidate, none checked against the
  disk, so a mount is a function of the file's text and resolver
  configuration). The base is "" and the prefix the full one when the
  router it is mounted on has a known prefix (an app, or a router the file
  mounts itself); it is an export name of the file's own router when that
  router is exported and not mounted in the file (the prefix is then
  relative to that router's mounts: a chain); otherwise the mount is not
  recorded.
- **The router file** records `route_exports`: (export name, method, path)
  for each route on a router it exports (`export default r`, `export const
  r`, `export { r as x }`, `module.exports = r`, `module.exports = { r }`,
  `exports.x = r`) and doesn't mount itself. Its extraction still names
  them `?` Endpoints; a route whose `?` name another unmounted route of the
  file shares is not deferred.
- **The dispatcher** (`index_paths`) keeps a `MountTable` of every Module's
  `route_mounts` (read once per batch when a file first needs it, then kept
  current with each JS/TS file the batch writes). A router file's prefixes
  per export are the mounts naming one of its candidate files and that
  export, a base other than "" followed to the mounting file's own mounts,
  at most `MAX_MOUNT_HOPS` (2) cross-file mounts in all: an app mounting an
  api router that mounts the users router resolves; a third level stays
  unmounted, and a cycle ends there. `mount_routes` renames each deferred
  route's Endpoint (and its `IMPLEMENTS`, and so its owning-service `CALLS`)
  to one per prefix, `"<METHOD> <prefix><path>"`; a router mounted nowhere
  keeps its `?` Endpoint. The Module records the prefixes it used as
  `route_prefixes`. The three properties are lists, absent when empty,
  hidden and reserved like `name_refs`.
- **Settling.** A router file's output depends on other files' mounts, so
  after pass 1 (when a batch file had or has any of the three properties)
  the batch reads every Module holding one, rebuilds the table and
  re-indexes each file whose `route_prefixes` differ from what the table
  gives its `route_exports` (`_stale_mounts`): a router file the batch
  wrote before the file mounting it (path order), and router files outside
  the batch whose mounts the batch changed, which join the batch (their
  previous nodes snapshotted first, so "added" stays exact). The records
  themselves are text, so one round settles. `remove_paths` does the same
  after deleting a file that held one (with `docs_path`/`mentions_enabled`,
  which it now takes, as the watcher passes them). Moving a router file is a
  delete and an add: the mount's candidates name the old path, so the moved
  router is unmounted (fresh and incremental alike), and a mount's own
  change re-indexes the routers under both its old and new targets.

Cost: a batch with no mounts or exported routes before or after pays one
indexed read of its own Modules; one that has them reads every Module of
the repository holding a mount property (one indexed scan) and re-indexes
only the router files whose prefixes changed. A full scan re-indexes the
router files that sort before the file mounting them. The fuzz mounts
another TS file's exported router on an app or on the file's own exported
router (so chains and cycles), unmounts it, changes the prefix, stops
exporting it, and moves the router file (`op_mount`, `op_mount_prefix`,
`op_export_router`, `op_move_router`); `tests/indexer/test_express_mounts_live.py`
walks a two-level chain through prefix changes, a second mount, an
unexported middle, a move and deletions.

Not read: a mount through a variable path, a router held in an object
(`routers.users`), a namespace import's member (`app.use('/x', ns.r)`),
`export default Router()` (no name to route on), Koa's
`app.use(router.routes())`, and other frameworks' cross-file groups (a gin
`*gin.RouterGroup` parameter, ASP.NET `IEndpointRouteBuilder`).

## Compose changes re-own the files outside the batch

The owning-service edges (`Endpoint -CALLS-> Service`, `Service -USES->`
a datastore) were written only for the batch's own files, so a batch of a
compose file alone, giving a service a `build:`, moving it or taking it
away, left every other file's edges at its old owner (the graph-accuracy
design's out-of-scope "compose-only batch"). Those edges are a function of
two things the graph holds: which files claim each Endpoint and datastore
node (its `sources`) and the Services' build contexts (the longest one
containing the file wins, `_match_owning_service`; since index format 20 a
context is resolved from the compose file's folder and "", the repository
root, contains every file). So a batch holding a
compose file (`_is_compose`), and a `remove_paths` deleting one, run
`dispatch._relink_owning_services` after the owning-service pass: one read
of every claimed Endpoint and datastore node with its claiming files and of
every owning-service edge with its `origins` (`engine.owning_service_state`),
then, for each claiming file outside the batch, the edges it should write
now are compared with the ones listing it: a stale one is unclaimed (the
file leaves its `origins`, the edge goes with the last,
`unclaim_owning_service_edges`) and a missing one written with the file as
origin. No file is read. A full scan skips it (no file is outside its
batch). A compose replace also clears a `build_context` the service no
longer has: the node is updated in place (`SET +=`), so the stale property
kept its old owner before. The cost is one read proportional to the
repository's Endpoints and datastore claims per batch holding a compose
file; other batches pay nothing. The fuzz gives compose services `build:`
contexts that come, move (to nested folders: the nearest wins) and go
(`op_build`); `tests/indexer/test_routes_live.py` moves one by hand.

## No reverse dependents

With Go, Java and Kotlin resolved, every code edge is a function of its
writer's text, its resolver configuration and whether its target exists
(for a C# shadow tier, which nodes of its target's name exist), so
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

### Results after C# match keys (index format 10)

Stage 1 of the C# namespace design (`.superpowers/csharp-ns-design.md`):
match keys and one `/.?.cs^@keys` pin per site, the project's `<Using>`
items and `global using`s read, no `<ProjectReference>` and no shadow
tiers. BEFORE is this branch's code before the extractor change.

| csharp fixture | Target | BEFORE | AFTER | |
|---|---|---|---|---|
| M1 resolved multi-target | < 2 % | 0 % (1 resolved, 18 package, 4 name) | 0 % (1 resolved, 21 package, 2 name) | met |
| M2 linked precision | ≥ 98 %, 0 | 89 % (17/19); 0 | 95 % (21/22); 0 | short |
| M3 `IMPORTS` precision / recall | ≥ 98 % / 95 % | 80 % / 94 % (16/17) | 94 % (17/18) / 100 % (17/17) | short |
| M4 `CALLS` recall | ≥ 95 % | 90 % (linked 81 %) | 100 % (linked 100 %) | met |
| M5 named sites | all | 27/31 | 30/31 | short |
| M6 largest `name_refs` | < 1 MiB | 1,109 B | 4,612 B | met |

- **What is left is shadowing.** `UserService.Register` (namespace
  `Shop.Domain.Services`, `using Legacy.Accounts`) still links both
  `User`s' `Display`, and `UserService.cs` imports both `User.cs`: the
  enclosing namespace's `User` hides the using's in C#, but the pin's keys
  are a union. Everything else the slice C notes listed is fixed:
  `UserMapper.Describe` links only Legacy's `Display`, `UserMapper` is
  found in `Mappers.cs`, the partial `Order`'s `Subtotal`/`Tax` link
  `Order.Pricing.cs`, the alias `LegacyUser` imports Legacy's `User` alone,
  and `user.Initials()` imports `UserExtensions.cs`. These match the
  design's estimates (M2 ~96-97 %, M3 17/18 and 17/17, M5 30/31);
  `test_measure_fixtures` keeps C# a strict xfail until shadow tiers.
- **Synthetic multi-project repository** (`measure_call_graph.py
  --cs-synthetic`, seed 1): N library projects declaring the same class
  names in their own namespaces, each service calling its own project's
  types and one other project's `Gateway.Send()`, with realistic usings
  (ten `global using`s per project, six of them `System`/`Microsoft`; 8 to
  15 usings per service file); linked `CALLS` against the generator's
  truth. BEFORE is the code before match keys, FIRST CUT the first match
  key change (one pin per site, its type and extension keys together,
  dropping the `@` over 32 keys, `System` usings counted), AFTER this one.
  A capped row is a key-pinned CALLS row whose group went over 32 keys.

| `--cs-synthetic` | BEFORE | FIRST CUT | AFTER |
|---|---|---|---|
| 10 projects: capped rows / key-pinned rows | n/a | 64 % (480 / 745) | 0 % (0 / 1,225) |
| 10 projects: cross-project wrong links / linked | 88.3 % (6,480 / 7,335) | 83.3 % (4,320 / 5,185) | 0 % (0 / 865) |
| 10 projects: linked precision, recall; `IMPORTS` P / R | 11.7 %, 98.8 %; 10.2 % / 83.9 % | 16.7 %, 100 %; 100 % / 100 % | 100 %, 100 %; 100 % / 100 % |
| 50 projects: capped rows / key-pinned rows | n/a | 66 % (2,401 / 3,663) | 0.4 % (25 / 6,087) |
| 50 projects: cross-project wrong links / linked | 97.7 % (176,400 / 180,637) | 96.5 % (117,625 / 121,912) | 0 % (0 / 4,287) |
| 50 projects: linked precision, recall; `IMPORTS` P / R | 2.3 %, 98.8 %; 2.0 % / 83.7 % | 3.5 %, 100 %; 100 % / 100 % | 100 %, 100 %; 100 % / 100 % |
| 50 projects: largest `name_refs` (gate < 1 MiB) | 1,955 B | 8,802 B | 8,802 B (total 0.54 MB to 4.0 MB) |
| 50 projects: `full_scan` | 92.5 s | 82.1 s | 70.0 s (fewer edges) |

  The 25 capped rows at 50 projects are `Startup.cs`'s: it has a `using`
  of every project's Api namespace (over 50 candidates), so its
  `ModuleN.Start()` sites fall back to `/ModuleN.cs^`, which is right
  here; the first cut linked every project's `Start()` from them (25
  wrong). With the realistic usings the first cut overflowed two thirds
  of its keyed rows (a typed receiver counted each namespace twice, and
  `System` usings counted) and linked any C# file from them, worse than
  BEFORE's class file pins.
- **Cost.** The fixture's `full_scan`, BEFORE and AFTER alternating in
  their own processes (12 scans after two warm-ups each, two series, the
  shared Neo4j otherwise idle): fastest 0.97 s and 1.03 s (+6 %); the
  medians are bimodal (about 1.0 s or 1.6 s, the index-wait polling of
  `provision_repository_schema`) and were 1.03/1.81 s and 1.61/2.37 s, so
  they settle nothing. A profiled scan of each issues the same queries
  bar two (the match-key write and its settle) and spends 0.07 s more
  upserting relationships (the extension `IMPORTS` rows and the `@` test).
  Extraction alone, 202 synthetic files: 0.22 s to 0.32 s. On the
  synthetic repositories the scan got faster (see the table: far fewer
  edges). C# `name_refs` grow about 4x to 7x (the fixture's largest 1.1 KB
  to 4.6 KB), as each entry stores its keys once.


### Results after C# shadow tiers (index format 14)

See the C# section's Shadow tiers. BEFORE is index format 13 (match keys,
one union per site), AFTER this branch.

| csharp fixture | Target | BEFORE | AFTER | |
|---|---|---|---|---|
| M1 resolved multi-target | < 2 % | 0 % | 0 % | met |
| M2 linked precision | ≥ 98 %, 0 | 95 % (21/22); 0 | 100 %; 0 | met |
| M3 `IMPORTS` precision / recall | ≥ 98 % / 95 % | 94 % (17/18) / 100 % | 100 % (17/17) / 100 % | met |
| M4 `CALLS` recall | ≥ 95 % | 100 % (linked 100 %) | 100 % (linked 100 %) | met |
| M5 named sites | all | 30/31 | 31/31 | met |
| M6 largest `name_refs` | < 1 MiB | 4,771 B (total 13,192 B) | 4,925 B (total 13,346 B) | met |

- `UserService.Register` now links Shop.Domain's `User.Display` alone and
  `UserService.cs` imports only Models/User.cs; `test_measure_fixtures`
  has no C# xfail left. The `name_refs` grow a little where an alias is
  used (its pin also names the namespaces nearer than its declaration).
  After the review fixes (reruns limited to the groups a node's keys meet,
  the alias at its level) the 50-project synthetic scanned in 61.7 s, its
  links and `name_refs` unchanged.
- **Synthetic multi-project repository** (`--cs-synthetic`, seed 1):
  nothing in it shadows, so links are unchanged (precision, recall and
  `IMPORTS` 100 %, 0 cross-project wrong links at 10 and 50 projects; 25
  capped rows at 50, Startup.cs's, un-tiered as before). A `;` takes a
  `,`'s place in a tiered pin, so `name_refs` are byte for byte the same
  size: largest 7,549 B (10 projects) and 8,970 B (50; total 4.10 MB).
- **Cost**, BEFORE and AFTER alternating in their own processes on the
  otherwise idle shared Neo4j: `full_scan` of the 10-project synthetic
  12.0/11.1 s before, 10.8/10.7 s after; 50 projects 76.9/69.1 s before,
  63.9/64.4 s after (pass 1 no longer writes the tiered rows that pass 2
  writes again). The fixture, 12 scans after two warm-ups, two series:
  fastest 0.92 s before and 0.90 s after, medians 0.97/1.00 s and
  0.94/0.94 s.
- Seeds 1 to 12 of the graph-accuracy fuzz (15 steps each, `cs_shadow`
  among its operations) and a scripted run of that operation equal a
  fresh scan after every step.

### Results after C# project visibility (index format 15)

See the C# section's Project visibility. BEFORE is index format 14
(shadow tiers), AFTER this branch, both measured on the generator as
extended here: `--cs-synthetic` now groups its projects in clusters of
five that `<ProjectReference>` earlier members of their cluster (one or
two each, written with `\`, so each reaches the first transitively, and
no cluster another), the App project references every project, a
service's cross-project `Gateway.Send()` goes to a project its own
reaches, and each cluster's first project declares `Co.Shared.Clock`
(`Co.Shared` a global using of every project), which every service calls:
the same namespace and type in projects that do not reference each other.

| | BEFORE | AFTER |
|---|---|---|
| csharp fixture (App and Tests reference Core) | all targets met; M2 100 %, `IMPORTS` 100 % / 100 %, 31/31 | the same |
| 10 projects: linked precision (wrong, all cross-project) | 89.1 % (120 / 1,105) | 100 % (0 / 985) |
| 10 projects: `IMPORTS` precision / recall | 87.8 % / 100 % | 100 % / 100 % |
| 50 projects: linked precision (wrong, all cross-project) | 47.5 % (5,400 / 10,287) | 100 % (0 / 4,887) |
| 50 projects: `IMPORTS` precision / recall | 44.3 % / 100 % | 100 % / 100 % |
| CALLS recall (10 and 50 projects) | 100 % | 100 % |
| 50 projects: largest / total `name_refs` | 9,027 B / 4.55 MB | the same |
| 50 projects: largest / total `name_ref_visible` | none | 51 projects, 347 B (App) / 13.8 KB |

- The wrong links BEFORE are every other cluster's `Clock.Now()` and
  `Clock` import (the namespace and name match; only the project tells
  them apart). The visible set is stored once per Module, so `name_refs`
  are byte for byte the same; the largest visible set is the App
  project's (itself and the 50 it references).
- **Cost**, BEFORE and AFTER alternating in their own processes on the
  otherwise idle shared Neo4j: the fixture's `full_scan`, eight scans
  each after a warm-up, fastest 0.93 s and 0.93 s, medians 0.98 s and
  0.97 s; the 10-project synthetic 11.2/10.7 s before, 10.8/10.6 s after;
  50 projects 73.3/80.5 s before, 67.3/68.4 s after (fewer edges to
  write). Pass 1 no longer writes a project file's path-pinned rows,
  which pass 2 writes anyway.
- Seeds 1 to 12 of the graph-accuracy fuzz (15 steps each, with
  `cs_reference` toggling a `<ProjectReference>`, a cycle included, and
  `cs_project_move` moving a file whose class another project declares
  too) and the scripted fuzz runs equal a fresh scan after every step.

### Results after JS/TS workspace packages (index format 17)

See the Resolver configuration's JS/TS workspace packages and the JS/TS
section. BEFORE is index format 16, AFTER this change, measured with
`--fixture`. A new fixture, `tests/fixtures/callgraph/tsmono`, is an npm
workspaces monorepo (`packages/*`, `apps/*`) with no tsconfig `paths`:
imports by package name through `main`, `module`, an `exports` condition
and subpath, a package subpath, barrels and a namespace import, a
`package.json` under no workspace glob (external), and lodash.

| | BEFORE | AFTER |
|---|---|---|
| ts fixture | all targets met; `IMPORTS` 100 % / 100 %, CALLS recall 100 %, 43/43 | the same |
| tsmono fixture: `IMPORTS` precision / recall | 100 % / 38.5 % | 100 % / 100 % |
| tsmono fixture: CALLS recall, linked precision | 35.7 %, 100 % | 100 %, 100 % |
| tsmono fixture: named sites | 3/12 | 12/12 |

The fuzz holds a workspace package (`ws/utils`, imported by name and by
a subpath from `ws/app`, its entry a barrel): it is renamed, its `main`
moves between entries or goes, the root `package.json` switches between
the array and Yarn's form or stops declaring workspaces, a
`pnpm-workspace.yaml` comes and goes, and the package's folder is deleted.
