"""Path helpers shared by the indexer, CLI and MCP tools."""

from __future__ import annotations

from pathlib import Path


def is_within(resolved_path: Path, root: Path) -> bool:
    """True if an already-resolved path is root itself or lies beneath it.

    Compares path components rather than string prefixes, so `/x/proj` does
    not contain `/x/proj-private/...`.
    """
    return resolved_path.is_relative_to(root.resolve())
