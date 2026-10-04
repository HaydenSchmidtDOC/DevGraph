"""Whether the user approved a repository's `devgraph.tools.yaml` (`devgraph config tools trust`).

Project tools are served only when the file's bytes hash to the sha256 the user
approved for that repository, recorded in the registry (never in the
repository). Looked up by path, like `project_switch`, because the tool plane's
fingerprint only knows a path. Reads the registry read-only and fails closed:
anything but a matching record means "not trusted".
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

#: The trust states: only "trusted" serves. "untrusted" = no approval (or not registered),
#: "changed" = an approval for other bytes, "error" = the registry could not be read.
TRUST_STATES = ("trusted", "untrusted", "changed", "error")


def tools_sha256(data: bytes) -> str:
    """The sha256 (hex) of a tools file's exact bytes: what an approval pins."""
    return hashlib.sha256(data).hexdigest()


def trust_command(repo_id: str) -> str:
    return f"devgraph config tools trust {repo_id}"


def untrusted_reason(repo_id: str, state: str, *, to_model: bool = False) -> str:
    """Why an untrusted file's tools are not served, naming the command that approves it.

    `to_model` words it for an MCP client's model: only the user can approve, at a terminal.
    """
    from devgraph.config.project_tools import TOOLS_FILENAME

    if to_model:
        reason = (f"project tools not trusted; ask the user to review them and run "
                  f"`{trust_command(repo_id)}` in a terminal")
    else:
        reason = f"project tools not trusted (run {trust_command(repo_id)})"
    if state == "changed":
        return f"{TOOLS_FILENAME} changed since it was trusted; {reason}"
    if state == "error":
        return f"the trust state of {TOOLS_FILENAME} could not be read; {reason}"
    return reason


def _registry_db_path() -> Path:
    from devgraph.config.settings import get_settings

    return get_settings().registry_db_path


def project_tools_trust(repo_root: Path | str, data: bytes) -> str:
    """The trust state of `data` (the tools file's bytes as the caller read them) for `repo_root`.

    A row whose stored path equals `repo_root` exactly wins; otherwise the first row whose
    path resolves to the same directory. Rows with a relative path are ignored, and a
    registry that lies inside the repository is never trusted: a repository cannot
    approve itself.
    """
    try:
        target = Path(repo_root).expanduser().resolve()
        db = _registry_db_path()
        if not db.exists():
            return "untrusted"
        if db.resolve().is_relative_to(target):
            logger.warning("the registry %s lies inside repository %s; not serving its project tools", db, target)
            return "error"
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=0.5)
        try:
            rows = conn.execute("SELECT path, project_tools_sha256 FROM repos").fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        logger.debug("project tools trust unreadable (%s); not serving project tools", exc)
        return "error"
    rows = [(path, sha256) for path, sha256 in rows if isinstance(path, str) and Path(path).is_absolute()]
    exact = [sha256 for path, sha256 in rows if path == str(repo_root)]
    if exact:
        recorded = exact[0]
    else:
        recorded = None
        for path, sha256 in rows:
            try:
                if Path(path).resolve() == target:
                    recorded = sha256
                    break
            except OSError:
                continue
    if not recorded:
        return "untrusted"
    return "trusted" if recorded == tools_sha256(data) else "changed"
