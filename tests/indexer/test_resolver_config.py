"""Resolver configuration (devgraph/indexer/resolver_config.py): tsconfig path
aliases through `extends` and `references`, the per-language fingerprints, and
the re-index a configuration change triggers (dispatch.sync_resolver_config).

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md
(Resolver configuration).
"""

import json
import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.dispatch import (
    full_scan,
    index_paths,
    remove_paths,
    sync_resolver_config,
)
from devgraph.indexer.resolver_config import (
    ResolverConfig,
    fingerprints,
    parse_jsonc,
    touches_config,
)
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def test_jsonc_allows_comments_and_trailing_commas():
    text = '{\n // a comment\n "a": "x // not a comment", /* block */ "b": [1, 2,],\n}'
    assert parse_jsonc(text) == {"a": "x // not a comment", "b": [1, 2]}
    assert parse_jsonc("{ broken") is None
    assert parse_jsonc("[1]") is None


def test_jsonc_allows_a_comment_between_a_trailing_comma_and_its_bracket():
    assert parse_jsonc('{"paths": {"@/*": ["src/*"], // app\n}, "a": [1, /* x */ ]}') == {
        "paths": {"@/*": ["src/*"]}, "a": [1],
    }
    assert parse_jsonc('{"s": ", }"}') == {"s": ", }"}  # a comma inside a string stays


def test_a_config_with_a_byte_order_mark_keeps_its_aliases(tmp_path):
    (tmp_path / "tsconfig.json").write_bytes(
        "\ufeff{\"compilerOptions\": {\"paths\": {\"@/*\": [\"src/*\"]}}}".encode("utf-8")
    )
    assert ResolverConfig(tmp_path).ts_alias("@/a", "x.ts") == ["src/a"]


def test_an_extends_cycle_is_malformed_whichever_config_is_read_first(tmp_path):
    write(tmp_path, "a.json", '{"extends": "./b.json", "compilerOptions": {"baseUrl": "a"}}')
    write(tmp_path, "b.json", '{"extends": "./a.json", "compilerOptions": {"baseUrl": "b"}}')
    write(tmp_path, "tsconfig.json", '{"extends": "./a.json"}')
    write(tmp_path, "web/tsconfig.json", '{"extends": "../b.json"}')
    for first, second in (("x.ts", "web/x.ts"), ("web/x.ts", "x.ts")):
        config = ResolverConfig(tmp_path)
        assert config.ts_alias("m", first) is None
        assert config.ts_alias("m", second) is None


def test_an_absolute_extends_path_is_outside_the_repository(tmp_path):
    write(tmp_path, "base.json", '{"compilerOptions": {"baseUrl": "src"}}')
    write(tmp_path, "tsconfig.json", '{"extends": "/base.json"}')
    assert ResolverConfig(tmp_path).ts_alias("m", "x.ts") is None


def test_paths_come_through_references_and_beat_base_url(tmp_path):
    write(tmp_path, "tsconfig.json", '{"files": [], "references": [{"path": "./tsconfig.app.json"}]}')
    write(tmp_path, "tsconfig.app.json", """\
        {
          // Vite's layout: the aliases live here.
          "compilerOptions": {"baseUrl": ".", "paths": {"@/*": ["src/*"], "@lib": ["src/lib/index.ts"]},},
        }
    """)
    config = ResolverConfig(tmp_path)
    assert config.ts_alias("@/lib/text", "src/pages/a.ts") == ["src/lib/text"]
    assert config.ts_alias("@lib", "src/a.ts") == ["src/lib/index.ts"]
    assert config.ts_alias("lib/x", "src/a.ts") == ["lib/x"]  # baseUrl, no pattern matched
    assert {"tsconfig.json", "tsconfig.app.json"} <= config.read


def test_no_config_or_no_mapping_is_external(tmp_path):
    assert ResolverConfig(tmp_path).ts_alias("react", "src/a.ts") is None
    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"strict": true}}')
    assert ResolverConfig(tmp_path).ts_alias("react", "src/a.ts") is None


def test_extends_resolves_against_the_config_defining_each_option(tmp_path):
    write(tmp_path, "configs/base.cfg", '{"compilerOptions": {"paths": {"~/*": ["../src/*"]}}}')
    write(tmp_path, "app/tsconfig.json", '{"extends": "../configs/base.cfg", "compilerOptions": {"strict": true}}')
    write(tmp_path, "pkg/tsconfig.json", '{"extends": ["@tsconfig/node18", "./local"]}')
    write(tmp_path, "pkg/local.json", '{"compilerOptions": {"baseUrl": "./src"}}')
    config = ResolverConfig(tmp_path)
    assert config.ts_alias("~/x", "app/main.ts") == ["src/x"]  # paths relative to configs/
    assert config.ts_alias("util/y", "pkg/src/a.ts") == ["pkg/src/util/y"]
    assert "configs/base.cfg" in config.read


def test_the_nearest_config_applies_and_paths_resolve_against_base_url(tmp_path):
    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["*"]}}}')
    write(tmp_path, "web/jsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["./app/*"]}}}')
    config = ResolverConfig(tmp_path)
    assert config.ts_alias("@/a", "x/y.ts") == ["src/a"]
    assert config.ts_alias("@/a", "web/deep/y.js") == ["web/app/a"]


def test_a_config_under_an_ignored_path_is_absent(tmp_path):
    write(tmp_path, ".gitignore", "generated/\n")
    write(tmp_path, "generated/tsconfig.json", '{"compilerOptions": {"baseUrl": "."}}')
    write(tmp_path, "node_modules/pkg/tsconfig.json", '{"compilerOptions": {"baseUrl": "."}}')
    config = ResolverConfig(tmp_path)
    assert config.ts_alias("x", "generated/a.ts") is None
    assert config.ts_alias("x", "node_modules/pkg/a.ts") is None
    assert fingerprints(tmp_path)["ts"]["inputs"] == []


def test_fingerprints_follow_only_what_resolution_reads(tmp_path):
    write(tmp_path, "tsconfig.json", '{"extends": "./base.json", "compilerOptions": {"strict": true}}')
    write(tmp_path, "base.json", '{"compilerOptions": {"paths": {"@/*": ["src/*"]}}}')
    write(tmp_path, "go.mod", "module example.com/app\n\ngo 1.22\n\nrequire example.com/x v1.0.0\n")
    before = fingerprints(tmp_path)
    assert "base.json" in before["ts"]["inputs"] and "go.mod" in before["go"]["inputs"]

    write(tmp_path, "tsconfig.json", '{"extends": "./base.json", "compilerOptions": {"strict": false}}')
    write(tmp_path, "go.mod", "module example.com/app\n\ngo 1.23\n\nrequire example.com/x v1.2.0\n")
    assert fingerprints(tmp_path) == before  # a compiler flag and a require bump change nothing

    write(tmp_path, "base.json", '{"compilerOptions": {"paths": {"@/*": ["lib/*"]}}}')
    write(tmp_path, "go.mod", "module example.com/other\n")
    after = fingerprints(tmp_path)
    assert after["ts"]["fingerprint"] != before["ts"]["fingerprint"]
    assert after["go"]["fingerprint"] != before["go"]["fingerprint"]


def test_what_can_touch_the_configuration():
    inputs = ["web/tsconfig.json", "configs/base.cfg"]
    assert touches_config("tsconfig.app.json", [])
    assert touches_config("a/go.mod", [])
    assert touches_config("configs/base.cfg", inputs)
    assert touches_config("configs", inputs)  # a deleted folder holding one
    assert not touches_config("web/a.ts", inputs)
    assert not touches_config("config", inputs)


# --- live ----------------------------------------------------------------------


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
    repo = f"zz-resolver-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def imports(engine, repo_id, name):
    rows = engine.run_cypher(
        "MATCH (:Module {repo_id: $r, name: $n})-[:IMPORTS]->(b:Module) RETURN b.name AS name", {"r": repo_id, "n": name}
    )
    return sorted(row["name"] for row in rows)


def equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_a_tsconfig_edit_re_indexes_the_files_it_resolves(engine, repo_id, tmp_path):
    config = write(tmp_path, "tsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["src/*"]}}}')
    write(tmp_path, "app.ts", "import { f } from '@/lib';\nexport function main() { return f(); }\n")
    write(tmp_path, "src/lib.ts", "export function f() { return 1; }\n")
    write(tmp_path, "other/lib.ts", "export function f() { return 2; }\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert imports(engine, repo_id, "app.ts") == ["src/lib.ts"]
    stored = engine.read_resolver_config(repo_id)
    assert stored["ts"]["inputs"] == ["tsconfig.json"]

    # A save of an unrelated file checks nothing.
    assert sync_resolver_config(engine, repo_id, tmp_path, {tmp_path / "src/lib.ts"}) == 0

    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"paths": {"@/*": ["other/*"]}}}')
    index_paths(engine, repo_id, tmp_path, {config})
    assert sync_resolver_config(engine, repo_id, tmp_path, {config}) == 3
    assert imports(engine, repo_id, "app.ts") == ["other/lib.ts"]
    assert engine.read_resolver_config(repo_id) != stored
    equals_fresh(engine, repo_id, tmp_path)

    config.unlink()
    remove_paths(engine, repo_id, tmp_path, {config})
    sync_resolver_config(engine, repo_id, tmp_path, {config})
    assert imports(engine, repo_id, "app.ts") == []
    equals_fresh(engine, repo_id, tmp_path)


def test_a_cut_short_re_index_keeps_the_old_fingerprint(engine, repo_id, tmp_path, monkeypatch):
    from devgraph.graph.engine import EngineClosed
    from devgraph.indexer import dispatch

    config = write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "."}}')
    write(tmp_path, "app.ts", "import { f } from 'lib';\nexport function main() { return f(); }\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    stored = engine.read_resolver_config(repo_id)

    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src"}}')

    def closed(*args, **kwargs):
        raise EngineClosed("shutting down")

    monkeypatch.setattr(dispatch, "index_paths", closed)
    with pytest.raises(EngineClosed):
        sync_resolver_config(engine, repo_id, tmp_path, {config})
    assert engine.read_resolver_config(repo_id) == stored
    monkeypatch.undo()
    assert sync_resolver_config(engine, repo_id, tmp_path, None) == 1
    assert json.dumps(engine.read_resolver_config(repo_id)) != json.dumps(stored)


def test_a_catch_up_follows_a_config_changed_while_nothing_watched(engine, repo_id, tmp_path):
    from datetime import datetime, timedelta, timezone

    from devgraph.indexer.dispatch import catch_up

    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "src"}}')
    write(tmp_path, "app.ts", "import { f } from 'lib';\nexport function main() { return f(); }\n")
    write(tmp_path, "src/lib.ts", "export function f() { return 1; }\n")
    write(tmp_path, "other/lib.ts", "export function f() { return 2; }\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    assert imports(engine, repo_id, "app.ts") == ["src/lib.ts"]

    write(tmp_path, "tsconfig.json", '{"compilerOptions": {"baseUrl": "other"}}')
    # Every stamp is older than `since`, so only the configuration check can catch it.
    catch_up(engine, repo_id, tmp_path, datetime.now(timezone.utc) + timedelta(hours=1))
    assert imports(engine, repo_id, "app.ts") == ["other/lib.ts"]
    equals_fresh(engine, repo_id, tmp_path)
