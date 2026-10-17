"""Every platform skip in the suite is listed here, with its reason.

A test skipped on a platform never runs on the others: a Windows-only test
never runs on the Linux development machine, so a mistake in it surfaces only
in CI. Before adding one, simulate the platform instead (monkeypatch
`sys.platform` or `os.name` in the module under test, junction or symlink
detection, ctypes calls), as tests/indexer/test_walk.py does for junctions, and
stub DevGraph's own functions with `tests.helpers.stub`. List a skip below only
when the test needs the real OS: real symlinks, file modes or change times, a
POSIX shell script, the real process table, inotify, the X11 tray backend.
"""

import ast
import re
from collections import Counter
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLATFORM = re.compile(r"\bsys\.platform\b|\bos\.name\b|__import__\('os'\)\.name|\bplatform\.\w+\(")

#: (test file, skip reason): how many tests or parameters skip for it.
ALLOWED = {
    # Real symlinks, which need privileges on Windows.
    ("indexer/test_catch_up.py", "symlinks need privileges on Windows"): 1,
    ("indexer/test_docs_cache.py", "symlinks need privileges on Windows"): 2,
    ("indexer/test_docs_provider.py", "symlinks need privileges on Windows"): 2,
    ("indexer/test_scan_guards.py", "symlinks need privileges on Windows"): 4,
    ("indexer/test_walk.py", "symlinks need privileges on Windows"): 3,
    ("watcher/test_watcher_events.py", "symlinks need privileges on Windows"): 1,
    ("watcher/test_watcher_reconcile.py", "symlinks need privileges on Windows"): 1,
    ("indexer/test_git_compare.py", "symlinks, exec bits and kill_after_timeout are POSIX"): 5,
    # Real file names, modes and change times.
    ("indexer/test_docs_provider.py", "POSIX file names"): 1,
    ("config/test_edits.py", "POSIX permission bits"): 1,
    ("dashboard/test_db_metrics.py", "chmod cannot make a directory unreadable on Windows"): 1,
    ("dashboard/test_db_metrics.py", "root can read anything"): 1,
    ("indexer/test_docs_cache.py", "root reads a mode-000 file"): 1,
    ("mcp/test_tool_trust.py", "chmod cannot make a file unreadable on Windows"): 1,
    ("indexer/test_repo_root_guard.py", "permission bits don't stop this user reading the folder"): 2,
    ("indexer/test_catch_up.py", "ctime is the creation time on Windows"): 1,
    ("indexer/test_docs_cache.py", "no change time; see spec Known limits"): 3,
    ("indexer/test_docs_provider_live.py", "no change time; see the read cache spec's Known limits"): 1,
    # POSIX shell scripts standing in for programs.
    ("cli/test_doctor_scope.py", "the fake podman is a shell script"): 4,
    ("cli/test_mcp_registration.py", "the stub claude is a POSIX shell script"): 1,
    ("mcp/test_tools_impact_diff.py", "the fake ssh command is a POSIX shell script"): 1,
    ("indexer/test_git_history_integration.py", "the fake ssh command is a POSIX shell script"): 1,
    # The real process table: Windows records no command line to read.
    ("agent/test_tray_pid_identity.py", "the command line is not read on Windows"): 3,
    # Real sockets, the kernel's file events and the desktop's tray backend.
    ("agent/test_dashboard_port.py", "the real socket option exists only on Windows"): 1,
    ("watcher/test_watcher_inotify.py", "inotify is Linux-only"): 1,
    ("watcher/test_watcher_reconcile.py", "the root watch is Linux-only"): 1,
    ("agent/test_tray_startup.py", "X11 display handling is Linux-only"): 2,
}


def _reason(call: ast.Call) -> str:
    """The reason a skip gives: `reason=` of a skipif, the message of a skip."""
    for keyword in call.keywords:
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    if call.args[1:] or not call.keywords:
        message = call.args[-1] if call.args else None
        if isinstance(message, ast.Constant) and isinstance(message.value, str):
            return message.value
    return "<no reason>"


def _is_call_to(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == name


def platform_skips(tree: ast.Module) -> Counter:
    """{reason: count} of the platform skips in a test module: each `skipif` on a
    platform condition (each use, for one bound to a name), and each `pytest.skip`
    directly under an `if` on one."""
    skips: Counter = Counter()
    named = {}  # a module-level marker name: its reason
    for node in tree.body:
        if (
            isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id != "pytestmark" and _is_call_to(node.value, "skipif")
            and PLATFORM.search(ast.unparse(node.value.args[0]))
        ):
            named[node.targets[0].id] = _reason(node.value)
    for node in ast.walk(tree):
        if _is_call_to(node, "skipif") and node.args and PLATFORM.search(ast.unparse(node.args[0])):
            if not any(node is assign.value for assign in tree.body if isinstance(assign, ast.Assign)
                       and isinstance(assign.targets[0], ast.Name) and assign.targets[0].id in named):
                skips[_reason(node)] += 1
        elif isinstance(node, ast.Name) and node.id in named and isinstance(node.ctx, ast.Load):
            skips[named[node.id]] += 1
        elif isinstance(node, ast.If) and PLATFORM.search(ast.unparse(node.test)):
            for statement in node.body:
                if isinstance(statement, ast.Expr) and _is_call_to(statement.value, "skip"):
                    skips[_reason(statement.value)] += 1
    return skips


def test_every_platform_skip_is_listed_with_its_reason():
    found = {}
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        if rel.startswith("fixtures/"):
            continue
        for reason, count in platform_skips(ast.parse(path.read_text(encoding="utf-8"))).items():
            found[(rel, reason)] = count
    unlisted = {key: n for key, n in found.items() if ALLOWED.get(key) != n}
    stale = {key: n for key, n in ALLOWED.items() if key not in found}
    assert not unlisted, (
        "platform skips not in ALLOWED (or a different count); simulate the platform instead, "
        f"or list them with their reason: {unlisted}"
    )
    assert not stale, f"ALLOWED lists platform skips that are gone: {stale}"


def test_the_scan_finds_each_kind_of_platform_skip():
    tree = ast.parse(
        "import os, sys\n"
        "import pytest\n"
        "posix = pytest.mark.skipif(sys.platform == 'win32', reason='named')\n"
        "@posix\n"
        "def test_a(): pass\n"
        "@posix\n"
        "def test_b(): pass\n"
        "@pytest.mark.skipif(os.name != 'nt', reason='direct')\n"
        "def test_c(): pass\n"
        "@pytest.mark.parametrize('x', [pytest.param(1, marks=pytest.mark.skipif(sys.platform == 'darwin'))])\n"
        "def test_d(x): pass\n"
        "def test_e():\n"
        "    if os.name == 'nt':\n"
        "        pytest.skip('imperative')\n"
        "@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='a capability, not a platform')\n"
        "def test_f(): pass\n"
    )
    assert platform_skips(tree) == {"named": 2, "direct": 1, "<no reason>": 1, "imperative": 1}
