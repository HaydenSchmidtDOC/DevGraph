"""Shared pytest configuration for environments without a desktop display."""

import os


if not os.environ.get("DISPLAY"):
    # pystray otherwise selects its X11 backend during module import and aborts
    # collection before tests that do not need a tray icon can run.
    os.environ.setdefault("PYSTRAY_BACKEND", "dummy")
