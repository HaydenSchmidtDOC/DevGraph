"""Decoding a source file's bytes for extraction.

Every extractor works on text, and a byte it can't decode must not cost an
identifier: `def café()` in a Latin-1 file read as UTF-8 with replacement
characters became `caf`. The order, for every file:

1. Python only: the PEP 263 coding declaration on line 1 or 2, when it names
   a codec that decodes the file.
2. UTF-8.
3. cp1252 (Windows "ANSI", the commonest undeclared non-UTF-8 source).
4. Latin-1, which decodes any byte, so nothing is ever replaced.
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

_PYTHON_SUFFIXES = (".py", ".pyi", ".pyw")


def decode_source(data: bytes, python: bool = False) -> str:
    """`data` as text, by the rules in the module docstring."""
    return decode_source_as(data, python)[0]


def decode_source_as(data: bytes, python: bool = False) -> tuple[str, str]:
    """`decode_source`, with the codec that decoded it ("utf-8" for valid UTF-8)."""
    if python:
        declared = declared_encoding(data)
        if declared is not None:
            try:
                return data.decode(declared), declared
            except (LookupError, UnicodeDecodeError):
                pass
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def is_python_path(path: str | Path) -> bool:
    """Whether a path's suffix gets the PEP 263 rule."""
    return Path(path).suffix in _PYTHON_SUFFIXES


def read_source(path: Path) -> str:
    """The file's text for extraction (see `decode_source`). Raises OSError
    when it can't be read."""
    return decode_source(Path(path).read_bytes(), python=is_python_path(path))


def declared_encoding(data: bytes) -> str | None:
    """The codec a PEP 263 declaration names, or None without one (a UTF-8
    BOM or no declaration: UTF-8 is tried anyway). `tokenize` applies the
    PEP's rules: line 1, or line 2 after a comment or blank line."""
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    except SyntaxError:  # an unknown codec, or one contradicting a BOM
        return None
    return None if encoding in ("utf-8", "utf-8-sig") else encoding
