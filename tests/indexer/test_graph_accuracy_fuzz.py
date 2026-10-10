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
`self.shared()` reaching a base in the same or another file.

TS files import through a tsconfig path alias, and the resolver configuration
changes under them: the tsconfig is rewritten (its own `paths`, `extends` to
an odd-named base, `references`), its base is edited or its folder deleted, a
root or nested go.mod comes and goes, and a config under an ignored folder is
edited. After every batch the fuzz runs what a live batch does,
`sync_resolver_config`. Kotlin files hold adjacent one-line classes, which
the extractor recovers from tree-sitter-kotlin's error nodes.

Path pins (docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md)
are fuzzed ahead of the resolvers that write them: a Python file's
`# pin calls <name> <pin>` and `# pin imports <pin>` comments become its
Module's pinned CALLS and IMPORTS rows (`_with_pin_comments`), so every pin
kind is written, relinked and removed as files come and go.

The spec's "Out of scope" gaps are left out by construction:
- compose services have `image:` only, never `build:`;
- no datastore or route code (so no handler stub either);
- no method moves out of its type's file: only free functions move, and the
  Go method and Rust inherent `impl` stay with their type;
- no C++, and no C#/TS partial classes;
- the default `mentions_ambiguous_mode` (`all`);
- a docs note's `id` never changes in place;
- a renamed docs note's old path is removed before its new path is indexed,
  when they go in separate batches (the id moving to another file first
  leaves the old path in its edges' `origins`).
"""

import os
import random
import uuid
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
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

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
    repo = f"zz-accfuzz-{uuid.uuid4().hex[:8]}"
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

    def copy(self):
        return replace(
            self, imports=list(self.imports), bases=dict(self.bases),
            funcs={k: list(v) for k, v in self.funcs.items()}, services=dict(self.services),
            froms=list(self.froms), mentions=list(self.mentions), links=list(self.links), pins=list(self.pins),
        )


def py_class(name: str, base: str | None) -> str:
    """A class whose `act` calls `self.shared()`, which only `Base` defines."""
    head = f"\n\nclass {name}({base}):\n" if base else f"\n\nclass {name}:\n"
    shared = "\n    def shared(self):\n        return 0\n" if name == "Base" else ""
    return head + "    def act(self):\n        return self.shared()\n" + shared


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
        return "".join(parts) or "X = 1\n"
    if f.kind == "rs":
        parts = [f"use crate::{m};\n" for m in f.imports]
        parts.append("\n" + f.extra)
        if f.impl:
            parts.append("\nimpl Display for Foo {}\n")
        parts += [
            f"\npub fn {name}() {{\n" + "".join(f"    {g}();\n" for g in calls) + "}\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts)
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
        ((cls, base),) = f.bases.items()
        parts = [f"package {f.head};\n\n"] + [f"import {m};\n" for m in f.imports]
        parts.append(f"\npublic class {cls}" + (f" extends {base}" if base else "") + " {\n")
        parts += [
            f"    void {name}() {{\n" + "".join(f"        {g}();\n" for g in calls) + "    }\n"
            for name, calls in f.funcs.items()
        ]
        return "".join(parts) + "}\n"
    if f.kind == "kt":
        # One-line class bodies, which tree-sitter-kotlin 1.1.0 needs recovered.
        parts = [f"package {f.head}\n\n"]
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
    "go.mod": [None, "module example.com/fz\n", "module example.com/other\n"],
    "go/b/go.mod": [None, "module example.com/fz/b\n"],
}
# Folders holding a configuration file, which the fuzz may delete whole.
CONFIG_DIRS = ["ts/configs", "go/b"]

RS_FOO = "pub struct Foo;\n\nimpl Foo {\n    pub fn new() -> Foo {\n        Foo\n    }\n}\n"
GO_SERVER = "type Server struct{}\n\nfunc (s *Server) Run() {\n\tServe()\n}\n"


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
        "rs/conv.rs": File("rs", imports=["display::Display"], impl=True, funcs={"convert": ["parse"]}),
        "java/app/K.java": File("java", head="app", imports=["base.Base"], bases={"K": "Base"},
                                funcs={"run": ["helper", "assist"]}),
        "java/base/Base.java": File("java", head="base", bases={"Base": None}, funcs={"assist": []}),
        "go/go.mod": File("static", head="module example.com/fz\n\ngo 1.22\n"),
        "go.mod": File("static", head="module example.com/fz\n"),
        "ts/tsconfig.json": File("static", head=TS_CONFIGS["ts/tsconfig.json"][0]),
        "ts/configs/base.cfg": File("static", head=TS_CONFIGS["ts/configs/base.cfg"][0]),
        "ts/app.ts": File("ts", imports=["import { f } from '@/lib';", "import * as u from '@/util';"],
                          funcs={"main": ["f", "u.g"]}),
        "ts/src/lib.ts": File("ts", funcs={"f": []}),
        "ts/src/util.ts": File("ts", imports=["import { f } from './lib';"], funcs={"g": ["f"]}),
        "ts/other/lib.ts": File("ts", funcs={"f": [], "g": []}),
        "kt/Models.kt": File("kt", head="kt", bases={"Clock": None, "Stopwatch": None}, funcs={"helper": []}),
        "kt/Main.kt": File("kt", head="kt", funcs={"main": ["helper", "clockRun"]}),
        ".gitignore": File("static", head="ignored/\n"),
        "ignored/tsconfig.json": File("static", head=TS_CONFIGS["ignored/tsconfig.json"][0]),
        "go/a/a.go": File("go", head="a", imports=["example.com/fz/b"], funcs={"Start": ["Serve"]}),
        "go/b/b.go": File("go", head="b", extra=GO_SERVER, funcs={"Serve": [], "Stop": []}),
        "compose.yaml": File("compose", services={"api": "python:3.12", "web": "nginx:1.27"}),
        "Dockerfile": File("docker", froms=["python:3.12"]),
        "notes.md": File("notes", mentions=["helper", "Foo", "Base", "K", "Serve", "api", "python"]),
        "docs/adr-1.md": File("note", head="ADR-1", links=["py/a.py"]),
        "docs/adr-2.md": File("note", head="ADR-2", supersedes="ADR-1"),
    }


IMPORTS = {
    "py": [
        "import py.b", "import py.c as c", "from py.b import helper", "from py import b", "from .c import util",
        "from . import a", "from pkg import helper", "from pkg import *", "from py.b import *",
        "from lib import util", "import pkg.impl", "from pkg.impl import pkgfn as alias",
    ],
    "rs": ["display::Display", "foo::Foo"],
    "go": ["example.com/fz/a", "example.com/fz/b", "example.com/fz/go/b"],
    "ts": ["import { f } from '@/lib';", "import * as u from '@/util';", "import { g } from './src/util';",
           "import { f } from './other/lib';", "import { f } from 'lib';", "import { g } from './src';"],
    "java": ["base.Base", "app.K"],
}
BASES = {"py": ["Base", "Child", "Foo", "Missing", "b.Base", "pkg.impl.Base"], "java": ["Base", "K"]}
# How a Python call to `{n}` is written: bare, through a module or alias, on
# an untyped value, on `self`, or on a literal.
PY_CALLS = ["{n}()", "b.{n}()", "c.{n}()", "obj.{n}()", "pkg.impl.{n}()", "alias.{n}()", "self.{n}()", "''.{n}()"]
# Path pins of every kind over the fuzz's own paths (renames add `_rN` to a stem).
CALL_PINS = ["py/.", ".", "pkg/.!other.py", "/b.py", "/lib/util.py", "/py/.", "/lib/.", "src/", "pkg/", "py/b.py"]
IMPORT_PINS = ["py/.", "pkg/.", ".", "src/lib/.!core.py", "/b.py", "/util.py", "/impl.py"]
# How a TS call to `{n}` is written: bare, through a namespace import, on `this` or an untyped value.
TS_CALLS = ["{n}", "u.{n}", "this.{n}", "obj.{n}"]
SERVICES = ["api", "web", "worker", "db"]
IMAGES = ["python:3.12", "nginx:1.27", "postgres:16", "alpine:3.20"]
EXTRA_NAMES = ["Foo", "Display", "Server", "K", "Child", "python", "postgres", "py/b.py", "missing"]
CODE = ("py", "rs", "go", "java", "ts", "kt")
MOVABLE = ("py", "rs", "go", "ts", "kt")  # free functions only: a Java method stays in its class
KT_CLASSES = ["Clock", "Stopwatch", "Tag", "Index"]


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
        return sorted({n for f in self.files.values() for n in f.funcs} | {"Run", "new", "missing_fn"})

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
        if self.files[path].kind == "py":
            names = [style.format(n=n) for style in PY_CALLS for n in names]
        elif self.files[path].kind == "ts":
            names = [style.format(n=n) for style in TS_CALLS for n in names]
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
        names = self._func_names()
        if self.files[path].kind == "py":
            names = [style.format(n=n) for style in PY_CALLS for n in names]
        elif self.files[path].kind == "ts":
            names = [style.format(n=n) for style in TS_CALLS for n in names]
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
        "rename", "delete", "restore", "service", "from", "mention", "link", "supersedes", "pin", "pin",
        "config", "config", "delete_dir", "kt_class",
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
