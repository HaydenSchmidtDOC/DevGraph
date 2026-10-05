"""The docs front-matter provider's pure half: selection, mapping, edges and the doctor report.

No Neo4j: every function here reads files and returns plain data.
"""

import itertools
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from devgraph.config.project_schema import parse_project_schema, resolve_declaration
from devgraph.indexer.providers import docs
from devgraph.indexer.providers.docs import (
    build_edges,
    build_nodes,
    docs_spec,
    glob_matches,
    read_front_matter,
    read_selected,
    source_report,
)

RUNBOOK = """
    version: 1
    node_types:
      - label: Runbook
        key: [path]
        metadata:
          - {name: path}
          - {name: owner, required: true}
          - {name: severity, type: integer}
          - {name: on_call}
        source:
          provider: docs
          paths: ["runbooks/**/*.md"]
          fields: {on_call: on-call-team}
    relationships:
      - type: RUNBOOK_FOR
        provider: docs
        from: Runbook
        to: Service
        field: service
"""


def effective(text: str):
    return resolve_declaration(parse_project_schema(textwrap.dedent(text), Path("devgraph.schema.yaml")))


def spec_of(text: str):
    return docs_spec(effective(text))


def one_type(
    paths=("**/*.md",),
    where="",
    metadata="[{name: path}, {name: value}]",
    fields="{}",
    relationships="",
):
    globs = ", ".join(f'"{p}"' for p in paths)
    return spec_of(f"""
        version: 1
        node_types:
          - label: Note
            key: [path]
            metadata: {metadata}
            source:
              provider: docs
              paths: [{globs}]
              where: [{where}]
              fields: {fields}
        {relationships}
    """)


def write(root: Path, files: dict[str, str]) -> dict[str, Path]:
    out = {}
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        out[rel] = path
    return out


def fm(body: str) -> str:
    return f"---\n{textwrap.dedent(body).strip()}\n---\n# Title\n"


def nodes_of(spec, files):
    return build_nodes(spec, "demo", read_selected(spec, files))


def edges_of(spec, files, targets=None):
    return build_edges(spec, "demo", read_selected(spec, files), targets)


def names(nodes):
    return sorted(n["name"] for n in nodes)


# --- spec ---------------------------------------------------------------------------------


def test_no_docs_sources_means_no_spec():
    assert docs_spec(resolve_declaration(None)) is None
    assert spec_of("""
        version: 1
        node_types:
          - label: File
            key: [path]
            metadata: [{name: path}]
            source: {provider: filesystem, kind: file}
    """) is None


def test_spec_carries_types_fields_and_relationships():
    spec = spec_of(RUNBOOK)
    (runbook,) = spec.types
    assert runbook.label == "Runbook"
    assert runbook.paths == ("runbooks/**/*.md",)
    assert [(f.name, f.key, f.type, f.required) for f in runbook.fields] == [
        ("owner", "owner", "string", True),
        ("severity", "severity", "integer", False),
        ("on_call", "on-call-team", "string", False),
    ]
    (rel,) = spec.relationships
    assert (rel.type, rel.from_labels, rel.to_label, rel.field) == ("RUNBOOK_FOR", ("Runbook",), "Service", "service")


# --- globs --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("globs", "rel", "selected"),
    [
        (("runbooks/*.md",), "runbooks/a.md", True),
        (("runbooks/*.md",), "Runbooks/a.md", False),  # case-sensitive
        (("runbooks/*.md",), "runbooks/a.MD", False),
        (("runbooks/*.md",), "runbooks/deep/a.md", False),
        (("runbooks/**/*.md",), "runbooks/deep/er/a.md", True),
        (("runbooks/**/*.md",), "runbooks/a.md", True),
        (("**/*.md",), "a.md", True),
        (("**/*",), "runbooks/a.markdown", True),
        (("**/*",), "runbooks/a.txt", False),  # never a non-Markdown file
        (("**/*",), "runbooks/a.mdx", False),
        (("**/*",), "README", False),
        (("a/*.md", "b/*.md"), "b/x.md", True),
    ],
)
def test_file_selection(globs, rel, selected):
    spec = one_type(paths=globs)
    assert docs.selects(spec.types[0], rel) is selected


@pytest.mark.parametrize(
    ("glob", "rel", "matched"),
    [
        ("a/*.md", "a/x.md", True),
        ("a/*.md", "a/b/x.md", False),  # * stays inside one folder
        ("*.md", "a/x.md", False),
        ("a/**/x.md", "a/x.md", True),  # ** may be no folders
        ("a/**/x.md", "a/b/c/x.md", True),
        ("**", "a/b/x.md", True),
        ("**/b/**/*.md", "b/x.md", True),
        ("**/b/**/*.md", "a/b/c/x.md", True),
        ("**/b/**/*.md", "a/c/x.md", False),
        ("a/x?.md", "a/x1.md", True),
        ("a/[xy].md", "a/y.md", True),
        ("a/[!xy].md", "a/y.md", False),
        ("a/*.md", "a/.hidden.md", True),
        ("A/*.md", "a/x.md", False),
        ("a/b", "a/b/c", False),
        ("a/b/c", "a/b", False),
    ],
)
def test_glob_matches_one_folder_at_a_time(glob, rel, matched):
    assert glob_matches(glob, rel) is matched


def test_no_malformed_glob_raises_when_matched():
    alphabet = "[]!*?-/a."
    for size in range(1, 5):
        for chars in itertools.product(alphabet, repeat=size):
            glob = "".join(chars)
            for rel in ("a", "a/b.md", "[a].md", "a/.md"):
                glob_matches(glob, rel)


def test_a_malformed_glob_validates_and_matches_nothing(tmp_path):
    spec = one_type(paths=("[", "a/[!]", "[a-"))
    assert not docs.selects(spec.types[0], "a/b.md")


@pytest.mark.parametrize(
    ("glob", "rel"),
    [
        ("*a" * 7 + "*.md", "a" * 61 + ".mx"),
        ("*a" * 30 + "*.md", "a" * 200 + ".mx"),
        ("**/*/" * 30 + "x", "a/" * 60 + "y"),  # refused by validation; still fast when matched
        ("**/" + "*a" * 20 + "/**/" + "*a" * 20 + "/x", "/".join(["a" * 50] * 60) + "/y"),
    ],
    ids=["review name", "longer name", "thirty globstars", "deep path"],
)
def test_matching_never_backtracks(glob, rel):
    started = time.perf_counter()
    assert glob_matches(glob, rel) is False
    assert time.perf_counter() - started < 0.1


REVIEW_GLOB = "*a" * 7 + "*.md"
# PurePosixPath.full_match takes about 0.8 s on this path; the segment matcher is instant.
REVIEW_PATH = "a" * 40 + "/b.md"


def test_selection_is_wired_to_the_segment_matcher():
    docs_type = one_type(paths=(REVIEW_GLOB,)).types[0]
    started = time.perf_counter()
    assert docs.selects(docs_type, REVIEW_PATH) is False
    assert time.perf_counter() - started < 0.1


def test_reading_and_building_are_wired_to_the_segment_matcher(tmp_path):
    spec = one_type(paths=(REVIEW_GLOB,))
    files = write(tmp_path, {REVIEW_PATH: fm("value: x")})
    started = time.perf_counter()
    assert read_selected(spec, files) == {}
    assert build_nodes(spec, "demo", read_selected(spec, files)) == ([], [])
    assert time.perf_counter() - started < 0.1


@pytest.mark.parametrize("last", ["a", "x"], ids=["no match", "match"])
def test_a_long_run_after_a_globstar_stays_fast_on_a_deep_path(last):
    glob = "**/" + "*/" * 98 + "x"
    rel = "/".join(["a"] * 2047 + [last])
    started = time.perf_counter()
    assert glob_matches(glob, rel) is (last == "x")
    assert time.perf_counter() - started < 0.02


def test_globstars_with_fixed_folders_between_them():
    assert glob_matches("a/**/b/c/**/*.md", "a/x/b/c/y/z.md")
    assert glob_matches("a/**/b/c/**/*.md", "a/b/c/z.md")
    assert not glob_matches("a/**/b/c/**/*.md", "a/b/x/c/z.md")
    assert not glob_matches("a/**/b/**/b", "a/b")
    assert glob_matches("a/**/b/**/b", "a/b/b")
    assert glob_matches("**/**", "x")


# --- conditions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("condition", "front_matter", "holds"),
    [
        ("{field: kind, is: runbook}", "kind: runbook", True),
        ("{field: kind, is: runbook}", "kind: Runbook", False),  # case-sensitive
        ("{field: version, is: 1}", "version: 1", True),
        ("{field: version, is: '1'}", "version: 1", True),
        ("{field: version, is: 1}", "version: '1'", True),
        ("{field: draft, is: true}", "draft: yes", True),
        ("{field: draft, is: 'true'}", "draft: true", True),
        ("{field: draft, is: false}", "draft: off", True),
        ("{field: tags, is: oncall}", "tags: [runbook, oncall]", True),
        ("{field: tags, is: 2}", "tags: [1, 2]", True),
        ("{field: tags, is: true}", "tags: [x, true]", True),
        ("{field: tags, is: other}", "tags: [runbook, oncall]", False),
        ("{field: title, starts_with: RB-}", "title: RB-12 db", True),
        ("{field: title, starts_with: RB-}", "title: rb-12", False),
        ("{field: title, starts_with: RB-}", "title: XRB-1", False),  # contains would hold
        ("{field: id, starts_with: 12}", "id: 1234", True),
        ("{field: title, contains: db}", "title: RB-12 db failover", True),
        ("{field: title, contains: DB}", "title: RB-12 db failover", False),
        ("{field: title, like: 'RB-*-db'}", "title: RB-12-db", True),
        ("{field: title, like: 'RB-*'}", "title: XRB-12", False),
        ("{field: title, like: 'rb-*'}", "title: RB-1", False),  # case-sensitive
        ("{field: title, like: 'a?c'}", "title: abc", False),  # only * is a wildcard
        ("{field: title, like: 'a?c'}", "title: a?c", True),
        ("{field: title, like: '[ab]*'}", "title: a1", False),
        ("{field: title, like: '[ab]*'}", "title: '[ab]1'", True),
        ("{field: title, like: 'a]*'}", "title: a]1", True),
        ("{field: v, is: 1}", "v: 0x" + "f" * 5000, False),  # huge ints never hold
        ("{field: v, like: '*'}", "v: 0b" + "1" * 70, False),
        ("{field: v, like: '*'}", "v: [0x" + "f" * 5000 + ", ok]", True),
        ("{field: v, like: '*'}", 'v: "\\ud800"', False),  # a lone surrogate never holds
        ("{field: 'on', is: x}", "on: x", True),  # keys are kept as written
        ("{field: 'yes', is: b}", "on: a\nyes: b", True),
        ("{field: '1', is: one}", "1: one", True),
        ("{field: tags, like: 'on*'}", "tags: [runbook, oncall]", True),
        ("{field: score, is: '1.5'}", "score: 1.5", False),  # floats never hold
        ("{field: when, is: '2026-01-01'}", "when: 2026-01-01", False),  # dates never hold
        ("{field: meta, contains: a}", "meta: {a: 1}", False),  # maps never hold
        ("{field: tags, contains: a}", "tags: [[a]]", False),  # nested lists never hold
        ("{field: kind, is: runbook}", "other: x", False),  # absent never holds
        ("{field: kind, like: '*'}", "kind: null", False),
    ],
)
def test_each_operator(tmp_path, condition, front_matter, holds):
    spec = one_type(where=condition)
    files = write(tmp_path, {"a.md": fm(front_matter)})
    nodes, _ = nodes_of(spec, files)
    assert bool(nodes) is holds


def test_a_value_over_4_kib_never_holds(tmp_path):
    spec = one_type(where="{field: body, like: '*'}")
    files = write(tmp_path, {"a.md": fm("body: " + "x" * 4097), "b.md": fm("body: " + "x" * 4096)})
    nodes, _ = nodes_of(spec, files)
    assert names(nodes) == ["b.md"]


def test_all_conditions_must_hold(tmp_path):
    spec = one_type(where="{field: kind, is: runbook}, {field: title, starts_with: RB-}")
    files = write(tmp_path, {
        "both.md": fm("kind: runbook\ntitle: RB-1"),
        "one.md": fm("kind: runbook\ntitle: other"),
    })
    nodes, problems = nodes_of(spec, files)
    assert names(nodes) == ["both.md"]
    assert problems == []


def test_no_front_matter_is_a_node_without_where_and_none_with_it(tmp_path):
    files = write(tmp_path, {"plain.md": "# Just a heading\n", "empty.md": ""})
    nodes, problems = nodes_of(one_type(), files)
    assert names(nodes) == ["empty.md", "plain.md"] and problems == []
    nodes, problems = nodes_of(one_type(where="{field: kind, is: x}"), files)
    assert nodes == [] and problems == []


def test_each_selected_file_is_read_once_and_others_never(tmp_path, monkeypatch):
    files = write(tmp_path, {"runbooks/a.md": fm("owner: ops\nservice: api"), "other/b.md": fm("owner: ops")})
    seen = []
    real = docs.read_front_matter
    monkeypatch.setattr(docs, "read_front_matter", lambda p: (seen.append(p), real(p))[1])
    spec = spec_of(RUNBOOK)
    selected = read_selected(spec, files)
    nodes, _ = build_nodes(spec, "demo", selected)
    rels = build_edges(spec, "demo", selected)
    assert seen == [files["runbooks/a.md"]]
    assert list(selected) == ["runbooks/a.md"] and selected["runbooks/a.md"][1] is None
    assert names(nodes) == ["runbooks/a.md"] and len(rels) == 1


# --- fields and coercion ------------------------------------------------------------------


def test_fields_rename_and_the_same_name_default(tmp_path):
    files = write(tmp_path, {"runbooks/a.md": fm("owner: ops\non-call-team: dba\non_call: ignored")})
    (node,), problems = nodes_of(spec_of(RUNBOOK), files)
    assert node["properties"]["owner"] == "ops"
    assert node["properties"]["on_call"] == "dba"
    assert problems == []


def test_node_shape_is_name_path_extractor_and_declared_fields(tmp_path):
    files = write(tmp_path, {"runbooks/a.md": fm("owner: ops\nextra: x\nname: spoof\nextractor: spoof")})
    (node,), _ = nodes_of(spec_of(RUNBOOK), files)
    assert node == {
        "label": "Runbook",
        "repo_id": "demo",
        "name": "runbooks/a.md",
        "properties": {
            "path": "runbooks/a.md",
            "extractor": "docs",
            "owner": "ops",
            "severity": None,
            "on_call": None,
        },
    }


def test_path_always_comes_from_the_file_not_the_front_matter(tmp_path):
    files = write(tmp_path, {"a.md": fm("path: elsewhere.md")})
    (node,), _ = nodes_of(one_type(), files)
    assert node["properties"]["path"] == "a.md"


INT64_MAX = 2**63 - 1


@pytest.mark.parametrize(
    ("declared", "yaml_value", "expected"),
    [
        ("string", "hello", "hello"),
        ("string", "12", "12"),
        ("string", "-7", "-7"),
        ("string", "true", "true"),
        ("string", "yes", "true"),  # YAML 1.1 boolean, written as text
        ("string", "off", "false"),
        ("string", "1.5", "1.5"),
        ("string", "'" + "x" * 4096 + "'", "x" * 4096),
        ("string", "'" + "x" * 4097 + "'", None),
        ("string", "[a, b]", None),
        ("string", "{a: 1}", None),
        ("string", "2026-01-01", None),
        ("string", "null", None),
        ("string", "0x" + "f" * 5000, None),
        ("string", "0b" + "1" * 70, None),
        ("string", "0x7fffffffffffffff", str(INT64_MAX)),
        ("string", '"\\ud800"', None),
        ("integer", "0x" + "f" * 5000, None),
        ("float", "0b" + "1" * 70, None),
        ("integer", "3", 3),
        ("integer", str(INT64_MAX), INT64_MAX),
        ("integer", str(-(2**63)), -(2**63)),
        ("integer", str(INT64_MAX + 1), None),
        ("integer", str(-(2**63) - 1), None),
        ("integer", "true", None),  # a bool is not an integer
        ("integer", "'3'", None),
        ("integer", "3.0", None),
        ("float", "1.5", 1.5),
        ("float", "2", 2.0),
        ("float", str(INT64_MAX + 1), None),
        ("float", "true", None),
        ("float", "'1.5'", None),
        ("boolean", "true", True),
        ("boolean", "no", False),
        ("boolean", "1", None),
        ("boolean", "'true'", None),
    ],
)
def test_coercion(tmp_path, declared, yaml_value, expected):
    spec = one_type(metadata=f"[{{name: path}}, {{name: value, type: {declared}}}]")
    files = write(tmp_path, {"a.md": fm(f"value: {yaml_value}")})
    (node,), _ = nodes_of(spec, files)
    value = node["properties"]["value"]
    assert value == expected and type(value) is type(expected)


@pytest.mark.parametrize(
    ("declared", "yaml_value", "reason"),
    [
        ("string", "0x" + "f" * 5000, "'value' does not fit in 64 bits, left blank"),
        ("string", '"\\ud800"', "'value' is not valid text, left blank"),
        ("string", "'" + "x" * 4097 + "'", "'value' is longer than 4 KiB, left blank"),
        ("integer", "0b" + "1" * 70, "'value' does not fit in 64 bits, left blank"),
    ],
)
def test_blank_reasons(tmp_path, declared, yaml_value, reason):
    spec = one_type(metadata=f"[{{name: path}}, {{name: value, type: {declared}}}]")
    files = write(tmp_path, {"a.md": fm(f"value: {yaml_value}")})
    _, problems = nodes_of(spec, files)
    assert [p.reason for p in problems] == [reason]


def test_a_field_named_like_a_yaml_boolean_is_found(tmp_path):
    spec = one_type(metadata="[{name: path}, {name: on}, {name: off, type: boolean}]".replace(
        "{name: on}", "{name: 'on'}").replace("{name: off,", "{name: 'off',"))
    files = write(tmp_path, {"a.md": fm("on: duty\noff: yes\nyes: other")})
    (node,), problems = nodes_of(spec, files)
    assert node["properties"]["on"] == "duty" and node["properties"]["off"] is True
    assert problems == []


def test_every_declared_field_is_emitted_even_when_absent(tmp_path):
    spec = one_type(metadata="[{name: path}, {name: a}, {name: b, type: integer}, {name: c, type: boolean}]")
    files = write(tmp_path, {"x.md": fm("b: not-a-number")})
    (node,), problems = nodes_of(spec, files)
    assert node["properties"] == {"path": "x.md", "extractor": "docs", "a": None, "b": None, "c": None}
    assert [(p.label, p.path, p.reason) for p in problems] == [
        ("Note", "x.md", "'b' is not a whole number, left blank")
    ]


def test_a_missing_required_field_skips_the_file_with_its_reason(tmp_path):
    files = write(tmp_path, {
        "runbooks/ok.md": fm("owner: ops"),
        "runbooks/db.md": fm("severity: 2"),
        "runbooks/blank.md": fm("owner:"),
    })
    nodes, problems = nodes_of(spec_of(RUNBOOK), files)
    assert names(nodes) == ["runbooks/ok.md"]
    assert sorted((p.path, p.reason) for p in problems) == [
        ("runbooks/blank.md", "missing required 'owner'"),
        ("runbooks/db.md", "missing required 'owner'"),
    ]


def test_a_required_field_that_cannot_be_coerced_skips_the_file(tmp_path):
    spec = one_type(metadata="[{name: path}, {name: value, type: integer, required: true}]")
    files = write(tmp_path, {"a.md": fm("value: lots")})
    nodes, problems = nodes_of(spec, files)
    assert nodes == []
    assert [p.reason for p in problems] == ["'value' is not a whole number; it is required, so the file is skipped"]


def test_problems_name_the_front_matter_key(tmp_path):
    files = write(tmp_path, {"runbooks/a.md": fm("owner: ops\non-call-team: [a]\nseverity: " + str(2**64))})
    _, problems = nodes_of(spec_of(RUNBOOK), files)
    assert sorted(p.reason for p in problems) == [
        "'on-call-team' is not text, left blank",
        "'severity' does not fit in 64 bits, left blank",
    ]


def test_a_file_can_be_a_node_of_several_types(tmp_path):
    spec = spec_of("""
        version: 1
        node_types:
          - label: A
            key: [path]
            metadata: [{name: path}]
            source: {provider: docs, paths: ["**/*.md"]}
          - label: B
            key: [path]
            metadata: [{name: path}]
            source: {provider: docs, paths: ["x/*.md"]}
    """)
    files = write(tmp_path, {"x/a.md": "", "y/b.md": ""})
    nodes, _ = nodes_of(spec, files)
    assert sorted((n["label"], n["name"]) for n in nodes) == [("A", "x/a.md"), ("A", "y/b.md"), ("B", "x/a.md")]


# --- hostile files ------------------------------------------------------------------------


def test_malformed_front_matter_is_skipped_with_its_reason(tmp_path):
    files = write(tmp_path, {"bad.md": "---\nkey: [unclosed\n---\n", "list.md": "---\n- a\n- b\n---\n"})
    nodes, problems = nodes_of(one_type(), files)
    assert nodes == []
    assert sorted((p.path, p.reason) for p in problems) == [
        ("bad.md", "front matter is not valid YAML"),
        ("list.md", "front matter is not a set of key: value lines"),
    ]


def test_an_alias_bomb_is_skipped(tmp_path):
    bomb = ["a: &a [x, x, x, x, x, x, x, x, x, x]"]
    for level in range(1, 8):
        prev = chr(ord("a") + level - 1)
        name = chr(ord("a") + level)
        bomb.append(f"{name}: &{name} [{', '.join('*' + prev for _ in range(10))}]")
    files = write(tmp_path, {"bomb.md": "---\n" + "\n".join(bomb) + "\n---\n"})
    nodes, problems = nodes_of(one_type(), files)
    assert nodes == [] and [p.reason for p in problems] == ["front matter is not valid YAML"]


def test_an_oversized_file_is_never_read(tmp_path):
    files = write(tmp_path, {"big.md": fm("value: x") + "x" * (1024 * 1024)})
    nodes, problems = nodes_of(one_type(), files)
    assert nodes == [] and [p.reason for p in problems] == ["is larger than 1 MiB, not read"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFOs")
def test_a_fifo_is_never_read(tmp_path):
    fifo = tmp_path / "pipe.md"
    os.mkfifo(fifo)
    nodes, problems = nodes_of(one_type(), {"pipe.md": fifo})
    assert nodes == [] and [p.reason for p in problems] == ["is not a regular file, not read"]


def test_a_vanished_file_is_skipped(tmp_path):
    nodes, problems = nodes_of(one_type(), {"gone.md": tmp_path / "gone.md"})
    assert nodes == [] and [p.reason for p in problems] == ["could not be read"]


def test_read_front_matter(tmp_path):
    files = write(tmp_path, {
        "a.md": fm("owner: ops"),
        "none.md": "# no front matter\n",
        "empty.md": "---\n\n---\n",
        "crlf.md": "---\r\nowner: ops\r\n---\r\n",
        "latin.md": b"---\nowner: \xe9\n---\n".decode("latin-1"),
    })
    assert read_front_matter(files["a.md"]) == ({"owner": "ops"}, None)
    assert read_front_matter(files["none.md"]) == ({}, None)
    assert read_front_matter(files["empty.md"]) == ({}, None)
    assert read_front_matter(files["crlf.md"]) == ({"owner": "ops"}, None)
    assert read_front_matter(write(tmp_path, {"keys.md": fm("on: a\nyes: b\n1: c")})["keys.md"]) == (
        {"on": "a", "yes": "b", "1": "c"}, None
    )
    assert read_front_matter(write(tmp_path, {"seq.md": "---\n? [a]\n: b\n---\n"})["seq.md"]) == (
        None, "front matter is not valid YAML"
    )
    (tmp_path / "bytes.md").write_bytes(b"---\nowner: \xff\n---\n")
    values, problem = read_front_matter(tmp_path / "bytes.md")
    assert problem is None and values["owner"] == "�"


# --- edges --------------------------------------------------------------------------------


def edge_spec(target="Service"):
    return spec_of(f"""
        version: 1
        node_types:
          - label: Runbook
            key: [path]
            metadata: [{{name: path}}, {{name: owner, required: true}}]
            source: {{provider: docs, paths: ["runbooks/**/*.md"], where: [{{field: kind, is: runbook}}]}}
        relationships:
          - type: RUNBOOK_FOR
            provider: docs
            from: Runbook
            to: {target}
            field: service
    """)


def targets_of(rels):
    return sorted((r["from_name"], r["to_name"]) for r in rels)


def test_edge_values_str_and_int_count_bool_does_not(tmp_path):
    files = write(tmp_path, {
        "runbooks/s.md": fm("kind: runbook\nowner: o\nservice: api"),
        "runbooks/i.md": fm("kind: runbook\nowner: o\nservice: 42"),
        "runbooks/b.md": fm("kind: runbook\nowner: o\nservice: yes"),
        "runbooks/l.md": fm("kind: runbook\nowner: o\nservice: [api, 7, true, 1.5, [x], {a: b}, api]"),
        "runbooks/f.md": fm("kind: runbook\nowner: o\nservice: 1.5"),
    })
    rels = edges_of(edge_spec(), files)
    assert targets_of(rels) == [
        ("runbooks/i.md", "42"), ("runbooks/l.md", "7"), ("runbooks/l.md", "api"), ("runbooks/s.md", "api"),
    ]
    assert rels[0] == {
        "from_label": "Runbook",
        "from_name": rels[0]["from_name"],
        "rel_type": "RUNBOOK_FOR",
        "to_label": "Service",
        "to_name": rels[0]["to_name"],
        "repo_id": "demo",
        "properties": {},
    }


def test_edge_values_drop_a_leading_dot_slash(tmp_path):
    files = write(tmp_path, {"runbooks/a.md": fm("kind: runbook\nowner: o\nservice: [./adr/1.md, ./, '']")})
    assert targets_of(edges_of(edge_spec("Runbook"), files)) == [("runbooks/a.md", "adr/1.md")]


def test_edge_values_drop_every_leading_dot_slash(tmp_path):
    files = write(tmp_path, {"runbooks/a.md": fm("kind: runbook\nowner: o\nservice: [././adr/1.md, ././]")})
    assert targets_of(edges_of(edge_spec("Runbook"), files)) == [("runbooks/a.md", "adr/1.md")]


def test_huge_ints_and_lone_surrogates_are_never_edge_values(tmp_path):
    files = write(tmp_path, {
        "runbooks/a.md": fm("kind: runbook\nowner: o\nservice: [0x" + "f" * 5000 + ', 0b' + "1" * 70 + ', "\\ud800", ok]'),
        "runbooks/b.md": fm("kind: runbook\nowner: o\nservice: 0x" + "f" * 5000),
    })
    assert targets_of(edges_of(edge_spec(), files)) == [("runbooks/a.md", "ok")]


def test_edge_lists_are_capped_at_100(tmp_path):
    values = ", ".join(f"s{i}" for i in range(150))
    files = write(tmp_path, {"runbooks/a.md": fm(f"kind: runbook\nowner: o\nservice: [{values}]")})
    rels = edges_of(edge_spec(), files)
    assert len(rels) == 100 and rels[-1]["to_name"] == "s99"


def test_edge_values_over_4_kib_are_dropped(tmp_path):
    files = write(tmp_path, {"runbooks/a.md": fm("kind: runbook\nowner: o\nservice: [" + "x" * 4097 + ", ok]")})
    assert targets_of(edges_of(edge_spec(), files)) == [("runbooks/a.md", "ok")]


def test_edges_only_leave_files_that_became_nodes(tmp_path):
    files = write(tmp_path, {
        "runbooks/node.md": fm("kind: runbook\nowner: o\nservice: api"),
        "runbooks/unmatched.md": fm("kind: other\nowner: o\nservice: api"),
        "runbooks/skipped.md": fm("kind: runbook\nservice: api"),
        "elsewhere/x.md": fm("kind: runbook\nowner: o\nservice: api"),
    })
    assert targets_of(edges_of(edge_spec(), files)) == [("runbooks/node.md", "api")]


def test_edges_can_be_filtered_to_targets(tmp_path):
    files = write(tmp_path, {
        "runbooks/a.md": fm("kind: runbook\nowner: o\nservice: [api, web]"),
        "runbooks/b.md": fm("kind: runbook\nowner: o\nservice: db"),
    })
    rels = edges_of(edge_spec(), files, targets={("Service", "web"), ("Service", "db"), ("Module", "api")})
    assert targets_of(rels) == [("runbooks/a.md", "web"), ("runbooks/b.md", "db")]
    assert edges_of(edge_spec(), files, targets=set()) == []


def test_no_relationships_means_no_edges(tmp_path):
    files = write(tmp_path, {"a.md": fm("value: x")})
    assert edges_of(one_type(), files) == []


# --- doctor report ------------------------------------------------------------------------


def report(tmp_path, files: dict[str, str], schema=RUNBOOK):
    written = write(tmp_path, files)
    return source_report(tmp_path, effective(schema), set(written.values()))


def test_report_counts_matching_files_and_entries(tmp_path):
    lines = report(tmp_path, {
        "runbooks/a.md": fm("owner: ops"),
        "runbooks/deep/b.md": fm("owner: ops"),
        "notes/c.md": fm("owner: ops"),
    })
    assert lines == [{"status": "ok", "detail": "Runbook: 2 files match, 2 Runbook entries"}]


def test_report_uses_singulars(tmp_path):
    lines = report(tmp_path, {"runbooks/a.md": fm("owner: ops")})
    assert lines == [{"status": "ok", "detail": "Runbook: 1 file matches, 1 Runbook entry"}]


def test_report_warns_when_no_file_matches(tmp_path):
    lines = report(tmp_path, {"Runbooks/a.md": fm("owner: ops")})
    assert lines == [{
        "status": "warning",
        "detail": "Runbook: no file matches runbooks/**/*.md "
                  "(matching is case-sensitive; use **/*.md for every folder)",
    }]


def test_report_names_up_to_five_files_with_their_reasons_then_and_n_more(tmp_path):
    files = {f"runbooks/{i}.md": fm("severity: 2") for i in range(7)}
    files["runbooks/bad.md"] = "---\nkey: [unclosed\n---\n"
    files["runbooks/blank.md"] = fm("owner: ops\nseverity: high")
    files["runbooks/ok.md"] = fm("owner: ops")
    lines = report(tmp_path, files)
    assert lines == [
        {"status": "ok", "detail": "Runbook: 10 files match, 2 Runbook entries"},
        {"status": "warning", "detail": "Runbook: runbooks/0.md: missing required 'owner'"},
        {"status": "warning", "detail": "Runbook: runbooks/1.md: missing required 'owner'"},
        {"status": "warning", "detail": "Runbook: runbooks/2.md: missing required 'owner'"},
        {"status": "warning", "detail": "Runbook: runbooks/3.md: missing required 'owner'"},
        {"status": "warning", "detail": "Runbook: runbooks/4.md: missing required 'owner'"},
        {"status": "warning", "detail": "Runbook: and 4 more files with problems"},
    ]


def test_report_joins_several_reasons_for_one_file(tmp_path):
    lines = report(tmp_path, {"runbooks/x.md": fm("owner: ops\nseverity: high\non-call-team: [a]")})
    assert lines[1:] == [{
        "status": "warning",
        "detail": "Runbook: runbooks/x.md: 'severity' is not a whole number, left blank; "
                  "'on-call-team' is not text, left blank",
    }]


def test_report_wording_for_a_bad_file(tmp_path):
    lines = report(tmp_path, {"runbooks/x.md": "---\nkey: [unclosed\n---\n"})
    assert lines == [
        {"status": "ok", "detail": "Runbook: 1 file matches, 0 Runbook entries"},
        {"status": "warning", "detail": "Runbook: runbooks/x.md: front matter is not valid YAML"},
    ]


def test_report_survives_huge_ints_and_surrogates(tmp_path):
    lines = report(tmp_path, {"runbooks/x.md": fm("owner: 0x" + "f" * 5000 + '\nservice: 0b' + "1" * 70)})
    assert lines[1:] == [{
        "status": "warning",
        "detail": "Runbook: runbooks/x.md: 'owner' does not fit in 64 bits; it is required, so the file is skipped",
    }]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file names")
def test_report_never_echoes_an_unprintable_path(tmp_path):
    (tmp_path / "runbooks").mkdir()
    escape = tmp_path / "runbooks" / "a\x1b[31m.md"
    escape.write_text(fm("severity: 2"))
    undecodable = os.fsencode(tmp_path / "runbooks") + b"/\xff.md"
    with open(undecodable, "w") as handle:
        handle.write(fm("severity: 2"))
    files = {escape, Path(os.fsdecode(undecodable))}
    lines = source_report(tmp_path, effective(RUNBOOK), files)
    details = [line["detail"] for line in lines]
    assert all(detail.isprintable() for detail in details)
    assert "Runbook: runbooks/a\\x1b[31m.md: missing required 'owner'" in details
    assert "Runbook: runbooks/\\udcff.md: missing required 'owner'" in details


def test_report_is_empty_without_docs_sources(tmp_path):
    assert source_report(tmp_path, resolve_declaration(None), set()) == []


def test_report_ignores_files_outside_the_repository(tmp_path):
    (tmp_path / "repo").mkdir()
    outside = write(tmp_path, {"runbooks/a.md": fm("owner: ops")})
    lines = source_report(tmp_path / "repo", effective(RUNBOOK), set(outside.values()))
    assert lines[0]["status"] == "warning"


def test_provider_and_walk_never_import_the_dispatcher():
    code = (
        "import sys\n"
        "import devgraph.indexer.providers.docs, devgraph.indexer.walk\n"
        "assert 'devgraph.indexer.dispatch' not in sys.modules, 'dispatch imported'\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
