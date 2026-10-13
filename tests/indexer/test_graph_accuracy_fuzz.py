"""A seeded fuzz of the graph accuracy work against a live Neo4j: random edits,
deletes, restores, renames and symbol moves over a small multi-language
repository, indexed incrementally as one batch or as one batch per path, must
leave the same graph as a fresh `full_scan` after every step.

See docs/superpowers/specs/2026-10-08-graph-accuracy-design.md. CI runs three
fixed seeds of 15 steps. `DEVGRAPH_ACCURACY_FUZZ_SEEDS` (a comma list) and
`DEVGRAPH_ACCURACY_FUZZ_STEPS` lengthen it locally.

Python files import and call in every style the call resolution reads
(docs/superpowers/specs/2026-10-10-python-call-resolution-design.md): plain,
aliased, relative and star imports, a package re-exporting from its module,
a src/ layout, calls through modules, aliases and untyped values, and
`self.shared()` reaching a base in the same or another file. A class `Twin`
comes and goes beside another class of a file, repeating its method names
(both nest a `Meta` with its own base), and so do a top-level `act` and one
nested in a function, so same-named symbols of one file are told apart by
their owner; another file calls `b.Twin().act()` and `b.Base().act()`, one
owner's member across files. go/b gains a type whose methods are named like
the package's functions, which other files call by package.

TS files import through a tsconfig path alias, and the resolver configuration
changes under them: the tsconfig is rewritten (its own `paths`, `extends` to
an odd-named base, `references`), its base is edited or its folder deleted, a
root or nested go.mod comes and goes, and a config under an ignored folder is
edited. After every batch the fuzz runs what a live batch does,
`sync_resolver_config`. C# projects change the same way: a .csproj or
Directory.Build file is written, created or deleted, and a C# file gains or
loses a `global using` (written behind a comment, `#if`, a byte order mark,
CRLF or `extern alias`), or moves to another project's folder or out of
every project (scripts/); a C# Module records its project's usings
(`_with_cs_projects`), so a change that did not re-index every C# file
would leave one stale. A C# file's namespace changes, two files swap their
classes, and a class takes the name of another file's class in another
namespace, so C# match keys change under the edges `@` pins gave them. Kotlin files hold adjacent one-line classes, which
the extractor recovers from tree-sitter-kotlin's error nodes, and import a
function or package of another one. Go, Java, Kotlin, Rust, C# and C++ calls
come in every style their resolvers read (bare, through a package, module,
crate, namespace or class, on `this`/`base`, on a typed or an untyped value);
a function moves between files of its Go or Kotlin package (and a C# method
to another file's class), a Java class moves to the same package under
another source root (another module), and a Rust crate's Cargo.toml renames
it, moves its root file or stops declaring it. An image
moves between the Dockerfile and a compose service in one step, so its
shared Container changes claimant.

Path pins (docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md)
are fuzzed ahead of the resolvers that write them: a Python file's
`# pin calls <name> <pin>` and `# pin imports <pin>` comments become its
Module's pinned CALLS and IMPORTS rows (`_with_pin_comments`), so every pin
kind is written, relinked and removed as files come and go.

The spec's "Out of scope" gaps are left out by construction:
- compose services have `image:` only, never `build:`;
- no datastore code, and routes only in Express (TS) and Spring (Java) files,
  which add, drop and change routes and move a handler (`op_route`,
  `op_route_change`, `op_route_move`), each Endpoint and handler stub
  claimed by every file routing it;
- no method moves out of its type's file: only free functions move, and the
  Go method and Rust inherent `impl` stay with their type;
- no TS partial classes (C#'s Jobs is partial over two files);
- the default `mentions_ambiguous_mode` (`all`);
- a docs note's `id` never changes in place;
- a renamed docs note's old path is removed before its new path is indexed,
  when they go in separate batches (the id moving to another file first
  leaves the old path in its edges' `origins`).
"""

import os
import random
from dataclasses import dataclass, field, replace

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer import dispatch
from devgraph.indexer.calls import call_rows, import_rows
from devgraph.indexer.dispatch import (
    full_scan,
    index_paths,
    remove_paths,
    sync_resolver_config,
)
from tests.watcher.live_helpers import (
    fresh_snapshot,
    graph_snapshot,
    live_repo_id,
    snapshot_diff,
    sweep_stale_live_repos,
)

SEEDS = [int(s) for s in os.environ.get("DEVGRAPH_ACCURACY_FUZZ_SEEDS", "1,2,3").split(",") if s.strip()]
STEPS = int(os.environ.get("DEVGRAPH_ACCURACY_FUZZ_STEPS", "15"))
DOCS = "docs"


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    sweep_stale_live_repos(engine, "zz-accfuzz")
    repo = live_repo_id("zz-accfuzz")
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


# --- the repository model --------------------------------------------------------


@dataclass
class File:
    """One file's content, as the fields the fuzz edits; `render` writes it."""

    kind: str  # py, rs, go, java, compose, docker, notes, note, static
    head: str = ""  # Go/Java package, a note's id, or a static file's text
    extra: str = ""  # fixed code that never changes (a type and its methods)
    imports: list = field(default_factory=list)
    bases: dict = field(default_factory=dict)  # class -> base or None
    funcs: dict = field(default_factory=dict)  # function -> callees
    impl: bool | None = None  # rs: `impl Display for Foo {}`, when the file has the toggle
    services: dict = field(default_factory=dict)  # compose: service -> image
    froms: list = field(default_factory=list)
    mentions: list = field(default_factory=list)
    links: list = field(default_factory=list)
    supersedes: str | None = None
    pins: list = field(default_factory=list)  # py: "calls <name> <pin>" / "imports <pin>"
    methods: list = field(default_factory=list)  # cpp: out-of-class definitions, "Widget::draw"
    block: bool = False  # cs: a block namespace, its `using`s inside it
    routes: list = field(default_factory=list)  # ts/java: (method, path, handler) routes

    def copy(self):
        return replace(
            self, imports=list(self.imports), bases=dict(self.bases),
            funcs={k: list(v) for k, v in self.funcs.items()}, services=dict(self.services),
            froms=list(self.froms), mentions=list(self.mentions), links=list(self.links), pins=list(self.pins),
            methods=list(self.methods), routes=list(self.routes),
        )


def py_class(name: str, base: str | None) -> str:
    """A class whose `act` calls `self.shared()`, which only `Base` and
    `Twin` define: a `Twin` beside another class in one file repeats its
    method names (two `act`s, maybe two `shared`s, each its own node)."""
    head = f"\n\nclass {name}({base}):\n" if base else f"\n\nclass {name}:\n"
    shared = "\n    def shared(self):\n        return 0\n" if name in ("Base", "Twin") else ""
    # Both also nest a `Meta` (two same-named classes of one file, each
    # with its own EXTENDS and `act`).
    meta = "\n    class Meta(Child):\n        def act(self):\n            return 1\n" if shared else ""
    return head + "    def act(self):\n        return self.shared()\n" + shared + meta


def render(f: File) -> str:
    if f.kind == "py":
        # A Python file's imports are whole statements and its calls whole
        # call expressions (`helper()`, `b.helper()`, `obj.helper()`).
        parts = [f"{m}\n" for m in f.imports] + [f"# pin {p}\n" for p in f.pins]
        parts += [py_class(c, b) for c, b in f.bases.items()]
        parts += [
            f"\n\ndef {name}():\n" + "".join(f"    {g}\n" for g in calls) + "    return 0\n"
            for name, calls in f.funcs.items()
        ]
        parts.append(f.extra)
        return "".join(parts) or "X = 1\n"
    if f.kind == "rs":
        parts = [f"{m}\n" for m in f.imports]
        parts.append("\n" + f.extra)
        if f.impl:
            parts.append("\nimpl Display for Foo {}\n")
        parts += [
            f"\npub fn {name}() {{\n" + "".join(f"    {g}();\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts)
    if f.kind == "cs":
        # One class per file (Jobs partial), with a field and a parameter
        # its methods call through as well as every other style, then any
        # fixed code (`extra`), in a file-scoped, block or no namespace.
        ((cls, base),) = f.bases.items()
        inner = [m for m in f.imports if f.block and f.head and m.startswith("using ")]
        head = "".join(f"{m}\n" for m in f.imports if m not in inner)
        body = f"public {'partial ' if cls == 'Jobs' else ''}class {cls}" + (f" : {base}" if base else "") + "\n{\n"
        body += "    private Jobs _jobs;\n"
        body += "".join(
            f"    public void {name}(Base p)\n    {{\n" + "".join(f"        {g}();\n" for g in calls) + "    }\n"
            for name, calls in f.funcs.items()
        )
        body += "}\n" + f.extra
        if not f.head:
            return head + body
        if f.block:
            return head + f"namespace {f.head}\n{{\n" + "".join(f"{m}\n" for m in inner) + body + "}\n"
        return head + f"namespace {f.head};\n\n" + body
    if f.kind == "cpp":
        # Classes, out-of-class method definitions (a Class stub for their
        # qualifier), and free functions with a typed local, so a typed
        # receiver's call has a type.
        parts = [f"{m}\n" for m in f.imports]
        parts += [f"\nclass {cls}" + (f" : public {base}" if base else "") + " {};\n" for cls, base in f.bases.items()]
        parts += [f"\nvoid {q}() {{}}\n" for q in f.methods]
        parts += [
            f"\nvoid {name}() {{\n    Widget w;\n" + "".join(f"    {g}();\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts) or "// Nothing.\n"
    if f.kind == "go":
        parts = [f"package {f.head}\n"]
        if f.imports:
            parts.append("\nimport (\n" + "".join(f'\t"{m}"\n' for m in f.imports) + ")\n")
        parts.append("\n" + f.extra)
        parts += [
            f"\nfunc {name}() {{\n" + "".join(f"\t{g}()\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts)
    if f.kind == "java":
        # A file with routes is a Spring controller under /api, each route
        # a mapping on its handler method (one the class no longer has maps nothing).
        ((cls, base),) = f.bases.items()
        parts = [f"package {f.head};\n\n"] + [f"import {m};\n" for m in f.imports]
        if f.routes:
            parts.append('\n@RestController\n@RequestMapping("/api")')
        parts.append(f"\npublic class {cls}" + (f" extends {base}" if base else "") + " {\n")
        mapped = {handler: (method, path) for method, path, handler in f.routes}
        parts += [
            (f'    @{SPRING_MAPPINGS[mapped[name][0]]}("{mapped[name][1]}")\n' if name in mapped else "")
            + f"    void {name}() {{\n" + "".join(f"        {g}();\n" for g in calls) + "    }\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts) + "}\n"
    if f.kind == "kt":
        # One-line class bodies, which tree-sitter-kotlin 1.1.0 needs recovered.
        parts = [f"package {f.head}\n\n"] + [f"import {m}\n" for m in f.imports]
        parts += [f"class {c} {{ fun {c.lower()}Run() = helper() }}\n" for c in f.bases]
        parts += [
            f"\nfun {name}() {{\n" + "".join(f"    {g}()\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts)
    if f.kind == "ts":
        parts = [f"{m}\n" for m in f.imports]
        parts += [
            f"\nexport function {name}() {{\n" + "".join(f"  {g}();\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        if f.routes:
            # An Express router: a handler of the file, an imported one, a
            # namespace's member, a name nothing defines, or inline.
            parts.append("\nconst router = Router();\n")
            parts += [f"router.{method}('{path}', {handler});\n" for method, path, handler in f.routes]
        return "".join(parts) or "export {};\n"
    if f.kind == "compose":
        return "services:\n" + "".join(f"  {s}:\n    image: {i}\n" for s, i in f.services.items()) if f.services \
            else "services: {}\n"
    if f.kind == "docker":
        return "".join(f"FROM {i}\nRUN true\n" for i in f.froms) or "# No stages.\n"
    if f.kind == "notes":
        return "# Notes\n\n" + "".join(f"Uses `{m}`.\n" for m in f.mentions)
    if f.kind == "note":
        front = ["type: design_decision", f"id: {f.head}"]
        if f.links:
            front.append("links: [" + ", ".join(f.links) + "]")
        if f.supersedes:
            front.append(f"supersedes: {f.supersedes}")
        return "---\n" + "\n".join(front) + "\n---\n# Note\n"
    return f.head


# The resolver configuration's versions, by path (None: the file is absent).
TS_CONFIGS = {
    "ts/tsconfig.json": [
        '{"extends": "./configs/base.cfg"}',
        '{"compilerOptions": {"paths": {"@/*": ["other/*"]}}}',
        '{"compilerOptions": {"baseUrl": "src"}}',
        '{"files": [], "references": [{"path": "./tsconfig.app.json"}]}',
        '{\n  // both\n  "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*", "other/*"],},},\n}',
    ],
    "ts/configs/base.cfg": [
        '{"compilerOptions": {"paths": {"@/*": ["../src/*"]}}}',
        '{"compilerOptions": {"paths": {"@/*": ["../other/*"]}}}',
        '{"compilerOptions": {"baseUrl": "../other"}}',
    ],
    "ts/tsconfig.app.json": [None, '{"extends": "./configs/base.cfg"}', '{"compilerOptions": {"baseUrl": "other"}}'],
    "ignored/tsconfig.json": ['{"compilerOptions": {"baseUrl": "."}}', '{"compilerOptions": {"baseUrl": ".."}}'],
    # go/go.mod (example.com/fz) holds go/a and go/b; a root go.mod is
    # another module around it, a nested go/b/go.mod takes go/b out of it,
    # and go.work's `replace` names go/b by a third path.
    "go.mod": [None, "module example.com/top\n", "module example.com/other\n"],
    "go/b/go.mod": [None, "module example.com/bee\n"],
    "go.work": [None, "go 1.22\n\nuse ./go\n\nreplace example.com/ext => ./go/b\n"],
    # crates/core is the crate shop_core, renamed, given another root file, or not a package at all.
    "crates/core/Cargo.toml": [
        '[package]\nname = "shop-core"\n', '[package]\nname = "store-core"\nversion = "0.2.0"\n',
        '[package]\nname = "shop-core"\n\n[lib]\npath = "src/main.rs"\n', "[workspace]\n", None,
    ],
}
# The C# projects' versions: cs/ is a project (of one or two .csproj
# files), cs/Models a nested one when its .csproj exists, and the nearest
# Directory.Build.props/.targets wrap their `<Using>` items (cs/Models' own
# props shadowing the root one). scripts/ is under no project.
CS_PROJECT = '<Project Sdk="Microsoft.NET.Sdk">\n  <ItemGroup>\n{}  </ItemGroup>\n</Project>\n'
CS_CONFIGS = {
    "cs/Shop.csproj": [
        CS_PROJECT.format(""), CS_PROJECT.format('    <Using Include="Shop.Models" />\n'),
        CS_PROJECT.format('    <Using Include="Shop.Jobs" Static="true" />\n    <Using Include="Shop.Jobs" Alias="J" />\n'),
        CS_PROJECT.format('    <Using Remove="Shop" />\n    <Using Include="Shop.App" />\n'), None,
    ],
    "cs/Shop.Tests.csproj": [None, CS_PROJECT.format(""), CS_PROJECT.format('    <Using Include="Shop.Tests" />\n')],
    "cs/Models/Models.csproj": [None, CS_PROJECT.format(""), CS_PROJECT.format('    <Using Include="Shop" />\n')],
    "Directory.Build.props": [None, CS_PROJECT.format('    <Using Include="Shop" />\n')],
    "cs/Models/Directory.Build.props": [None, CS_PROJECT.format('    <Using Include="Shop.Text" />\n')],
    "cs/Directory.Build.targets": [None, CS_PROJECT.format('    <Using Remove="Shop.Models" />\n')],
}
# A C# file's `global using`s, with what may come before one in a file's
# head (a byte order mark only counts first; elsewhere it ends the head, as
# a comment inside a directive does).
CS_GLOBAL_USINGS = [
    "global using Shop.Models;", "// Shared.\nglobal using static Shop.Jobs;", "#if NET8_0\nglobal using J = Shop.Jobs;\n#endif",
    "\ufeffglobal using Shop.Text;", "// CRLF\r\nglobal using Shop.Data;\r", "extern alias Legacy;\nglobal using Shop.Legacy;",
    "global using unsafe P = int*;", "global using Shop // cut\n.Cut;",
]
# The folders a C# file moves between: two projects and no project.
CS_DIRS = ["cs/", "cs/Models/", "scripts/"]
# Folders holding a configuration file, which the fuzz may delete whole.
CONFIG_DIRS = ["ts/configs", "go/b", "crates/core", "cs/Models"]

RS_FOO = "pub struct Foo;\n\nimpl Foo {\n    pub fn new() -> Foo {\n        Foo\n    }\n}\n"
GO_SERVER = "type Server struct{}\n\nfunc (s *Server) Run() {\n\tServe()\n}\n"
# Methods named like go/b's functions Serve and Stop.
GO_TWIN = "\ntype Twin struct{}\n\nfunc (t Twin) Serve() {}\n\nfunc (t *Twin) Stop() {\n\tt.Serve()\n}\n"
# A function nested in another, named like the classes' methods.
PY_NESTED = "\n\ndef outer():\n    def act():\n        return shared()\n    return act()\n"


def initial_repo() -> dict:
    return {
        "py/a.py": File("py", imports=["from py.b import helper", "from pkg import pkgfn", "import py.c as c"],
                        funcs={"main": ["helper()", "c.util()", "obj.helper()", "pkgfn()"]}),
        "py/b.py": File("py", imports=["from .c import util"], bases={"Base": None}, funcs={"helper": ["util()"]}),
        "py/c.py": File("py", imports=["from py import b"], bases={"Child": "b.Base"},
                        funcs={"main": ["b.helper()"], "util": []}),
        # A package re-exporting from its own module, a sibling module, and a
        # src/ layout project.
        "pkg/__init__.py": File("py", imports=["from pkg.impl import helper, pkgfn"]),
        "pkg/impl.py": File("py", funcs={"helper": [], "pkgfn": ["helper()"]}),
        "pkg/other.py": File("py", funcs={"spare": []}),
        "src/lib/core.py": File("py", imports=["from lib import util"], funcs={"core": ["util.render()"]}),
        "src/lib/util.py": File("py", funcs={"render": []}),
        "rs/foo.rs": File("rs", extra=RS_FOO, funcs={"parse": ["render"]}),
        "rs/display.rs": File("rs", extra="pub trait Display {}\n", funcs={"render": []}),
        "rs/conv.rs": File("rs", imports=["use crate::display::Display;"], impl=True,
                           funcs={"convert": ["parse", "shop_core::run"]}),
        "crates/core/Cargo.toml": File("static", head=TS_CONFIGS["crates/core/Cargo.toml"][0]),
        "crates/core/src/lib.rs": File("rs", imports=["pub mod util;"], funcs={"run": ["util::tidy"]}),
        "crates/core/src/util.rs": File("rs", funcs={"tidy": []}),
        "cs/Shop.csproj": File("static", head=CS_CONFIGS["cs/Shop.csproj"][0]),
        "cs/Jobs.cs": File("cs", head="Shop", bases={"Jobs": None}, funcs={"Run": [], "Stop": []}),
        "cs/Program.cs": File("cs", head="Shop.App", imports=["using Shop;"], bases={"Program": "Jobs"},
                              funcs={"Main": ["Jobs.Run", "Stop", "new Jobs().Stop"]}),
        "cs/Models/Base.cs": File("cs", head="Shop.Models", bases={"Base": None}, funcs={"Helper": []},
                                  extra=CS_NESTED),
        # Jobs's other part, its extension methods, a block namespace with a
        # relative `using Models;` (Shop.App.Models or Shop.Models), the
        # global namespace, and more namespaces in scope than a pin's keys.
        "cs/Jobs.Part.cs": File("cs", head="Shop", bases={"Jobs": None}, funcs={"Halt": ["Run", "Tidy"]}),
        "cs/JobsExtensions.cs": File("cs", head="Shop", bases={"Ext": None}, funcs={"Use": ["_jobs.Tidy"]},
                                     extra=CS_EXTENSIONS),
        "cs/Block.cs": File("cs", head="Shop.App", block=True, imports=["using Models;", "using Shop;"],
                            bases={"Block": None}, funcs={"Go": ["p.Helper", "_jobs.Tidy", "Outer.Inner.Run"]}),
        "cs/Global.cs": File("cs", head="", bases={"Root": None}, funcs={"Run": ["Jobs.Run", "_jobs.Stop"]}),
        "cs/Wide.cs": File("cs", head="Shop.Wide", imports=[f"using N{i};" for i in range(40)] + ["using Shop;"],
                           bases={"Wide": None}, funcs={"Spread": ["Jobs.Run", "_jobs.Stop", "p.Helper"]}),
        "scripts/Tool.cs": File("cs", head="Tools", imports=["global using Shop.Models;"], bases={"Tool": None}),
        "cpp/util/strings.h": File("cpp", funcs={"trim": []}),
        "cpp/util/strings.cpp": File("cpp", imports=['#include "strings.h"'], methods=["Widget::draw"],
                                     funcs={"upper": ["trim"]}),
        "cpp/widget.h": File("cpp", bases={"Widget": None, "Gadget": "Widget"}),
        "cpp/main.cpp": File("cpp", imports=['#include "util/strings.h"'], funcs={"main": ["util::upper", "trim"]}),
        "java/app/K.java": File("java", head="app", imports=["base.Base"], bases={"K": "Base"},
                                funcs={"run": ["helper", "assist"]}, routes=[("get", "/items", "run")]),
        "java/base/Base.java": File("java", head="base", bases={"Base": None}, funcs={"assist": []}),
        "go/go.mod": File("static", head="module example.com/fz\n\ngo 1.22\n"),
        "go.mod": File("static", head="module example.com/top\n"),
        "ts/tsconfig.json": File("static", head=TS_CONFIGS["ts/tsconfig.json"][0]),
        "ts/configs/base.cfg": File("static", head=TS_CONFIGS["ts/configs/base.cfg"][0]),
        "ts/app.ts": File("ts", imports=["import { f } from '@/lib';", "import * as u from '@/util';"],
                          funcs={"main": ["f", "u.g"]}, routes=[("get", "/items", "main"), ("post", "/api/items", "f")]),
        "ts/src/lib.ts": File("ts", funcs={"f": []}),
        "ts/src/util.ts": File("ts", imports=["import { f } from './lib';"], funcs={"g": ["f"]}),
        "ts/other/lib.ts": File("ts", funcs={"f": [], "g": []}),
        "kt/Models.kt": File("kt", head="kt", bases={"Clock": None, "Stopwatch": None}, funcs={"helper": []}),
        "kt/Main.kt": File("kt", head="kt", imports=["kt.util.slug"], funcs={"main": ["helper", "clockRun", "slug"]}),
        "kt/util/Text.kt": File("kt", head="kt.util", funcs={"slug": []}),
        ".gitignore": File("static", head="ignored/\n"),
        "ignored/tsconfig.json": File("static", head=TS_CONFIGS["ignored/tsconfig.json"][0]),
        "go/a/a.go": File("go", head="a", imports=["example.com/fz/b"], funcs={"Start": ["Serve"]}),
        "go/b/b.go": File("go", head="b", extra=GO_SERVER, funcs={"Serve": [], "Stop": []}),
        "go/b/halt.go": File("go", head="b", funcs={"Halt": ["Stop"]}),
        "compose.yaml": File("compose", services={"api": "alpine:3.20", "web": "nginx:1.27"}),
        "Dockerfile": File("docker", froms=["python:3.12"]),
        "notes.md": File("notes", mentions=["helper", "Foo", "Base", "K", "Serve", "api", "python", "nginx"]),
        "docs/adr-1.md": File("note", head="ADR-1", links=["py/a.py"]),
        "docs/adr-2.md": File("note", head="ADR-2", supersedes="ADR-1"),
    }


IMPORTS = {
    "py": [
        "import py.b", "import py.c as c", "from py.b import helper", "from py import b", "from .c import util",
        "from . import a", "from pkg import helper", "from pkg import *", "from py.b import *",
        "from lib import util", "import pkg.impl", "from pkg.impl import pkgfn as alias",
    ],
    "rs": ["use crate::display::Display;", "use crate::foo::Foo;", "use shop_core::util;", "mod display;",
           "use crate::util::tidy;", "use super::display::render;"],
    "cs": ["using Shop;", "using Shop.Models;", "using static Shop.Jobs;", "using J = Shop.Jobs;", "using Models;"],
    "cpp": ['#include "util/strings.h"', '#include "strings.h"', '#include "widget.h"',
            '#include "../cpp/util/strings.h"', "#include <vector>"],
    "go": ["example.com/fz/a", "example.com/fz/b", "example.com/top/go/b", "example.com/bee", "example.com/ext"],
    "ts": ["import { f } from '@/lib';", "import * as u from '@/util';", "import { g } from './src/util';",
           "import { f } from './other/lib';", "import { f } from 'lib';", "import { g } from './src';"],
    "java": ["base.Base", "app.K", "base.*"],
    "kt": ["kt.util.slug", "kt.util.*", "kt.Clock", "kt.helper"],
}
BASES = {
    "py": ["Base", "Child", "Foo", "Missing", "b.Base", "pkg.impl.Base"], "java": ["Base", "K"],
    "cs": ["Jobs", "Base", "Program", "Missing"], "cpp": ["Widget", "Gadget", "Missing"],
}
# How a Python call to `{n}` is written: bare, through a module or alias, on
# an untyped value, on `self`, or on a literal.
PY_CALLS = ["{n}()", "b.{n}()", "c.{n}()", "obj.{n}()", "pkg.impl.{n}()", "alias.{n}()", "self.{n}()", "''.{n}()",
            "b.Twin().{n}()", "b.Base().{n}()"]
# Path pins of every kind over the fuzz's own paths (renames add `_rN` to a stem).
CALL_PINS = ["py/.", ".", "pkg/.!other.py", "/b.py", "/lib/util.py", "/py/.", "/lib/.", "src/", "pkg/", "py/b.py"]
IMPORT_PINS = ["py/.", "pkg/.", ".", "src/lib/.!core.py", "/b.py", "/util.py", "/impl.py"]
# How a TS call to `{n}` is written: bare, through a namespace import, on `this` or an untyped value.
TS_CALLS = ["{n}", "u.{n}", "this.{n}", "obj.{n}"]
# How a Go call to `{n}` is written: bare, through a package, on a typed value or an untyped one.
GO_CALLS = ["{n}", "b.{n}", "bee.{n}", "(&Server{{}}).{n}", "obj.{n}"]
# How a Java call to `{n}` is written: bare, on `this`, static, on a new or an untyped object.
JAVA_CALLS = ["{n}", "this.{n}", "Base.{n}", "new K().{n}", "obj.{n}"]
# How a Kotlin call to `{n}` is written: bare, on a constructed or an untyped value.
KT_CALLS = ["{n}", "Clock().{n}", "x.{n}"]
# How a Rust call to `{n}` is written: bare, through a module, a crate path,
# another crate, a type, or on an untyped value.
RS_CALLS = ["{n}", "display::{n}", "crate::foo::{n}", "shop_core::{n}", "shop_core::util::{n}", "Foo::{n}",
            "x.{n}"]
# How a C# call to `{n}` is written: bare, on `this`/`base`, static, through
# an alias, a qualified name or a nested type, on a new object, a typed field
# or parameter, or an untyped object.
CS_CALLS = ["{n}", "this.{n}", "base.{n}", "Jobs.{n}", "J.{n}", "Shop.Models.Base.{n}", "Outer.Inner.{n}",
            "new Jobs().{n}", "_jobs.{n}", "p.{n}", "obj.{n}"]
# The namespaces a C# file's class moves between ("" the global one).
CS_NAMESPACES = ["Shop", "Shop.App", "Shop.Models", "Tools", "Legacy", ""]
# A C# extension class of Jobs, and a nested type, as fixed code after a file's class.
CS_EXTENSIONS = "\npublic static class JobsExtensions\n{\n    public static void Tidy(this Jobs jobs) { }\n}\n"
CS_NESTED = "\npublic class Outer\n{\n    public class Inner\n    {\n        public static void Run() { }\n    }\n}\n"
# How a C++ call to `{n}` is written: bare, namespace-qualified, on a typed or an untyped value.
CPP_CALLS = ["{n}", "util::{n}", "w.{n}", "obj->{n}"]
# C++ out-of-class method definitions: of a header's class, of a class
# declared nowhere, and through a namespace.
CPP_METHODS = ["Widget::draw", "Widget::size", "Gadget::run", "Missing::stop", "ui::Widget::paint"]
# The source roots a Java file moves between (another Maven module).
JAVA_ROOTS = ["java/", "lib/src/main/java/"]
SERVICES = ["api", "web", "worker", "db"]
IMAGES = ["python:3.12", "nginx:1.27", "postgres:16", "alpine:3.20"]
EXTRA_NAMES = ["Foo", "Display", "Server", "K", "Child", "python", "postgres", "py/b.py", "missing", "act", "shared"]
CODE = ("py", "rs", "go", "java", "ts", "kt", "cs", "cpp")
# Free functions, and C# methods (one class per file, so a method moves to another class);
# a Java method stays in its class.
MOVABLE = ("py", "rs", "go", "ts", "kt", "cs", "cpp")
KT_CLASSES = ["Clock", "Stopwatch", "Tag", "Index"]
# Routes: Express paths (some meeting a Spring controller's under /api) and
# handlers beyond the file's own functions, and Spring's mappings.
ROUTE_METHODS = ["get", "post", "put"]
TS_ROUTE_PATHS = ["/items", "/items/:id", "/api/items", "/api/orders", "/health"]
JAVA_ROUTE_PATHS = ["/items", "/orders", "/items/{id}"]
TS_HANDLERS = ["f", "g", "u.g", "missing", "(req, res) => res.end()"]
SPRING_MAPPINGS = {"get": "GetMapping", "post": "PostMapping", "put": "PutMapping"}


class Fuzz:
    def __init__(self, rng: random.Random):
        self.rng = rng
        self.files = initial_repo()
        self.deleted: list[tuple[str, File]] = []
        self.counter = 0
        # (old, new) paths of the docs notes renamed in the current step.
        self.moved_notes: list[tuple[str, str]] = []
        # Folders deleted whole in the current step.
        self.removed_dirs: list[str] = []

    def _fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def _pick(self, pred):
        paths = sorted(p for p, f in self.files.items() if pred(f))
        return self.rng.choice(paths) if paths else None

    def _func_names(self) -> list:
        return sorted({n for f in self.files.values() for n in f.funcs} | {"Run", "new", "missing_fn", "Tidy", "act", "shared"})

    @staticmethod
    def _styled(kind: str, names: list) -> list:
        """Each name written as every call style of the language."""
        styles = {
            "py": PY_CALLS, "ts": TS_CALLS, "go": GO_CALLS, "java": JAVA_CALLS, "kt": KT_CALLS, "rs": RS_CALLS,
            "cs": CS_CALLS, "cpp": CPP_CALLS,
        }.get(kind, ["{n}"])
        return [style.format(n=n) for style in styles for n in names]

    # Each operation returns (description, touched paths), or None when it can't apply.

    def op_call(self):
        path = self._pick(lambda f: f.kind in CODE and f.funcs)
        if not path:
            return None
        fn = self.rng.choice(sorted(self.files[path].funcs))
        calls = self.files[path].funcs[fn]
        if calls and self.rng.random() < 0.5:
            gone = calls.pop(self.rng.randrange(len(calls)))
            return f"remove call {fn}->{gone} in {path}", {path}
        names = self._func_names()
        names = self._styled(self.files[path].kind, names)
        new = self.rng.choice([n for n in names if n not in calls])
        calls.append(new)
        return f"add call {fn}->{new} in {path}", {path}

    def op_base(self):
        path = self._pick(lambda f: f.kind in BASES and f.bases)
        if not path:
            return None
        f = self.files[path]
        cls = self.rng.choice(sorted(f.bases))
        if f.bases[cls]:
            f.bases[cls] = None
            return f"remove base of {cls} in {path}", {path}
        f.bases[cls] = self.rng.choice([b for b in BASES[f.kind] if b != cls])
        return f"set base {cls}({f.bases[cls]}) in {path}", {path}

    def op_twin(self):
        """A Python file with a class gains or loses a second class, `Twin`,
        whose methods share the first one's names."""
        path = self._pick(lambda f: f.kind == "py" and f.bases)
        if not path:
            return None
        f = self.files[path]
        if "Twin" in f.bases:
            del f.bases["Twin"]
            return f"remove class Twin from {path}", {path}
        beside = sorted(f.bases)[0]
        f.bases["Twin"] = None
        return f"add class Twin beside {beside} in {path}", {path}

    def op_shadow(self):
        """A Python file with a class gains or loses a top-level `act`, the
        name its classes' methods have."""
        path = self._pick(lambda f: f.kind == "py" and f.bases)
        if not path:
            return None
        f = self.files[path]
        if "act" in f.funcs:
            del f.funcs["act"]
            return f"remove top-level act from {path}", {path}
        f.funcs["act"] = ["shared()"]
        return f"add top-level act in {path}", {path}

    def op_nested(self):
        """A Python file gains or loses `outer`, with an `act` nested in it
        (owned by `outer`) calling `shared`."""
        path = self._pick(lambda f: f.kind == "py")
        if not path:
            return None
        f = self.files[path]
        f.extra = "" if f.extra else PY_NESTED
        return f"{'add' if f.extra else 'remove'} nested act in {path}", {path}

    def op_go_twin(self):
        """go/b/b.go gains or loses a type `Twin` whose methods are named
        like the package's functions `Serve` and `Stop`."""
        path = "go/b/b.go"
        if path not in self.files:
            return None
        f = self.files[path]
        f.extra = GO_SERVER if f.extra != GO_SERVER else GO_SERVER + GO_TWIN
        return f"{'add' if f.extra != GO_SERVER else 'remove'} type Twin in {path}", {path}

    def op_import(self):
        path = self._pick(lambda f: f.kind in IMPORTS)
        if not path:
            return None
        f = self.files[path]
        m = self.rng.choice(IMPORTS[f.kind])
        if m in f.imports:
            f.imports.remove(m)
            return f"remove import {m} in {path}", {path}
        f.imports.append(m)
        return f"add import {m} in {path}", {path}

    def op_import_style(self):
        """Write one of a Python file's imports another way."""
        path = self._pick(lambda f: f.kind == "py" and f.imports)
        if not path:
            return None
        f = self.files[path]
        i = self.rng.randrange(len(f.imports))
        old, f.imports[i] = f.imports[i], self.rng.choice([m for m in IMPORTS["py"] if m not in f.imports])
        return f"switch import {old!r} to {f.imports[i]!r} in {path}", {path}

    def op_pin(self):
        """Add or remove one of a Python file's pin comments."""
        path = self._pick(lambda f: f.kind == "py")
        if not path:
            return None
        pins = self.files[path].pins
        if pins and self.rng.random() < 0.4:
            gone = pins.pop(self.rng.randrange(len(pins)))
            return f"remove pin {gone!r} in {path}", {path}
        if self.rng.random() < 0.6:
            new = f"calls {self.rng.choice(self._func_names())} {self.rng.choice(CALL_PINS)}"
        else:
            new = f"imports {self.rng.choice(IMPORT_PINS)}"
        if new in pins:
            return None
        pins.append(new)
        return f"add pin {new!r} in {path}", {path}

    def op_method(self):
        """Add or remove a C++ out-of-class method definition."""
        path = self._pick(lambda f: f.kind == "cpp")
        if not path:
            return None
        methods = self.files[path].methods
        q = self.rng.choice(CPP_METHODS)
        if q in methods:
            methods.remove(q)
            return f"remove {q} in {path}", {path}
        methods.append(q)
        return f"add {q} in {path}", {path}

    def op_impl(self):
        path = self._pick(lambda f: f.impl is not None)
        if not path:
            return None
        self.files[path].impl = not self.files[path].impl
        return f"{'add' if self.files[path].impl else 'remove'} impl Display for Foo in {path}", {path}

    def op_add_function(self):
        path = self._pick(lambda f: f.kind in CODE)
        if not path:
            return None
        name = self._fresh("fn")
        names = self._styled(self.files[path].kind, self._func_names())
        self.files[path].funcs[name] = self.rng.sample(names, self.rng.randrange(3))
        return f"add function {name} in {path}", {path}

    def op_remove_function(self):
        path = self._pick(lambda f: f.kind in CODE and f.funcs)
        if not path:
            return None
        name = self.rng.choice(sorted(self.files[path].funcs))
        del self.files[path].funcs[name]
        return f"remove function {name} from {path}", {path}

    def op_move_function(self):
        source = self._pick(lambda f: f.kind in MOVABLE and f.funcs)
        if not source:
            return None
        lang = self.files[source].kind
        name = self.rng.choice(sorted(self.files[source].funcs))
        target = self._pick(lambda f: f.kind == lang and f is not self.files[source] and name not in f.funcs)
        if not target:
            return None
        self.files[target].funcs[name] = self.files[source].funcs.pop(name)
        return f"move function {name} from {source} to {target}", {source, target}

    def op_move_within_package(self):
        """Move a free function to another file of its directory (its Go or Kotlin package)."""
        source = self._pick(lambda f: f.kind in ("go", "kt") and f.funcs)
        if not source:
            return None
        folder = source.rpartition("/")[0]
        name = self.rng.choice(sorted(self.files[source].funcs))
        targets = sorted(
            p for p, f in self.files.items()
            if p != source and p.rpartition("/")[0] == folder and f.kind == self.files[source].kind
            and name not in f.funcs
        )
        if not targets:
            return None
        target = self.rng.choice(targets)
        self.files[target].funcs[name] = self.files[source].funcs.pop(name)
        return f"move function {name} from {source} to {target}", {source, target}

    def op_move_java_module(self):
        """Move a Java class to the same package under another source root."""
        path = self._pick(lambda f: f.kind == "java")
        if not path:
            return None
        old = next(root for root in JAVA_ROOTS if path.startswith(root))
        new = self.rng.choice([root for root in JAVA_ROOTS if root != old]) + path[len(old):]
        if new in self.files:
            return None
        self.files[new] = self.files.pop(path)
        return f"move {path} to {new}", {path, new}

    def op_rename(self):
        path = self._pick(lambda f: f.kind not in ("compose", "docker", "static"))
        if not path:
            return None
        stem, dot, ext = path.rpartition(".")
        new = f"{stem}_{self._fresh('r')}{dot}{ext}"
        self.files[new] = self.files.pop(path)
        if self.files[new].kind == "note":
            self.moved_notes.append((path, new))
        return f"rename {path} to {new}", {path, new}

    def op_kt_class(self):
        """Add or remove a one-line Kotlin class."""
        path = self._pick(lambda f: f.kind == "kt")
        if not path:
            return None
        bases = self.files[path].bases
        name = self.rng.choice(KT_CLASSES)
        if name in bases:
            del bases[name]
            return f"remove one-line class {name} in {path}", {path}
        bases[name] = None
        return f"add one-line class {name} in {path}", {path}

    def op_config(self):
        """Rewrite, create or delete a resolver configuration file."""
        path = self.rng.choice(sorted(TS_CONFIGS))
        current = self.files[path].head if path in self.files else None
        text = self.rng.choice([t for t in TS_CONFIGS[path] if t != current])
        if text is None:
            del self.files[path]
            return f"delete config {path}", {path}
        self.files[path] = File("static", head=text)
        return f"write config {path}: {text!r}", {path}

    def op_cs_csproj(self):
        """Write, create or delete a C# project file or a Directory.Build file."""
        path = self.rng.choice(sorted(CS_CONFIGS))
        current = self.files[path].head if path in self.files else None
        text = self.rng.choice([t for t in CS_CONFIGS[path] if t != current])
        if text is None:
            del self.files[path]
            return f"delete {path}", {path}
        self.files[path] = File("static", head=text)
        return f"write {path}: {text!r}", {path}

    def op_cs_global_using(self):
        """Add or remove a `global using` at the top of a C# file."""
        path = self._pick(lambda f: f.kind == "cs")
        if not path:
            return None
        imports = self.files[path].imports
        using = self.rng.choice(CS_GLOBAL_USINGS)
        if using in imports:
            imports.remove(using)
            return f"remove {using!r} in {path}", {path}
        imports.insert(0, using)
        return f"add {using!r} in {path}", {path}

    def op_cs_move(self):
        """Move a C# file to another project's folder, or out of every project."""
        path = self._pick(lambda f: f.kind == "cs")
        if not path:
            return None
        folder, _sep, name = path.rpartition("/")
        new = self.rng.choice([d for d in CS_DIRS if d != folder + "/"]) + name
        if new in self.files:
            return None
        self.files[new] = self.files.pop(path)
        return f"move {path} to {new}", {path, new}

    def op_cs_namespace(self):
        """Change a C# file's namespace."""
        path = self._pick(lambda f: f.kind == "cs")
        if not path:
            return None
        f = self.files[path]
        f.head = self.rng.choice([n for n in CS_NAMESPACES if n != f.head])
        return f"namespace of {path} to {f.head}", {path}

    def op_cs_move_class(self):
        """Swap two C# files' classes (name, base and methods), each file
        keeping its namespace."""
        paths = sorted(p for p, f in self.files.items() if f.kind == "cs")
        if len(paths) < 2:
            return None
        a, b = self.rng.sample(paths, 2)
        fa, fb = self.files[a], self.files[b]
        fa.bases, fb.bases = fb.bases, fa.bases
        fa.funcs, fb.funcs = fb.funcs, fa.funcs
        return f"swap the classes of {a} and {b}", {a, b}

    def op_cs_dup_class(self):
        """Give a C# file's class the name of another file's class, in
        another namespace."""
        paths = sorted(p for p, f in self.files.items() if f.kind == "cs")
        if len(paths) < 2:
            return None
        a, b = self.rng.sample(paths, 2)
        ((name, _base),) = self.files[a].bases.items()
        fb = self.files[b]
        ((_old, base),) = fb.bases.items()
        fb.bases = {name: base}
        if fb.head == self.files[a].head:
            fb.head = self.rng.choice([n for n in CS_NAMESPACES if n != fb.head])
        return f"class of {b} named {name} in namespace {fb.head}", {b}

    def op_delete_dir(self):
        """Delete a folder holding a configuration file, with everything in it."""
        folder = self.rng.choice(CONFIG_DIRS)
        paths = sorted(p for p in self.files if p.startswith(folder + "/"))
        if not paths:
            return None
        for path in paths:
            self.deleted.append((path, self.files.pop(path)))
        self.removed_dirs.append(folder)
        return f"delete folder {folder}", set(paths)

    def _route_handlers(self, path: str) -> list:
        f = self.files[path]
        if f.kind == "java":  # a method of the class not mapped yet
            return sorted(set(f.funcs) - {handler for _m, _p, handler in f.routes})
        return sorted(f.funcs) + TS_HANDLERS

    def op_route(self):
        """Add or remove an Express or Spring route."""
        path = self._pick(lambda f: f.kind in ("ts", "java"))
        if not path:
            return None
        f = self.files[path]
        if f.routes and self.rng.random() < 0.4:
            gone = f.routes.pop(self.rng.randrange(len(f.routes)))
            return f"remove route {gone} in {path}", {path}
        handlers = self._route_handlers(path)
        if not handlers:
            return None
        paths = JAVA_ROUTE_PATHS if f.kind == "java" else TS_ROUTE_PATHS
        route = (self.rng.choice(ROUTE_METHODS), self.rng.choice(paths), self.rng.choice(handlers))
        f.routes.append(route)
        return f"add route {route} in {path}", {path}

    def op_route_change(self):
        """Change a route's method or path."""
        path = self._pick(lambda f: f.routes)
        if not path:
            return None
        routes = self.files[path].routes
        at = self.rng.randrange(len(routes))
        method, route_path, handler = routes[at]
        if self.rng.random() < 0.5:
            method = self.rng.choice([m for m in ROUTE_METHODS if m != method])
        else:
            paths = JAVA_ROUTE_PATHS if self.files[path].kind == "java" else TS_ROUTE_PATHS
            route_path = self.rng.choice([p for p in paths if p != route_path])
        routes[at] = (method, route_path, handler)
        return f"change route {at} to {routes[at]} in {path}", {path}

    def op_route_move(self):
        """Move a route's handler to another file of its language: a Spring
        handler method with its mapping to another controller, an Express
        handler function out of its router's file (the route then names it
        unresolved) or the route itself to another router."""
        path = self._pick(lambda f: f.routes)
        if not path:
            return None
        f = self.files[path]
        route = self.rng.choice(f.routes)
        handler = route[2]
        target = self._pick(lambda o: o.kind == f.kind and o is not f and handler not in o.funcs)
        if not target or (f.kind == "java" and handler not in f.funcs):
            return None
        if f.kind == "java" or handler not in f.funcs or self.rng.random() < 0.5:
            f.routes.remove(route)
            self.files[target].routes.append(route)
            if f.kind == "java":
                self.files[target].funcs[handler] = f.funcs.pop(handler)
            return f"move route {route} from {path} to {target}", {path, target}
        self.files[target].funcs[handler] = f.funcs.pop(handler)
        return f"move handler {handler} from {path} to {target}", {path, target}

    def op_delete(self):
        path = self._pick(lambda f: True)
        if not path or path == ".gitignore":
            return None
        self.deleted.append((path, self.files.pop(path)))
        return f"delete {path}", {path}

    def op_restore(self):
        if not self.deleted:
            return None
        path, f = self.deleted.pop()
        self.files[path] = f
        return f"restore {path}", {path}

    def op_service(self):
        path = self._pick(lambda f: f.kind == "compose")
        if not path:
            return None
        services = self.files[path].services
        name = self.rng.choice(SERVICES)
        if name in services:
            del services[name]
            return f"remove service {name} in {path}", {path}
        services[name] = self.rng.choice(IMAGES)
        return f"add service {name} ({services[name]}) in {path}", {path}

    def op_from(self):
        path = self._pick(lambda f: f.kind == "docker")
        if not path:
            return None
        froms = self.files[path].froms
        image = self.rng.choice(IMAGES)
        if image in froms:
            froms.remove(image)
            return f"remove FROM {image} in {path}", {path}
        froms.append(image)
        return f"add FROM {image} in {path}", {path}

    def op_move_image(self):
        """Move an image only one of the Dockerfile and the compose file runs
        to the other, in one step, so its shared Container changes claimant.
        A Dockerfile claims only its first FROM's image."""
        docker = self._pick(lambda f: f.kind == "docker")
        compose = self._pick(lambda f: f.kind == "compose")
        if not docker or not compose:
            return None
        froms, services = self.files[docker].froms, self.files[compose].services
        claimed = froms[:1]
        movable = sorted(set(claimed) ^ set(services.values()))
        free = [s for s in SERVICES if s not in services]
        if not movable or not free:
            return None
        image = self.rng.choice(movable)
        if image in claimed:
            froms.remove(image)
            services[self.rng.choice(free)] = image
            return f"move {image} from {docker} to {compose}", {docker, compose}
        for name in sorted(s for s, i in services.items() if i == image):
            del services[name]
        if image in froms:
            froms.remove(image)
        froms.insert(0, image)
        return f"move {image} from {compose} to {docker}", {docker, compose}

    def op_mention(self):
        path = self._pick(lambda f: f.kind == "notes")
        if not path:
            return None
        mentions = self.files[path].mentions
        name = self.rng.choice(self._func_names() + EXTRA_NAMES + SERVICES)
        if name in mentions:
            mentions.remove(name)
            return f"remove mention {name} in {path}", {path}
        mentions.append(name)
        return f"add mention {name} in {path}", {path}

    def op_link(self):
        path = self._pick(lambda f: f.kind == "note")
        if not path:
            return None
        links = self.files[path].links
        target = self.rng.choice(sorted({p for p, f in self.files.items() if f.kind in CODE} | {"py/b.py"}))
        if target in links:
            links.remove(target)
            return f"remove link {target} in {path}", {path}
        links.append(target)
        return f"add link {target} in {path}", {path}

    def op_supersedes(self):
        path = self._pick(lambda f: f.kind == "note")
        if not path:
            return None
        f = self.files[path]
        f.supersedes = None if f.supersedes else ("ADR-2" if f.head == "ADR-1" else "ADR-1")
        return f"{'set' if f.supersedes else 'remove'} supersedes in {path}", {path}

    OPS = [
        "call", "base", "import", "import_style", "impl", "add_function", "remove_function", "move_function",
        "rename", "delete", "restore", "service", "from", "move_image", "mention", "link", "supersedes", "pin", "pin",
        "config", "config", "delete_dir", "kt_class", "move_within_package", "move_java_module", "method", "method",
        "cs_csproj", "cs_csproj", "cs_global_using", "cs_global_using", "cs_move", "cs_namespace", "cs_move_class",
        "cs_dup_class", "twin", "twin", "shadow", "nested", "go_twin", "go_twin", "route", "route",
        "route_change", "route_move",
    ]

    def draw(self):
        while True:
            done = getattr(self, f"op_{self.rng.choice(self.OPS)}")()
            if done:
                return done

    def write(self, root, touched):
        for rel in touched:
            path = root / rel
            if rel in self.files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(render(self.files[rel]))
            elif path.exists():
                path.unlink()


@pytest.fixture(autouse=True)
def _with_pin_comments(monkeypatch):
    """A Python file's `# pin` comments as its Module's pinned rows: calls
    collapse per callee (calls.call_rows), imports per file (calls.import_rows)."""
    real = dispatch.extract_python_file

    def extract(content, rel_path, repo_id):
        result = real(content, rel_path, repo_id)
        calls: dict[str, set] = {}
        imports: set = set()
        for line in content.splitlines():
            words = line.split()
            if words[:3] == ["#", "pin", "calls"]:
                calls.setdefault(words[3], set()).add(words[4])
            elif words[:3] == ["#", "pin", "imports"]:
                imports.add(words[3])
        rows = [row for name, pins in sorted(calls.items())
                for row in call_rows("Module", rel_path, name, pins, False, None, rel_path, repo_id)]
        # The file's own imports by path join its pinned ones, as a resolver
        # writing both would collapse them.
        own = {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}
        result.relationships = [r for r in result.relationships if r.rel_type != "IMPORTS"]
        rows += import_rows(rel_path, repo_id, own, imports)
        for row in rows:
            row.origin = rel_path
        result.relationships += rows
        return result

    monkeypatch.setattr(dispatch, "extract_python_file", extract)


@pytest.fixture(autouse=True)
def _with_cs_projects(monkeypatch):
    """A C# file's Module records its project's usings (`fz_usings`), so its
    extraction depends on all of the C# configuration, not only the part its
    resolved edges show: a configuration change that does not re-index
    every C# file leaves a stale one."""
    real = dispatch.extract_csharp_file

    def extract(content, rel_path, repo_id, config=None):
        result = real(content, rel_path, repo_id, config)
        project = config.cs_project(rel_path)
        usings = [] if project is None else [project.dir, *project.csproj, *project.usings, *project.global_usings]
        result.nodes[0].properties["fz_usings"] = [str(each) for each in usings]
        return result

    monkeypatch.setattr(dispatch, "extract_csharp_file", extract)


def check(engine, repo_id, root, seed, step, log):
    expected = fresh_snapshot(engine, repo_id, root, mentions_enabled=True, docs_path=DOCS)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail(
            f"seed {seed}, step {step}: graph does not equal a fresh full_scan\noperations:\n"
            + "\n".join(log) + "\n" + snapshot_diff(expected, actual)
        )


@pytest.mark.parametrize("seed", SEEDS)
def test_graph_accuracy_fuzz(engine, repo_id, tmp_path, seed):
    rng = random.Random(seed)
    fuzz = Fuzz(rng)
    root = tmp_path
    fuzz.write(root, set(fuzz.files))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root, docs_path=DOCS, mentions_enabled=True)
    log: list[str] = []
    check(engine, repo_id, root, seed, "initial", log)

    def resync(paths):
        # What a live batch does after indexing (agent/sync.py on_changes).
        sync_resolver_config(engine, repo_id, root, {root / rel for rel in paths}, docs_path=DOCS,
                             mentions_enabled=True)

    for step in range(STEPS):
        fuzz.moved_notes = []
        fuzz.removed_dirs = []
        # The foreign-source edge (set (b)) is toggled on every seed.
        ops = [fuzz.op_impl()] if step == 0 else []
        ops += [fuzz.draw() for _ in range(rng.randint(1, 3))]
        touched = set().union(*(paths for _desc, paths in ops))
        fuzz.write(root, touched)
        folders = {d for d in fuzz.removed_dirs if not any(p.startswith(d + "/") for p in fuzz.files)}
        for folder in sorted(folders, reverse=True):
            if (root / folder).is_dir() and not any((root / folder).iterdir()):
                (root / folder).rmdir()
        gone = {rel for rel in touched if rel not in fuzz.files}
        changed = touched - gone
        split = rng.random() < 0.5
        log.append(f"step {step} ({'split' if split else 'one batch'}): " + "; ".join(d for d, _p in ops))
        if split:
            order = rng.sample(sorted(touched), len(touched))
            if fuzz.moved_notes:
                # A renamed note's old path goes first (see the module docstring).
                order.sort(key=lambda rel: not (rel in gone and rel.startswith(f"{DOCS}/")))
            for rel in order + sorted(folders):
                if rel in gone or rel in folders:
                    remove_paths(engine, repo_id, root, {root / rel})
                else:
                    index_paths(engine, repo_id, root, {root / rel}, docs_path=DOCS, mentions_enabled=True)
                resync({rel})
        else:
            if gone or folders:
                remove_paths(engine, repo_id, root, {root / rel for rel in gone | folders})
            if changed:
                index_paths(engine, repo_id, root, {root / rel for rel in changed}, docs_path=DOCS,
                            mentions_enabled=True)
            resync(touched | folders)
        check(engine, repo_id, root, seed, step, log)


class _Scripted:
    """An rng whose choices are given in advance: each `choice` or `sample`
    returns the next of `picks`, which must be one the call could make."""

    def __init__(self, picks):
        self.picks = list(picks)

    def choice(self, options):
        pick = self.picks.pop(0)
        assert pick in options, (pick, options)
        return pick

    def sample(self, options, k):
        pick = self.picks.pop(0)
        assert len(pick) == k and set(pick) <= set(options), (pick, options)
        return pick


def test_cs_rekey_ops_retract_callers_in_other_files(engine, repo_id, tmp_path, monkeypatch):
    """Each C# rekeying operation changes a node's match keys under edges
    other files' `@` pins gave it: the batch retracts those edges and the
    graph still equals a fresh scan. A method that moves to another class
    is another node (its owner changed), not a rekeyed one: its old node
    goes with its edges, and the new one is added and relinked."""
    retracted = []
    real = GraphEngine.write_match_keys

    def write_match_keys(self, repo, nodes):
        for label, name, file, owner, _keys, retract in nodes:
            if retract:
                retracted.append(self.run_cypher(
                    f"MATCH (a)-[r:CALLS|IMPORTS]->(b:{label} {{repo_id: $r, name: $n, file: $f, owner: $o}}) "
                    "WHERE coalesce(a.file, a.source_file) <> $f RETURN count(r) AS n",
                    {"r": repo, "n": name, "f": file, "o": owner},
                )[0]["n"])
        return real(self, repo, nodes)

    monkeypatch.setattr(GraphEngine, "write_match_keys", write_match_keys)
    fuzz = Fuzz(random.Random(0))
    root = tmp_path
    fuzz.write(root, set(fuzz.files))
    provision_repository_schema(engine, root)
    engine.upsert_repository(repo_id, repo_id, str(root))
    full_scan(engine, repo_id, root, docs_path=DOCS, mentions_enabled=True)
    steps = [
        # Jobs.cs's Run becomes Root's (owner Jobs to Root): another node, under Program's and Wide's Jobs.Run.
        ("cs_move_class", [["cs/Jobs.cs", "cs/Global.cs"]], False),
        # Base.cs's class is named Jobs: its Helper is another node, under Block's and Wide's p.Helper.
        ("cs_dup_class", [["cs/Global.cs", "cs/Models/Base.cs"]], False),
        # Tidy's extension key moves to the global namespace under Block's and Use's _jobs.Tidy.
        ("cs_namespace", ["cs/JobsExtensions.cs", ""], True),
        # Jobs (now in Global.cs) moves from the global namespace to Shop under the Jobs.Run calls.
        ("cs_namespace", ["cs/Global.cs", "Shop"], True),
    ]
    for step, (op, picks, rekeys) in enumerate(steps):
        fuzz.rng = _Scripted(picks)
        description, touched = getattr(fuzz, f"op_{op}")()
        retracted.clear()
        fuzz.write(root, touched)
        index_paths(engine, repo_id, root, {root / rel for rel in touched}, docs_path=DOCS, mentions_enabled=True)
        if rekeys:
            assert any(retracted), f"{description}: no edge from another file retracted"
        check(engine, repo_id, root, "scripted", step, [description])

