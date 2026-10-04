"""Loader and validator for devgraph.tools.yaml. Pure; no Neo4j."""

import textwrap

import pytest

from devgraph.config.project_tools import (
    TOOLS_FILENAME,
    ProjectToolsError,
    load_project_tools,
    project_tools_json_schema,
    query_parameters,
    write_clauses,
)

LIST_FOLDER = """
    version: 1
    tools:
      - name: list_folder
        description: List the files directly inside a folder.
        cypher: |
          MATCH (f:File {repo_id: $repo_id})-[:IS_CHILD_OF]->(:Folder {repo_id: $repo_id, path: $folder})
          RETURN f.path AS path ORDER BY path
        parameters:
          - name: folder
            description: Repo-relative folder path.
"""


def write(tmp_path, text):
    (tmp_path / TOOLS_FILENAME).write_text(textwrap.dedent(text))
    return tmp_path


def tool_text(cypher, params="", extra=""):
    lines = ["version: 1", "tools:", "  - name: t", "    description: d", "    cypher: |"]
    lines += ["      " + line for line in textwrap.dedent(cypher).strip().splitlines()]
    if params:
        lines += ["    parameters:"] + ["      " + line for line in textwrap.dedent(params).strip().splitlines()]
    if extra:
        lines += ["    " + line for line in textwrap.dedent(extra).strip().splitlines()]
    return "\n".join(lines) + "\n"


def load_text(tmp_path, text):
    (tmp_path / TOOLS_FILENAME).write_text(text)
    return load_project_tools(tmp_path)


def test_absent_file_is_none(tmp_path):
    assert load_project_tools(tmp_path) is None


def test_example_loads_with_defaults(tmp_path):
    tools = load_project_tools(write(tmp_path, LIST_FOLDER))
    (tool,) = tools.tools
    assert tool.name == "list_folder" and tool.max_rows == 100 and tool.timeout_s == 10
    (param,) = tool.parameters
    assert (param.name, param.type, param.required, param.default) == ("folder", "string", True, None)


@pytest.mark.parametrize("text", ["", "[1, 2]\n", "version: 2\ntools: []\n", "version: 1\ntools: []\nextra: 1\n"])
def test_bad_documents_fail_closed(tmp_path, text):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, text)


def test_unknown_tool_keys_are_rejected(tmp_path):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", extra="script: x.py"))


@pytest.mark.parametrize("name", ["Upper", "1st", "has-dash", "a" * 65])
def test_tool_names_are_identifiers(tmp_path, name):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n").replace("name: t", f"name: {name}"))


def test_tool_names_are_unique(tmp_path):
    one = tool_text("MATCH (n {repo_id: $repo_id}) RETURN n")
    body = one.split("tools:\n", 1)[1]
    with pytest.raises(ProjectToolsError, match="more than once"):
        load_text(tmp_path, one + body)


@pytest.mark.parametrize("description", ["''", "'   '", "'" + "x" * 1025 + "'"])
def test_descriptions_must_be_meaningful(tmp_path, description):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n").replace("description: d", f"description: {description}"))


def test_the_query_must_use_the_injected_repo_id(tmp_path):
    with pytest.raises(ProjectToolsError, match=r"\$repo_id"):
        load_text(tmp_path, tool_text("MATCH (n) RETURN n"))


def test_repo_id_cannot_be_declared(tmp_path):
    with pytest.raises(ProjectToolsError, match="repo_id"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", params="- name: repo_id"))


def test_used_parameters_must_be_declared(tmp_path):
    with pytest.raises(ProjectToolsError, match="folder"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id, path: $folder}) RETURN n"))


def test_declared_parameters_must_be_used(tmp_path):
    with pytest.raises(ProjectToolsError, match="folder"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", params="- name: folder"))


def test_a_parameter_only_inside_a_string_is_not_used(tmp_path):
    with pytest.raises(ProjectToolsError, match="folder"):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) WHERE n.name = '$folder' RETURN n", params="- name: folder"))


@pytest.mark.parametrize(
    "clause",
    [
        "CREATE (m:X {repo_id: $repo_id})",
        "MERGE (m:X {repo_id: $repo_id})",
        "MATCH (m {repo_id: $repo_id}) SET m.x = 1",
        "MATCH (m {repo_id: $repo_id}) DELETE m",
        "MATCH (m {repo_id: $repo_id}) DETACH DELETE m",
        "MATCH (m {repo_id: $repo_id}) REMOVE m.x",
        "MATCH (m {repo_id: $repo_id}) FOREACH (x IN [1] | SET m.y = x)",
        "CALL db.labels() YIELD label MATCH (m {repo_id: $repo_id}) RETURN label",
        "LOAD CSV FROM 'file:///x' AS row MATCH (m {repo_id: $repo_id}) RETURN row",
        "USE system MATCH (m {repo_id: $repo_id}) RETURN m",
        "match (m {repo_id: $repo_id}) set m.x = 1",
    ],
)
def test_write_and_procedure_clauses_are_rejected(tmp_path, clause):
    with pytest.raises(ProjectToolsError, match="read-only"):
        load_text(tmp_path, tool_text(clause + " RETURN 1"))


def test_keywords_in_strings_comments_and_backticks_are_fine(tmp_path):
    cypher = """
        // DELETE nothing; this only reads
        MATCH (n {repo_id: $repo_id}) /* CREATE? no */
        WHERE n.name = 'SET or MERGE' AND n.note <> "DROP TABLE"
        RETURN n.`set` AS s, n.create_time AS t
    """
    assert load_text(tmp_path, tool_text(cypher)).tools[0].name == "t"


@pytest.mark.parametrize("extra", ["max_rows: 0", "max_rows: 1001", "max_rows: true", "timeout_s: 0", "timeout_s: 61", "timeout_s: true"])
def test_limits_are_bounded_and_never_booleans(tmp_path, extra):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", extra=extra))


def test_limits_within_bounds_load(tmp_path):
    tool = load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id}) RETURN n", extra="max_rows: 1000\ntimeout_s: 60")).tools[0]
    assert (tool.max_rows, tool.timeout_s) == (1000, 60)


@pytest.mark.parametrize(
    "param",
    [
        "- {name: p, type: integer, required: false, default: 'x'}",
        "- {name: p, type: integer, required: false, default: true}",
        "- {name: p, type: boolean, required: false, default: 1}",
        "- {name: p, type: string, required: false, default: 3}",
        "- {name: p, type: string, default: x}",  # required with a default
        "- {name: p, type: date}",
        "- {name: p}\n- {name: p}",
    ],
)
def test_bad_parameters_are_rejected(tmp_path, param):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id, x: $p}) RETURN n", params=param))


def test_an_integer_default_is_a_valid_float(tmp_path):
    tool = load_text(
        tmp_path, tool_text("MATCH (n {repo_id: $repo_id, x: $p}) RETURN n", params="- {name: p, type: float, required: false, default: 2}")
    ).tools[0]
    assert tool.parameters[0].default == 2


def test_insert_is_a_write_keyword(tmp_path):
    with pytest.raises(ProjectToolsError, match="read-only"):
        load_text(tmp_path, tool_text("INSERT (m:X {repo_id: $repo_id}) RETURN 1"))


def test_keyword_boundaries_digits_before(tmp_path):
    with pytest.raises(ProjectToolsError, match="read-only"):
        load_text(tmp_path, tool_text("RETURN 1CREATE (m:X {repo_id: $repo_id})"))


def test_keyword_boundaries_digits_after(tmp_path):
    with pytest.raises(ProjectToolsError, match="read-only"):
        load_text(tmp_path, tool_text("WHERE n.x = 1SET n.y = 2 MATCH (m {repo_id: $repo_id}) RETURN m"))


@pytest.mark.parametrize(
    "identifier",
    ["n.created", "dataset", "settings", "callCount", ":CALLS", "_SET"],
)
def test_keyword_lookalikes_in_identifiers_are_fine(tmp_path, identifier):
    cypher = f"MATCH (m {{repo_id: $repo_id}}) WHERE m.x = {identifier} RETURN m"
    assert load_text(tmp_path, tool_text(cypher)).tools[0].name == "t"


def test_apoc_references_are_rejected(tmp_path):
    with pytest.raises(ProjectToolsError, match="apoc"):
        load_text(tmp_path, tool_text("RETURN apoc.cypher.runFirstColumnSingle('CREATE (x)', {}) MATCH (m {repo_id: $repo_id}) RETURN m"))


def test_apoc_in_string_is_fine(tmp_path):
    cypher = "MATCH (m {repo_id: $repo_id}) WHERE m.note = 'apoc.x' RETURN m"
    assert load_text(tmp_path, tool_text(cypher)).tools[0].name == "t"


def test_apoc_case_insensitive(tmp_path):
    with pytest.raises(ProjectToolsError, match="apoc"):
        load_text(tmp_path, tool_text("RETURN APOC.x(123) MATCH (m {repo_id: $repo_id}) RETURN m"))


def test_unicode_parameters_not_satisfied_by_repo_id(tmp_path):
    with pytest.raises(ProjectToolsError, match=r"\$repo_id"):
        load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_idé}) RETURN m"))


def test_backtick_quoted_parameters_are_used(tmp_path):
    tool = load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_id, x: $`folder`}) RETURN m", params="- name: folder")).tools[0]
    assert tool.parameters[0].name == "folder"


def test_unicode_parameter_undeclared_is_rejected(tmp_path):
    with pytest.raises(ProjectToolsError, match="éx"):
        load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_id, x: $éx}) RETURN m"))


@pytest.mark.parametrize(
    "bad_name",
    ["bad name", "", "é", "1x"],
)
def test_parameter_names_must_be_identifiers(tmp_path, bad_name):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_id, x: $p}) RETURN m", params=f"- name: {bad_name!r}"))


def test_dollar_in_backtick_identifier_does_not_count_as_parameter(tmp_path):
    """Backtick-quoted identifiers can contain $ but should not be treated as parameters."""
    with pytest.raises(ProjectToolsError, match=r"\$repo_id"):
        load_text(tmp_path, tool_text("RETURN n.`$repo_id`"))


def test_dollar_in_backtick_label_does_not_count_as_parameter(tmp_path):
    with pytest.raises(ProjectToolsError, match=r"\$repo_id"):
        load_text(tmp_path, tool_text("MATCH (n:`$repo_id`) RETURN n"))


def test_dollar_in_backtick_prefix_does_not_count_as_parameter(tmp_path):
    with pytest.raises(ProjectToolsError, match=r"\$repo_id"):
        load_text(tmp_path, tool_text("RETURN `a$repo_id`"))


def test_apoc_with_space_before_dot_is_rejected(tmp_path):
    with pytest.raises(ProjectToolsError, match="apoc"):
        load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_id}) RETURN apoc .x()"))


def test_apoc_with_newline_before_dot_is_rejected(tmp_path):
    with pytest.raises(ProjectToolsError, match="apoc"):
        load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_id}) RETURN apoc\n.x()"))


def test_apoc_with_comment_before_dot_is_rejected(tmp_path):
    with pytest.raises(ProjectToolsError, match="apoc"):
        load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_id}) RETURN apoc /*comment*/ .x()"))


def test_backtick_quoted_apoc_is_rejected(tmp_path):
    with pytest.raises(ProjectToolsError, match="apoc"):
        load_text(tmp_path, tool_text("MATCH (m {repo_id: $repo_id}) RETURN `apoc`.x()"))


@pytest.mark.parametrize(
    "benign",
    ["apocalypse", "apoc_count", "n.apoc.y"],
)
def test_apoc_lookalikes_are_fine(tmp_path, benign):
    cypher = f"MATCH (m {{repo_id: $repo_id}}) RETURN {benign}"
    assert load_text(tmp_path, tool_text(cypher)).tools[0].name == "t"


def test_backtick_quoted_parameter_still_works(tmp_path):
    """$`repo_id` (backtick-quoted parameter) should still satisfy the repo_id requirement."""
    tool = load_text(tmp_path, tool_text("MATCH (m {repo_id: $`repo_id`}) RETURN m")).tools[0]
    assert tool.name == "t"


def test_helpers_ignore_literals():
    assert write_clauses("MATCH (n) WHERE n.x = 'CREATE' RETURN n") == []
    assert write_clauses("MATCH (n) DETACH DELETE n") == ["DETACH", "DELETE"]
    assert write_clauses("LOAD   csv FROM 'x' AS r RETURN r") == ["LOAD CSV"]
    assert query_parameters("RETURN $a, '$b', $repo_id // $c") == {"a", "repo_id"}


def test_json_schema_describes_the_file():
    schema = project_tools_json_schema()
    assert "tools" in schema["properties"]
    assert "ToolParameter" in schema["$defs"] and "CypherTool" in schema["$defs"]


@pytest.mark.parametrize(
    "clause",
    [
        "SHOW TRANSACTIONS YIELD * WHERE $repo_id IS NOT NULL",
        "SHOW SETTINGS WHERE $repo_id IS NOT NULL",
        "TERMINATE TRANSACTIONS 'x' WHERE $repo_id IS NOT NULL",
        "GRANT ROLE r TO u WHERE $repo_id IS NOT NULL",
        "ALTER DATABASE d SET ACCESS READ ONLY WHERE $repo_id IS NOT NULL",
        "DENY MATCH {*} ON GRAPH g TO r WHERE $repo_id IS NOT NULL",
        "REVOKE ROLE r FROM u WHERE $repo_id IS NOT NULL",
        "RENAME ROLE a TO b WHERE $repo_id IS NOT NULL",
    ],
)
def test_administration_clauses_are_rejected(tmp_path, clause):
    with pytest.raises(ProjectToolsError, match="read-only"):
        load_text(tmp_path, tool_text(clause + " RETURN 1"))


def test_keywords_after_dot_or_dollar_are_fine(tmp_path):
    cypher = "MATCH (a {repo_id: $repo_id}) RETURN a.set, a {.set} AS m, $create AS c"
    tool = load_text(tmp_path, tool_text(cypher, params="- {name: create}")).tools[0]
    assert tool.parameters[0].name == "create"


def test_set_clause_and_digit_prefixed_set_still_rejected(tmp_path):
    for cypher in ("MATCH (n {repo_id: $repo_id}) SET n.x = 1", "RETURN 1SET n.x = 1 MATCH (m {repo_id: $repo_id})"):
        with pytest.raises(ProjectToolsError, match="read-only"):
            load_text(tmp_path, tool_text(cypher))


@pytest.mark.parametrize("text", ["version: true\ntools: []\n", "version: 1.0\ntools: []\n", 'version: "1"\ntools: []\n'])
def test_version_must_be_the_integer_one(tmp_path, text):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, text)


@pytest.mark.parametrize("required", ['"yes"', "1", '"true"'])
def test_required_must_be_a_boolean(tmp_path, required):
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id, x: $p}) RETURN n", params=f"- {{name: p, required: {required}}}"))


def test_parameter_description_is_capped(tmp_path):
    long = "x" * 1025
    with pytest.raises(ProjectToolsError):
        load_text(tmp_path, tool_text("MATCH (n {repo_id: $repo_id, x: $p}) RETURN n", params=f"- {{name: p, description: {long}}}"))


def test_catalog_module_does_not_import_mcp():
    import subprocess
    import sys

    code = "import sys, devgraph.mcp.catalog as c; assert 'mcp' not in sys.modules; assert 'search_component' in c.builtin_tool_names()"
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("name, why", [("from", "Python keyword"), ("in", "Python keyword"), ("model_config", "model_")])
def test_reserved_parameter_names_are_rejected(tmp_path, name, why):
    text = tool_text(
        f"MATCH (n {{repo_id: $repo_id}}) RETURN ${name}",
        params=f"- name: {name}\n  description: x",
    )
    with pytest.raises(ProjectToolsError) as err:
        load_text(tmp_path, text)
    assert name in str(err.value) and why in str(err.value)
