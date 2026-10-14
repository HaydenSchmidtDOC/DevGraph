"""Resolver configuration: the files besides a source file's own text that its
edges depend on. A TypeScript path alias (`@/lib/text`) means nothing without
the `tsconfig.json` that declares it, nor a Go import path without `go.mod`,
nor another Rust crate's name without its `Cargo.toml`, nor a Python package
in a source root that `pyproject.toml` declares (`lib/`) without that file.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md
(Resolver configuration). An extractor reads the configuration through one
`ResolverConfig` per batch; `fingerprints` summarises every configuration file
of the repository per language, so a batch that changes one can re-index that
language's files (dispatch.sync_resolver_config), and a fresh scan and an
incremental one extract every file under the same configuration.

A configuration file under a path the walk ignores (an ignored directory, or a
`.gitignore` rule) is treated as absent.

Rust's configuration is each Cargo.toml's library crate and every .rs file
that re-exports (`pub use`, `rust_reexports`): a call through
`shop_core::Client` reaches the file that defines `Client` only through
the re-exporting file's text.

C#'s configuration is its projects (`csharp_projects`): each `.csproj` and
the nearest Directory.Build.props and Directory.Build.targets above it (their
`<Using>` items and `<ProjectReference>`s), and every `.cs` file whose head
(`cs_global_usings`) holds a `global using`.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import astuple, dataclass, replace
from pathlib import Path

from devgraph.indexer.gitignore import IGNORECASE, is_gitignored
from devgraph.indexer.walk import indexable_paths, is_ignored_path

__all__ = [
    "LANGUAGE_SUFFIXES", "CsProject", "CsUsing", "ResolverConfig", "config_language", "cs_global_usings",
    "csharp_projects", "fingerprints", "parse_jsonc", "touches_config",
]

#: The source files each language's configuration applies to.
LANGUAGE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "ts": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
    "go": (".go",),
    "rust": (".rs",),
    "csharp": (".cs",),
    "python": (".py",),
}

#: The largest configuration file read; a bigger one is treated as absent.
_MAX_CONFIG_BYTES = 1024 * 1024


#: Directory.Build.props and Directory.Build.targets: MSBuild imports the
#: nearest of each above a project into it.
_CS_BUILD_FILES = ("Directory.Build.props", "Directory.Build.targets")


def config_language(name: str) -> str | None:
    """The language a file named `name` configures by its name alone (a
    tsconfig*.json or jsconfig.json, a go.mod or go.work, a Cargo.toml, a
    *.csproj or Directory.Build.props/.targets, a pyproject.toml), else
    None. A .cs file
    holding a `global using` is C# configuration by its head, not its name,
    and a .rs file re-exporting is Rust's by its `pub use`s
    (see `touches_config`). A
    config reached only through `extends` can have any name; it is followed
    by path (see `ResolverConfig.read`)."""
    if name == "jsconfig.json" or (name.startswith("tsconfig") and name.endswith(".json")):
        return "ts"
    if name in ("go.mod", "go.work"):
        return "go"
    if name == "Cargo.toml":
        return "rust"
    if name.endswith(".csproj") or name in _CS_BUILD_FILES:
        return "csharp"
    if name == "pyproject.toml":
        return "python"
    return None


def touches_config(rel: str, inputs: list[str], repo_root: Path) -> bool:
    """Whether a changed or deleted repo-relative path can change the
    configuration: it is named like a configuration file, is one of the
    `inputs` a fingerprint read, or is a directory holding one, or it is a
    .cs file of a project (a `.csproj` among `inputs` at or above it) whose
    head now holds a `global using` (one whose head held one is an input; a
    file of no project counts for nothing until a `.csproj` above it, itself
    a change, makes it one), or a .rs file that now re-exports (one that
    did is an input)."""
    if config_language(posixpath.basename(rel)):
        return True
    if any(each == rel or each.startswith(rel + "/") for each in inputs):
        return True
    if rel.endswith(".rs"):
        return bool(_rust_file_reexports(Path(repo_root) / rel))
    if not rel.endswith(".cs"):
        return False
    project_dirs = {posixpath.dirname(each) for each in inputs if each.endswith(".csproj")}
    return _cs_owner(rel, project_dirs) is not None and bool(cs_global_usings(Path(repo_root) / rel))


_JSONC_COMMENTS = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
_JSONC_TRAILING_COMMAS = re.compile(r'"(?:[^"\\]|\\.)*"|,(?=\s*[}\]])')


def _keep_strings(match: re.Match) -> str:
    return match.group(0) if match.group(0).startswith('"') else ""


def parse_jsonc(text: str) -> dict | None:
    """A JSON-with-comments document (tsconfig's dialect: `//` and `/* */`
    comments, trailing commas, a byte order mark) as a dict; None when it is
    malformed or not an object. Comments go first, so a trailing comma
    followed by a comment (`["a"], // x` before `}`) is still found; both
    passes leave strings alone."""
    stripped = _JSONC_COMMENTS.sub(_keep_strings, text.removeprefix("\ufeff"))
    stripped = _JSONC_TRAILING_COMMAS.sub(_keep_strings, stripped)
    try:
        value = json.loads(stripped)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _normalize(path: str) -> str | None:
    """`path` (POSIX, repo-relative, may hold `.`/`..`) normalised, or None
    when it leaves the repository."""
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts)


def _join(folder: str, path: str) -> str | None:
    return _normalize(f"{folder}/{path}" if folder else path)


def _ancestors(folder: str) -> list[str]:
    """`folder` and every directory above it, innermost first, "" last."""
    found = [folder]
    while folder:
        folder = posixpath.dirname(folder)
        found.append(folder)
    return found


# --- C# projects -------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class CsUsing:
    """A using a C# project's files share without writing it: a `<Using>`
    item of its project files or a `global using` of one of its .cs files.
    `name` is a namespace (`Shop.Models`), with `static` a type whose static
    members are in scope (`using static Shop.Jobs`); with `alias`
    (`using J = Shop.Jobs`) the alias names `name`, a namespace or a type,
    and nothing comes into scope otherwise. Names are written without
    whitespace, `global::` or a verbatim `@`."""

    name: str
    static: bool = False
    alias: str = ""


@dataclass(frozen=True)
class CsProject:
    """One C# project, as `csharp_projects` reads it.

    `dir` is the repo-relative directory of its project files ("" the
    root), `csproj` those files: several in one directory are one project,
    their items unioned. A .cs file belongs to the project of the nearest
    directory at or above it holding a `.csproj`; one with none above it
    has no project. `usings` are the `<Using>` items (after `Remove`s) of
    the project files, with the nearest Directory.Build.props before and the
    nearest Directory.Build.targets after each, and `global_usings` the
    `global using` directives in the heads (`cs_global_usings`) of the
    project's own .cs files, not those of a nested project. Both are sorted
    and without repeats. `references` are the directories of the projects
    its project files' and its nearest Directory.Build files'
    `<ProjectReference>`s reach, transitively, sorted, its own left out
    (`_cs_project_references`; compared case-folded where the file system
    ignores case, `_case_key`)."""

    dir: str
    csproj: tuple[str, ...]
    usings: tuple[CsUsing, ...]
    global_usings: tuple[CsUsing, ...]
    references: tuple[str, ...] = ()


#: The most of a .cs file read for its head (`cs_global_usings`).
_CS_HEAD_BYTES = 32 * 1024

# What may come before a `global using` in a .cs file's head: whitespace, a
# comment, a preprocessor line (`#if`, `#region`, `#nullable`, ...) and an
# `extern alias`; and a using directive itself, global or not (C# 12's
# `using unsafe P = int*;` included). A name runs possessively up to its `;`
# and holds no `/`, so a comment inside a directive ends the head and an
# unterminated one costs one pass, not a backtrack per character.
_CS_HEAD_SKIP = re.compile(r"\s+|//[^\n]*|/\*.*?\*/|#[^\n]*|extern\s+alias\s+@?\w+\s*;", re.DOTALL)
_CS_USING = re.compile(
    r"(global\s++)?using\s++(static\s++)?(?:unsafe\s++)?(?:(@?\w++)\s*+=\s*+([^;{}/]++)|([^;=(){}/]++));"
)


def _cs_name(text: str) -> str:
    return re.sub(r"\s+", "", text).removeprefix("global::").replace("@", "")


def _cs_head_usings(text: str) -> tuple[CsUsing, ...]:
    """The `global using` directives of a .cs file's head, read from its
    start for as long as it holds only whitespace, comments, preprocessor
    lines, `extern alias` and using directives, global or not: the compiler
    allows a `global using` nowhere else. A directive under an `#if` counts
    whatever the condition. The first anything else ends the head: an
    attribute, a namespace or a type, a top-level statement (`using var`),
    or a directive the pattern does not read (one with a comment inside
    it, say)."""
    found = set()
    pos, text = 0, text.removeprefix("﻿")
    while pos < len(text):
        match = _CS_HEAD_SKIP.match(text, pos) or _CS_USING.match(text, pos)
        if match is None:
            break
        if match.re is _CS_USING and match.group(1):
            alias = (match.group(3) or "").replace("@", "")
            found.add(CsUsing(_cs_name(match.group(4) or match.group(5)), bool(match.group(2)), alias))
        pos = match.end()
    return tuple(sorted(found))


def cs_global_usings(path: Path) -> tuple[CsUsing, ...]:
    """The `global using` directives in the head of the .cs file at `path`
    (`_cs_head_usings`), sorted; only its first `_CS_HEAD_BYTES` are read,
    so one past them is not seen, by a fresh scan and an incremental one
    alike. A missing or unreadable file has none."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(_CS_HEAD_BYTES)
    except OSError:
        return ()
    return _cs_head_usings(head.decode("utf-8", errors="replace"))


def _cs_using_items(text: str | None) -> list[tuple[str, CsUsing]]:
    """A project file's `<Using>` items in document order, as ("Include" or
    "Remove", the using), their `Static` and `Alias` metadata read from
    attributes or child elements. Nothing is evaluated: an item under a
    `Condition` counts as if it held, an `<Import>` is not followed, and a
    name holding a property, item or metadata reference or a wildcard
    (`$(`, `@(`, `%(`, `*`) is left out. A malformed file has none."""
    try:
        root = ET.fromstring(text or "")
    except (ET.ParseError, ValueError):
        return []
    items = []
    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag.rpartition("}")[2] != "Using":
            continue
        meta = dict(element.attrib)
        for child in element:
            if isinstance(child.tag, str):
                meta.setdefault(child.tag.rpartition("}")[2], (child.text or "").strip())
        static = meta.get("Static", "").strip().lower() == "true"
        alias = meta.get("Alias", "").strip()
        for op in ("Include", "Remove"):
            for name in meta.get(op, "").split(";"):
                if name.strip() and not any(c in name for c in "$@%*"):
                    items.append((op, CsUsing(_cs_name(name), static, alias)))
    return items


#: An MSBuild escape (`%20`, `%3B`) in an item's `Include`.
_MSBUILD_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
#: A drive-qualified (`C:/x`) path, absolute on Windows.
_DRIVE_PATH = re.compile(r"[A-Za-z]:/")


def _cs_reference_items(text: str | None) -> list[str]:
    """The `Include` paths of a project or Directory.Build file's
    `<ProjectReference>` items, as written but with `\\` taken as `/` and
    MSBuild's `%XX` escapes decoded, sorted. As with `<Using>` nothing is
    evaluated: an item under a `Condition` counts as if it held, and a path
    holding a property, item or metadata reference (`$(`, `@(`, `%(`) or a
    wildcard (`*`, `?`) is left out, as is an absolute or drive-qualified
    one. A malformed file has none."""
    try:
        root = ET.fromstring(text or "")
    except (ET.ParseError, ValueError):
        return []
    found = set()
    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag.rpartition("}")[2] != "ProjectReference":
            continue
        for name in element.attrib.get("Include", "").split(";"):
            name = name.strip()
            if not name or any(each in name for each in ("$(", "@(", "%(", "*", "?")):
                continue
            name = _MSBUILD_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), name).replace("\\", "/")
            if not name.startswith("/") and not _DRIVE_PATH.match(name):
                found.add(name)
    return sorted(found)


def _cs_project_references(items: list[str], folder: str) -> list[str]:
    """`_cs_reference_items`' paths read from the directory `folder` of the
    project that imports them, repo-relative: a `.csproj`'s own, and those
    of the nearest Directory.Build.props/.targets above it, whose relative
    paths MSBuild also reads from each importing project. One leaving the
    repository is left out."""
    return sorted({path for item in items if (path := _join(folder, item)) is not None})


def _case_key(path: str) -> str:
    """How a referenced project file's path is compared with the
    repository's: case-folded where the file system ignores case (as
    `.gitignore` matching does, gitignore.IGNORECASE), so
    `..\\core\\core.csproj` names `Core/Core.csproj` there."""
    return path.casefold() if IGNORECASE else path


@dataclass(frozen=True)
class _TsProject:
    """One tsconfig's resolved options, its `extends` chain applied: the
    `baseUrl` directory and the `paths` patterns' directory (repo-relative,
    "" the root; None when unset), and `paths` itself."""

    base_url: str | None
    paths_base: str | None
    paths: tuple[tuple[str, tuple[str, ...]], ...]


class ResolverConfig:
    """The resolver configuration of one repository, read once per batch.

    `read` collects every configuration path looked for (`extends` and
    `references` targets included), found or not, repo-relative.

    `go_files` gives the repository's go.mod and go.work paths, as the
    fingerprint the batch extracts under found them (`fingerprints`'
    `inputs`), when a Go file first asks (`go_modules`); None, or a None
    answer, walks the repository for them. `rust_files` does the same for
    the Cargo.toml files and the re-exporting .rs files (`rust_crates`,
    `rust_reexports`), `cs_files` for the C#
    project files and the .cs files holding a `global using`
    (`cs_projects`), and `python_files` for the pyproject.toml files
    (`python_roots`)."""

    def __init__(
        self, repo_root: Path, go_files: Callable[[], list[str] | None] | None = None,
        rust_files: Callable[[], list[str] | None] | None = None,
        cs_files: Callable[[], list[str] | None] | None = None,
        python_files: Callable[[], list[str] | None] | None = None,
    ) -> None:
        self.root = Path(repo_root)
        self.read: set[str] = set()
        self._texts: dict[str, str | None] = {}
        self._options: dict[str, dict] = {}
        self._nearest: dict[str, list[_TsProject]] = {}
        self._go_files = go_files
        self._go_modules: list[tuple[str, str]] | None = None
        self._go_mod_dirs: list[str] = []
        self._rust_files = rust_files
        self._rust_crates: dict[str, list[str]] | None = None
        self._rust_packages: set[str] = set()
        self._rust_reexports: dict[str, list[list]] | None = None
        self._cs_files = cs_files
        self._cs_projects: dict[str, CsProject] | None = None
        self._python_files = python_files
        self._python_roots: dict[str, list[str]] | None = None

    # --- files ---------------------------------------------------------------

    def _text(self, rel: str) -> str | None:
        """A configuration file's text, or None when it is missing, too
        large, unreadable or under a path the walk ignores."""
        if rel not in self._texts:
            text = None
            path = self.root / rel
            if rel and not is_ignored_path(Path(rel)) and not is_gitignored(self.root, rel):
                try:
                    if path.is_file() and path.stat().st_size <= _MAX_CONFIG_BYTES:
                        text = path.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    text = None
            self._texts[rel] = text
        # Looked for, found or not: creating it later can change the result.
        self.read.add(rel)
        return self._texts[rel]

    def _config_file(self, folder: str, target: str) -> str | None:
        """An `extends` or `references` target: a file, the same with `.json`
        added, or a directory's tsconfig.json. A package name (no leading
        `.`) or an absolute path is outside the repository: None."""
        if not target.startswith("."):
            return None
        rel = _join(folder, target)
        if rel is None:
            return None
        for candidate in (rel, f"{rel}.json", f"{rel}/tsconfig.json" if rel else "tsconfig.json"):
            if candidate and self._text(candidate) is not None:
                return candidate
        return None

    # --- tsconfig --------------------------------------------------------------

    def _ts_options(self, rel: str) -> dict:
        """`rel`'s `baseUrl` and `paths`, each with the directory of the
        config that sets it, after its `extends` chain (a later base, then
        the config itself, overriding an earlier one). A chain that extends
        itself is malformed, as TypeScript has it: no options. Only a whole
        chain's result is cached, so a config read first through a cycle is
        judged the same as one read directly."""
        if rel not in self._options:
            self._options[rel] = self._ts_chain(rel, frozenset()) or {}
        return self._options[rel]

    def _ts_chain(self, rel: str, seen: frozenset[str]) -> dict | None:
        """`_ts_options` of `rel` reached through `seen`; None on a cycle."""
        merged: dict = {}
        data = parse_jsonc(self._text(rel) or "") or {}
        folder = posixpath.dirname(rel)
        extends = data.get("extends")
        for base in [extends] if isinstance(extends, str) else extends if isinstance(extends, list) else []:
            if isinstance(base, str) and (base_rel := self._config_file(folder, base)):
                if base_rel == rel or base_rel in seen:
                    return None
                inherited = self._ts_chain(base_rel, seen | {rel})
                if inherited is None:
                    return None
                merged.update(inherited)
        options = data.get("compilerOptions")
        if isinstance(options, dict):
            if isinstance(options.get("baseUrl"), str):
                merged["baseUrl"] = (options["baseUrl"], folder)
            if isinstance(options.get("paths"), dict):
                merged["paths"] = (options["paths"], folder)
        return merged

    def _ts_project(self, rel: str) -> _TsProject:
        options = self._ts_options(rel)
        base_url = None
        if "baseUrl" in options:
            value, folder = options["baseUrl"]
            base_url = _join(folder, value)
        paths: list[tuple[str, tuple[str, ...]]] = []
        paths_base = None
        if "paths" in options:
            value, folder = options["paths"]
            paths_base = base_url if base_url is not None else folder
            paths = [
                (pattern, tuple(t for t in targets if isinstance(t, str)))
                for pattern, targets in value.items()
                if isinstance(pattern, str) and isinstance(targets, list) and pattern.count("*") <= 1
            ]
        return _TsProject(base_url, paths_base, tuple(paths))

    def _ts_projects(self, folder: str) -> list[_TsProject]:
        """The projects that apply in `folder`: the nearest tsconfig.json
        (else jsconfig.json) above it, then the configs its `references` name,
        depth first."""
        if folder in self._nearest:
            return self._nearest[folder]
        rel = None
        for name in ("tsconfig.json", "jsconfig.json"):
            candidate = f"{folder}/{name}" if folder else name
            if self._text(candidate) is not None:
                rel = candidate
                break
        if rel is not None:
            projects: list[_TsProject] = []
            seen: set[str] = set()

            def visit(config: str) -> None:
                if config in seen:
                    return
                seen.add(config)
                projects.append(self._ts_project(config))
                data = parse_jsonc(self._text(config) or "") or {}
                references = data.get("references")
                for ref in references if isinstance(references, list) else []:
                    path = ref.get("path") if isinstance(ref, dict) else None
                    if isinstance(path, str) and (ref_rel := self._config_file(posixpath.dirname(config), path)):
                        visit(ref_rel)

            visit(rel)
        elif folder:
            projects = self._ts_projects(posixpath.dirname(folder))
        else:
            projects = []
        self._nearest[folder] = projects
        return projects

    def ts_alias(self, specifier: str, file_path: str) -> list[str] | None:
        """The repo-relative paths (extension not yet applied) a bare
        specifier names through the importing file's tsconfig: the first
        project with a `paths` pattern matching it (an exact pattern, else
        the longest prefix), else the first with a `baseUrl`. None when
        none applies: the specifier is external."""
        projects = self._ts_projects(posixpath.dirname(file_path))
        for project in projects:
            best = None
            for pattern, targets in project.paths:
                prefix, star, suffix = pattern.partition("*")
                if not star:
                    if specifier == pattern:
                        best = ("", targets, len(pattern) + 1)
                        break
                elif specifier.startswith(prefix) and specifier.endswith(suffix) and len(specifier) >= len(
                    prefix
                ) + len(suffix):
                    captured = specifier[len(prefix): len(specifier) - len(suffix)]
                    if best is None or len(prefix) > best[2]:
                        best = (captured, targets, len(prefix))
            if best is not None:
                captured, targets, _length = best
                found = [
                    rel for target in targets
                    if (rel := _join(project.paths_base or "", target.replace("*", captured))) is not None
                ]
                return found
        for project in projects:
            if project.base_url is not None:
                rel = _join(project.base_url, specifier)
                return [rel] if rel is not None else []
        return None

    # --- fingerprints ------------------------------------------------------------

    def _ts_digest(self, rel: str) -> object:
        data = parse_jsonc(self._text(rel) or "")
        if data is None:
            return None
        options = data.get("compilerOptions") if isinstance(data.get("compilerOptions"), dict) else {}
        return {
            "extends": data.get("extends"), "references": data.get("references"),
            "baseUrl": options.get("baseUrl"), "paths": options.get("paths"),
        }

    # --- go.mod / go.work -------------------------------------------------------

    def _go_entries(self, rel: str) -> list[tuple[str, str]]:
        """What one go.mod or go.work says about where import paths live:
        (import path prefix, repo-relative directory) for its `module` line
        (a go.mod's own directory) and for each `replace` whose target is a
        local directory (`=> ../shared`), resolved against the file's
        directory; one outside the repository is left out."""
        folder = posixpath.dirname(rel)
        entries = []
        in_replace = False
        for raw in (self._text(rel) or "").splitlines():
            line = raw.split("//", 1)[0].strip()
            if in_replace:
                if line == ")":
                    in_replace = False
                    continue
                directive = "replace " + line
            elif line.startswith("replace") and line[len("replace"):].strip() == "(":
                in_replace = True
                continue
            else:
                directive = line
            words = directive.split()
            if rel.endswith("go.mod") and len(words) == 2 and words[0] == "module":
                entries.append((words[1].strip('"'), folder))
            elif words[:1] == ["replace"] and "=>" in words:
                arrow = words.index("=>")
                old, target = words[1:2], words[arrow + 1:arrow + 2]
                local = bool(target) and (target[0] in (".", "..") or target[0].startswith(("./", "../")))
                path = _join(folder, target[0]) if old and local else None
                if path is not None:
                    entries.append((old[0].strip('"'), path))
        return entries

    def _go_digest(self, rel: str) -> object:
        return sorted(self._go_entries(rel))

    def go_modules(self) -> list[tuple[str, str]]:
        """Every in-repository Go import path prefix and the directory it
        names, longest prefix first (then by directory): each go.mod's
        module path and every local `replace` of a go.mod or go.work."""
        if self._go_modules is None:
            files = self._go_files() if self._go_files is not None else None
            if files is None:
                files = sorted(
                    path.relative_to(self.root).as_posix() for path in indexable_paths(self.root)
                    if path.name in ("go.mod", "go.work")
                )
            entries = {entry for rel in files for entry in self._go_entries(rel)}
            self._go_modules = sorted(entries, key=lambda entry: (-len(entry[0]), entry[1], entry[0]))
            self._go_mod_dirs = sorted({posixpath.dirname(rel) for rel in files if rel.endswith("go.mod")})
        return self._go_modules

    def go_package_dir(self, import_path: str) -> str | None:
        """The repo-relative directory of the Go package `import_path`, by
        the longest in-repository module path it is under (`go_modules`)
        whose module holds that directory (a directory with a go.mod of its
        own below the module's is another module); None when there is none
        (an external package)."""
        found = self.go_package(import_path)
        return found[1] if found is not None else None

    def go_package(self, import_path: str) -> tuple[str, str] | None:
        """(the module path, the directory) `go_package_dir` resolves
        `import_path` by, or None."""
        for prefix, folder in self.go_modules():
            if import_path == prefix or import_path.startswith(prefix + "/"):
                rest = import_path[len(prefix):].strip("/")
                found = _join(folder, rest) if rest else folder
                if found is not None and not any(
                    other != folder and (folder == "" or other.startswith(folder + "/"))
                    and (found == other or found.startswith(other + "/"))
                    for other in self._go_mod_dirs
                ):
                    return prefix, found
        return None

    # --- Cargo.toml ----------------------------------------------------------------

    def _cargo_entries(self, rel: str) -> list[tuple[str, str]]:
        """The library crate one Cargo.toml declares, as (the name code uses
        for it, its repo-relative root file): the `[lib]` name, else the
        `[package]` name with `-` as `_`, and the `[lib]` path, else the
        package's `src/lib.rs`. A workspace-only or malformed Cargo.toml
        declares none."""
        try:
            data = tomllib.loads(self._text(rel) or "")
        except tomllib.TOMLDecodeError:
            return []
        package = data.get("package")
        name = package.get("name") if isinstance(package, dict) else None
        if not isinstance(name, str):
            return []
        lib = data.get("lib") if isinstance(data.get("lib"), dict) else {}
        crate = lib.get("name") if isinstance(lib.get("name"), str) else name.replace("-", "_")
        lib_path = lib.get("path") if isinstance(lib.get("path"), str) else "src/lib.rs"
        root = _join(posixpath.dirname(rel), lib_path)
        return [(crate, root)] if root and crate else []

    def _rust_digest(self, rel: str) -> object:
        if rel.endswith(".rs"):
            return _rust_file_reexports(self.root / rel)
        return sorted(self._cargo_entries(rel))

    def _rust_inputs(self) -> list[str]:
        files = self._rust_files() if self._rust_files is not None else None
        return _rust_inputs(self.root) if files is None else files

    def rust_crates(self) -> dict[str, list[str]]:
        """Every in-repository library crate's name and its root files (two
        packages can share a name)."""
        if self._rust_crates is None:
            files = [rel for rel in self._rust_inputs() if posixpath.basename(rel) == "Cargo.toml"]
            crates: dict[str, set[str]] = {}
            for rel in files:
                for crate, root in self._cargo_entries(rel):
                    crates.setdefault(crate, set()).add(root)
                    self._rust_packages.add(posixpath.dirname(rel))
            self._rust_crates = {crate: sorted(roots) for crate, roots in sorted(crates.items())}
        return self._rust_crates

    def rust_packages(self) -> set[str]:
        """The directories of every in-repository package's Cargo.toml (""
        for the repository root)."""
        self.rust_crates()
        return self._rust_packages

    def rust_reexports(self) -> dict[str, list[list]]:
        """Every re-exporting .rs file's module-level re-exports, by its
        repo-relative path (`rust.extractor.reexports`)."""
        if self._rust_reexports is None:
            found = {}
            for rel in self._rust_inputs():
                if rel.endswith(".rs") and (each := _rust_file_reexports(self.root / rel)):
                    found[rel] = each
            self._rust_reexports = found
        return self._rust_reexports

    # --- pyproject.toml --------------------------------------------------------------

    def _pyproject_roots(self, rel: str) -> list[str]:
        """The source roots one pyproject.toml declares, repo-relative with a
        trailing `/` ("" the repository root): setuptools' `package-dir`
        (`"" = "lib"`, or `pkg = "lib/pkg"`, whose root is `lib`) and
        `packages.find` `where`, Poetry's `packages` `from`, and the
        directories of Hatch's wheel `packages`. A malformed file, or a
        root leaving the repository, declares none."""
        try:
            data = tomllib.loads(self._text(rel) or "")
        except tomllib.TOMLDecodeError:
            return []

        def table(parent: object, key: str) -> dict:
            value = parent.get(key) if isinstance(parent, dict) else None
            return value if isinstance(value, dict) else {}

        tool = table(data, "tool")
        setuptools = table(tool, "setuptools")
        dirs: list[str] = []
        for package, folder in table(setuptools, "package-dir").items():
            tail = package.replace(".", "/")
            if not isinstance(folder, str):
                continue
            if not package:
                dirs.append(folder)
            elif folder == tail or folder.endswith("/" + tail):
                dirs.append(folder[: -len(tail)])
        where = table(table(setuptools, "packages"), "find").get("where")
        dirs += [each for each in where if isinstance(each, str)] if isinstance(where, list) else []
        poetry = table(tool, "poetry").get("packages")
        dirs += [
            each["from"] for each in poetry if isinstance(each, dict) and isinstance(each.get("from"), str)
        ] if isinstance(poetry, list) else []
        hatch = table(table(table(table(tool, "hatch"), "build"), "targets"), "wheel").get("packages")
        dirs += [posixpath.dirname(each.rstrip("/")) for each in hatch if isinstance(each, str)] \
            if isinstance(hatch, list) else []
        roots = set()
        for each in dirs:
            joined = _join(posixpath.dirname(rel), each)
            if joined is not None:
                roots.add(f"{joined}/" if joined else "")
        return sorted(roots)

    def _python_digest(self, rel: str) -> object:
        return self._pyproject_roots(rel)

    def python_roots(self, file_path: str) -> tuple[str, ...]:
        """The source roots the pyproject.toml files at or above the Python
        file `file_path` (repo-relative) declare, sorted."""
        if self._python_roots is None:
            files = self._python_files() if self._python_files is not None else None
            if files is None:
                files = sorted(
                    path.relative_to(self.root).as_posix() for path in indexable_paths(self.root)
                    if path.name == "pyproject.toml"
                )
            self._python_roots = {rel: self._pyproject_roots(rel) for rel in files}
        found = set()
        for rel, roots in self._python_roots.items():
            folder = posixpath.dirname(rel)
            if not folder or file_path.startswith(folder + "/"):
                found.update(roots)
        return tuple(sorted(found))

    # --- C# projects ---------------------------------------------------------------

    def _cs_digest(self, rel: str) -> object:
        if rel.endswith(".cs"):
            return [list(astuple(each)) for each in cs_global_usings(self.root / rel)]
        text = self._text(rel)
        return [[op, *astuple(each)] for op, each in _cs_using_items(text)] + [
            ["ProjectReference", path] for path in _cs_reference_items(text)
        ]

    def cs_projects(self) -> dict[str, CsProject]:
        """Every C# project of the repository by its directory (see
        `CsProject`), read from the inputs `cs_files` gives (else
        `_cs_inputs` walks for them)."""
        if self._cs_projects is None:
            files = self._cs_files() if self._cs_files is not None else None
            if files is None:
                files = _cs_inputs(self.root)
            csproj: dict[str, list[str]] = {}
            for rel in sorted(files):
                if rel.endswith(".csproj"):
                    csproj.setdefault(posixpath.dirname(rel), []).append(rel)
            build = {rel for rel in files if posixpath.basename(rel) in _CS_BUILD_FILES}

            def nearest(folder: str, name: str) -> str | None:
                for each in _ancestors(folder):
                    rel = f"{each}/{name}" if each else name
                    if rel in build:
                        return rel
                return None

            def wrapping(folder: str) -> tuple[str | None, str | None]:
                return nearest(folder, _CS_BUILD_FILES[0]), nearest(folder, _CS_BUILD_FILES[1])

            global_usings: dict[str, set[CsUsing]] = {folder: set() for folder in csproj}
            for rel in files:
                if rel.endswith(".cs") and (owner := _cs_owner(rel, csproj)) is not None:
                    global_usings[owner].update(cs_global_usings(self.root / rel))
            projects = {}
            for folder, paths in sorted(csproj.items()):
                props, targets = (_cs_using_items(self._text(rel)) if rel else [] for rel in wrapping(folder))
                usings: set[CsUsing] = set()
                for path in paths:
                    kept: dict[CsUsing, None] = {}
                    for op, using in props + _cs_using_items(self._text(path)) + targets:
                        if op == "Include":
                            kept[using] = None
                        else:
                            kept = {each: None for each in kept if each.name != using.name}
                    usings.update(kept)
                projects[folder] = CsProject(
                    folder, tuple(paths), tuple(sorted(usings)), tuple(sorted(global_usings[folder]))
                )
            # A reference to a project file the repository does not hold
            # (missing, ignored, outside it) reaches nothing; a cycle ends
            # where it comes back. The nearest Directory.Build files' are
            # read from each project they wrap.
            owners = {_case_key(path): folder for folder, paths in csproj.items() for path in paths}
            direct = {}
            for folder, paths in csproj.items():
                items = [item for rel in [*paths, *wrapping(folder)] if rel
                         for item in _cs_reference_items(self._text(rel))]
                direct[folder] = {
                    owners[key] for ref in _cs_project_references(items, folder)
                    if (key := _case_key(ref)) in owners
                }
            for folder, project in projects.items():
                reached, todo = set(), list(direct[folder])
                while todo:
                    if (each := todo.pop()) not in reached:
                        reached.add(each)
                        todo += direct[each]
                reached.discard(folder)
                projects[folder] = replace(project, references=tuple(sorted(reached)))
            self._cs_projects = projects
        return self._cs_projects

    def cs_project(self, file_path: str) -> CsProject | None:
        """The project the .cs file `file_path` (repo-relative) belongs to:
        the nearest directory at or above it holding a `.csproj`. None when
        there is none."""
        projects = self.cs_projects()
        owner = _cs_owner(file_path, projects)
        return projects[owner] if owner is not None else None


def _cs_owner(rel: str, project_dirs) -> str | None:
    """The directory, among `project_dirs`, of the project `rel` belongs to:
    the nearest at or above it."""
    return next((each for each in _ancestors(posixpath.dirname(rel)) if each in project_dirs), None)


def _cs_inputs(root: Path, walked: list[str] | None = None) -> list[str]:
    """The C# configuration files of the repository at `root` (its
    indexable paths, or `walked`): every `.csproj`, Directory.Build.props
    and Directory.Build.targets, and every .cs file of a project whose head
    holds a `global using`, sorted."""
    if walked is None:
        walked = []
        for path in indexable_paths(root):
            try:
                walked.append(path.relative_to(root).as_posix())
            except ValueError:
                continue
    found = sorted(rel for rel in walked if config_language(posixpath.basename(rel)) == "csharp")
    project_dirs = {posixpath.dirname(rel) for rel in found if rel.endswith(".csproj")}
    found += sorted(
        rel for rel in walked
        if rel.endswith(".cs") and _cs_owner(rel, project_dirs) is not None and cs_global_usings(root / rel)
    )
    return sorted(found)


def _rust_file_reexports(path: Path) -> list[list]:
    """The re-exports of the .rs file at `path` (none when it is missing,
    unreadable or larger than a configuration file may be)."""
    from devgraph.indexer.rust.extractor import reexports  # the extractor imports this module

    try:
        if not path.is_file() or path.stat().st_size > _MAX_CONFIG_BYTES:
            return []
        return reexports(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


def _rust_inputs(root: Path, walked: list[str] | None = None) -> list[str]:
    """The Rust configuration files of the repository at `root` (its
    indexable paths, or `walked`): every Cargo.toml and every .rs file
    that re-exports, sorted."""
    if walked is None:
        walked = []
        for path in indexable_paths(root):
            try:
                walked.append(path.relative_to(root).as_posix())
            except ValueError:
                continue
    return sorted(
        rel for rel in walked
        if posixpath.basename(rel) == "Cargo.toml" or (rel.endswith(".rs") and _rust_file_reexports(root / rel))
    )


def csharp_projects(repo_root: Path) -> list[CsProject]:
    """Every C# project of the repository at `repo_root`, by directory (see
    `CsProject` and `ResolverConfig.cs_project`)."""
    return list(ResolverConfig(repo_root).cs_projects().values())


def fingerprints(repo_root: Path) -> dict[str, dict]:
    """Per language, a hash of the relevant content of every configuration
    file of the repository (`fingerprint`) and the paths it looked at
    (`inputs`): for TS each tsconfig*/jsconfig.json's `extends`,
    `references`, `baseUrl` and `paths`, with every config its `extends` and
    `references` reach; for Go each go.mod's `module` line and the local
    `replace` directives of each go.mod and go.work; for Rust each
    Cargo.toml's library crate name and root (`_cargo_entries`) and each
    re-exporting .rs file's re-exports (`_rust_inputs`); for C# each
    project file's and Directory.Build.props/.targets' `<Using>` items and
    `<ProjectReference>` paths and the `global using`
    directives of each project's .cs files
    (`_cs_inputs`); for Python each pyproject.toml's source roots
    (`_pyproject_roots`). A compiler flag, a `require` bump or a dependency or
    version change counts for nothing."""
    root = Path(repo_root)
    config = ResolverConfig(root)
    found: dict[str, set[str]] = {"ts": set(), "go": set(), "rust": set(), "csharp": set(), "python": set()}
    walked = []
    for path in indexable_paths(root):
        try:
            walked.append(path.relative_to(root).as_posix())
        except ValueError:
            continue
        lang = config_language(path.name)
        if lang is not None:
            found[lang].add(walked[-1])
    found["csharp"] = set(_cs_inputs(root, walked))
    found["rust"] = set(_rust_inputs(root, walked))
    result = {}
    for lang, files in found.items():
        config.read = set()
        if lang == "ts":
            for rel in sorted(files):
                config._ts_projects(posixpath.dirname(rel))  # reads extends and references
                config._ts_options(rel)
            digest = [[rel, config._ts_digest(rel)] for rel in sorted(config.read | files)]
        elif lang == "go":
            digest = [[rel, config._go_digest(rel)] for rel in sorted(files)]
        elif lang == "csharp":
            digest = [[rel, config._cs_digest(rel)] for rel in sorted(files)]
        elif lang == "python":
            digest = [[rel, config._python_digest(rel)] for rel in sorted(files)]
        else:
            digest = [[rel, config._rust_digest(rel)] for rel in sorted(files)]
        inputs = sorted(config.read | files)
        result[lang] = {
            "fingerprint": hashlib.sha256(json.dumps(digest, sort_keys=True).encode()).hexdigest(),
            "inputs": inputs,
        }
    return result
