"""The Config page form's field lists, enums and limits, checked against the models.

The form is written by hand (spec G2b-3 §2.9), so nothing ties it to the
models except this test: a field or enum value added to `CypherTool`,
`ToolParameter`, `NodeTypeDecl`, `MetadataField`, `FilesystemSource`,
`RelationshipDecl` or `CustomProvider` fails here
until the form models it (or such entries are made to open as YAML), and the
patterns and limits behind the form's advisory hints must be the validators'
own. The JS constants come straight out of index.html via config_form_dump.js.

Skipped when node isn't on PATH, like the other dashboard JS checks.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from devgraph.config import project_schema, project_tools
from devgraph.config.project_schema import CustomProvider, FilesystemSource, MetadataField, NodeTypeDecl, RelationshipDecl
from devgraph.config.project_tools import CypherTool, ToolParameter

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_SCRIPT = Path(__file__).with_name("config_form_dump.js")

# The form doesn't edit docs sources or docs relationships yet: their
# `provider` value is outside the form's lists, so such entries refuse into
# YAML. These exclusions go when the form models them.
_NOT_YET_IN_FORM = {"relationship": {"field"}}


@pytest.fixture(scope="module")
def dumped() -> dict:
    result = subprocess.run(
        [shutil.which("node"), str(_SCRIPT)],
        input=json.dumps({"fixtures": []}),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "form_key, model",
    [
        ("tool", CypherTool),
        ("parameter", ToolParameter),
        ("node_type", NodeTypeDecl),
        ("metadata", MetadataField),
        ("source", FilesystemSource),
        ("relationship", RelationshipDecl),
        ("custom", CustomProvider),
    ],
)
def test_form_fields_are_the_model_properties(dumped, form_key, model):
    properties = set(model.model_json_schema(by_alias=True)["properties"]) - _NOT_YET_IN_FORM.get(form_key, set())
    assert sorted(dumped["fields"][form_key]) == sorted(properties)


def test_form_enums_are_the_model_enums(dumped):
    fields = dumped["fields"]
    assert fields["parameter_types"] == list(project_tools.PARAMETER_TYPES)
    assert fields["metadata_types"] == list(project_schema.METADATA_TYPES)
    assert fields["filesystem_kinds"] == list(project_schema.FILESYSTEM_KINDS)
    assert fields["source_providers"] == [p for p in project_schema.NODE_SOURCE_PROVIDERS if p != "docs"]
    assert fields["relationship_providers"] == [p for p in project_schema.PROVIDER_KINDS if p != "docs"]


def test_form_limits_are_the_validators_limits(dumped):
    assert dumped["limits"] == {
        "NAME_PATTERN": project_tools.NAME_PATTERN.pattern,
        "LABEL_PATTERN": project_schema.LABEL_PATTERN.pattern,
        "PROPERTY_NAME_PATTERN": project_schema.PROPERTY_NAME_PATTERN.pattern,
        "RELATIONSHIP_TYPE_PATTERN": project_schema.RELATIONSHIP_TYPE_PATTERN.pattern,
        "COLOR_PATTERN": project_schema.COLOR_PATTERN,
        "MAX_DESCRIPTION_LENGTH": project_tools.MAX_DESCRIPTION_LENGTH,
        "MAX_ROWS_LIMIT": project_tools.MAX_ROWS_LIMIT,
        "MAX_TIMEOUT_S": project_tools.MAX_TIMEOUT_S,
        "DEFAULT_MAX_ROWS": project_tools.DEFAULT_MAX_ROWS,
        "DEFAULT_TIMEOUT_S": project_tools.DEFAULT_TIMEOUT_S,
    }
