"""Per-repo dashboard layout sidecar.

Follows the same sidecar-JSON-next-to-the-registry convention as
`repo_issues.json` (see routes.py's `_get_repo_issues`) rather than adding a
table to the registry: layout positions are client-cache data the dashboard
can happily lose, not something the indexer or MCP tools ever need to read.
One file per repo (not one shared file) keeps a corrupt or oversized layout
for one repo from taking down every other repo's cache.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from devgraph.config.settings import get_settings

_LAYOUT_VERSION = 1


def _layout_path(repo_id: str) -> Path:
    return get_settings().registry_db_path.parent / "layouts" / f"{repo_id}.json"


def load_layout(repo_id: str) -> dict[str, list[float]]:
    """Read the saved `{key: [x, y]}` positions map for `repo_id`.

    A missing, empty, or corrupt file is indistinguishable from "nothing
    saved yet" to the caller -- this is a position cache, not a source of
    truth, so failing to read it should never surface as an error, only as
    an empty layout the canvas falls back to auto-layout for.
    """
    path = _layout_path(repo_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    positions = raw.get("positions") if isinstance(raw, dict) else None
    return positions if isinstance(positions, dict) else {}


def save_layout(repo_id: str, positions: dict[str, list[float]]) -> None:
    """Atomically overwrite `repo_id`'s layout file with `positions`.

    Written to a temp file in the same directory and moved into place with
    `os.replace` so a reader (or a crash mid-write) never observes a
    half-written file -- the same failure mode `os.replace`'s atomicity on a
    single filesystem exists to rule out.
    """
    path = _layout_path(repo_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _LAYOUT_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
    }

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
