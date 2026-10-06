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

from devgraph.config.project_schema import NodeTypeDecl, RelationshipDecl
from devgraph.config.project_tools import CypherTool

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_SCRIPT = Path(__file__).with_name("config_form_dump.js")


def _dump(fixtures: list[dict]) -> dict:
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        input=json.dumps({"fixtures": fixtures}),
        capture_output=True,
        text=True,
        encoding="utf-8",
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
        "description": "Ops\nrunbooks.",
        "color": "#1f77b4",
        "metadata": [
            {"name": "slug", "type": "string", "required": True},
            {"name": "owner", "description": "Team name"},
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
    # Markdown front matter: conditions in any key order, integer and boolean
    # values kept as written, and front-matter keys in metadata order
    {
        "label": "Runbook",
        "key": ["path"],
        "metadata": [
            {"name": "path"},
            {"name": "owner", "required": True},
            {"name": "severity", "type": "integer"},
            {"name": "on_call"},
        ],
        "source": {
            "provider": "docs",
            "paths": ["runbooks/**/*.md", "ops/*.markdown"],
            "where": [
                {"field": "type", "is": "runbook"},
                {"starts_with": "RB-", "field": "title"},
                {"field": "version", "is": 1},
                {"field": "draft", "is": False},
                {"field": "tags", "contains": "oncall"},
                {"field": "slug", "like": "db-*"},
            ],
            "fields": {"severity": "sev", "on_call": "on-call-team"},
        },
    },
    # keyed on a front-matter field: the key row writes no `required` (the form only shows it as required)
    {
        "label": "Decision",
        "key": ["adr_id"],
        "metadata": [{"name": "path"}, {"name": "adr_id"}, {"name": "title"}],
        "source": {"provider": "docs", "paths": ["decisions/**/*.md"], "fields": {"adr_id": "id"}},
    },
    # no where or fields
    {"label": "Adr", "key": ["path"], "source": {"provider": "docs", "paths": ["docs/adr/*.md"]}, "metadata": [{"name": "path"}]},
    # empty where and fields, keys reordered, and a field named like a YAML 1.1 boolean
    {
        "label": "Note",
        "key": ["path"],
        "source": {"paths": ["**/*.md"], "provider": "docs", "fields": {"on": "yes"}, "where": []},
        "metadata": [{"name": "path"}, {"name": "on", "type": "boolean"}],
    },
]

VALID_RELATIONSHIPS = [
    {"type": "CALLS", "from": "Function", "to": "Function"},
    {
        "type": "DOCUMENTS",
        "from": "Runbook",
        "to": "Service",
        "provider": "custom",
        "custom": {"name": "runbook_links", "params": {}},
        "color": "#1f77b4",
    },
    {"type": "IS_CHILD_OF", "from": ["File", "Folder"], "to": "Folder", "provider": "filesystem"},
    {"type": "OWNS", "from": ["Team"], "to": "Service", "provider": "custom", "custom": {"name": "owners"}},
    {"to": "Function", "provider": "builtin", "from": ["Function", "Service"], "type": "USES", "custom": None, "color": None},
    {"type": "RUNBOOK_FOR", "provider": "docs", "from": "Runbook", "to": "Service", "field": "service"},
    {"field": "see also", "type": "SEE_ALSO", "from": ["Runbook"], "to": "Runbook", "provider": "docs", "color": "#1f77b4"},
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
        # only the textarea-backed fields, so line breaks still go through the form
        fixtures.append({"section": "tools", "mapping": {"name": "t", "description": text, "cypher": text}})
        fixtures.append({"section": "node_types", "mapping": {"label": "N", "description": text}})
        fixtures.append({"section": "relationships", "mapping": {
            "type": text, "from": text, "to": text, "provider": "custom", "custom": {"name": text}, "color": text,
        }})
        fixtures.append({"section": "relationships", "mapping": {"type": "T", "from": ["A", text], "to": "B"}})
        fixtures.append({"section": "relationships", "mapping": {
            "type": "T", "from": "A", "to": "B", "provider": "custom", "custom": {"name": text, "params": {}}, "color": text,
        }})
        fixtures.append({"section": "relationships", "mapping": {"type": "T", "from": text, "to": "B"}})
        fixtures.append({"section": "relationships", "mapping": {"type": "T", "from": "A", "to": "B", "provider": "docs", "field": text}})
        fixtures.append({"section": "node_types", "mapping": {
            "label": "N", "key": ["path"],
            "source": {"provider": "docs", "paths": [text], "where": [{"field": text, "contains": text}], "fields": {"owner": text}},
            "metadata": [{"name": "path"}, {"name": "owner"}],
        }})
        # the text as a front-matter key's metadata name: keys are quoted like values
        fixtures.append({"section": "node_types", "mapping": {
            "label": "N", "metadata": [{"name": text}], "source": {"provider": "docs", "paths": ["*.md"], "fields": {text: "fm"}},
        }})
    return fixtures


# A single-line input can't hold these, and a textarea turns CR into LF: the
# form refuses such entries (they open in YAML) rather than rewrite them.
_LINE_BREAKS = "\n\r\x85\u2028\u2029"
_SINGLE_LINE = {
    "tools": (["name"], "parameters", ["name", "default", "description"]),
    "node_types": (["label", "color"], "metadata", ["name", "description"]),
}
_TEXTAREA = {"tools": ["description", "cypher"], "node_types": ["description"]}
# what JavaScript's String.prototype.trim() strips (not Python's str.strip())
_JS_SPACE = "\t\n\v\f\r \xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_FIELD = "This entry has a field the form doesn't edit: `{}`. Edit it as YAML."
_FROM_LIST = (
    "This entry's `from` can't be shown as comma-separated labels exactly (a label with a comma, "
    "surrounding spaces or a non-text value). Edit it as YAML."
)


def _broken(value) -> bool:
    return isinstance(value, str) and any(c in value for c in _LINE_BREAKS)


def _from_refused(value) -> bool:
    """True unless `from` survives the form's split on commas and trim."""
    labels = [value] if isinstance(value, str) else value
    if not isinstance(labels, list) or not labels:
        return True
    return any(
        not isinstance(label, str) or _broken(label) or "," in label or label != label.strip(_JS_SPACE)
        or (label == "" and not isinstance(value, str))
        for label in labels
    )


def _relationship_refusal(mapping: dict) -> str | None:
    """The reason the form gives for the first field (in key order) it can't show."""
    for key, value in mapping.items():
        if key == "from" and _from_refused(value):
            return _FROM_LIST
        if key == "custom" and isinstance(value, dict) and _broken(value.get("name")):
            return _FIELD.format("custom.name")
        if key in ("type", "to", "color", "field") and _broken(value):
            return _FIELD.format(key)
    return None


def _form_refuses(fixture: dict) -> str | None:
    """The reason the form refuses the fixture with, or None when it shows it."""
    mapping = fixture["mapping"]
    if fixture["section"] == "relationships":
        return _relationship_refusal(mapping)
    top, rows, row_fields = _SINGLE_LINE[fixture["section"]]
    single = [mapping.get(k) for k in top] + [r.get(k) for r in mapping.get(rows) or [] for k in row_fields]
    source = mapping.get("source") or {}
    if source.get("provider") == "docs":
        fields = source.get("fields", {})
        single += list(source["paths"]) + [v for c in source.get("where", []) for v in c.values()] + list(fields.values())
        if "" in fields.values():  # a blank front-matter key reads as "same as the field name"
            return _FIELD.format("")
    area = [mapping.get(k) for k in _TEXTAREA[fixture["section"]]]
    if any(_broken(v) for v in single) or any(isinstance(v, str) and "\r" in v for v in area):
        # which field comes first depends on the walk; the reason is the field one
        return _FIELD.format("")
    return None


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
    # condition values compare as text, but an untouched number or boolean comes back as written
    {"section": "node_types", "mapping": {"label": "N", "key": ["path"], "metadata": [{"name": "path"}], "source": {
        "provider": "docs", "paths": ["*.md"],
        "where": [{"field": "v", "is": 1}, {"field": "d", "like": True}, {"field": "n", "contains": -(2**53 - 1)}],
    }}},
]


def _fixtures() -> list[dict]:
    return (
        [{"section": "tools", "mapping": m} for m in VALID_TOOLS]
        + [{"section": "node_types", "mapping": m} for m in VALID_NODE_TYPES]
        + [{"section": "relationships", "mapping": m} for m in VALID_RELATIONSHIPS]
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
    refused = set()
    for fixture, result in zip(_fixtures(), dumped["results"]):
        reason = _form_refuses(fixture)
        if reason:
            refused.add(reason)
            assert result["form_yaml"] is None, fixture
            if fixture["section"] == "relationships":
                assert result["reason"] == reason, (fixture, result["reason"])
            else:
                assert result["reason"].startswith(reason[: reason.index("`") + 1]), result["reason"]
            continue
        assert result["form_yaml"] is not None, (fixture, result["reason"])
        text = result["form_yaml"]
        assert _ordered(yaml.safe_load(text)) == _ordered(fixture["mapping"]), text
    for reason in (_FROM_LIST, _FIELD.format("type"), _FIELD.format("custom.name")):
        assert reason in refused, f"no fixture exercised the refusal {reason!r}"


def test_valid_fixtures_pass_the_real_models(dumped):
    results = dumped["results"]
    for i, mapping in enumerate(VALID_TOOLS):
        for key in ("yaml", "form_yaml"):
            CypherTool.model_validate(yaml.safe_load(results[i][key]))
    offset = len(VALID_TOOLS)
    for i, mapping in enumerate(VALID_NODE_TYPES):
        for key in ("yaml", "form_yaml"):
            NodeTypeDecl.model_validate(yaml.safe_load(results[offset + i][key]))
    offset += len(VALID_NODE_TYPES)
    for i, mapping in enumerate(VALID_RELATIONSHIPS):
        RelationshipDecl.model_validate(mapping)
        for key in ("yaml", "form_yaml"):
            RelationshipDecl.model_validate(yaml.safe_load(results[offset + i][key]))


def test_multiline_cypher_is_a_literal_block(dumped):
    text = dumped["results"][0]["yaml"]
    assert "cypher: |\n  MATCH (f:Function {repo_id: $repo_id})\n" in text, text
    assert 'color: "#1f77b4"' in dumped["results"][len(VALID_TOOLS)]["yaml"]


def test_each_template_entry_is_its_template_parsed(dumped):
    assert sorted(dumped["templates"]) == ["node_types", "relationships", "tools"]
    for section, template in dumped["templates"].items():
        assert template["entry"] is not None, section
        assert _ordered(yaml.safe_load(template["template"])) == _ordered(template["entry"]), section
