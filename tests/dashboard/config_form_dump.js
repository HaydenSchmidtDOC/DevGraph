/* Runs the Config page's pure form functions, lifted verbatim out of
   index.html, for the Python round-trip and drift tests
   (test_config_form_roundtrip.py, test_config_form_drift.py).

   stdin: {"fixtures": [{"section": "tools" | "node_types", "mapping": {...}}]}
   stdout: {"fields", "limits", "templates": {section: {template, entry}},
            "results": [{"yaml", "form_yaml", "reason"}]}
   `yaml` serialises the mapping directly; `form_yaml` takes it through the
   form first (mapping -> form state -> mapping -> YAML), or is null with the
   refusal `reason` when the form can't show the mapping. */
const fs = require("fs");
const path = require("path");

const html = fs.readFileSync(
  path.join(__dirname, "..", "..", "devgraph", "dashboard", "static", "index.html"), "utf8");
const start = html.search(/^\/\* ── Config page/m);
const end = html.indexOf("/* ── end Config page ── */", start);
if (start < 0 || end < 0) throw new Error("could not find the Config page block");
const api = new Function("document",
  html.slice(start, end) + "\nreturn { CONFIG_SECTIONS, CONFIG_FORM_FIELDS, CONFIG_FORM_LIMITS," +
  " configFormFromEntry, configEntryFromForm, configEntryYaml };")({});

const { fixtures } = JSON.parse(fs.readFileSync(0, "utf8"));
const results = fixtures.map(({ section, mapping }) => {
  const order = Object.keys(mapping);
  const rep = api.configFormFromEntry(section, mapping);
  return {
    yaml: api.configEntryYaml(section, mapping, order),
    form_yaml: rep.ok ? api.configEntryYaml(section, api.configEntryFromForm(section, rep.form, order), order) : null,
    reason: rep.ok ? null : rep.reason,
  };
});
const templates = Object.fromEntries(["tools", "node_types"].map(s =>
  [s, { template: api.CONFIG_SECTIONS[s].template, entry: api.CONFIG_SECTIONS[s].entry }]));
process.stdout.write(JSON.stringify({
  fields: api.CONFIG_FORM_FIELDS, limits: api.CONFIG_FORM_LIMITS, templates, results,
}));
