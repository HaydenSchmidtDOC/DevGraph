"""The Config page form's YAML serialiser, checked against the real parser.

The form never sends a mapping: it writes YAML text into the editor's
textarea, and the server reads that text with `yaml.safe_load`. So the
guarantee is that the text the browser's `configEntryYaml` emits (run in node
by config_form_dump.js, straight out of index.html) parses back to exactly the
mapping, key order included, for ordinary entries and for strings built to
break a hand-rolled emitter -- both serialised directly and after a trip
through the form state. Valid fixtures must also pass the real models, and
each Add template's `entry` must be what its template text parses to.

Skipped when node isn't on PATH, like the other dashboard JS checks.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from devgraph.config.project_schema import NodeTypeDecl
from devgraph.config.project_tools import CypherTool

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_SCRIPT = Path(__file__).with_name("config_form_dump.js")


def _dump(fixtures: list[dict]) -> dict:
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        input=json.dumps({"fixtures": fixtures}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _ordered(value):
    """Mappings as (key, value) pairs, so equality also checks key order, and
    leaves paired with their type, so 3 != 3.0 and True != 1."""
    if isinstance(value, dict):
        return [(k, _ordered(v)) for k, v in value.items()]
    if isinstance(value, list):
        return [_ordered(v) for v in value]
    return (type(value), value)


CYPHER = "MATCH (f:Function {repo_id: $repo_id})\nWHERE f.calls > $min\nRETURN f.name AS name\nLIMIT $limit\n"

VALID_TOOLS = [
    {
        "name": "hot_paths",
        "description": "Functions on hot paths.",
        "cypher": CYPHER,
        "parameters": [
            {"name": "min", "type": "integer", "required": False, "default": 3},
            {"name": "limit", "type": "integer", "required": False, "default": 25, "description": "Rows."},
        ],
        "max_rows": 200,
        "timeout_s": 5,
    },
    {
        "name": "every_type",
        "description": "yes",
        "cypher": "MATCH (n {repo_id: $repo_id}) WHERE n.a = $s AND n.b = $i AND n.c = $f AND n.d = $b AND n.e = $r\n"
        "RETURN n",
        "parameters": [
            {"name": "s", "required": False, "default": "null"},
            {"name": "i", "type": "integer", "required": False, "default": -7},
            {"name": "f", "type": "float", "required": False, "default": 0.5},
            {"name": "b", "type": "boolean", "required": False, "default": False},
            {"name": "r", "type": "string", "required": True, "description": "Required, spelled out."},
        ],
    },
    # trailing spaces on a line and no final newline
    {"name": "trailing", "description": "d", "cypher": "MATCH (n {repo_id: $repo_id})   \nRETURN n  "},
    # hand-ordered keys
    {"cypher": "MATCH (n {repo_id: $repo_id}) RETURN n\n", "timeout_s": 60, "name": "reordered", "description": "d"},
]

VALID_NODE_TYPES = [
    {
        "label": "Runbook",
        "key": ["slug"],
        "description": "Ops runbooks.",
        "color": "#1f77b4",
        "metadata": [
            {"name": "slug", "type": "string", "required": True},
            {"name": "owner", "description": "Team\nname"},
        ],
    },
    {
        "label": "Doc",
        "key": ["path"],
        "source": {"provider": "filesystem", "kind": "file"},
        "metadata": [{"name": "path"}],
    },
    {
        "label": "Pair",
        "key": ["a", "b"],
        "description": None,
        "color": None,
        "source": None,
        "metadata": [{"name": "a"}, {"name": "b", "type": "integer", "required": False, "description": None}],
    },
    {"metadata": [{"name": "id", "required": True}], "key": ["id"], "label": "Reordered"},
]

HOSTILE_STRINGS = [
    "", "yes", "No", "ON", "off", "y", "n", "null", "Null", "~", "true", "False",
    "123", "-1", "0x1F", "0o17", "017", "1_000", "1e3", "1.5", ".5", ".inf", ".NaN", "1:20", "2026-10-05",
    "2026-10-05 10:00:00", "#1f77b4", "a: b", "a #b", "a:b", " leading", "trailing ", "- dash", "* star", "&anchor",
    "*alias", "!tag", "%dir", "@at", "`tick", "'quoted'", '"double"', "[x]", "{x}", ">", "|", "?", "?x", "<<", "=",
    ",", "-", "---", "...", "unicode é 漢字 🙂", "tab\there", "line\u2028sep", "para\u2029sep", "nel\x85x",
    "cr\r\nlf", "lone\rcr", "multi\nline\n", "multi\nline", "multi\n\n", "multi\n\n\n", "\nleading newline",
    "\n  indented after a newline\n", "  indented\nsecond", "\ttab first\nx", "x\n  ", "x\n  \n", "x\n\ty",
    "\n", "\n\n", " ", "ctrl\x01char", "del\x7fchar", "c1\x9bchar", "\ufeffbom", "nonchar\ufffe",
    "<img src=x onerror=alert(1)>", "x\n---\ny", "x\n...\n", 'quote"in\\back', "# comment\nx", "key: |\n  y",
    "lone\ud800\nx\n", "lo\nne\udc00",
]


def _hostile_fixtures() -> list[dict]:
    fixtures = []
    for text in HOSTILE_STRINGS:
        fixtures.append({"section": "tools", "mapping": {
            "name": text, "description": text, "cypher": text,
            "parameters": [{"name": text, "default": text, "description": text, "required": False}],
        }})
        fixtures.append({"section": "node_types", "mapping": {
            "label": text, "key": [text], "color": text, "description": text,
            "metadata": [{"name": text, "description": text}],
        }})
    return fixtures


NUMBER_FIXTURES = [
    {"section": "tools", "mapping": {"name": "n", "max_rows": 0, "timeout_s": -5}},
    {"section": "tools", "mapping": {"name": "n", "max_rows": 2**53 - 1, "timeout_s": -(2**53 - 1)}},
    {"section": "tools", "mapping": {"name": "n", "parameters": [
        {"name": "a", "type": "float", "default": 1e-7},
        {"name": "b", "type": "float", "default": 1e21},
        {"name": "c", "type": "float", "default": -1.5e-300},
        {"name": "d", "type": "float", "default": 2.5},
        {"name": "e", "type": "boolean", "default": True},
        {"name": "i", "type": "integer", "default": 3},
        {"name": "j", "type": "boolean", "default": False},
        {"name": "f", "type": "integer", "default": "12"},
        {"name": "g", "default": None, "description": None},
        {"name": "h", "type": "string", "default": 1.25},
    ]}},
    {"section": "tools", "mapping": {"name": "n", "parameters": []}},
    {"section": "node_types", "mapping": {"label": "N", "key": [], "metadata": []}},
]


def _fixtures() -> list[dict]:
    return (
        [{"section": "tools", "mapping": m} for m in VALID_TOOLS]
        + [{"section": "node_types", "mapping": m} for m in VALID_NODE_TYPES]
        + _hostile_fixtures()
        + NUMBER_FIXTURES
    )


@pytest.fixture(scope="module")
def dumped() -> dict:
    return _dump(_fixtures())


def test_serialised_yaml_parses_back_to_the_mapping(dumped):
    for fixture, result in zip(_fixtures(), dumped["results"]):
        text = result["yaml"]
        assert _ordered(yaml.safe_load(text)) == _ordered(fixture["mapping"]), text


def test_every_fixture_survives_a_trip_through_the_form(dumped):
    for fixture, result in zip(_fixtures(), dumped["results"]):
        assert result["form_yaml"] is not None, (fixture, result["reason"])
        text = result["form_yaml"]
        assert _ordered(yaml.safe_load(text)) == _ordered(fixture["mapping"]), text


def test_valid_fixtures_pass_the_real_models(dumped):
    results = dumped["results"]
    for i, mapping in enumerate(VALID_TOOLS):
        for key in ("yaml", "form_yaml"):
            CypherTool.model_validate(yaml.safe_load(results[i][key]))
    offset = len(VALID_TOOLS)
    for i, mapping in enumerate(VALID_NODE_TYPES):
        for key in ("yaml", "form_yaml"):
            NodeTypeDecl.model_validate(yaml.safe_load(results[offset + i][key]))


def test_multiline_cypher_is_a_literal_block(dumped):
    text = dumped["results"][0]["yaml"]
    assert "cypher: |\n  MATCH (f:Function {repo_id: $repo_id})\n" in text, text
    assert 'color: "#1f77b4"' in dumped["results"][len(VALID_TOOLS)]["yaml"]


def test_each_template_entry_is_its_template_parsed(dumped):
    for section, template in dumped["templates"].items():
        assert template["entry"] is not None, section
        assert _ordered(yaml.safe_load(template["template"])) == _ordered(template["entry"]), section
