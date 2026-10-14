"""Resolve a Python file's imports to the repository files they can name.

Everything here is a pure function of the importing file's path and text: no
filesystem, no other file, no repository configuration. An import names a
set of *candidate* files, and the graph links only the candidates that exist
as Modules, so an edge never depends on anything but the writer's text and
whether its target exists (see
docs/superpowers/specs/2026-10-10-python-call-resolution-design.md).

Absolute imports are tried under every ancestor directory of the importer:
`import a.b` in `x/y/f.py` names `a/b`, `x/a/b` and `x/y/a/b`. That covers a
flat layout, a `src/` layout and a monorepo's nested projects without
reading any configuration. A module path `p` is the file `p.py` or the
package `p/__init__.py`; the package's directory `p/` is its prefix, which a
re-exported name can live anywhere under.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def ancestor_roots(file_path: str) -> tuple[str, ...]:
    """The directories an absolute import is tried under, outermost first:
    `x/y/f.py` gives `""`, `"x/"`, `"x/y/"`."""
    parts = file_path.split("/")[:-1]
    return tuple("/".join(parts[:i]) + "/" if i else "" for i in range(len(parts) + 1))


def relative_dir(current_dir: str, dot_count: int) -> str:
    """The directory a relative import's leading dots name: one dot is
    `current_dir`, each further dot one parent up (never above the root)."""
    parts = [p for p in current_dir.split("/") if p]
    levels_up = dot_count - 1
    if levels_up > 0:
        parts = parts[:-levels_up] if levels_up <= len(parts) else []
    return "/".join(parts)


@dataclass(frozen=True)
class ModuleRef:
    """One imported module, as its candidate module paths (no extension,
    `""` for the repository root). `package_only` marks a relative import's
    own package (`from . import x`), which is a directory, never a `.py`."""

    paths: tuple[str, ...]
    package_only: bool = False

    def files(self) -> list[str]:
        """The candidate files: `p.py` and `p/__init__.py` for each path."""
        out = []
        for path in self.paths:
            if not self.package_only:
                out.append(f"{path}.py")
            out.append(f"{path}/__init__.py" if path else "__init__.py")
        return out

    def dirs(self) -> list[str]:
        """The package prefixes `p/`. The repository root is never one."""
        return [f"{path}/" for path in self.paths if path]

    def child(self, name: str) -> ModuleRef:
        """The submodule `name` of this module, as a package."""
        return ModuleRef(tuple(f"{path}/{name}" if path else name for path in self.paths))


def absolute_module(dotted: str, file_path: str) -> ModuleRef:
    """`import a.b` written in `file_path`: `a/b` under each ancestor root."""
    rel = dotted.replace(".", "/")
    return ModuleRef(tuple(f"{root}{rel}" for root in ancestor_roots(file_path)))


def relative_module(module_text: str, current_dir: str) -> ModuleRef:
    """`from .m import n` / `from .. import n` written in `current_dir`: the
    one module path the dots and the remainder name, or the package itself."""
    dot_count = len(module_text) - len(module_text.lstrip("."))
    remainder = module_text[dot_count:]
    base = relative_dir(current_dir, dot_count)
    if not remainder:
        return ModuleRef((base,), package_only=True)
    rel = remainder.replace(".", "/")
    return ModuleRef((f"{base}/{rel}" if base else rel,))


@dataclass(frozen=True)
class Symbol:
    """A name bound by `from P import n [as alias]`: `n` can be a name defined
    in P (its files, or anywhere under its package) or the submodule P.n.
    `name` is `n` as P defines it, whatever the alias."""

    name: str
    module: ModuleRef
    submodule: ModuleRef


@dataclass
class Bindings:
    """A file's import bindings, file-wide (a function-local import counts
    everywhere in the file), and the Module names its IMPORTS edges target.

    `modules` maps a receiver as written (`os.path`, an `as` alias) to the
    module it names, `symbols` a from-imported local name to where it can
    come from, and `stars` lists the modules of `from P import *`.
    """

    modules: dict[str, ModuleRef] = field(default_factory=dict)
    symbols: dict[str, Symbol] = field(default_factory=dict)
    stars: list[ModuleRef] = field(default_factory=list)
    targets: set[str] = field(default_factory=set)

    def add_import(self, dotted: str, alias: str | None, file_path: str) -> None:
        """`import a.b [as x]`: binds the receiver `a.b` (or `x`). Without an
        alias it also binds the top package `a`, as Python does; only `a.b`
        is an import target."""
        ref = absolute_module(dotted, file_path)
        self.modules[alias or dotted] = ref
        if alias is None and "." in dotted:
            top = dotted.split(".", 1)[0]
            self.modules.setdefault(top, absolute_module(top, file_path))
        self.targets.update(ref.files())

    def add_from(self, module: ModuleRef, names: list[tuple[str, str | None]], star: bool) -> None:
        """`from P import n [as k], ...` or `from P import *`. Each name may be a
        submodule of P, so its candidates are import targets too."""
        self.targets.update(module.files())
        if star:
            self.stars.append(module)
        for name, alias in names:
            submodule = module.child(name)
            self.symbols[alias or name] = Symbol(name, module, submodule)
            self.targets.update(submodule.files())
