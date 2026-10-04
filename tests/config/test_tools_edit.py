import pytest
import yaml

from devgraph.config.project_tools import parse_project_tools
from devgraph.config.tools_edit import (
    ToolsEditError,
    add_tool_text,
    delete_tool_text,
    dump_tool,
    replace_tool_text,
    tool_mappings,
)

DOC = """\
# Project tools for demo
version: 1
tools:
  # finds files
  - name: list_files
    description: List files.
    cypher: |
      MATCH (f:File {repo_id: $repo_id})
      RETURN f.path AS path
  # counts them
  - name: count_files  # inline note
    description: Count files.
    cypher: "MATCH (f:File {repo_id: $repo_id}) RETURN count(f) AS n"
# end of file
"""


def tool(name, cypher="MATCH (f:File {repo_id: $repo_id}) RETURN f"):
    return {"name": name, "description": f"Tool {name}.", "cypher": cypher}


def names(text):
    return [t.name for t in parse_project_tools(text, "x").tools]


def lines_in_order(original, result):
    it = iter(result.splitlines())
    return all(line in it for line in original.splitlines())


def test_tool_mappings():
    assert [m["name"] for m in tool_mappings(DOC)] == ["list_files", "count_files"]
    assert tool_mappings("") == []
    assert tool_mappings("version: 1\n") == []
    assert tool_mappings("version: 1\ntools: []\n") == []


def test_add_appends_with_same_indent_and_keeps_every_line():
    result = add_tool_text(DOC, tool("third"))
    assert lines_in_order(DOC, result)
    assert "  - name: third\n" in result
    assert names(result) == ["list_files", "count_files", "third"]
    # inserted before the trailing top-level comment? it must stay last or at least present
    assert result.endswith("\n")


def test_add_multiline_cypher_keeps_block_and_indent():
    result = add_tool_text(DOC, tool("multi", "MATCH (f:File {repo_id: $repo_id})\nRETURN f.path"))
    assert names(result)[-1] == "multi"
    assert tool_mappings(result)[-1]["cypher"] == "MATCH (f:File {repo_id: $repo_id})\nRETURN f.path"


def test_delete_first_tool_keeps_other_comments():
    result = delete_tool_text(DOC, "list_files")
    assert names(result) == ["count_files"]
    for kept in ("# Project tools for demo", "# counts them", "# end of file", "# inline note"):
        assert kept in result
    assert "list_files" not in result and "MATCH (f:File {repo_id: $repo_id})\n" not in result
    assert "# finds files" in result  # leading comment is outside the item


def test_delete_last_tool_keeps_trailing_comment():
    result = delete_tool_text(DOC, "count_files")
    assert names(result) == ["list_files"]
    assert "# end of file" in result and "# counts them" in result and "# inline note" not in result


def test_delete_only_tool_leaves_valid_empty_list():
    one = add_tool_text("", tool("a"))
    result = delete_tool_text(one, "a")
    assert parse_project_tools(result, "x").tools == ()


def test_replace_keeps_other_comments():
    result = replace_tool_text(DOC, "list_files", tool("renamed"))
    assert names(result) == ["renamed", "count_files"]
    for kept in ("# Project tools for demo", "# finds files", "# counts them", "# inline note", "# end of file"):
        assert kept in result


def test_replace_last_keeps_trailing_comment():
    result = replace_tool_text(DOC, "count_files", tool("count_files", "MATCH (f {repo_id: $repo_id})\nRETURN f"))
    assert names(result) == ["list_files", "count_files"]
    assert result.rstrip().endswith("# end of file")


def test_deeper_trailing_comment_stays_with_last_tool():
    doc = "version: 1\ntools:\n  - name: a\n    description: A.\n    cypher: |\n      MATCH ($repo_id)\n      # deep comment\n"
    assert "deep comment" not in delete_tool_text(doc, "a")
    assert "deep comment" in add_tool_text(doc, tool("b"))


def test_no_trailing_newline_and_inline_comment_tail():
    doc = "version: 1\ntools:\n- name: a\n  description: A.\n  cypher: 'MATCH ($repo_id)'"
    result = add_tool_text(doc, tool("b"))
    assert result.endswith("\n") and names(result) == ["a", "b"]
    assert result.splitlines()[2] == "- name: a"


def test_flow_list_refused():
    doc = "version: 1\ntools: [{name: a, description: A., cypher: 'MATCH ($repo_id)'}]\n"
    for fn in (lambda: add_tool_text(doc, tool("b")), lambda: delete_tool_text(doc, "a"), lambda: replace_tool_text(doc, "a", tool("a"))):
        with pytest.raises(ToolsEditError, match="block list"):
            fn()


def test_empty_flow_list_becomes_block_on_add():
    result = add_tool_text("version: 1\ntools: []  # none yet\n", tool("a"))
    assert result.startswith("version: 1\ntools:")
    assert "[]" not in result and "# none yet" in result
    assert names(result) == ["a"]


def test_missing_and_null_tools_key_on_add():
    assert names(add_tool_text("version: 1\n", tool("a"))) == ["a"]
    assert names(add_tool_text("version: 1\ntools:\n", tool("a"))) == ["a"]
    assert names(add_tool_text("version: 1\ntools: null\n", tool("a"))) == ["a"]
    assert names(add_tool_text("version: 1\ntools:\nother: 1\n".replace("other: 1\n", ""), tool("a"))) == ["a"]


def test_tools_key_before_another_key():
    doc = "tools:\n- name: a\n  description: A.\n  cypher: 'MATCH ($repo_id)'\nversion: 1\n"
    result = add_tool_text(doc, tool("b"))
    assert names(result) == ["a", "b"] and "version: 1" in result
    assert names(delete_tool_text(result, "a")) == ["b"]


def test_empty_text_creates_document():
    result = add_tool_text("", tool("a"))
    assert result.startswith("version: 1\ntools:\n- name: a\n")
    assert names(result) == ["a"]


def test_crlf_preserved():
    doc = DOC.replace("\n", "\r\n")
    for result in (add_tool_text(doc, tool("c")), delete_tool_text(doc, "list_files"), replace_tool_text(doc, "count_files", tool("z"))):
        assert "\r\n" in result
        assert result.replace("\r\n", "") .count("\n") == 0 and result.count("\r\n") == result.count("\n")
        parse_project_tools(result, "x")


def test_errors():
    for fn in (lambda: delete_tool_text(DOC, "nope"), lambda: replace_tool_text(DOC, "nope", tool("nope")), lambda: add_tool_text(DOC, tool("list_files"))):
        with pytest.raises(ToolsEditError):
            fn()
    with pytest.raises(ToolsEditError):
        add_tool_text("- a\n- b\n", tool("a"))
    with pytest.raises(ToolsEditError):
        add_tool_text("version: 1\ntools: 5\n", tool("a"))
    with pytest.raises(ToolsEditError):
        tool_mappings("version: 1\ntools: {a: 1}\n")


def test_dump_tool_block_scalar_and_key_order():
    text = dump_tool({"name": "a", "description": "d", "cypher": "MATCH 1\nRETURN 2"})
    assert text == "name: a\ndescription: d\ncypher: |-\n  MATCH 1\n  RETURN 2\n"
    assert yaml.safe_load(text)["cypher"] == "MATCH 1\nRETURN 2"
