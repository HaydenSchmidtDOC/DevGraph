"""Alias expansion is bounded wherever repository YAML is read."""

import time

import pytest
import yaml

from devgraph.config.project_schema import SCHEMA_FILENAME, ProjectSchemaError, load_project_schema
from devgraph.config.yaml_bound import YAML_MAX_NODES, YAMLBoundError, bounded_safe_load
from devgraph.indexer.containers.extractor import ContainerExtractor
from devgraph.indexer.docs.extractor import DocsExtractor


def _laughs(depth: int = 9) -> str:
    """Anchors nesting ten-fold `depth` times: about 10**depth nodes once expanded, a few hundred bytes."""
    lines = ["[&l0 [lol, lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    for i in range(1, depth + 1):
        lines.append(f", &l{i} [" + ", ".join([f"*l{i - 1}"] * 10) + "]")
    return "".join(lines) + "]"


SCHEMA_BOMB = (
    "version: 1\nnode_types:\n  - label: Ticket\n    key: [id]\n"
    "    metadata:\n      - name: id\n        type: string\n"
    f"    description: {_laughs()}\n"
)
DOC_BOMB = f"---\ntype: requirement\nid: r1\nextra: {_laughs()}\n---\n# Title\nBody\n"
COMPOSE_BOMB = f"services:\n  web:\n    image: nginx\nx-bomb: {_laughs()}\n"


def _quick(fn, *args, **kwargs):
    start = time.monotonic()
    result = fn(*args, **kwargs)
    assert time.monotonic() - start < 2
    return result


def _pad_doc(expanded: int) -> str:
    """A document of exactly `expanded` nodes once aliases expand, most of them via one alias.

    root, `t`, pad sequence = 3; `&u` = 10 (a sequence of 9 scalars); `r` trailing scalars.
    """
    m, r = divmod(expanded - 13, 10)
    items = ["&u [x, x, x, x, x, x, x, x, x]"] + ["*u"] * m + ["y"] * r
    return "t: [" + ", ".join(items) + "]\n"


def test_bound_is_ten_thousand_nodes():
    assert YAML_MAX_NODES == 10_000


def test_bound_is_exact_on_the_helper():
    assert bounded_safe_load(_pad_doc(50), max_nodes=50) == yaml.safe_load(_pad_doc(50))
    with pytest.raises(YAMLBoundError):
        bounded_safe_load(_pad_doc(51), max_nodes=50)


@pytest.mark.parametrize("text", ["a: &a [*a]", "a: &a {b: *a}"])
def test_recursive_alias_is_refused(text):
    with pytest.raises(YAMLBoundError, match="refers to itself"):
        bounded_safe_load(text)


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
when: 2024-05-01
"""


@pytest.mark.parametrize("text", [ANCHORED, "", "# only a comment\n", "just text", "[1, 2.5, true, null]"])
def test_normal_documents_load_as_safe_load_does(text):
    assert bounded_safe_load(text) == yaml.safe_load(text)


@pytest.mark.parametrize("text", ["a: 1\n---\nb: 2\n", "a: !!python/object:os.system x\n", "a: !custom x\n"])
def test_multi_document_and_unsafe_tags_are_refused_like_safe_load(text):
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)
    with pytest.raises(yaml.YAMLError):
        bounded_safe_load(text)


def test_project_schema_refuses_the_bomb_quickly(tmp_path):
    (tmp_path / SCHEMA_FILENAME).write_text(SCHEMA_BOMB)
    with pytest.raises(ProjectSchemaError, match="malformed YAML"):
        _quick(load_project_schema, tmp_path)


def test_project_schema_still_parses_anchors_and_merge_keys(tmp_path):
    (tmp_path / SCHEMA_FILENAME).write_text(
        "version: 1\nnode_types:\n  - label: Ticket\n    key: &key [id]\n    metadata:\n"
        "      - &id_field {name: id, type: string}\n      - <<: *id_field\n        name: title\n"
        "        required: true\n  - label: Story\n    key: *key\n    metadata: [*id_field]\n"
    )
    ticket = load_project_schema(tmp_path).node_types[0]
    assert [(f.name, f.required) for f in ticket.metadata] == [("id", False), ("title", True)]


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
