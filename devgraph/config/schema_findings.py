"""Cross-repository project schema findings, shared by the CLI and the dashboard.

Pure: reads each repository's optional `devgraph.schema.yaml` and nothing else.
No Rich, no Typer, no graph connection, no registry write.
"""

from __future__ import annotations

from typing import Any


def enable_hint_id(repo: Any) -> str:
    """The repo id to show in `devgraph config enable <id>` hints.

    Registered repositories carry it as `repo_id`; a path-only stand-in
    (`config validate --repo`) supplies `hint_id` instead.
    """
    return getattr(repo, "hint_id", repo.repo_id)


def project_schema_findings(
    repos: list[Any], *, overrides: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Per-repository project schema state, plus cross-repository conflicts.

    Reads each registered repository's optional `devgraph.schema.yaml` and
    nothing else: no graph connection, no registry write, no cached or
    persisted schema state. Every finding carries an explicit `failed` flag
    rather than leaving the caller to infer severity from the rendered text.

    Each repository reports `absent` (no file — the built-in schema, exactly
    as before), `valid`, or `invalid` (the loader's own message). A final
    `conflict` finding is emitted per label that two or more repositories
    declare incompatibly. Labels are grouped case-insensitively because the
    generated constraint names are lower-cased: `Widget` and `widget` would
    generate one constraint name, and the loser's
    `CREATE CONSTRAINT ... IF NOT EXISTS` would silently no-op, shipping a
    label with no uniqueness constraint at all. Identical `(label, key)`
    declarations are not a conflict — they provision the same constraint, and
    registered repositories deliberately share one database.

    `overrides` maps a repo id to a parsed declaration (or `None` for "no
    file") that is used instead of reading that repository's file.
    """
    from devgraph.config.project_schema import (
        SCHEMA_FILENAME,
        ProjectSchemaError,
        load_project_schema,
        project_schema_path,
        resolve_declaration,
    )
    from devgraph.config.project_switch import project_config_enabled

    findings: list[dict[str, Any]] = []
    # Case-folded label -> the (repo_id, label, key) triples declaring it.
    declared: dict[str, list[tuple[str, str, tuple[str, ...]]]] = {}
    disabled_ids: set[str] = set()

    for repo in sorted(repos, key=lambda r: r.repo_id):
        disabled = not project_config_enabled(repo.path)
        if disabled:
            disabled_ids.add(repo.repo_id)
            findings.append(
                {
                    "repo_id": repo.repo_id,
                    "status": "disabled",
                    "detail": (
                        "project config disabled: devgraph.schema.yaml and devgraph.tools.yaml are "
                        f"ignored (`devgraph config enable {enable_hint_id(repo)}` to turn them on)"
                    ),
                    "failed": False,
                }
            )
        try:
            # `load_project_schema` returns None if and only if the file is
            # absent, and raises for every unreadable/malformed/invalid one,
            # so absent-vs-invalid is the loader's own distinction, not a
            # second `exists()` check that could disagree with it.
            # The file is checked even while the repository's project config
            # is switched off; the switch is reported separately above.
            if overrides is not None and repo.repo_id in overrides:
                declaration = overrides[repo.repo_id]
            else:
                declaration = load_project_schema(repo.path, respect_switch=False)
            if declaration is None:
                findings.append(
                    {
                        "repo_id": repo.repo_id,
                        "status": "absent",
                        "detail": f"no {SCHEMA_FILENAME} (built-in schema)",
                        "failed": False,
                    }
                )
                continue
            effective = resolve_declaration(
                declaration, origin=str(project_schema_path(repo.path))
            )
        except ProjectSchemaError as exc:
            findings.append(
                {
                    "repo_id": repo.repo_id,
                    "status": "invalid",
                    "detail": str(exc),
                    "failed": True,
                }
            )
            continue

        labels = ", ".join(node_type.label for node_type in effective.node_types)
        findings.append(
            {
                "repo_id": repo.repo_id,
                "status": "valid",
                "detail": f"extends: {effective.extends}; node types: {labels or 'none'}",
                "failed": False,
            }
        )
        for node_type in effective.node_types:
            declared.setdefault(node_type.label.casefold(), []).append(
                (repo.repo_id, node_type.label, tuple(node_type.key))
            )

    for folded, entries in sorted(declared.items()):
        if len({(label, key) for _repo_id, label, key in entries}) < 2:
            continue
        # A disabled repo stays in conflict detection: its constraints stay
        # in the shared database until its next rescan applies the built-in schema.
        described = "; ".join(
            f"{repo_id}{' (disabled)' if repo_id in disabled_ids else ''} "
            f"declares {label} keyed on ({', '.join(key)})"
            for repo_id, label, key in sorted(entries)
        )
        findings.append(
            {
                "repo_id": None,
                "status": "conflict",
                "label": folded,
                "repo_ids": sorted({repo_id for repo_id, _label, _key in entries}),
                "declarations": [
                    {
                        "repo_id": repo_id,
                        "label": label,
                        "key": list(key),
                        "disabled": repo_id in disabled_ids,
                    }
                    for repo_id, label, key in sorted(entries)
                ],
                "detail": (
                    f"incompatible declarations of label {folded!r} in one shared "
                    f"database: {described}. Only the first provisioned constraint "
                    f"takes effect; align the key or rename one label."
                ),
                "failed": True,
            }
        )

    return findings


def schema_conflicts(
    repos: list[Any], *, overrides: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Only the cross-repository `conflict` findings."""
    return [f for f in project_schema_findings(repos, overrides=overrides) if f["status"] == "conflict"]


def introduced_conflicts(repos: list[Any], repo_id: str, before: Any, after: Any) -> list[str]:
    """Details of conflicts naming `repo_id` that `after` creates and `before` did not.

    `before` and `after` are `repo_id`'s parsed declaration (or `None`) either
    side of an edit. A label that already conflicted for `repo_id` is not new.
    """

    def conflicted(declaration: Any) -> dict[str, str]:
        found = schema_conflicts(repos, overrides={repo_id: declaration})
        return {f["label"]: f["detail"] for f in found if repo_id in f["repo_ids"]}

    existing = conflicted(before)
    return [
        f"Creates a schema conflict: {detail}"
        for label, detail in conflicted(after).items()
        if label not in existing
    ]
