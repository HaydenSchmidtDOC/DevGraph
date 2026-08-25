"""Shared path/Podman/version-resolution helpers for `doctor` and `client-config`.

Both commands need the same three facts: the resolved venv python, the
resolved repo root, and where (if anywhere) Podman lives — building this once
avoids duplicating machine-portability logic across two commands.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


def resolve_venv_python() -> Path:
    """Resolve the currently-running interpreter's path.

    Since this only runs after `pip install -e .` succeeded and `devgraph` is
    a console-script, `sys.executable` inside the running process already IS
    the venv python — this is what makes output machine-portable instead of
    a hardcoded literal path.
    """
    return Path(sys.executable)


def resolve_repo_root() -> Path:
    """Walk up from this file to the directory containing pyproject.toml."""
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    raise RuntimeError("could not locate repo root (no pyproject.toml found above _env.py)")


def resolve_podman() -> Path | None:
    """Find podman on PATH, then at the documented Windows fallback location.

    Never mutates the user's persistent PATH — returns a full path for the
    caller to invoke directly when only the fallback location has it.
    """
    on_path = shutil.which("podman")
    if on_path:
        return Path(on_path)

    import os

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        fallback = Path(local_appdata) / "Programs" / "Podman" / "podman.exe"
        if fallback.exists():
            return fallback

    return None
