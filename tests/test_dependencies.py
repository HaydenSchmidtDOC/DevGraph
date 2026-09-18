"""Regression guard: every declared Tree-sitter grammar must be installed.

Real bug this prevents: adding Kotlin support declared
`tree-sitter-kotlin>=1.1,<1.2` in `pyproject.toml` but the environment the
`devgraph` script actually runs in never had it installed. Because
`devgraph/indexer/dispatch.py` eagerly imports every language extractor at
module top, and `devgraph/cli/main.py` → `devgraph/agent/tray.py` import
`dispatch` eagerly, a single missing grammar made `devgraph tray start`
(and every other CLI command) crash with `ModuleNotFoundError` — even for
users who never touched a `.kt` file.

The invariant under test: for every `tree-sitter-*` dependency declared in
`pyproject.toml`, the corresponding importable Python module (the package
name with `-` mapped to `_`: `tree-sitter-c-sharp` -> `tree_sitter_c_sharp`)
must resolve. This catches the declare-but-don't-install failure mode at
test time, with a clear per-package message.
"""

import importlib.util
import tomllib
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _PROJECT_ROOT / "pyproject.toml"


def _distribution_name(dep: str) -> str:
    """Strip a PEP 508 requirement down to its bare distribution name."""
    return dep.split(">=")[0].split("<")[0].split(",")[0].strip()


def _declared_tree_sitter_grammars() -> list[str]:
    """Return the `tree-sitter-*` distribution names from pyproject.toml."""
    with _PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    deps = data["project"]["dependencies"]
    return [d for d in deps if _distribution_name(d).startswith("tree-sitter-")]


def test_pyproject_declares_at_least_one_grammar():
    """Sanity: pyproject.toml is present and declares grammars to guard."""
    grammars = _declared_tree_sitter_grammars()
    assert _PYPROJECT.exists()
    assert grammars, "expected tree-sitter-* dependencies in pyproject.toml"


@pytest.mark.parametrize("dist_name", _declared_tree_sitter_grammars())
def test_tree_sitter_grammar_is_installed(dist_name):
    """A declared grammar's module must be importable in the running env."""
    # distribution name -> importable module name: '-' -> '_'
    module_name = _distribution_name(dist_name).replace("-", "_")
    spec = importlib.util.find_spec(module_name)
    assert spec is not None, (
        f"{dist_name!r} is declared in pyproject.toml but {module_name!r} "
        f"is not importable. Run `pip install -e '.[dev]'` to install it — "
        f"a missing grammar crashes `devgraph` entirely (eager import chain)."
    )