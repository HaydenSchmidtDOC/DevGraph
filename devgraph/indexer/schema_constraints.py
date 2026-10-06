"""Keep the constraints and indexes project schemas generate in line with what is declared.

A user node type generates a `<label>_repo_key` uniqueness constraint and, if
filesystem-sourced, a `<label>_repo_name` lookup index (config/project_schema.py).
Both are database-wide: every registered repository -- and any other DevGraph
installation or test run pointed at the same server -- shares them, so a
repository dropping a label must not drop a constraint another one still uses.
The registry is per installation and cannot answer that; the graph can. Each
`Repository` node records the user labels and keys its graph was last built
with (dispatch.apply_project_schema), and that recorded state is the authority
here. See docs/superpowers/specs/2026-10-05-schema-constraint-cleanup-design.md.

Only objects matching DevGraph's generated naming on a non-built-in label are
ever touched.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from devgraph.config.project_schema import (
    LABEL_PATTERN,
    PROPERTY_NAME_PATTERN,
    NodeTypeDecl,
    _builtin_constraint_names,
    _filesystem_index_name,
    _filesystem_index_statement,
    _user_constraint_name,
    _user_constraint_statement,
)
from devgraph.graph.engine import GraphEngine
from devgraph.graph.schema import NODE_LABELS

logger = logging.getLogger(__name__)

_BUILTIN_LABELS = frozenset(label.casefold() for label in NODE_LABELS)
_UNIQUENESS_TYPES = ("UNIQUENESS", "NODE_PROPERTY_UNIQUENESS")
_INDEX_PROPERTIES = ("repo_id", "name")


@dataclass(frozen=True, slots=True)
class GeneratedObject:
    """A constraint or index whose name and shape match what a user node type generates."""

    kind: str  # "constraint" or "index"
    name: str
    label: str
    properties: tuple[str, ...]

    def drop_statement(self) -> str:
        return f"DROP {self.kind.upper()} {self.name} IF EXISTS"

    def create_statement(self) -> str:
        properties = ", ".join(f"n.{p}" for p in self.properties)
        if self.kind == "constraint":
            return f"CREATE CONSTRAINT {self.name} IF NOT EXISTS FOR (n:{self.label}) REQUIRE ({properties}) IS UNIQUE"
        return f"CREATE INDEX {self.name} IF NOT EXISTS FOR (n:{self.label}) ON ({properties})"


def _is_user_label(label: object) -> bool:
    return isinstance(label, str) and bool(LABEL_PATTERN.fullmatch(label)) and label.casefold() not in _BUILTIN_LABELS


def _generated(row: dict) -> GeneratedObject | None:
    labels = row.get("labelsOrTypes") or []
    properties = tuple(row.get("properties") or ())
    if row.get("entityType") != "NODE" or len(labels) != 1 or not _is_user_label(labels[0]):
        return None
    label, name = labels[0], row.get("name")
    if name in _builtin_constraint_names():
        return None
    if not all(PROPERTY_NAME_PATTERN.fullmatch(p) for p in properties):
        return None
    if row["kind"] == "constraint":
        if row.get("type") not in _UNIQUENESS_TYPES or name != _user_constraint_name(label):
            return None
        if properties[:1] != ("repo_id",) or len(properties) < 2:
            return None
    elif row.get("type") != "RANGE" or name != _filesystem_index_name(label) or properties != _INDEX_PROPERTIES:
        return None
    return GeneratedObject(row["kind"], name, label, properties)


def generated_objects(engine: GraphEngine) -> dict[str, GeneratedObject]:
    """DevGraph-generated constraints and indexes in the database, by name."""
    found = (_generated(row) for row in engine.list_schema_objects())
    return {obj.name: obj for obj in found if obj is not None}


def encode_keys(node_types: Iterable[NodeTypeDecl]) -> list[str]:
    """The `schema_keys` recorded on a Repository node: one "Label:k1,k2" per node type."""
    return [f"{node_type.label}:{','.join(node_type.key)}" for node_type in node_types]


def recorded_declarations(engine: GraphEngine) -> dict[str, list[tuple[str, str, tuple[str, ...] | None]]]:
    """Case-folded label -> (repo_id, label, key) for every repository's recorded schema.

    Folded because generated names are lower-cased. `key` is None for a
    repository recorded before keys were (it counts as unknown).
    """
    declared: dict[str, list[tuple[str, str, tuple[str, ...] | None]]] = {}
    for state in engine.read_all_applied_schemas():
        keys: dict[str, tuple[str, ...]] = {}
        for entry in state.get("keys") or []:
            label, _, key = str(entry).partition(":")
            keys[label] = tuple(key.split(","))
        for label in state.get("labels") or []:
            if _is_user_label(label):
                declared.setdefault(label.casefold(), []).append((state["repo_id"], label, keys.get(label)))
    return declared


def release_labels(engine: GraphEngine, labels: Iterable[str]) -> list[str]:
    """Drop the generated objects of `labels` that no repository uses any more; returns the names dropped.

    A label is still used while any Repository node records it (compared
    case-insensitively, like the generated names) or any node carries it.
    """
    candidates = {label.casefold() for label in labels if _is_user_label(label)}
    if not candidates:
        return []
    in_use = recorded_declarations(engine)
    dropped: list[str] = []
    for obj in generated_objects(engine).values():
        folded = obj.label.casefold()
        if folded not in candidates or folded in in_use:
            continue
        if engine.label_has_nodes(obj.label):
            logger.info("keeping %s %s: nodes labelled %s remain", obj.kind, obj.name, obj.label)
            continue
        engine.run_schema_statement(obj.drop_statement())
        logger.info("dropped %s %s: no repository declares %s any more", obj.kind, obj.name, obj.label)
        dropped.append(obj.name)
    return dropped


def realign_keys(engine: GraphEngine, node_types: Iterable[NodeTypeDecl]) -> list[str]:
    """Replace generated objects whose definition differs from `node_types`; returns the names replaced.

    `CREATE ... IF NOT EXISTS` keys on the name alone, so a changed key (or a
    label differing only by case) otherwise keeps the old definition forever.
    Replaced only when every repository recording the label records exactly
    this label and key; otherwise it is a conflict (`devgraph config validate`
    reports it) and the object is left alone.
    """
    existing = generated_objects(engine)
    in_use: dict[str, list[tuple[str, str, tuple[str, ...] | None]]] | None = None
    replaced: list[str] = []
    for node_type in node_types:
        wanted: list[tuple[GeneratedObject, str]] = []
        constraint = existing.get(_user_constraint_name(node_type.label))
        if constraint is not None and (constraint.label, constraint.properties) != (
            node_type.label, ("repo_id", *node_type.key)
        ):
            wanted.append((constraint, _user_constraint_statement(node_type)))
        index = existing.get(_filesystem_index_name(node_type.label))
        if node_type.source is not None and index is not None and index.label != node_type.label:
            wanted.append((index, _filesystem_index_statement(node_type)))
        if not wanted:
            continue

        if in_use is None:
            in_use = recorded_declarations(engine)
        others = [
            (repo_id, label, key)
            for repo_id, label, key in in_use.get(node_type.label.casefold(), [])
            if (label, key) != (node_type.label, tuple(node_type.key))
        ]
        if others:
            described = "; ".join(
                f"{repo_id} declares {label} keyed on ({', '.join(key) if key else 'unknown until rescanned'})"
                for repo_id, label, key in sorted(others, key=lambda o: o[0])
            )
            logger.warning(
                "not replacing the generated constraint/index of %s keyed on (%s): other repositories "
                "disagree (%s); align the key or rename one label",
                node_type.label, ", ".join(node_type.key), described,
            )
            continue

        for old, create in wanted:
            # Checked before dropping, so data the new key rejects never costs
            # an unconstrained window (and is not retried on every apply).
            if old.kind == "constraint" and engine.has_duplicate_keys(node_type.label, ("repo_id", *node_type.key)):
                logger.warning(
                    "not replacing %s %s: duplicate nodes of %s share a (repo_id, %s) value; "
                    "remove the duplicates and rescan",
                    old.name, old.kind, node_type.label, ", ".join(node_type.key),
                )
                continue
            new = GeneratedObject(
                old.kind, old.name, node_type.label,
                ("repo_id", *node_type.key) if old.kind == "constraint" else _INDEX_PROPERTIES,
            )
            engine.run_schema_statement(old.drop_statement())
            try:
                engine.run_schema_statement(create)
            except Exception as exc:  # e.g. a node written since the duplicate check
                engine.run_schema_statement(old.create_statement())
                logger.warning(
                    "could not replace %s %s for %s keyed on (%s); kept the old definition: %s",
                    old.kind, old.name, node_type.label, ", ".join(node_type.key), exc,
                )
                continue
            if generated_objects(engine).get(old.name) != new:
                logger.warning("replaced %s %s but it does not read back as %s keyed on (%s)",
                               old.kind, old.name, node_type.label, ", ".join(node_type.key))
                continue
            logger.info("replaced %s %s to match %s keyed on (%s)", old.kind, old.name, node_type.label, ", ".join(node_type.key))
            replaced.append(old.name)
    return replaced


def constraint_drift(engine: GraphEngine) -> list[dict]:
    """Applied declarations the database does not enforce, for `devgraph doctor`.

    Per repository and recorded label (with a recorded key): `missing` when
    the generated constraint does not exist (a rescan re-provisions it), and
    `blocked` when it has another definition and duplicate nodes stop the new
    key from being created. Without duplicates, a differing definition is a
    `conflict` when another repository records the constraint's key, so
    realign_keys will not replace it; that entry also carries the
    `constraint_key` and the repositories that `declared_by` it. Otherwise it
    is replaced on the next apply, or is a cross-repository conflict that
    `devgraph config validate` reports.
    """
    existing = generated_objects(engine)
    drift: list[dict] = []
    for entries in recorded_declarations(engine).values():
        for repo_id, label, key in entries:
            if key is None:
                continue
            obj = existing.get(_user_constraint_name(label))
            wanted = ("repo_id", *key)
            extra: dict = {}
            if obj is None:
                status = "missing"
            elif (obj.label, obj.properties) == (label, wanted):
                continue
            elif all(PROPERTY_NAME_PATTERN.fullmatch(p) for p in key) and engine.has_duplicate_keys(label, wanted):
                status = "blocked"
            else:
                declared_by = sorted(
                    other for other, other_label, other_key in entries
                    if other != repo_id and (other_label, other_key) == (obj.label, obj.properties[1:])
                )
                if not declared_by:
                    continue
                status = "conflict"
                extra = {"constraint_key": obj.properties[1:], "declared_by": declared_by}
            drift.append({"repo_id": repo_id, "label": label, "status": status, "key": key, **extra})
    return sorted(drift, key=lambda d: (d["repo_id"], d["label"]))


def stale_generated_objects(engine: GraphEngine, declared: set[str]) -> list[GeneratedObject]:
    """Generated objects no repository uses: none records the label, it is not in
    `declared` (case-folded labels the registered repositories' files declare),
    and no node carries it. Sorted by name."""
    in_use = recorded_declarations(engine)
    return sorted(
        (
            obj for obj in generated_objects(engine).values()
            if obj.label.casefold() not in in_use
            and obj.label.casefold() not in declared
            and not engine.label_has_nodes(obj.label)
        ),
        key=lambda obj: obj.name,
    )
