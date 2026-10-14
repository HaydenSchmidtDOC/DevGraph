"""Python CALLS resolve through scope and imports (the extractor's tiers):
same-file defs, imported names, `self`/`super()` methods, module attributes
and typed receivers are pinned to the files they can be in; an unknown
receiver keeps a bare-name edge unless it is a literal or a builtin type's
method; every call site of one caller to one name collapses to one set of
rows.

See docs/superpowers/specs/2026-10-10-python-call-resolution-design.md (T3).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import full_scan, index_paths, remove_paths
from devgraph.indexer.python.extractor import extract_python_file
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


def calls(source: str, file_path: str = "app/main.py") -> set[tuple]:
    """(caller, callee, to_file, confidence, caller_class) per CALLS row."""
    result = extract_python_file(textwrap.dedent(source), file_path, "repo")
    return {
        (r.from_name, r.to_name, r.to_file, r.properties["confidence"], r.properties.get("caller_class"))
        for r in result.relationships
        if r.rel_type == "CALLS"
    }


def targets(source: str, caller: str, callee: str, file_path: str = "app/main.py") -> set[tuple]:
    return {(to_file, conf) for c, n, to_file, conf, _cls in calls(source, file_path) if (c, n) == (caller, callee)}


def test_same_file_and_enclosing_defs_resolve_to_this_file():
    source = """\
        def helper():
            pass


        def outer():
            def inner():
                pass
            inner()
            helper()
        """
    assert targets(source, "outer", "inner") == {("app/main.py", "resolved")}
    assert targets(source, "outer", "helper") == {("app/main.py", "resolved")}


def test_a_from_imported_name_goes_to_its_module_and_package():
    found = targets("from pkg.mod import f\n\n\ndef g():\n    f()\n", "g", "f", "main.py")
    assert found == {("pkg/mod.py", "resolved"), ("pkg/mod/__init__.py", "resolved"), ("pkg/mod/", "package")}


def test_an_alias_calls_the_name_it_was_imported_as():
    source = "from pkg import f as h\n\n\ndef g():\n    h()\n"
    assert targets(source, "g", "f", "main.py") == {
        ("pkg.py", "resolved"), ("pkg/__init__.py", "resolved"), ("pkg/", "package"),
    }
    assert targets(source, "g", "h", "main.py") == set()


def test_a_relative_import_resolves_against_the_package():
    found = targets("from .util import f\n\n\ndef g():\n    f()\n", "g", "f", "a/b/main.py")
    assert found == {("a/b/util.py", "resolved"), ("a/b/util/__init__.py", "resolved"), ("a/b/util/", "package")}


def test_a_module_attribute_resolves_to_the_module():
    source = "import lib.lifecycle as lifecycle\n\n\ndef g():\n    lifecycle.start()\n"
    assert targets(source, "g", "start", "main.py") == {
        ("lib/lifecycle.py", "resolved"), ("lib/lifecycle/__init__.py", "resolved"), ("lib/lifecycle/", "package"),
    }
    dotted = "import lib.lifecycle\n\n\ndef g():\n    lib.lifecycle.start()\n"
    assert ("lib/lifecycle.py", "resolved") in targets(dotted, "g", "start", "main.py")


def test_self_methods_in_the_class_or_an_in_file_base_resolve_here():
    source = """\
        class Base:
            def shared(self):
                pass


        class Child(Base):
            def own(self):
                pass

            def act(self):
                self.own()
                self.shared()
                super().shared()
        """
    assert targets(source, "act", "own") == {("app/main.py", "resolved")}
    assert targets(source, "act", "shared") == {("app/main.py", "resolved")}


def test_self_method_of_an_imported_base_goes_to_the_base_module():
    source = """\
        from base import Base


        class Child(Base):
            def act(self):
                self.shared()
        """
    assert targets(source, "act", "shared", "main.py") == {
        ("base.py", "resolved"), ("base/__init__.py", "resolved"), ("base/", "package"),
    }


def test_annotated_and_constructed_receivers_resolve_to_their_class():
    source = """\
        from engine import GraphEngine


        class Local:
            def run(self):
                pass


        def f(engine: GraphEngine, maybe: "GraphEngine | None" = None, other: Optional[Local] = None):
            engine.upsert_nodes()
            maybe.close_all()
            other.run()
            built = GraphEngine(1)
            built.flush_all()
        """
    expected = {("engine.py", "resolved"), ("engine/__init__.py", "resolved"), ("engine/", "package")}
    assert targets(source, "f", "upsert_nodes", "main.py") == expected
    assert targets(source, "f", "close_all", "main.py") == expected
    assert targets(source, "f", "flush_all", "main.py") == expected
    assert targets(source, "f", "run", "main.py") == {("main.py", "resolved")}


def test_a_parameter_named_after_a_fixture_in_the_file_is_typed_by_it():
    source = """\
        import pytest
        from engine import GraphEngine


        @pytest.fixture
        def engine():
            built = GraphEngine()
            yield built


        @pytest.fixture
        def other() -> GraphEngine:
            return make()


        def helper(engine):
            return engine


        def test_it(engine, other, helper):
            engine.run_cypher()
            other.close_all()
            helper.work()
        """
    expected = {("engine.py", "resolved"), ("engine/__init__.py", "resolved"), ("engine/", "package")}
    assert targets(source, "test_it", "run_cypher", "test_x.py") == expected
    assert targets(source, "test_it", "close_all", "test_x.py") == expected
    assert targets(source, "test_it", "work", "test_x.py") == {(None, "name")}  # not a fixture


@pytest.mark.parametrize("annotation", ["Any", "typing.Any", "t.Any", "object", '"Any"', "Any | None"])
def test_an_any_or_object_annotation_is_an_untyped_receiver(annotation):
    source = f"import typing\nimport typing as t\nfrom typing import Any\n\n\ndef f(engine: {annotation}):\n    engine.upsert_nodes()\n"
    assert targets(source, "f", "upsert_nodes", "main.py") == {(None, "name")}


def test_an_in_file_subclass_receiver_walks_its_bases():
    source = """\
        from engine import GraphEngine


        class Fake(GraphEngine):
            pass


        class Local:
            def own(self):
                pass


        def f(fake: Fake):
            Fake().run_cypher()
            fake.close_all()
            Local().own()
            Local().missing()
        """
    expected = {("engine.py", "resolved"), ("engine/__init__.py", "resolved"), ("engine/", "package")}
    assert targets(source, "f", "run_cypher", "main.py") == expected
    assert targets(source, "f", "close_all", "main.py") == expected
    assert targets(source, "f", "own", "main.py") == {("main.py", "resolved")}
    # Nothing in the file or its imports defines it: an untyped call by name.
    assert targets(source, "f", "missing", "main.py") == {(None, "name")}


def test_a_parameter_shadows_an_import_of_its_name():
    source = """\
        import os
        from pkg import helper


        def go(helper, os):
            helper()
            os.getcwd()
        """
    assert targets(source, "go", "helper", "main.py") == set()
    assert targets(source, "go", "getcwd", "main.py") == {(None, "name")}


def test_importing_a_submodule_binds_its_top_package():
    source = "import os.path\n\n\ndef f():\n    os.getcwd()\n"
    assert targets(source, "f", "getcwd", "main.py") == {
        ("os.py", "resolved"), ("os/__init__.py", "resolved"), ("os/", "package"),
    }


def test_builtin_names_are_a_fixed_set():
    from devgraph.indexer.python import extractor

    assert "print" in extractor._BUILTIN_NAMES and "len" in extractor._BUILTIN_NAMES
    assert "breakpoint" in extractor._BUILTIN_NAMES and "helper" not in extractor._BUILTIN_NAMES
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(extractor))
    (assign,) = [n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "_BUILTIN_NAMES"]
    assert isinstance(assign.value, ast.Call) and isinstance(assign.value.args[0], ast.Set)


def test_an_unknown_receiver_keeps_a_bare_name_edge():
    assert targets("def f(obj):\n    obj.work()\n", "f", "work") == {(None, "name")}


def test_literals_builtin_methods_and_builtins_link_nothing():
    found = calls("""\
        def f(obj):
            "".join([])
            {}.update()
            obj.get("k")
            obj.append(1)
            print(len([]))
            undefined_name()
        """)
    assert found == set()


def test_a_stoplisted_method_still_resolves_on_a_known_receiver():
    source = "class Store:\n    def get(self):\n        pass\n\n    def use(self):\n        return self.get()\n"
    assert targets(source, "use", "get") == {("app/main.py", "resolved")}


def test_a_star_import_binds_unknown_bare_names_but_not_builtins():
    source = "from pkg.mod import *\n\n\ndef g():\n    f()\n    print()\n"
    assert targets(source, "g", "f", "main.py") == {
        ("pkg/mod.py", "resolved"), ("pkg/mod/__init__.py", "resolved"), ("pkg/mod/", "package"),
    }
    assert targets(source, "g", "print", "main.py") == set()


def test_a_resolved_call_suppresses_the_bare_row_of_the_same_name():
    """The exclusivity rule (and its recall loss): `runner.run()` would be a
    bare edge on its own, but `subprocess.run()` resolves `run` in the same
    caller, so only the resolved rows are written."""
    source = "import subprocess\n\n\ndef f(runner):\n    subprocess.run()\n    runner.run()\n"
    found = targets(source, "f", "run", "main.py")
    assert found and (None, "name") not in found


def test_a_package_row_leaves_out_the_exact_files_and_this_file():
    result = extract_python_file("from pkg import f\n\n\ndef g():\n    f()\n", "pkg/user.py", "repo")
    (prefix,) = [r for r in result.relationships if r.rel_type == "CALLS" and r.to_file == "pkg/"]
    assert prefix.exclude == sorted({"pkg.py", "pkg/__init__.py", "pkg/pkg.py", "pkg/pkg/__init__.py", "pkg/user.py"})


def test_call_sites_collapse_to_one_caller_class_per_callee():
    source = """\
        class B:
            def run(self):
                obj.work()


        class A:
            def run(self):
                obj.work()
        """
    assert calls(source) == {("run", "work", None, "name", "A")}


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
    repo = f"zz-pycalls-{uuid.uuid4().hex[:8]}"
    ids = [repo, f"{repo}_fresh"]
    for each in ids:
        engine.delete_repository(each)
    yield repo
    for each in ids:
        engine.delete_repository(each)


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def caller_edges(engine, repo_id, caller):
    rows = engine.run_cypher(
        "MATCH (a {repo_id: $r, name: $c})-[x:CALLS]->(b) RETURN b.name AS n, b.file AS f, x.confidence AS conf",
        {"r": repo_id, "c": caller},
    )
    return sorted((row["n"], row["f"], row["conf"]) for row in rows)


def equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_a_re_exported_function_is_found_and_followed_when_it_moves(engine, repo_id, tmp_path):
    write(tmp_path, "app.py", "from pkg import get_settings\n\n\ndef main():\n    return get_settings()\n")
    write(tmp_path, "pkg/__init__.py", "from pkg.settings import get_settings\n")
    settings = write(tmp_path, "pkg/settings.py", "def get_settings():\n    return 1\n")
    write(tmp_path, "other/settings.py", "def get_settings():\n    return 2\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert caller_edges(engine, repo_id, "main") == [("get_settings", "pkg/settings.py", "package")]

    write(tmp_path, "pkg/settings.py", "X = 1\n")
    moved = write(tmp_path, "pkg/config/core.py", "def get_settings():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {settings, moved})
    assert caller_edges(engine, repo_id, "main") == [("get_settings", "pkg/config/core.py", "package")]
    equals_fresh(engine, repo_id, tmp_path)


def test_a_resolved_call_returns_with_its_deleted_module(engine, repo_id, tmp_path):
    write(tmp_path, "app.py", "import lib.tools as tools\n\n\ndef main():\n    return tools.run()\n")
    tools = write(tmp_path, "lib/tools.py", "def run():\n    return 1\n")
    write(tmp_path, "elsewhere.py", "def run():\n    return 2\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert caller_edges(engine, repo_id, "main") == [("run", "lib/tools.py", "resolved")]

    tools.unlink()
    remove_paths(engine, repo_id, tmp_path, {tools})
    assert caller_edges(engine, repo_id, "main") == []
    write(tmp_path, "lib/tools.py", "def run():\n    return 1\n")
    index_paths(engine, repo_id, tmp_path, {tools})
    assert caller_edges(engine, repo_id, "main") == [("run", "lib/tools.py", "resolved")]
    equals_fresh(engine, repo_id, tmp_path)


def test_the_index_upgrade_replaces_bare_python_calls(engine, repo_id, tmp_path, monkeypatch):
    """A graph indexed before calls were resolved (format 3) has bare-name
    CALLS with no confidence and seven-field name_refs; catch_up's automatic
    upgrade (a full_scan) leaves only the resolved edges."""
    from datetime import datetime, timezone

    from devgraph.indexer import dispatch

    write(tmp_path, "app.py", "from lib import helper\n\n\ndef main():\n    return helper()\n")
    write(tmp_path, "lib.py", "def helper():\n    return 1\n")
    write(tmp_path, "other.py", "def helper():\n    return 2\n")
    real = dispatch.extract_python_file

    def old_extract(content, rel_path, repo_id):
        result = real(content, rel_path, repo_id)
        for rel in result.relationships:
            if rel.rel_type == "CALLS":
                rel.to_file, rel.exclude, rel.properties = None, None, None
        return result

    with monkeypatch.context() as old_extractor:
        old_extractor.setattr(dispatch, "extract_python_file", old_extract)
        provision_repository_schema(engine, tmp_path)
        engine.upsert_repository(repo_id, repo_id, str(tmp_path))
        full_scan(engine, repo_id, tmp_path)
    engine.run_cypher(
        "MATCH (m:Module {repo_id: $r}) SET m.name_refs = [e IN m.name_refs | "
        "reduce(s = '', f IN split(e, $sep)[0..7] | s + CASE WHEN s = '' THEN '' ELSE $sep END + f)]",
        {"r": repo_id, "sep": "\x1f"},
    )
    engine.set_index_format(repo_id, 3)
    assert caller_edges(engine, repo_id, "main") == [("helper", "lib.py", None), ("helper", "other.py", None)]
    assert dispatch.index_outdated(engine, repo_id)

    dispatch.catch_up(engine, repo_id, tmp_path, since=datetime.now(timezone.utc))
    assert caller_edges(engine, repo_id, "main") == [("helper", "lib.py", "resolved")]
    assert not dispatch.index_outdated(engine, repo_id)
    equals_fresh(engine, repo_id, tmp_path)


def test_an_untyped_member_call_never_links_its_own_caller(engine, repo_id, tmp_path):
    """`items.save()` inside `save` is a call on some other object, never a
    recursion; the same name elsewhere keeps its bare edge."""
    write(tmp_path, "repo.py", "class Repo:\n    def save(self, items):\n        items.save(1)\n")
    write(tmp_path, "store.py", "def save(x):\n    return x\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert caller_edges(engine, repo_id, "save") == [("save", "store.py", "name")]
    equals_fresh(engine, repo_id, tmp_path)
