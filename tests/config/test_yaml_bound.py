"""Alias expansion is bounded wherever repository and global config YAML is read."""

import time

import pytest
import yaml

from devgraph.config import edits, global_tools
from devgraph.config.list_edit import ListEditError, entries
from devgraph.config.project_schema import SCHEMA_FILENAME, ProjectSchemaError, parse_project_schema
from devgraph.config.project_tools import TOOLS_FILENAME, ProjectToolsError, load_project_tools
from devgraph.config.yaml_bound import YAML_MAX_NODES, YAMLBoundError, bounded_safe_load
from devgraph.indexer.containers.extractor import ContainerExtractor
from devgraph.indexer.docs.extractor import DocsExtractor


def _laughs(depth: int = 9) -> str:
    """Anchors nesting ten-fold `depth` times: about 10**depth nodes once expanded, a few hundred bytes."""
    lines = ["[&l0 [lol, lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    for i in range(1, depth + 1):
        lines.append(f", &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]")
    return "".join(lines) + "]"


# The bomb sits inside a node type's description, a field the schema allows.
SCHEMA_BOMB = (
    "version: 1\nnode_types:\n  - label: Ticket\n    key: [id]\n"
    "    metadata:\n      - name: id\n        type: string\n"
    f"    description: {_laughs()}\n"
)
TOOLS_BOMB = (
    "version: 1\ntools:\n  - name: count_things\n    description: Count things\n"
    "    cypher: 'MATCH (n {repo_id: $repo_id}) RETURN count(n) AS n'\n"
    f"    params: {_laughs()}\n"
)


def _tools_doc(expanded: int) -> str:
    """A tools document of exactly `expanded` nodes once aliases expand, most of them via one alias.

    root, `tools`, its sequence, the entry, `name`, `t`, `pad` = 7; the pad sequence = 1;
    each copy of `&u` = 10 (a sequence of 9 scalars); `r` trailing scalars.
    """
    m, r = divmod(expanded - 18, 10)
    items = ["&u [x, x, x, x, x, x, x, x, x]"] + ["*u"] * m + ["y"] * r
    return "tools:\n  - name: t\n    pad: [" + ", ".join(items) + "]\n"


DOC_BOMB = f"---\ntype: requirement\nid: r1\nextra: {_laughs()}\n---\n# Title\nBody\n"
COMPOSE_BOMB = f"services:\n  web:\n    image: nginx\nx-bomb: {_laughs()}\n"


def _quick(fn, *args, **kwargs):
    start = time.monotonic()
    result = fn(*args, **kwargs)
    assert time.monotonic() - start < 2
    return result


def test_bound_is_ten_thousand_nodes():
    assert YAML_MAX_NODES == 10_000


def test_exactly_the_bound_is_accepted_and_one_more_is_refused():
    at = _tools_doc(YAML_MAX_NODES)
    assert len(at) < 10_000  # the size comes from the alias, not from the text
    [entry] = entries(at, key="tools")
    assert len(entry["pad"]) == 1 + (YAML_MAX_NODES - 18) // 10 + (YAML_MAX_NODES - 18) % 10
    with pytest.raises(ListEditError) as excinfo:
        entries(_tools_doc(YAML_MAX_NODES + 1), key="tools")
    assert excinfo.value.code == "malformed"
    assert "more than 10000" in str(excinfo.value)


def test_bound_is_exact_on_the_helper():
    assert bounded_safe_load(_tools_doc(50), max_nodes=50) == yaml.safe_load(_tools_doc(50))
    with pytest.raises(YAMLBoundError):
        bounded_safe_load(_tools_doc(51), max_nodes=50)


def test_list_edit_entries_refuses_the_bomb_quickly():
    with pytest.raises(ListEditError) as excinfo:
        _quick(entries, SCHEMA_BOMB, key="node_types")
    assert excinfo.value.code == "malformed"


def test_parse_project_schema_refuses_the_bomb_quickly(tmp_path):
    with pytest.raises(ProjectSchemaError, match="malformed YAML"):
        _quick(parse_project_schema, SCHEMA_BOMB, tmp_path / SCHEMA_FILENAME)


def test_load_project_tools_refuses_the_bomb_quickly(tmp_path):
    (tmp_path / TOOLS_FILENAME).write_text(TOOLS_BOMB)
    with pytest.raises(ProjectToolsError, match="malformed YAML"):
        _quick(load_project_tools, tmp_path)


def test_global_store_refuses_the_bomb_quickly(tmp_path, monkeypatch):
    path = tmp_path / "global-tools.json"
    path.write_text(TOOLS_BOMB)
    monkeypatch.setattr(global_tools, "_default_path", lambda: path)
    with pytest.raises(ProjectToolsError, match="malformed YAML"):
        _quick(global_tools.load_global_tools)


def test_reset_works_on_a_bomb_quickly(tmp_path):
    (tmp_path / SCHEMA_FILENAME).write_text(SCHEMA_BOMB)
    (tmp_path / TOOLS_FILENAME).write_text(TOOLS_BOMB)

    schema = _quick(edits.reset_schema, tmp_path, dry_run=True)
    tools = _quick(edits.reset_tools, tmp_path, dry_run=True)

    assert schema.removed == {"node_types": None, "relationships": None}
    assert tools.removed["tools"] is None
    _quick(edits.reset_schema, tmp_path)
    assert not (tmp_path / SCHEMA_FILENAME).exists()


@pytest.mark.parametrize("text", ["a: &a [*a]", "a: &a {b: *a}"])
def test_recursive_alias_is_refused(text):
    with pytest.raises(YAMLBoundError, match="refers to itself"):
        bounded_safe_load(text)
    with pytest.raises(ListEditError):
        entries(text, key="tools")


ANCHORED = """\
version: 1
defaults: &defaults
  type: string
  required: false
node_types:
  - label: Ticket
    key: [id]
    metadata:
      - <<: *defaults
        name: id
      - <<: *defaults
        name: title
        required: true
  - label: Story
    key: &story_key [id]
    metadata:
      - {<<: *defaults, name: id}
relationships:
  - type: BLOCKS
    from: [Ticket, Story]
    to: Ticket
when: 2024-05-01
"""


@pytest.mark.parametrize("text", [ANCHORED, "", "# only a comment\n", "just text", "[1, 2.5, true, null]"])
def test_normal_documents_load_as_safe_load_does(text):
    assert bounded_safe_load(text) == yaml.safe_load(text)


SCHEMA_ANCHORED = """\
version: 1
node_types:
  - label: Ticket
    key: &key [id]
    metadata:
      - &id_field {name: id, type: string}
      - <<: *id_field
        name: title
        required: true
  - label: Story
    key: *key
    metadata: [*id_field]
"""


def test_anchors_and_merge_keys_still_parse_as_a_schema(tmp_path):
    assert bounded_safe_load(SCHEMA_ANCHORED) == yaml.safe_load(SCHEMA_ANCHORED)
    schema = parse_project_schema(SCHEMA_ANCHORED, tmp_path / SCHEMA_FILENAME)
    ticket = schema.node_types[0]
    assert [(f.name, f.required) for f in ticket.metadata] == [("id", False), ("title", True)]


@pytest.mark.parametrize("text", ["a: 1\n---\nb: 2\n", "a: !!python/object:os.system x\n", "a: !custom x\n"])
def test_multi_document_and_unsafe_tags_are_refused_like_safe_load(text):
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)
    with pytest.raises(yaml.YAMLError):
        bounded_safe_load(text)


def test_docs_frontmatter_bomb_is_skipped_quickly():
    result = _quick(DocsExtractor("r").extract_from_source, DOC_BOMB, "r1.md")
    assert result.docs == []


def test_docs_frontmatter_with_anchors_and_merge_keys_still_parses():
    content = (
        "---\ntype: requirement\nid: r1\nbase: &b {links: [Auth]}\nmore:\n  <<: *b\n"
        "links: [Auth, Billing]\n---\n# Title\nBody\n"
    )
    [doc] = DocsExtractor("r").extract_from_source(content, "r1.md").docs
    assert doc.name == "r1"


def test_compose_bomb_is_skipped_quickly():
    result = _quick(ContainerExtractor("r").extract_from_compose_file, COMPOSE_BOMB, "docker-compose.yml")
    assert result.services == []


def test_compose_with_anchors_and_merge_keys_still_parses():
    content = (
        "x-common: &common\n  image: nginx\n  restart: always\n"
        "services:\n  web:\n    <<: *common\n  worker:\n    <<: *common\n"
    )
    result = ContainerExtractor("r").extract_from_compose_file(content, "docker-compose.yml")
    assert sorted(s.name for s in result.services) == ["web", "worker"]
