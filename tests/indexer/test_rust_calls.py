"""Rust CALLS and IMPORTS resolve through crate roots, module paths and types
(the extractor's tiers): a function of the file resolves here; a `use`
binding, a `mod` declaration, a `crate::`/`self::`/`super::` path or another
workspace crate's name names a module's files; `Type::f()` and a method on a
typed receiver (a parameter, a local, a struct field, a loop over a field)
go to the type's module; a std or unknown path links nothing, and an untyped
receiver keeps a bare-name edge unless its method is stoplisted.

A crate's root is the nearest `src/` above the file (`src/bin/*.rs`, and the
files of `tests/`, `benches/` and `examples/`, are crate roots of their own);
another crate is named through its Cargo.toml (the resolver configuration).

See docs/superpowers/specs/2026-10-11-nonpython-call-resolution-design.md (Rust).
"""

import textwrap
import uuid

import pytest

from devgraph.graph.engine import GraphEngine, provision_repository_schema
from devgraph.indexer.common import best_pin
from devgraph.indexer.dispatch import full_scan, index_paths, sync_resolver_config
from devgraph.indexer.resolver_config import ResolverConfig, fingerprints
from devgraph.indexer.rust.extractor import extract_rust_file
from tests.watcher.live_helpers import fresh_snapshot, graph_snapshot, snapshot_diff

WORKSPACE = {
    "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
    "crates/core/Cargo.toml": '[package]\nname = "shop-core"\nversion = "0.1.0"\n',
    "crates/app/Cargo.toml": '[package]\nname = "shop-app"\n\n[dependencies]\nshop-core = { path = "../core" }\n',
}


def _config(tmp_path, files=None):
    for rel, text in (WORKSPACE if files is None else files).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return ResolverConfig(tmp_path)


def _rows(source, file_path, config=None, rel_type="CALLS"):
    result = extract_rust_file(textwrap.dedent(source), file_path, "repo", config)
    return [r for r in result.relationships if r.rel_type == rel_type]


def reached(source, file_path, caller, callee, candidates, config=None):
    """{candidate path: confidence} for each of `candidates` a CALLS row of
    `caller` to `callee` links (by its pins), or {"<name>": "name"} for a
    bare-name row."""
    rows = [r for r in _rows(source, file_path, config) if (r.from_name, r.to_name) == (caller, callee)]
    if any(r.to_file is None for r in rows):
        return {"<name>": rows[0].properties["confidence"]}
    pins = {r.to_file: r.properties["confidence"] for r in rows}
    found = {}
    for path in candidates:
        pin = best_pin(pins, path, file_path)
        if pin is not None:
            found[path] = pins[pin]
    return found


def imported(source, file_path, candidates, config=None):
    """The candidate Module paths the file's IMPORTS rows reach."""
    rows = _rows(source, file_path, config, "IMPORTS")
    names = {r.to_name for r in rows if r.to_file is None}
    pins = [r.to_file for r in rows if r.to_file is not None]
    return {
        path for path in candidates
        if path in names or best_pin(pins, path, file_path) is not None
    }


CORE = ["src/models/user.rs", "src/models/order.rs", "src/net/client.rs", "src/net/mod.rs", "src/util/text.rs",
        "src/util/money.rs", "src/util/mod.rs", "src/lib.rs", "src/main.rs"]


def test_the_type_path_picks_the_file_of_new():
    source = """
        use crate::models::user::User;

        pub struct Client { base: String }

        impl Client {
            pub fn new(base: &str) -> Self { Client { base: base.to_string() } }
            pub fn fetch_user(&self, name: &str) -> User {
                let url = self.url(name);
                let c = Client::new(&url);
                User::new(&url)
            }
            fn url(&self, path: &str) -> String { format!("{}/{}", self.base, path) }
        }
    """
    assert reached(source, "src/net/client.rs", "fetch_user", "new", CORE) == {
        "src/models/user.rs": "resolved", "src/net/client.rs": "resolved",
    }
    assert reached(source, "src/net/client.rs", "fetch_user", "url", CORE) == {"src/net/client.rs": "resolved"}
    # A std type's constructor and a stoplisted method on an untyped value link nothing.
    assert reached(source, "src/net/client.rs", "new", "to_string", CORE) == {}
    assert reached(source, "src/net/client.rs", "url", "format", CORE) == {}  # a macro, not a call


def test_std_constructors_and_external_paths_link_nothing():
    source = """
        use std::collections::HashMap;
        use std::env;

        pub fn new(name: &str) -> Vec<String> {
            let seen: HashMap<String, u32> = HashMap::new();
            let it = env::args();
            seen.insert(name.to_string(), 1);
            serde_json::to_string(&seen);
            frobnicate();
            Vec::new()
        }
    """
    for callee in ("new", "args", "insert", "to_string", "frobnicate"):
        assert reached(source, "src/models/user.rs", "new", callee, CORE) == {}, callee


def test_an_untyped_receiver_keeps_a_bare_row_off_its_caller_unless_stoplisted():
    source = """
        pub fn apply(x: impl Fn()) {
            let mut v = make();
            v.frobnicate();
            v.push(1);
        }
        fn make() -> u32 { 1 }
    """
    rows = [r for r in _rows(source, "src/lib.rs") if r.from_name == "apply"]
    bare = {(r.to_name, r.to_file, r.properties["confidence"], r.no_self) for r in rows}
    assert ("frobnicate", None, "name", True) in bare
    assert not any(r.to_name == "push" for r in rows)
    assert reached(source, "src/lib.rs", "apply", "make", CORE) == {"src/lib.rs": "resolved"}


def test_self_and_super_paths_walk_the_module_tree_inline_modules_included():
    source = """
        use self::fmt::cents_to_string;

        pub fn format(cents: u32) -> String {
            cents_to_string(cents)
        }

        mod fmt {
            pub fn cents_to_string(c: u32) -> String {
                super::super::text::normalize(&format!("{}", c))
            }
        }
    """
    assert reached(source, "src/util/money.rs", "format", "cents_to_string", CORE) == {
        "src/util/money.rs": "resolved"
    }
    assert reached(source, "src/util/money.rs", "cents_to_string", "normalize", CORE) == {
        "src/util/text.rs": "resolved"
    }
    # A use of an inline module of the file imports no other file.
    assert imported(source, "src/util/money.rs", CORE) == set()

    sup = "use super::order::Order;\n\npub fn total(o: Order) -> u32 { o.amount() }\n"
    assert reached(sup, "src/models/user.rs", "total", "amount", CORE + ["src/models/order.rs"]) == {
        "src/models/order.rs": "resolved"
    }
    assert imported(sup, "src/models/user.rs", CORE) == {"src/models/order.rs"}

    in_mod_rs = "pub mod client;\n\npub fn connect(b: &str) { self::client::Client::new(b); }\n"
    assert reached(in_mod_rs, "src/net/mod.rs", "connect", "new", CORE) == {"src/net/client.rs": "resolved"}


def test_mod_declarations_follow_the_2018_rule():
    assert imported("pub mod order;\npub mod user;\n", "src/models.rs", CORE) == {
        "src/models/order.rs", "src/models/user.rs"
    }
    assert imported("pub mod models;\npub mod net;\n", "src/lib.rs", CORE + ["src/models.rs"]) == {
        "src/models.rs", "src/net/mod.rs"
    }
    assert imported("mod money;\n", "src/util/mod.rs", CORE) == {"src/util/money.rs"}
    # A module named like the file's own module, under a non-mod.rs file.
    assert imported("mod user;\n", "src/models.rs", ["src/user.rs", "src/models/user.rs"]) == {"src/models/user.rs"}

    main = "mod cli;\n\nfn main() { cli::parse_args(); }\n"
    assert imported(main, "app/src/main.rs", ["app/src/cli.rs"]) == {"app/src/cli.rs"}
    assert reached(main, "app/src/main.rs", "main", "parse_args", ["app/src/cli.rs", "app/src/main.rs"]) == {
        "app/src/cli.rs": "resolved"
    }


def test_a_use_imports_the_file_of_the_module_it_names():
    source = """
        use crate::util::text::normalize;
        use crate::util::money;
        use crate::models::user::User;

        pub fn f(u: &User) -> String { money::format(normalize(&u.name)) }
    """
    assert imported(source, "src/report.rs", CORE) == {"src/util/text.rs", "src/util/money.rs", "src/models/user.rs"}
    assert reached(source, "src/report.rs", "f", "format", CORE) == {"src/util/money.rs": "resolved"}
    assert reached(source, "src/report.rs", "f", "normalize", CORE) == {"src/util/text.rs": "resolved"}


def test_typed_receivers_resolve_to_their_types_module():
    source = """
        use crate::models::order::Order;
        use crate::net::client::Client;

        pub struct User { orders: Vec<Order>, client: Client }

        impl User {
            pub fn total(&self) -> u32 {
                let mut sum = 0;
                for order in &self.orders { sum += order.amount(); }
                self.client.ping();
                sum
            }
        }

        pub fn run(c: &Client, o: Order) {
            c.fetch_user("a");
            let d = Client::new("b");
            d.close();
            let e: Order = o;
            e.amount();
        }
    """
    files = CORE + ["src/models/order.rs"]
    assert reached(source, "src/models/user.rs", "total", "amount", files) == {"src/models/order.rs": "resolved"}
    assert reached(source, "src/models/user.rs", "total", "ping", files) == {"src/net/client.rs": "resolved"}
    assert reached(source, "src/models/user.rs", "run", "fetch_user", files) == {"src/net/client.rs": "resolved"}
    assert reached(source, "src/models/user.rs", "run", "close", files) == {"src/net/client.rs": "resolved"}
    assert reached(source, "src/models/user.rs", "run", "amount", files) == {"src/models/order.rs": "resolved"}


def test_calls_inside_macro_arguments_are_calls(tmp_path):
    config = _config(tmp_path)
    source = """
        use shop_core::util::money;

        fn main() {
            println!("core {} {}", shop_core::version(), money::format(total()));
            assert_eq!(helper(), 1);
        }
        fn helper() -> u32 { 1 }
        fn total() -> u32 { 1 }
    """
    files = ["crates/core/src/lib.rs", "crates/core/src/util/money.rs", "crates/app/src/main.rs"]
    assert reached(source, "crates/app/src/main.rs", "main", "version", files, config) == {
        "crates/core/src/lib.rs": "resolved"
    }
    assert reached(source, "crates/app/src/main.rs", "main", "format", files, config) == {
        "crates/core/src/util/money.rs": "resolved"
    }
    assert reached(source, "crates/app/src/main.rs", "main", "helper", files, config) == {
        "crates/app/src/main.rs": "resolved"
    }
    assert reached(source, "crates/app/src/main.rs", "main", "total", files, config) == {
        "crates/app/src/main.rs": "resolved"
    }


def test_a_workspace_crate_is_reached_by_its_package_name(tmp_path):
    config = _config(tmp_path)
    source = """
        use shop_core::models::user::User;
        use shop_core::net::client::Client;

        fn main() {
            let client = Client::new("x");
            let user = client.fetch_user("ada");
            let local = User::new("local");
        }
    """
    files = ["crates/core/src/models/user.rs", "crates/core/src/net/client.rs", "crates/core/src/models/order.rs"]
    path = "crates/app/src/main.rs"
    assert reached(source, path, "main", "fetch_user", files, config) == {"crates/core/src/net/client.rs": "resolved"}
    assert reached(source, path, "main", "new", files, config) == {
        "crates/core/src/net/client.rs": "resolved", "crates/core/src/models/user.rs": "resolved",
    }
    assert imported(source, path, files, config) == {"crates/core/src/models/user.rs", "crates/core/src/net/client.rs"}
    # Without the configuration another crate is external.
    assert reached(source, path, "main", "fetch_user", files) == {}
    assert imported(source, path, files) == set()


def test_tests_and_bins_are_crate_roots_of_their_own(tmp_path):
    config = _config(tmp_path)
    test = """
        use shop_core::models::user::User;
        use crate::common::setup;

        mod common;

        #[test]
        fn totals_orders() {
            setup();
            let mut user = User::new("Ada");
            user.add_order(1);
        }
    """
    path = "crates/core/tests/integration.rs"
    files = ["crates/core/src/models/user.rs", "crates/core/tests/common/mod.rs", "crates/core/src/common.rs"]
    assert reached(test, path, "totals_orders", "add_order", files, config) == {
        "crates/core/src/models/user.rs": "resolved"
    }
    assert reached(test, path, "totals_orders", "setup", files, config) == {
        "crates/core/tests/common/mod.rs": "resolved"
    }
    assert imported(test, path, files, config) == {
        "crates/core/src/models/user.rs", "crates/core/tests/common/mod.rs"
    }

    tool = "mod helper;\n\nfn main() { helper::run(); crate::helper::run(); }\n"
    files = ["src/bin/helper.rs", "src/helper.rs", "src/bin/tool/helper.rs"]
    assert imported(tool, "src/bin/tool.rs", files) == {"src/bin/helper.rs"}
    assert reached(tool, "src/bin/tool.rs", "main", "run", files) == {"src/bin/helper.rs": "resolved"}
    assert imported("mod helper;\n", "src/bin/tool/main.rs", files) == {"src/bin/tool/helper.rs"}


def test_a_root_package_beside_a_workspace_names_only_its_own_files(tmp_path):
    config = _config(tmp_path, {
        "Cargo.toml": '[package]\nname = "app"\n\n[workspace]\nmembers = ["crates/*"]\n',
        "crates/core/Cargo.toml": '[package]\nname = "shop-core"\n',
    })
    source = "use app::net;\n\nfn main() { app::run(); net::connect(); }\n"
    files = ["src/lib.rs", "src/net.rs", "crates/core/src/lib.rs", "crates/core/src/net.rs"]
    path = "crates/core/src/main.rs"
    assert reached(source, path, "main", "run", files, config) == {"src/lib.rs": "resolved"}
    assert reached(source, path, "main", "connect", files, config) == {"src/net.rs": "resolved"}
    assert imported(source, path, files, config) == {"src/net.rs"}


def test_two_packages_of_one_name_are_package_confidence(tmp_path):
    config = _config(tmp_path, {
        "a/Cargo.toml": '[package]\nname = "util"\n', "b/Cargo.toml": '[package]\nname = "util"\n',
    })
    source = "fn main() { util::run(); }\n"
    files = ["a/src/lib.rs", "b/src/lib.rs", "a/src/other.rs"]
    assert reached(source, "app/src/main.rs", "main", "run", files, config) == {
        "a/src/lib.rs": "package", "b/src/lib.rs": "package",
    }
    assert imported("use util::run;\n", "app/src/main.rs", files, config) == {"a/src/lib.rs", "b/src/lib.rs"}


def test_more_crate_roots_main_rs_targets_build_scripts_and_lib_paths(tmp_path):
    files = ["tests/smoke/helper.rs", "tests/helper.rs", "examples/demo/helper.rs", "benches/load/helper.rs"]
    for root in ("tests/smoke", "examples/demo", "benches/load"):
        assert imported("mod helper;\n", f"{root}/main.rs", files) == {f"{root}/helper.rs"}, root
    # A file beside Cargo.toml (build.rs) is a crate root of its own.
    config = _config(tmp_path, {"crates/core/Cargo.toml": '[package]\nname = "core"\n\n[lib]\npath = "lib/core.rs"\n'})
    build = "mod gen;\n\nfn main() { gen::run(); }\n"
    files = ["crates/core/gen.rs", "crates/core/build/gen.rs"]
    assert imported(build, "crates/core/build.rs", files, config) == {"crates/core/gen.rs"}
    # A custom [lib] path is its crate's root: crate:: and mod resolve beside it.
    lib = "mod net;\n\npub fn run() { crate::net::go(); }\n"
    files = ["crates/core/lib/net.rs", "crates/core/lib/core/net.rs", "crates/core/src/net.rs"]
    assert imported(lib, "crates/core/lib/core.rs", files, config) == {"crates/core/lib/net.rs"}
    assert reached(lib, "crates/core/lib/core.rs", "run", "go", files, config) == {
        "crates/core/lib/net.rs": "resolved"
    }
    other = "fn main() { core::run(); }\n"
    assert reached(other, "app/src/main.rs", "main", "run", ["crates/core/lib/core.rs", "crates/core/src/lib.rs"],
                   config) == {"crates/core/lib/core.rs": "resolved"}


def test_the_cargo_fingerprint_follows_only_crate_names(tmp_path):
    _config(tmp_path)
    before = fingerprints(tmp_path)
    assert "crates/core/Cargo.toml" in before["rust"]["inputs"]
    (tmp_path / "crates/core/Cargo.toml").write_text(
        '[package]\nname = "shop-core"\nversion = "0.2.0"\n\n[dependencies]\nserde = "1"\n'
    )
    assert fingerprints(tmp_path) == before  # a version or dependency bump changes nothing
    (tmp_path / "crates/core/Cargo.toml").write_text('[package]\nname = "store-core"\n')
    assert fingerprints(tmp_path)["rust"]["fingerprint"] != before["rust"]["fingerprint"]
    # A malformed Cargo.toml names no crate.
    (tmp_path / "crates/core/Cargo.toml").write_text("[package\nname = ")
    assert ResolverConfig(tmp_path).rust_crates() == {"shop_app": ["crates/app/src/lib.rs"]}


def test_reexports_are_read_from_a_files_top_level():
    from devgraph.indexer.rust.extractor import reexports

    source = textwrap.dedent("""\
        pub mod net;
        mod util;
        use crate::models as m;
        pub use net::Client;
        pub(crate) use util::{text::*, money::format as fmt};
        pub use m::User;
        use net::Hidden;
        pub use std::fmt::Display;
        mod inner { pub use super::net::Inner; }
        """)
    assert reexports(source) == [
        ["*", ["self", "util", "text"]],
        ["Client", ["self", "net", "Client"]],
        ["Display", ["std", "fmt", "Display"]],
        ["User", ["crate", "models", "User"]],
        ["fmt", ["self", "util", "money", "format"]],
    ]
    assert reexports("pub fn use_it() {}\nuse a::b;\n") == []


REEXPORTS = {
    **WORKSPACE,
    "crates/core/src/lib.rs": textwrap.dedent("""\
        pub mod net;
        pub mod util;
        pub use net::Client;
        pub use util::*;
        pub use net::client::fetch_all as get_all;
        """),
    "crates/core/src/net/mod.rs": "pub mod client;\npub use self::client::Client;\n",
    "crates/core/src/util.rs": "pub fn tidy() {}\npub fn new() {}\n",
}
FACADE = ["crates/core/src/lib.rs", "crates/core/src/net/mod.rs", "crates/core/src/net/client.rs",
        "crates/core/src/util.rs", "crates/app/src/main.rs"]


def test_a_crate_root_reexport_reaches_the_defining_file(tmp_path):
    """`pub use` in lib.rs, followed through net/mod.rs's own `pub use`:
    `shop_core::Client::new` and a method on the value it makes reach
    net/client.rs; a glob re-export reaches its module unless the name is
    re-exported by name; a renamed function is called by its own name."""
    config = _config(tmp_path, REEXPORTS)
    source = """\
        use shop_core::Client;
        use shop_core::get_all;

        fn main() {
            let c = Client::new("u");
            c.fetch();
            shop_core::Client::new("v");
            shop_core::tidy();
            get_all();
        }
        """
    path = "crates/app/src/main.rs"
    # lib.rs is pinned too (it links nothing: it defines no `new`).
    lib = {"crates/core/src/lib.rs": "resolved"}
    # net/mod.rs, which re-exports it in turn, may define it too.
    client = {**lib, "crates/core/src/net/mod.rs": "resolved", "crates/core/src/net/client.rs": "resolved"}
    assert reached(source, path, "main", "new", FACADE, config) == client
    assert reached(source, path, "main", "fetch", FACADE, config) == client
    # A glob's module at package confidence: lib.rs may define tidy itself.
    assert reached(source, path, "main", "tidy", FACADE, config) == {**lib, "crates/core/src/util.rs": "package"}
    assert reached(source, path, "main", "fetch_all", FACADE, config) == {"crates/core/src/net/client.rs": "resolved"}
    assert reached(source, path, "main", "get_all", FACADE, config) == lib
    # net/mod.rs only passes Client on: no import of it.
    assert imported(source, path, FACADE, config) == {"crates/core/src/lib.rs", "crates/core/src/net/client.rs"}
    # A local `use .. as` alias is called by the function's own name too.
    aliased = "use shop_core::util::tidy as neat;\n\nfn main() { neat(); }\n"
    assert reached(aliased, path, "main", "tidy", FACADE, config) == {"crates/core/src/util.rs": "resolved"}
    # Within the crate, through `crate::`.
    inner = "fn go() { crate::Client::new(\"w\"); }\n"
    assert reached(inner, "crates/core/src/util.rs", "go", "new", FACADE, config) == client
    # Without the configuration's re-exports, only lib.rs.
    plain = ResolverConfig(tmp_path, rust_files=lambda: [rel for rel in REEXPORTS if rel.endswith("Cargo.toml")])
    assert reached(source, path, "main", "new", FACADE, plain) == lib


def test_a_named_reexport_keeps_its_module_beside_that_modules_glob(tmp_path):
    """lib.rs `pub use util::dig;`, util.rs `pub use crate::deep::*;`: dig
    may be util.rs's own (resolved) or deep.rs's through the glob (package)."""
    config = _config(tmp_path, {
        **WORKSPACE,
        "crates/core/src/lib.rs": "pub mod util;\npub mod deep;\npub use util::dig;\n",
        "crates/core/src/util.rs": "pub use crate::deep::*;\npub fn dig() {}\n",
        "crates/core/src/deep.rs": "pub fn dig() {}\n",
    })
    files = ["crates/core/src/lib.rs", "crates/core/src/util.rs", "crates/core/src/deep.rs"]
    found = reached("fn main() { shop_core::dig(); }\n", "crates/app/src/main.rs", "main", "dig", files, config)
    assert found == {
        "crates/core/src/lib.rs": "resolved", "crates/core/src/util.rs": "resolved",
        "crates/core/src/deep.rs": "package",
    }


def test_a_glob_reexport_ranks_below_the_reexporting_modules_own_items(tmp_path):
    """lib.rs `pub use util::*;` and its own `fn tidy`: Rust takes lib.rs's,
    so lib.rs is the resolved pin and util.rs only a package one, which
    leaves lib.rs out (its `exclude`); adding a function is no configuration
    change."""
    config = _config(tmp_path, {
        **WORKSPACE,
        "crates/core/src/lib.rs": "pub mod util;\npub use util::*;\npub fn tidy() {}\n",
        "crates/core/src/util.rs": "pub fn tidy() {}\n",
    })
    rows = [r for r in _rows("fn main() { shop_core::tidy(); }\n", "crates/app/src/main.rs", config)
            if r.to_name == "tidy"]
    pins = {r.to_file: r.properties["confidence"] for r in rows}
    assert pins == {
        "crates/core/src/lib.rs": "resolved", "crates/core/src/.~?util.rs": "package",
        "crates/core/src/util/.~?mod.rs": "package", "crates/core/src/util/": "package",
    }
    (loose,) = [r for r in rows if r.to_file.endswith("util.rs")]
    assert "crates/core/src/lib.rs" in loose.exclude


def test_reexport_cycles_and_external_paths_end(tmp_path):
    config = _config(tmp_path, {
        **WORKSPACE,
        "crates/core/src/lib.rs": "pub mod a;\npub use a::X;\npub use serde::Y;\n",
        "crates/core/src/a.rs": "pub use crate::X;\n",
    })
    source = "use shop_core::{X, Y};\n\nfn main() { X::go(); Y::go(); }\n"
    found = reached(source, "crates/app/src/main.rs", "main", "go", ["crates/core/src/lib.rs", "crates/core/src/a.rs"],
                    config)
    assert found == {"crates/core/src/lib.rs": "resolved", "crates/core/src/a.rs": "resolved"}


def test_reexports_are_rust_configuration(tmp_path):
    _config(tmp_path, REEXPORTS)
    before = fingerprints(tmp_path)["rust"]
    assert {"crates/core/src/lib.rs", "crates/core/src/net/mod.rs"} <= set(before["inputs"])
    assert "crates/core/src/util.rs" not in before["inputs"]
    (tmp_path / "crates/core/src/lib.rs").write_text(REEXPORTS["crates/core/src/lib.rs"] + "pub fn version() {}\n")
    assert fingerprints(tmp_path)["rust"] == before  # a new function changes nothing
    (tmp_path / "crates/core/src/lib.rs").write_text("pub mod net;\npub mod util;\npub use net::Client as C;\n")
    assert fingerprints(tmp_path)["rust"]["fingerprint"] != before["fingerprint"]
    from devgraph.indexer.resolver_config import touches_config

    assert touches_config("crates/core/src/lib.rs", [], tmp_path)  # it re-exports
    assert not touches_config("crates/core/src/util.rs", [], tmp_path)


# --- live --------------------------------------------------------------------------


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
    repo = f"zz-rustcalls-{uuid.uuid4().hex[:8]}"
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)
    yield repo
    for each in (repo, f"{repo}_fresh"):
        engine.delete_repository(each)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _edges(engine, repo_id, rel_type):
    rows = engine.run_cypher(
        f"MATCH (a {{repo_id: $r}})-[x:{rel_type}]->(b {{repo_id: $r}}) "
        "RETURN coalesce(a.file, a.name) AS a, a.name AS an, coalesce(b.file, b.name) AS b, b.name AS bn, "
        "x.confidence AS c",
        {"r": repo_id},
    )
    return sorted((r["a"], r["an"], r["b"], r["bn"], r["c"]) for r in rows)


def _equals_fresh(engine, repo_id, root):
    expected = fresh_snapshot(engine, repo_id, root)
    actual = graph_snapshot(engine, repo_id)
    if actual != expected:
        pytest.fail("graph does not equal a fresh full_scan:\n" + snapshot_diff(expected, actual))


def test_renaming_a_crate_re_indexes_its_users_and_a_moved_function_relinks(engine, repo_id, tmp_path):
    """A package rename in Cargo.toml changes what `shop_core::` names, so
    the Rust files are re-indexed; a function moving between modules of the
    crate follows its `use`. Every step equals a fresh scan."""
    _config(tmp_path)
    main = _write(tmp_path, "crates/app/src/main.rs",
                  "use shop_core::util;\n\nfn main() { util::run(); }\n")
    _write(tmp_path, "crates/core/src/lib.rs", "pub mod util;\n")
    util = _write(tmp_path, "crates/core/src/util.rs", "pub fn run() {}\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    edge = ("crates/app/src/main.rs", "main", "crates/core/src/util.rs", "run", "resolved")
    assert edge in _edges(engine, repo_id, "CALLS")
    _equals_fresh(engine, repo_id, tmp_path)

    cargo = _write(tmp_path, "crates/core/Cargo.toml", '[package]\nname = "store-core"\n')
    assert sync_resolver_config(engine, repo_id, tmp_path, {cargo}) > 0
    assert edge not in _edges(engine, repo_id, "CALLS")
    _equals_fresh(engine, repo_id, tmp_path)

    main.write_text("use store_core::util;\n\nfn main() { util::run(); }\n")
    index_paths(engine, repo_id, tmp_path, {main})
    assert edge in _edges(engine, repo_id, "CALLS")
    # run moves out of util.rs into util/mod.rs's place: the call follows.
    util.unlink()
    moved = _write(tmp_path, "crates/core/src/util/mod.rs", "pub fn run() {}\n")
    from devgraph.indexer.dispatch import remove_paths

    remove_paths(engine, repo_id, tmp_path, {util})
    index_paths(engine, repo_id, tmp_path, {moved})
    assert ("crates/app/src/main.rs", "main", "crates/core/src/util/mod.rs", "run", "resolved") in _edges(
        engine, repo_id, "CALLS"
    )
    _equals_fresh(engine, repo_id, tmp_path)


def test_the_index_upgrade_replaces_bare_rust_csharp_and_cpp_calls(engine, repo_id, tmp_path, monkeypatch):
    """A graph indexed at format 7 has bare Rust, C# and C++ CALLS with no
    confidence; catch_up's automatic upgrade (a full_scan) leaves the
    resolved edges."""
    from datetime import datetime, timezone

    from devgraph.indexer import dispatch
    from devgraph.indexer.common import GraphRelationship

    _write(tmp_path, "src/main.rs", "mod util;\n\nfn main() { util::run(); }\n")
    _write(tmp_path, "src/util.rs", "pub fn run() {}\n")
    _write(tmp_path, "tools/other.rs", "pub fn run() {}\n")
    _write(tmp_path, "cs/Program.cs", "class Program { static void Main() { Jobs.Run(); } }\n")
    _write(tmp_path, "cs/Jobs.cs", "class Jobs { public static void Run() {} }\n")
    _write(tmp_path, "cs/Other.cs", "class Other { public static void Run() {} }\n")
    _write(tmp_path, "cpp/main.cpp", '#include "jobs.h"\n\nint main() { jobs::start(); }\n')
    _write(tmp_path, "cpp/jobs.h", "namespace jobs { inline void start() {} }\n")
    _write(tmp_path, "cpp/other.cpp", "void start() {}\n")
    callers = {"src/main.rs": "main", "cs/Program.cs": "Main", "cpp/main.cpp": "main"}

    def old(real):
        def extract(content, rel_path, repo_id, *args):
            # Format 7: every Rust, C# and C++ call a bare name.
            result = real(content, rel_path, repo_id, *args)
            result.relationships = [r for r in result.relationships if r.rel_type != "CALLS"]
            if rel_path in callers:
                result.relationships.append(GraphRelationship(
                    from_label="Function", from_name=callers[rel_path], rel_type="CALLS", to_label="Function",
                    to_name={".rs": "run", ".cs": "Run", "cpp": "start"}[rel_path[-3:]], repo_id=repo_id,
                    from_file=rel_path, origin=rel_path,
                ))
            return result
        return extract

    with monkeypatch.context() as patched:
        for name in ("extract_rust_file", "extract_csharp_file", "extract_cpp_file"):
            patched.setattr(dispatch, name, old(getattr(dispatch, name)))
        provision_repository_schema(engine, tmp_path)
        engine.upsert_repository(repo_id, repo_id, str(tmp_path))
        full_scan(engine, repo_id, tmp_path)
    engine.set_index_format(repo_id, 7)

    def calls():
        return [e for e in _edges(engine, repo_id, "CALLS") if e[0] in callers]

    assert {(e[0], e[2], e[4]) for e in calls()} == {
        ("src/main.rs", "src/util.rs", None), ("src/main.rs", "tools/other.rs", None),
        ("cs/Program.cs", "cs/Jobs.cs", None), ("cs/Program.cs", "cs/Other.cs", None),
        ("cpp/main.cpp", "cpp/jobs.h", None), ("cpp/main.cpp", "cpp/other.cpp", None),
    }
    assert dispatch.index_outdated(engine, repo_id)

    dispatch.catch_up(engine, repo_id, tmp_path, since=datetime.now(timezone.utc))
    assert {(e[0], e[2], e[4]) for e in calls()} == {
        ("src/main.rs", "src/util.rs", "resolved"), ("cs/Program.cs", "cs/Jobs.cs", "package"),
        ("cpp/main.cpp", "cpp/jobs.h", "resolved"),
    }
    assert not dispatch.index_outdated(engine, repo_id)


def test_a_reexport_change_re_indexes_its_users(engine, repo_id, tmp_path):
    """lib.rs re-exports Client from net, then from models, then nothing:
    each change re-indexes the Rust files, so main's call moves with it,
    and every step equals a fresh scan."""
    _config(tmp_path, REEXPORTS)
    _write(tmp_path, "crates/core/src/net/client.rs", "pub struct Client;\nimpl Client { pub fn new() {} }\n")
    _write(tmp_path, "crates/core/src/models.rs", "pub struct Client;\nimpl Client { pub fn new() {} }\n")
    _write(tmp_path, "crates/app/src/main.rs", "use shop_core::Client;\n\nfn main() { Client::new(); }\n")
    provision_repository_schema(engine, tmp_path)
    engine.upsert_repository(repo_id, repo_id, str(tmp_path))
    full_scan(engine, repo_id, tmp_path)
    via_net = ("crates/app/src/main.rs", "main", "crates/core/src/net/client.rs", "new", "resolved")
    via_models = ("crates/app/src/main.rs", "main", "crates/core/src/models.rs", "new", "resolved")
    calls = _edges(engine, repo_id, "CALLS")
    assert via_net in calls and via_models not in calls

    lib = _write(tmp_path, "crates/core/src/lib.rs", "pub mod net;\npub mod models;\npub use models::Client;\n")
    index_paths(engine, repo_id, tmp_path, {lib})
    assert sync_resolver_config(engine, repo_id, tmp_path, {lib}) > 0
    calls = _edges(engine, repo_id, "CALLS")
    assert via_models in calls and via_net not in calls
    _equals_fresh(engine, repo_id, tmp_path)

    # net/mod.rs stops re-exporting: no file's text but its own changed.
    net = _write(tmp_path, "crates/core/src/net/mod.rs", "pub mod client;\n")
    _write(tmp_path, "crates/core/src/lib.rs", "pub mod net;\npub mod models;\npub use net::Client;\n")
    index_paths(engine, repo_id, tmp_path, {lib, net})
    sync_resolver_config(engine, repo_id, tmp_path, {lib, net})
    calls = _edges(engine, repo_id, "CALLS")
    assert via_net not in calls and via_models not in calls
    _equals_fresh(engine, repo_id, tmp_path)
