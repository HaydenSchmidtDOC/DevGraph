from pathlib import Path

import pytest

from devgraph.config.list_edit import (
    ListEditError,
    add_entry_text,
    delete_entry_text,
    dump_entry,
    entries,
    replace_entry_text,
)
from devgraph.config.project_schema import parse_project_schema

NODE = {"key": "node_types", "ident": "label"}
REL = {"key": "relationships", "ident": "type"}

DOC = """\
# Header comment
version: 1
extends: default

node_types:
  # the widget
  - label: Widget
    key: [slug]
    metadata:
      - name: slug

  # the gadget
  - label: Gadget
    key: [slug]
    metadata:
      - name: slug
# between blocks
relationships:
  - type: USES
    from: Widget
    to: Gadget
  - type: USES
    from: Gadget
    to: Widget
  - type: OWNS
    from: Widget
    to: Gadget
    provider: custom
    custom: {name: c}
# trailing comment
"""

PATH = Path("devgraph.schema.yaml")


def split(text, start, end):
    i = text.index(start)
    j = text.index(end) if end else len(text)
    return text[i:j]


def test_entries_lists_mappings():
    assert [n["label"] for n in entries(DOC, key="node_types")] == ["Widget", "Gadget"]
    assert entries("", key="node_types") == []


def test_add_node_type_leaves_relationships_and_comments_alone():
    new = {"label": "Thing", "key": ["slug"], "metadata": [{"name": "slug"}]}
    out = add_entry_text(DOC, new, version=1, **NODE)
    assert split(out, "# between blocks", None) == split(DOC, "# between blocks", None)
    assert out.startswith(split(DOC, "# Header", "# between blocks"))
    assert "- label: Thing" in out
    assert [n.label for n in parse_project_schema(out, PATH).node_types] == ["Widget", "Gadget", "Thing"]


def test_replace_node_type_keeps_everything_outside_entry():
    new = {"label": "Widget", "key": ["id"], "metadata": [{"name": "id"}]}
    out = replace_entry_text(DOC, "Widget", new, **NODE)
    assert split(out, "# between blocks", None) == split(DOC, "# between blocks", None)
    assert "# the widget" in out and "# the gadget" in out
    assert "key: [id]" not in out  # re-dumped in block style
    assert parse_project_schema(out, PATH).node_types[0].key == ("id",)


def test_delete_node_type_keeps_everything_outside_entry():
    out = delete_entry_text(DOC, "Widget", **NODE)
    assert split(out, "# between blocks", None) == split(DOC, "# between blocks", None)
    assert "# the gadget" in out and "Widget\n    key" not in out
    assert [n.label for n in parse_project_schema(out, PATH).node_types] == ["Gadget"]


def test_add_relationship_without_key_appends_at_end():
    text = "# c\nversion: 1\nnode_types:\n  - label: A\n    key: [s]\n    metadata:\n      - name: s\n# tail\n"
    new = {"type": "REFERENCES", "from": "A", "to": "A", "provider": "builtin"}
    out = add_entry_text(text, new, version=1, **REL)
    assert out.startswith(text)
    assert out[len(text):].startswith("relationships:\n- type: REFERENCES")
    assert parse_project_schema(out, PATH).relationships[0].type == "REFERENCES"


def test_new_document_starts_with_version():
    out = add_entry_text("", {"label": "A"}, version=7, **NODE)
    assert out == "version: 7\nnode_types:\n- label: A\n"


@pytest.mark.parametrize("op", ["replace", "delete"])
def test_duplicate_identity_refused(op):
    with pytest.raises(ListEditError, match=r"'USES' is declared 2 times; edit the file by hand"):
        if op == "replace":
            replace_entry_text(DOC, "USES", {"type": "USES"}, **REL)
        else:
            delete_entry_text(DOC, "USES", **REL)


def test_single_relationship_of_other_type_still_editable():
    out = delete_entry_text(DOC, "OWNS", **REL)
    assert out.endswith("# trailing comment\n") and "OWNS" not in out


def test_noun_in_messages():
    with pytest.raises(ListEditError, match="no widget named 'x'"):
        delete_entry_text(DOC, "x", noun="widget", **NODE)
    with pytest.raises(ListEditError, match="a node type named 'Widget' already exists"):
        add_entry_text(DOC, {"label": "Widget"}, version=1, noun="node type", **NODE)


def test_dump_entry_block_scalars():
    assert dump_entry({"a": "x\ny"}) == "a: |-\n  x\n  y\n"


def test_default_noun_reads_with_an_article():
    with pytest.raises(ListEditError, match="an entry named 'Widget' already exists"):
        add_entry_text(DOC, {"label": "Widget"}, version=1, **NODE)


def test_nested_node_types_key_in_a_relationship_is_ignored():
    text = "version: 1\nrelationships:\n  - type: USES\n    from: A\n    to: B\n    node_types:\n      - label: Ghost\n"
    assert entries(text, key="node_types") == []
    out = add_entry_text(text, {"label": "Real"}, version=1, **NODE)
    assert out.startswith(text) and out.endswith("node_types:\n- label: Real\n")


def test_unique_relationship_edit_leaves_node_types_byte_identical():
    head = split(DOC, "# Header", "relationships:")
    added = add_entry_text(DOC, {"type": "NEW", "from": "A", "to": "B"}, version=1, **REL)
    assert added.startswith(head)
    replaced = replace_entry_text(DOC, "OWNS", {"type": "OWNS", "from": "A", "to": "B"}, **REL)
    assert replaced.startswith(head)


@pytest.mark.parametrize("op", ["replace", "delete"])
def test_tools_duplicate_name_refused(op):
    text = "version: 1\ntools:\n  - name: t\n    cypher: a\n  - name: t\n    cypher: b\n"
    kw = {"key": "tools", "ident": "name"}
    with pytest.raises(ListEditError, match="declared 2 times"):
        if op == "replace":
            replace_entry_text(text, "t", {"name": "t"}, **kw)
        else:
            delete_entry_text(text, "t", **kw)


TOOLS = {"key": "tools", "ident": "name"}
TOOL_DOC = "version: 1\ntools:\n  - name: one\n    cypher: RETURN 1\n"


@pytest.mark.parametrize("sep", [" ", " ", "\x85", "\r"])
def test_unicode_line_breaks_cannot_inject_keys(sep):
    # YAML reads these as line breaks; a `|` block would let the text after them
    # land at mapping level once the entry is indented into the list.
    entry = {"name": "two", "description": f"a\nb{sep}    max_rows: 7", "cypher": "RETURN 1"}

    for text in (add_entry_text(TOOL_DOC, entry, version=1, **TOOLS),
                 replace_entry_text(TOOL_DOC, "one", entry, **TOOLS)):
        assert entries(text, key="tools")[-1] == entry
    assert dump_entry({"d": f"x{sep}y"}).startswith('d: "')


def test_splice_that_diverges_from_the_entry_is_refused(monkeypatch):
    import devgraph.config.list_edit as list_edit

    entry = {"name": "two", "description": "a", "cypher": "RETURN 1"}
    monkeypatch.setattr(list_edit, "dump_entry", lambda e: "name: two\ndescription: a\ncypher: RETURN 1\nmax_rows: 7\n")
    for call in (lambda: add_entry_text(TOOL_DOC, entry, version=1, **TOOLS),
                 lambda: replace_entry_text(TOOL_DOC, "one", entry, **TOOLS)):
        with pytest.raises(ListEditError) as exc:
            call()
        assert exc.value.code == "invalid"


def test_delete_that_touches_other_entries_is_refused(monkeypatch):
    import devgraph.config.list_edit as list_edit

    real = list_edit._Doc.span
    monkeypatch.setattr(list_edit._Doc, "span", lambda self, position: (real(self, position)[0], len(self.lines)))
    doc = TOOL_DOC + "  - name: two\n    cypher: RETURN 2\n"
    with pytest.raises(ListEditError) as exc:
        delete_entry_text(doc, "one", **TOOLS)
    assert exc.value.code == "invalid"


@pytest.mark.parametrize("ident", [b"bin", 3, None, ["x"]])
def test_identity_must_be_a_plain_string(ident):
    entry = {"name": ident, "cypher": "RETURN 1"}
    for call in (lambda: add_entry_text(TOOL_DOC, entry, version=1, **TOOLS),
                 lambda: replace_entry_text(TOOL_DOC, "one", entry, **TOOLS)):
        with pytest.raises(ListEditError) as exc:
            call()
        assert exc.value.code == "invalid"
