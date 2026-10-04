"""The validated write path for `devgraph.tools.yaml`, the global tools store and `devgraph.schema.yaml`.

UI-independent: the CLI (`devgraph config tools|schema`) and the dashboard both call
these functions. They return structured results (new text, warnings, notes) and raise
`ConfigEditError` with a machine-readable `code`; nothing here prints or exits.

Every mutating function reads the current text, optionally compares a fingerprint
(`stale`), computes the new text with the comment-preserving splicers, validates the
whole resulting document, and only then writes it atomically. `dry_run=True` stops
before the write. Import this module directly, like `devgraph.config.project_tools`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

SCHEMA_SECTIONS = {
    "node_types": ("label", "node type"),
    "relationships": ("type", "relationship"),
}

TOOLS_RELOAD_NOTE = "Running MCP sessions pick this up within 2 seconds."


def tools_trust_note(repo_id: str) -> str:
    """What any write to a repository's tools file means: its sha256 changes, so it needs approving again."""
    return (f"Saving stops {repo_id}'s project tools being served until you run "
            f"`devgraph config tools trust {repo_id}`; running MCP sessions pick that up within 2 seconds.")
GLOBAL_TOOLS_NOTE = (
    "Global tools are served only in MCP sessions scoped to a registered repository; "
    "running sessions there pick up changes within 2 seconds."
)


# Ends the message of an add refused because the name is taken; the CLI swaps it
# for the `devgraph config ... edit <name>` command (see `_edit_errors`).
EDIT_INSTEAD = " — edit it instead"


class ConfigEditError(Exception):
    """A config edit was refused. `code` is one of: locked, exists, not_found, ambiguous,
    invalid, stale, not_regular, unreadable, io. `name` is the taken entry name on a
    tool or node type `exists`, so a caller can offer to replace that entry instead."""

    def __init__(self, message: str, code: str = "invalid", name: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.name = name


@dataclass
class EditResult:
    """What a mutation did (or, for `dry_run`, would do)."""

    path: Path
    text: str  # the new file contents
    written: bool
    fingerprint: str = ""  # of the file after the write (of `text` for a dry run); computed under the lock
    before: Any = None  # schema edits: declaration before / after (None when empty or invalid)
    after: Any = None
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    removed: dict[str, list[str] | None] | None = None  # resets: names the file declared; None = not readable


# --- files -----------------------------------------------------------------------------------------

_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


def _target_lock(path: Path) -> threading.RLock:
    """One re-entrant lock per resolved target path, serialising read-compare-write within this process."""
    key = path.resolve()
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


@contextlib.contextmanager
def _guard(path: Path, expected_fingerprint: str | None):
    """Hold the path's lock, then refuse a stale fingerprint and an unsafe target, in that order.

    Public mutators enter this before any existence/duplicate/locate check, so a stale
    `expected_fingerprint` is always `stale` and those checks run under the lock.
    """
    with _target_lock(path):
        _check_fingerprint(path, expected_fingerprint)
        _check_target(path)
        yield


def _after_fingerprint(path: Path, text: str, written: bool) -> str:
    if written:
        return file_fingerprint(path)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_fingerprint(path: Path) -> str:
    """`absent`, or `sha256:<hex>` of the file's bytes (same scheme as `schema_file_hash`, switch-independent).

    `not_regular` for a directory, FIFO or device, which is never opened (a FIFO would block the read).
    """
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "not_regular"
        return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable:{type(exc).__name__}"


def _not_regular(path: Path) -> ConfigEditError:
    return ConfigEditError(f"{path.name} cannot be read: not a regular file; fix or remove it by hand", "not_regular")


def _check_target(path: Path) -> None:
    """Refuse a symlink target (`os.replace` would swap the link for a file) and any non-regular file.

    Only the final component is checked; a symlinked parent directory is followed as the OS does.
    """
    if path.is_symlink():
        target = path.resolve()
        raise ConfigEditError(f"{path.name} is a symlink to {target}; edit {target} directly", "not_regular")
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise _not_regular(path)


def _check_fingerprint(path: Path, expected: str | None) -> None:
    if expected is not None and file_fingerprint(path) != expected:
        raise ConfigEditError(f"{path} changed since it was loaded; reload and try again", "stale")


def read_text(path: Path) -> str:
    """The file's text, or "" when absent; a directory, FIFO or device is `not_regular` and never opened."""
    try:
        if not path.exists():
            return ""
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise _not_regular(path)
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigEditError(f"{path}: cannot be read: {exc}", "unreadable")


def read_text_lossy(path: Path) -> str:
    """Like `read_text`, but "" when the file cannot be read: the project-config toggle only warns, so a broken file must not block it."""
    try:
        return read_text(path)
    except ConfigEditError:
        return ""


def write_atomically(path: Path, text: str) -> None:
    """Replace `path` with `text` via a temporary file in the same directory, keeping its mode."""
    mode = path.stat().st_mode & 0o7777 if path.exists() else None
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(temp, mode)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(temp, 0o666 & ~umask)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _splice_error(exc: Exception) -> ConfigEditError:
    """A splicer (`ListEditError`) error as a `ConfigEditError` carrying the splicer's code."""
    return ConfigEditError(str(exc), getattr(exc, "code", "invalid"))


# --- tools -----------------------------------------------------------------------------------------


def tools_path(root: Path | None) -> Path:
    """The scope's tools file: the repository's `devgraph.tools.yaml`, or the global store for `None`."""
    from devgraph.config.global_tools import global_tools_path
    from devgraph.config.project_tools import tools_file_path

    return global_tools_path() if root is None else tools_file_path(root)


def tools_effect_note(root: Path | None, record: Any) -> str:
    """Whether running MCP sessions will serve what is in this scope's file.

    `record` is the registered repository record for `root` (None when not registered).
    A registered repository's write always needs re-trusting: it changes the file's sha256.
    """
    if root is None:
        return GLOBAL_TOOLS_NOTE
    if record is None or not record.active:
        return (f"{root} is not a registered repository, so MCP sessions won't serve its tools; "
                "register it with `devgraph add`.")
    if not record.project_config_enabled:
        return (f"project config is disabled for {record.repo_id}, so MCP sessions won't serve its tools; "
                f"enable it with `devgraph config enable {record.repo_id}`.")
    return tools_trust_note(record.repo_id)


def refuse_builtin(name: Any) -> None:
    from devgraph.mcp.catalog import builtin_tool_names

    if isinstance(name, str) and name in builtin_tool_names():
        raise ConfigEditError(
            f"{name!r} is the name of a built-in tool; built-in tools are locked, choose another name", "locked"
        )


def tool_entries(root: Path | None) -> list[dict]:
    """The raw tool mappings in the scope's file."""
    from devgraph.config.tools_edit import ToolsEditError, tool_mappings

    try:
        return tool_mappings(read_text(tools_path(root)))
    except ToolsEditError as exc:
        raise _splice_error(exc)


def find_tool(root: Path | None, name: str) -> dict:
    """The tool named `name` in the scope; `not_found` if there is none."""
    current = next((m for m in tool_entries(root) if isinstance(m, dict) and m.get("name") == name), None)
    if current is None:
        raise ConfigEditError(f"no tool named {name!r} in this scope", "not_found")
    return current


def write_tools(
    root: Path | None,
    edit: Callable[[str], str],
    *,
    expected_fingerprint: str | None = None,
    dry_run: bool = False,
) -> EditResult:
    """Apply `edit(text) -> new text` to the scope's file, validate the whole result, then write.

    Nothing is written unless the resulting file is valid. Raises `ConfigEditError`.
    """
    from devgraph.config.global_tools import global_tools_text, save_global_tools
    from devgraph.config.project_tools import ProjectToolsError, parse_project_tools
    from devgraph.config.tools_edit import ToolsEditError, tool_mappings

    path = tools_path(root)
    with _guard(path, expected_fingerprint):
        text = read_text(path)
        try:
            if root is None:
                # The store is JSON: edit the mapping list (as one-per-line text), re-serialised by the store.
                import yaml

                mappings = tool_mappings(text)
                new_mappings = tool_mappings(
                    edit(yaml.safe_dump({"tools": mappings}, sort_keys=False) if mappings else "")
                )
                new_text = global_tools_text(new_mappings, path)
                if not dry_run:
                    save_global_tools(new_mappings)
            else:
                new_text = edit(text)
                parse_project_tools(new_text, path)
                if not dry_run:
                    write_atomically(path, new_text)
        except ToolsEditError as exc:
            raise _splice_error(exc)
        except ProjectToolsError as exc:
            raise ConfigEditError(str(exc), "invalid")
        except (OSError, TypeError, ValueError) as exc:
            raise ConfigEditError(str(exc), "io")
        return EditResult(path, new_text, not dry_run, _after_fingerprint(path, new_text, not dry_run))


def add_tool(root: Path | None, entry: dict, *, expected_fingerprint: str | None = None, dry_run: bool = False) -> EditResult:
    """Add one tool. `locked` for a built-in name, `exists` for a duplicate."""
    from devgraph.config.tools_edit import add_tool_text

    with _guard(tools_path(root), expected_fingerprint):
        name = entry.get("name")
        refuse_builtin(name)
        if any(isinstance(m, dict) and m.get("name") == name for m in tool_entries(root)):
            raise ConfigEditError(
                f"a tool named {name!r} already exists in this scope{EDIT_INSTEAD}", "exists",
                name=name,
            )
        return write_tools(root, lambda text: add_tool_text(text, entry), dry_run=dry_run)


def replace_tool(
    root: Path | None, name: str, entry: dict, *, expected_fingerprint: str | None = None, dry_run: bool = False
) -> EditResult:
    """Replace the tool `name`; `not_found` if absent, `locked`/`exists` if renamed to a built-in or taken name."""
    from devgraph.config.tools_edit import replace_tool_text

    with _guard(tools_path(root), expected_fingerprint):
        find_tool(root, name)
        new_name = entry.get("name")
        if new_name != name:
            refuse_builtin(new_name)
            if any(isinstance(m, dict) and m.get("name") == new_name for m in tool_entries(root)):
                raise ConfigEditError(f"a tool named {new_name!r} already exists in this scope", "exists", name=new_name)
        return write_tools(root, lambda text: replace_tool_text(text, name, entry), dry_run=dry_run)


def delete_tool(
    root: Path | None, name: str, *, expected_fingerprint: str | None = None, dry_run: bool = False
) -> EditResult:
    """Remove the tool `name`; `not_found` if absent."""
    from devgraph.config.tools_edit import delete_tool_text

    with _guard(tools_path(root), expected_fingerprint):
        return write_tools(root, lambda text: delete_tool_text(text, name), dry_run=dry_run)


# --- schema ----------------------------------------------------------------------------------------


def schema_effect_note(root: Path, record: Any) -> str:
    """When a schema change takes effect. `record` is the registered repository record for `root` (or None)."""
    from devgraph.agent.schema_rescan import QUIET_PERIOD_S

    if record is not None and not record.active:
        record = None
    if record is None:
        return f"{root} is not a registered repository, so DevGraph does not index it; register it with `devgraph add`."
    if not record.project_config_enabled:
        return (f"Not applied while the project config is disabled for {record.repo_id}; "
                f"enable it with `devgraph config enable {record.repo_id}`.")
    if record.watch_enabled:
        minutes = round(QUIET_PERIOD_S / 60)
        return (f"The change is applied about {minutes} minutes after the last edit while the DevGraph agent (tray or headless) "
                f"is running, or now with `devgraph rescan {record.repo_id} --now`.")
    return f"Not watched: run `devgraph rescan {record.repo_id} --now` to apply it."


def schema_declaration(text: str, path: Path):
    """The parsed declaration, or None when the text is empty or invalid."""
    from devgraph.config.project_schema import ProjectSchemaError, parse_project_schema

    try:
        return parse_project_schema(text, path)
    except ProjectSchemaError:
        return None


def entry_section(entry: dict) -> str:
    """`node_types` or `relationships`, from whether the mapping has `label` or `type`."""
    if ("label" in entry) == ("type" in entry):
        raise ConfigEditError(
            "a schema entry needs exactly one of `label` (a node type) or `type` (a relationship)", "invalid"
        )
    return "node_types" if "label" in entry else "relationships"


def locate_entry(text: str, name: str, node_type: bool = False, relationship: bool = False) -> str:
    """The section (`node_types`/`relationships`) that `name` addresses."""
    from devgraph.config.list_edit import ListEditError, entries
    from devgraph.config.project_schema import SCHEMA_FILENAME
    from devgraph.graph.schema import NODE_LABELS, RELATIONSHIP_TYPES

    if node_type and relationship:
        raise ConfigEditError("use either --node-type or --relationship, not both", "invalid")
    try:
        found = [
            section for section, (ident, _) in SCHEMA_SECTIONS.items()
            if any(isinstance(e, dict) and e.get(ident) == name for e in entries(text, key=section))
        ]
    except ListEditError as exc:
        raise _splice_error(exc)
    chosen = [s for s, flag in (("node_types", node_type), ("relationships", relationship)) if flag]
    if chosen:
        if chosen[0] not in found:
            raise ConfigEditError(f"no {SCHEMA_SECTIONS[chosen[0]][1]} named {name!r} in {SCHEMA_FILENAME}", "not_found")
        return chosen[0]
    if len(found) > 1:
        raise ConfigEditError(
            f"{name!r} is both a node type and a relationship type; pass --node-type or --relationship", "ambiguous"
        )
    if not found:
        hint = "; built-in schema entries cannot be changed" if name in NODE_LABELS + RELATIONSHIP_TYPES else ""
        raise ConfigEditError(f"no node type or relationship named {name!r} in {SCHEMA_FILENAME}{hint}", "not_found")
    return found[0]


def _section_entries(text: str, section: str) -> list:
    """The raw entries of one schema section."""
    from devgraph.config.list_edit import ListEditError, entries

    try:
        return entries(text, key=section)
    except ListEditError as exc:
        raise _splice_error(exc)


def find_schema_entry(text: str, name: str, section: str) -> dict:
    """The one entry of `section` named `name`; `ambiguous` when it is declared more than once."""
    from devgraph.config.list_edit import ListEditError, entries

    ident, noun = SCHEMA_SECTIONS[section]
    try:
        matches = [e for e in entries(text, key=section) if isinstance(e, dict) and e.get(ident) == name]
    except ListEditError as exc:
        raise _splice_error(exc)
    if len(matches) > 1:
        raise ConfigEditError(f"{noun} {name!r} is declared {len(matches)} times; edit the file by hand", "ambiguous")
    return matches[0]


def duplicate_relationship(entry: dict, existing: list) -> bool:
    """Whether `entry` equals an existing relationship once both are validated (`from: X` == `from: [X]`)."""
    from pydantic import ValidationError

    from devgraph.config.project_schema import RelationshipDecl

    def normal(raw):
        decl = RelationshipDecl.model_validate(raw)
        return (decl.type, decl.from_labels, decl.to, decl.provider, decl.custom)

    try:
        new = normal(entry)
    except ValidationError:
        return False  # invalid entries are reported by the write's own validation
    for other in existing:
        try:
            if normal(other) == new:
                return True
        except ValidationError:
            continue
    return False


def schema_edit(
    path: Path,
    edit: Callable[[str], str],
    *,
    invalid_prefix: str = "",
    invalid_hint: str = "",
    expected_fingerprint: str | None = None,
    dry_run: bool = False,
) -> tuple[str, str]:
    """Apply `edit(text) -> new text`, validate the whole result as the indexer would, then write it atomically.

    Returns (old text, new text).
    """
    from devgraph.config.list_edit import ListEditError
    from devgraph.config.project_schema import ProjectSchemaError, parse_project_schema, resolve_declaration

    with _guard(path, expected_fingerprint):
        old_text = read_text(path)
        try:
            new_text = edit(old_text)
            try:
                resolve_declaration(parse_project_schema(new_text, path), origin=str(path))
            except ProjectSchemaError as exc:
                raise ConfigEditError(f"{invalid_prefix}{exc}{invalid_hint}", "invalid")
            if not dry_run:
                write_atomically(path, new_text)
        except ListEditError as exc:
            raise _splice_error(exc)
        except OSError as exc:
            raise ConfigEditError(str(exc), "io")
    return old_text, new_text


def removed_types(before, after) -> tuple[list[str], list[str]]:
    """Declared node labels and relationship types in `before` but not `after` (None counts as empty).

    Built-in relationship types are never deleted by the indexer, so they are not reported.
    """
    from devgraph.graph.schema import RELATIONSHIP_TYPES

    def names(declaration):
        if declaration is None:
            return set(), set()
        return (
            {n.label for n in declaration.node_types},
            {r.type for r in declaration.relationships if r.type not in RELATIONSHIP_TYPES},
        )

    old_labels, old_types = names(before)
    new_labels, new_types = names(after)
    return sorted(old_labels - new_labels), sorted(old_types - new_types)


def pruned_types(before, after) -> list[str]:
    """Labels kept in `after` whose filesystem-sourced nodes the next apply deletes: source dropped or kind changed."""
    if before is None or after is None:
        return []
    now = {n.label: n for n in after.node_types}
    pruned = []
    for old in before.node_types:
        new = now.get(old.label)
        if new is None or old.source is None:
            continue
        if new.source is None:
            pruned.append(f"{old.label} (source removed)")
        elif new.source.kind != old.source.kind:
            pruned.append(f"{old.label} (kind {old.source.kind} -> {new.source.kind})")
    return sorted(pruned)


def changed_keys(before, after) -> list[tuple[str, tuple[str, ...]]]:
    """(label, old key) for node types present in both whose key changed."""
    if before is None or after is None:
        return []
    now = {n.label: n for n in after.node_types}
    return [(o.label, tuple(o.key)) for o in before.node_types if o.label in now and tuple(now[o.label].key) != tuple(o.key)]


def schema_change_warnings(before, after, record: Any = None) -> list[str]:
    """Plain-text warnings for a schema change: lost nodes and relationships, key changes.

    `record` (the registered repository record, or None) only chooses the wording of when they apply.
    """
    labels, types = removed_types(before, after)
    pruned = pruned_types(before, after)
    enabled = record is not None and record.active and record.project_config_enabled
    when = "the next rescan" if enabled else "applying this schema"
    warnings = []
    if labels:
        warnings.append(f"{when} deletes the nodes of the removed node type(s): {', '.join(labels)}.")
    if pruned:
        warnings.append(f"{when} deletes the nodes of node type(s) whose filesystem source changed: {', '.join(pruned)}.")
    if types:
        warnings.append(f"{when} removes the relationships of the removed relationship type(s): {', '.join(types)}.")
    for label, old_key in changed_keys(before, after):
        warnings.append(
            f"the uniqueness constraint on {label} keeps the old key ({', '.join(old_key)}) until this schema is "
            f"applied and every repository declaring {label} uses the new key; it also stays if existing nodes "
            f"violate the new key."
        )
    return warnings


def schema_entry_notes(entry: dict | None) -> list[str]:
    """Notes about a just-written entry: a node type with no source is never populated."""
    if entry is not None and "label" in entry and entry.get("source") is None:
        return [
            f"Note: no provider produces {entry['label']} nodes yet; only node types with "
            f"`source: {{provider: filesystem}}` are populated."
        ]
    return []


def _schema_result(path: Path, old_text: str, new_text: str, entry: dict | None, record: Any, dry_run: bool) -> EditResult:
    before = schema_declaration(old_text, path)
    after = schema_declaration(new_text, path)
    return EditResult(
        path=path,
        text=new_text,
        written=not dry_run,
        fingerprint=_after_fingerprint(path, new_text, not dry_run),
        before=before,
        after=after,
        warnings=schema_change_warnings(before, after, record),
        notes=schema_entry_notes(entry),
    )


def add_schema_entry(
    root: Path, entry: dict, *, record: Any = None, expected_fingerprint: str | None = None, dry_run: bool = False
) -> EditResult:
    """Add a node type (`label`) or relationship (`type`) to the repository's schema file."""
    from devgraph.config.list_edit import ListEditError, add_entry_text, entries
    from devgraph.config.project_schema import SCHEMA_VERSION, project_schema_path

    path = project_schema_path(root)
    with _guard(path, expected_fingerprint):
        section = entry_section(entry)
        ident, noun = SCHEMA_SECTIONS[section]
        try:
            existing = entries(read_text(path), key=section)
        except ListEditError as exc:
            raise _splice_error(exc)
        name = entry[ident]
        if section == "node_types" and any(isinstance(e, dict) and e.get(ident) == name for e in existing):
            raise ConfigEditError(
                f"a node type named {name!r} already exists{EDIT_INSTEAD}", "exists", name=name
            )
        if section == "relationships" and duplicate_relationship(entry, existing):
            raise ConfigEditError(f"an identical relationship {name!r} already exists", "exists")
        # Relationships may share a type with different endpoints; only node type labels are unique.
        old_text, new_text = schema_edit(
            path,
            lambda text: add_entry_text(
                text, entry, key=section, ident=ident, version=SCHEMA_VERSION, noun=noun, unique=section == "node_types"
            ),
            invalid_prefix="the new entry is invalid: ",
            dry_run=dry_run,
        )
        return _schema_result(path, old_text, new_text, entry, record, dry_run)


def replace_schema_entry(
    root: Path,
    name: str,
    entry: dict,
    *,
    node_type: bool = False,
    relationship: bool = False,
    record: Any = None,
    expected_fingerprint: str | None = None,
    dry_run: bool = False,
) -> EditResult:
    """Replace one entry; the new entry must stay in the same section."""
    from devgraph.config.list_edit import replace_entry_text
    from devgraph.config.project_schema import project_schema_path

    path = project_schema_path(root)
    with _guard(path, expected_fingerprint):
        section = locate_entry(read_text(path), name, node_type, relationship)
        ident, noun = SCHEMA_SECTIONS[section]
        find_schema_entry(read_text(path), name, section)
        if entry_section(entry) != section:
            raise ConfigEditError(f"the new entry must be a {noun} (with `{ident}`)", "invalid")
        new_name = entry.get(ident)
        if section == "node_types" and new_name != name and any(
            isinstance(e, dict) and e.get(ident) == new_name for e in _section_entries(read_text(path), section)
        ):
            raise ConfigEditError(f"a node type named {new_name!r} already exists", "exists")
        old_text, new_text = schema_edit(
            path,
            lambda current: replace_entry_text(current, name, entry, key=section, ident=ident, noun=noun),
            invalid_prefix="the new entry is invalid: ",
            dry_run=dry_run,
        )
        return _schema_result(path, old_text, new_text, entry, record, dry_run)


def delete_schema_entry(
    root: Path,
    name: str,
    *,
    node_type: bool = False,
    relationship: bool = False,
    record: Any = None,
    expected_fingerprint: str | None = None,
    dry_run: bool = False,
) -> EditResult:
    """Remove one entry from the repository's schema file."""
    from devgraph.config.list_edit import delete_entry_text
    from devgraph.config.project_schema import project_schema_path

    path = project_schema_path(root)
    with _guard(path, expected_fingerprint):
        section = locate_entry(read_text(path), name, node_type, relationship)
        ident, noun = SCHEMA_SECTIONS[section]
        old_text, new_text = schema_edit(
            path,
            lambda current: delete_entry_text(current, name, key=section, ident=ident, noun=noun),
            invalid_hint="; delete or edit the relationships that use it first" if section == "node_types" else "",
            dry_run=dry_run,
        )
        return _schema_result(path, old_text, new_text, None, record, dry_run)


# --- whole-file reset ------------------------------------------------------------------------------


def _reset_snapshot(path: Path) -> tuple[str, bool, Any, str]:
    """(fingerprint, readable, data, text) of one read of the file, so a reset's listing, its warnings
    and the fingerprint its confirm must match all describe the same bytes. Never raises: reset must
    work on a broken file."""
    import yaml

    from devgraph.config.project_tools import YAML_LOAD_ERRORS

    try:
        raw = path.read_bytes()
    except OSError:
        return file_fingerprint(path), False, None, ""
    fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return fingerprint, False, None, ""
    try:
        return fingerprint, True, yaml.safe_load(text), text
    except YAML_LOAD_ERRORS:
        return fingerprint, False, None, text


def _names(data: Any, key: str, ident: str) -> list[str]:
    entries = data.get(key) if isinstance(data, dict) else None
    return [e[ident] for e in entries if isinstance(e, dict) and isinstance(e.get(ident), str)] if isinstance(entries, list) else []


def _nothing_to_reset(path: Path, removed: dict[str, list[str] | None]) -> EditResult:
    return EditResult(path, "", False, "absent", notes=[f"Nothing to reset: {path} does not exist."], removed=removed)


def reset_tools(
    root: Path | None,
    *,
    record: Any = None,
    expected_fingerprint: str | None = None,
    dry_run: bool = False,
) -> EditResult:
    """Remove every tool in the scope: delete `devgraph.tools.yaml`, or empty the global store.

    The `removed` listing is best effort and never blocks the reset (it is the way out of a broken file).
    A dry run changes nothing and reports the fingerprint of the exact bytes it listed (what a confirm must match).
    """
    from devgraph.config.global_tools import ProjectToolsError, load_global_tools, save_global_tools
    from devgraph.mcp.catalog import builtin_tool_names

    path = tools_path(root)
    with _guard(path, expected_fingerprint):
        if not path.exists():
            return _nothing_to_reset(path, {"tools": []})
        fingerprint, readable, data, _ = _reset_snapshot(path)
        names = _names(data, "tools", "name") if readable else None
        notes: list[str] = []
        if root is None:
            if names:
                notes.append(
                    f"Removes {len(names)} global tool(s) from every repository's MCP sessions; "
                    "repositories with a project tool of the same name keep theirs."
                )
        elif names:
            try:
                store = load_global_tools()
            except ProjectToolsError:
                store = None
            builtin = builtin_tool_names()
            serves_project = record is None or record.project_config_enabled
            served = {t.name for t in store.tools if t.name not in builtin} if store is not None and serves_project else set()
            where = record.repo_id if record is not None else path.parent.name
            notes += [f"After the reset, global tool {n} is served in {where}." for n in names if n in served]
        if dry_run:
            return EditResult(path, "", False, fingerprint, notes=notes, removed={"tools": names})
        try:
            if root is None:
                save_global_tools([])
            else:
                path.unlink()
        except (OSError, ProjectToolsError) as exc:
            raise ConfigEditError(str(exc), "io")
        return EditResult(path, "", True, file_fingerprint(path), notes=notes, removed={"tools": names})


def reset_schema(
    root: Path, *, record: Any = None, expected_fingerprint: str | None = None, dry_run: bool = False
) -> EditResult:
    """Delete `devgraph.schema.yaml`, returning the repository to the built-in schema."""
    from devgraph.config.project_schema import project_schema_path

    path = project_schema_path(root)
    with _guard(path, expected_fingerprint):
        if not path.exists():
            return _nothing_to_reset(path, {"node_types": [], "relationships": []})
        fingerprint, readable, data, text = _reset_snapshot(path)
        removed = {
            "node_types": _names(data, "node_types", "label") if readable else None,
            "relationships": _names(data, "relationships", "type") if readable else None,
        }
        before = schema_declaration(text, path)
        if before is None and not (readable and data is None):
            warnings = [
                "The file is invalid, so what it declared can't be listed; the next rescan returns this repository "
                "to the built-in schema and deletes the nodes of any project type applied earlier."
            ]
        else:
            warnings = schema_change_warnings(before, None, record)
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                raise ConfigEditError(str(exc), "io")
        return EditResult(
            path, "", not dry_run, file_fingerprint(path) if not dry_run else fingerprint,
            before=before, warnings=warnings, removed=removed,
        )


def project_config_notes(repo_id: str) -> list[str]:
    """When flipping a repository's project-config switch takes effect (both directions; shared by the CLI and the dashboard)."""
    return [
        f"schema: applied at the next rescan (`devgraph rescan {repo_id} --now` to apply now)",
        "project tools: picked up by running MCP sessions within 2 s",
    ]


def project_config_change(record: Any, enabled: bool) -> tuple[list[str], list[str]]:
    """(warnings, notes) of switching `record`'s project config on or off. Pure; nothing is written.

    The project tools that stop being served are read from the tools file itself, not from what the
    tool plane resolves now, so the answer does not depend on the switch's current position.
    """
    from devgraph.config.global_tools import ProjectToolsError, load_global_tools
    from devgraph.config.project_schema import project_schema_path
    from devgraph.config.project_tools import parse_project_tools, tools_file_path
    from devgraph.mcp.catalog import builtin_tool_names

    root = Path(record.path)
    path = project_schema_path(root)
    decl = schema_declaration(read_text_lossy(path), path) if path.is_file() else None
    warnings = schema_change_warnings(None, decl, record) if enabled else schema_change_warnings(decl, None, record)
    tools_file = tools_file_path(root)
    if not enabled and tools_file.is_file():
        try:
            names = sorted({t.name for t in parse_project_tools(read_text(tools_file), tools_file).tools} - builtin_tool_names())
        except (ConfigEditError, ProjectToolsError):
            warnings.append(
                f"{tools_file.name} is invalid; project tools may still be served from the last good file "
                f"until the session restarts."
            )
            names = []
        if names:
            warnings.append(f"Project tools no longer served in {record.repo_id}: {', '.join(names)}")
            try:
                store = load_global_tools()
            except ProjectToolsError:
                store = None
            takeover = [n for n in names if store is not None and any(t.name == n for t in store.tools)]
            if takeover:
                warnings.append(f"Global tools of the same name take over in {record.repo_id}: {', '.join(takeover)}")
    return warnings, project_config_notes(record.repo_id)
