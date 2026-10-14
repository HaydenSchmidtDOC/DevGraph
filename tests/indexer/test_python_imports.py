"""Python imports resolve to the repository files they can name (resolve.py):
every candidate under the importer's ancestor directories, packages and
submodules included, and only the candidates that exist become edges.

See docs/superpowers/specs/2026-10-10-python-call-resolution-design.md (T1).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan
from devgraph.indexer.python.extractor import extract_python_file
from devgraph.indexer.python.resolve import (
    ModuleRef,
    Symbol,
    absolute_module,
    ancestor_roots,
    relative_dir,
    relative_module,
)


def targets(source: str, file_path: str) -> set[str]:
    result = extract_python_file(textwrap.dedent(source), file_path, "repo")
    return {r.to_name for r in result.relationships if r.rel_type == "IMPORTS"}


def bindings(source: str, file_path: str):
    from devgraph.indexer.python.extractor import _extract_imports, _make_parser

    data = textwrap.dedent(source).encode()
    return _extract_imports(_make_parser().parse(data).root_node, data, file_path)


# --- resolve.py ------------------------------------------------------------


def test_ancestor_roots_are_every_directory_above_the_file():
    assert ancestor_roots("main.py") == ("",)
    assert ancestor_roots("x/y/f.py") == ("", "x/", "x/y/")


def test_relative_dir_walks_up_one_parent_per_extra_dot():
    assert relative_dir("a/b/c", 1) == "a/b/c"
    assert relative_dir("a/b/c", 2) == "a/b"
    assert relative_dir("a/b/c", 3) == "a"
    assert relative_dir("a", 5) == ""


def test_a_module_names_its_file_its_package_and_its_prefix():
    ref = absolute_module("a.b", "x/f.py")
    assert ref.files() == ["a/b.py", "a/b/__init__.py", "x/a/b.py", "x/a/b/__init__.py"]
    assert ref.dirs() == ["a/b/", "x/a/b/"]
    assert ref.child("c").files() == ["a/b/c.py", "a/b/c/__init__.py", "x/a/b/c.py", "x/a/b/c/__init__.py"]


def test_a_relative_package_is_a_directory_never_a_file():
    ref = relative_module(".", "pkg/sub")
    assert ref == ModuleRef(("pkg/sub",), package_only=True)
    assert ref.files() == ["pkg/sub/__init__.py"]
    assert ref.dirs() == ["pkg/sub/"]


def test_the_repository_root_is_never_a_prefix():
    ref = relative_module(".", "")
    assert ref.files() == ["__init__.py"]
    assert ref.dirs() == []
    assert ref.child("n").files() == ["n.py", "n/__init__.py"]


@pytest.mark.parametrize(("module", "current", "paths"), [
    (".m", "a/b", ("a/b/m",)),
    ("..m", "a/b", ("a/m",)),
    ("...m.n", "a/b", ("m/n",)),
    ("..", "a/b", ("a",)),
])
def test_relative_modules(module, current, paths):
    assert relative_module(module, current).paths == paths


# --- IMPORTS targets ---------------------------------------------------------


def test_from_pkg_import_mod_targets_the_package_and_the_submodule():
    assert targets("from pkg import mod\n", "app.py") == {
        "pkg.py", "pkg/__init__.py", "pkg/mod.py", "pkg/mod/__init__.py",
    }


def test_a_name_imported_from_a_package_can_live_in_its_init():
    assert "pkg/__init__.py" in targets("from pkg import name\n", "app.py")


def test_src_layout_resolves_under_the_src_root():
    found = targets("from mylib.core import run\n", "src/mylib/cli.py")
    assert {"src/mylib/core.py", "src/mylib/core/__init__.py"} <= found


def test_nested_monorepo_root_resolves():
    found = targets("import shared.util\n", "services/api/app/main.py")
    assert "services/api/shared/util.py" in found
    assert "shared/util.py" in found


@pytest.mark.parametrize(("source", "expected"), [
    ("from . import x\n", {"a/b/__init__.py", "a/b/x.py", "a/b/x/__init__.py"}),
    ("from .. import x\n", {"a/__init__.py", "a/x.py", "a/x/__init__.py"}),
    ("from ... import x\n", {"__init__.py", "x.py", "x/__init__.py"}),
    ("from .m import n\n", {"a/b/m.py", "a/b/m/__init__.py", "a/b/m/n.py", "a/b/m/n/__init__.py"}),
])
def test_relative_import_targets(source, expected):
    assert targets(source, "a/b/f.py") == expected


def test_no_bare_dotted_targets():
    found = targets("import os\nimport a.b\nfrom typing import List\n", "app.py")
    assert found == {
        "os.py", "os/__init__.py", "a/b.py", "a/b/__init__.py",
        "typing.py", "typing/__init__.py", "typing/List.py", "typing/List/__init__.py",
    }


def test_function_local_and_aliased_imports_count():
    found = targets(
        """\
        def f():
            import json as j
            from pkg.mod import helper as h
        """,
        "app.py",
    )
    assert {"json.py", "pkg/mod.py", "pkg/mod/helper.py"} <= found


def test_each_target_is_one_edge():
    result = extract_python_file("from pkg import a, b\nfrom pkg import a\n", "app.py", "repo")
    imports = [r.to_name for r in result.relationships if r.rel_type == "IMPORTS"]
    assert len(imports) == len(set(imports))


# --- bindings -----------------------------------------------------------------


def test_bindings_record_modules_symbols_aliases_and_stars():
    b = bindings(
        """\
        import a.b
        import c as d
        from pkg import name as alias, other
        from .rel import *


        def f():
            from late import thing
        """,
        "x/f.py",
    )
    assert b.modules == {
        "a.b": absolute_module("a.b", "x/f.py"),
        "a": absolute_module("a", "x/f.py"),  # `import a.b` binds `a` too
        "d": absolute_module("c", "x/f.py"),
    }
    pkg = absolute_module("pkg", "x/f.py")
    assert b.symbols["alias"] == Symbol("name", pkg, pkg.child("name"))
    assert b.symbols["other"] == Symbol("other", pkg, pkg.child("other"))
    assert "thing" in b.symbols  # a function-local import binds file-wide
    assert b.stars == [relative_module(".rel", "x")]


# --- live ---------------------------------------------------------------------


@pytest.fixture
def engine():
    test_engine = GraphEngine(uri="bolt://127.0.0.1:7687", user="neo4j", password="devgraph-local-dev")
    try:
        test_engine.verify_connectivity()
    except Exception as e:
        pytest.skip(f"Neo4j not available: {e}")
    yield test_engine
    test_engine.close()


@pytest.fixture
def repo_id(engine):
    repo = f"zz-pyimports-{uuid.uuid4().hex[:8]}"
    engine.delete_repository(repo)
    yield repo
    engine.delete_repository(repo)


def test_imports_link_only_existing_candidates(engine, repo_id, tmp_path):
    files = {
        "pkg/__init__.py": "from pkg.impl import helper\n",
        "pkg/impl.py": "def helper():\n    return 1\n",
        "pkg/sub/__init__.py": "from . import leaf\nfrom .. import impl\n",
        "pkg/sub/leaf.py": "X = 1\n",
        "app.py": "from pkg import impl, helper\nimport pkg.sub.leaf\nimport os\n",
        "src/lib/core.py": "from lib import util\n",
        "src/lib/util.py": "Y = 2\n",
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)

    rows = engine.run_cypher(
        "MATCH (a:Module {repo_id: $r})-[:IMPORTS]->(b:Module {repo_id: $r}) RETURN a.name AS a, b.name AS b",
        {"r": repo_id},
    )
    assert {(row["a"], row["b"]) for row in rows} == {
        ("pkg/__init__.py", "pkg/impl.py"),
        ("pkg/sub/__init__.py", "pkg/sub/leaf.py"),
        ("pkg/sub/__init__.py", "pkg/__init__.py"),
        ("pkg/sub/__init__.py", "pkg/impl.py"),
        ("app.py", "pkg/__init__.py"),
        ("app.py", "pkg/impl.py"),
        ("app.py", "pkg/sub/leaf.py"),
        ("src/lib/core.py", "src/lib/util.py"),
    }
