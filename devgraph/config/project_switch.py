"""Whether a repository's project config files are switched on (`devgraph config enable|disable`).

Looked up by path because the schema and tools loaders only know a path. Reads
the registry read-only and never creates it; anything unknown is "enabled".
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def _registry_db_path() -> Path:
    from devgraph.config.settings import get_settings

    return get_settings().registry_db_path


def project_config_enabled(repo_root: Path | str) -> bool:
    """True unless the registry has `repo_root` registered with its project config off."""
    db = _registry_db_path()
    if not db.exists():
        return True
    target = Path(repo_root).expanduser().resolve()
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error as exc:
        logger.debug("project config switch unreadable (%s); treating %s as enabled", exc, target)
        return True
    try:
        rows = conn.execute("SELECT path, project_config_enabled FROM repos").fetchall()
    except sqlite3.Error as exc:  # no table or no column yet, or locked
        logger.debug("project config switch unreadable (%s); treating %s as enabled", exc, target)
        return True
    finally:
        conn.close()
    for path, enabled in rows:
        try:
            if Path(path).expanduser().resolve() == target:
                return bool(enabled)
        except OSError:
            continue
    return True
