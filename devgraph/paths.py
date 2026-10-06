"""Path helpers shared by the indexer, CLI and MCP tools."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_within(resolved_path: Path, root: Path) -> bool:
    """True if an already-resolved path is root itself or lies beneath it.

    Compares path components rather than string prefixes, so `/x/proj` does
    not contain `/x/proj-private/...`.
    """
    return resolved_path.is_relative_to(root.resolve())


#: The largest config file (tools file, schema file, global store) DevGraph reads.
MAX_CONFIG_BYTES = 1024 * 1024


class NotRegularFile(OSError):
    """A directory, FIFO, device or socket where a regular file was expected."""


class FileTooLarge(OSError):
    """A file larger than the cap `read_bounded` was given."""


def read_bounded(path: Path, max_bytes: int | None = None) -> bytes:
    """The bytes of a regular file of at most `max_bytes` (default `MAX_CONFIG_BYTES`).

    Opened non-blocking (a FIFO can't stall the open) and checked on the open
    descriptor, so a FIFO or device (a symlink to /dev/zero, say) is never read.
    Raises `NotRegularFile`, `FileTooLarge`, or the open's own `OSError`.
    """
    cap = MAX_CONFIG_BYTES if max_bytes is None else max_bytes
    # O_BINARY: Windows otherwise opens in text mode and rewrites CRLF to LF,
    # so hashes and fingerprints would not be of the file's bytes.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotRegularFile(f"{path}: not a regular file")
        chunks, size = [], 0
        while size <= cap:
            chunk = os.read(fd, cap + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > cap:
        raise FileTooLarge(f"{path}: larger than {cap} bytes")
    return b"".join(chunks)
