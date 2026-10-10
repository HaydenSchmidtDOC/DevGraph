"""Resolver configuration: the files besides a source file's own text that its
edges depend on. A TypeScript path alias (`@/lib/text`) means nothing without
the `tsconfig.json` that declares it, nor a Go import path without `go.mod`.

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md
(Resolver configuration). An extractor reads the configuration through one
`ResolverConfig` per batch; `fingerprints` summarises every configuration file
of the repository per language, so a batch that changes one can re-index that
language's files (dispatch.sync_resolver_config), and a fresh scan and an
incremental one extract every file under the same configuration.

A configuration file under a path the walk ignores (an ignored directory, or a
`.gitignore` rule) is treated as absent.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from devgraph.indexer.gitignore import is_gitignored
from devgraph.indexer.walk import indexable_paths, is_ignored_path

__all__ = [
    "LANGUAGE_SUFFIXES", "ResolverConfig", "config_language", "fingerprints", "parse_jsonc", "touches_config",
]

#: The source files each language's configuration applies to.
LANGUAGE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "ts": (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"),
    "go": (".go",),
}

#: The largest configuration file read; a bigger one is treated as absent.
_MAX_CONFIG_BYTES = 1024 * 1024


def config_language(name: str) -> str | None:
    """The language a file named `name` configures by its name alone (a
    tsconfig*.json or jsconfig.json, a go.mod or go.work), else None. A
    config reached only through `extends` can have any name; it is followed
    by path (see `ResolverConfig.read`)."""
    if name == "jsconfig.json" or (name.startswith("tsconfig") and name.endswith(".json")):
        return "ts"
    if name in ("go.mod", "go.work"):
        return "go"
    return None


def touches_config(rel: str, inputs: list[str]) -> bool:
    """Whether a changed or deleted repo-relative path can change the
    configuration: it is named like a configuration file, is one of the
    `inputs` a fingerprint read, or is a directory holding one."""
    if config_language(posixpath.basename(rel)):
        return True
    return any(each == rel or each.startswith(rel + "/") for each in inputs)


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
    answer, walks the repository for them."""

    def __init__(self, repo_root: Path, go_files: Callable[[], list[str] | None] | None = None) -> None:
        self.root = Path(repo_root)
        self.read: set[str] = set()
        self._texts: dict[str, str | None] = {}
        self._options: dict[str, dict] = {}
        self._nearest: dict[str, list[_TsProject]] = {}
        self._go_files = go_files
        self._go_modules: list[tuple[str, str]] | None = None
        self._go_mod_dirs: list[str] = []

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
        for prefix, folder in self.go_modules():
            if import_path == prefix or import_path.startswith(prefix + "/"):
                rest = import_path[len(prefix):].strip("/")
                found = _join(folder, rest) if rest else folder
                if found is not None and not any(
                    other != folder and (folder == "" or other.startswith(folder + "/"))
                    and (found == other or found.startswith(other + "/"))
                    for other in self._go_mod_dirs
                ):
                    return found
        return None


def fingerprints(repo_root: Path) -> dict[str, dict]:
    """Per language, a hash of the relevant content of every configuration
    file of the repository (`fingerprint`) and the paths it looked at
    (`inputs`): for TS each tsconfig*/jsconfig.json's `extends`,
    `references`, `baseUrl` and `paths`, with every config its `extends` and
    `references` reach; for Go each go.mod's `module` line and the local
    `replace` directives of each go.mod and go.work. A compiler flag or a
    `require` bump changes nothing."""
    root = Path(repo_root)
    config = ResolverConfig(root)
    found: dict[str, set[str]] = {"ts": set(), "go": set()}
    for path in indexable_paths(root):
        lang = config_language(path.name)
        if lang is not None:
            try:
                found[lang].add(path.relative_to(root).as_posix())
            except ValueError:
                continue
    result = {}
    for lang, files in found.items():
        config.read = set()
        if lang == "ts":
            for rel in sorted(files):
                config._ts_projects(posixpath.dirname(rel))  # reads extends and references
                config._ts_options(rel)
            digest = [[rel, config._ts_digest(rel)] for rel in sorted(config.read | files)]
        else:
            digest = [[rel, config._go_digest(rel)] for rel in sorted(files)]
        inputs = sorted(config.read | files)
        result[lang] = {
            "fingerprint": hashlib.sha256(json.dumps(digest, sort_keys=True).encode()).hexdigest(),
            "inputs": inputs,
        }
    return result
