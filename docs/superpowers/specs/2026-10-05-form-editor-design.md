# Dashboard Config page form editor (G2b-3) — design

Stacked on #44 (G2b-2, `epic1/g2b2-config-copy`).
Epic refs: §10 (one Config page; edit entries in place).
Builds on `docs/superpowers/specs/2026-10-05-config-page-design.md` (the G2a spec: §1 deferred the "structured form editor", §3 Q1 chose YAML first so that validation, messages and docs stay shared with the CLI), `docs/superpowers/specs/2026-10-05-config-extras-design.md` (G2b-1) and `docs/superpowers/specs/2026-10-05-config-copy-design.md` (G2b-2). The G2a §2.2 API envelope and error table, §2.3 write-safety rules and the dry-run / confirm / arming rules of all three apply unchanged.
Line numbers were verified on the tip of #44.

---

## 1. Scope

### PR G2b-3 (this PR): "A form view for tools and node types in the Config editor, serialising to the same YAML"

In:
1. A **Form | YAML** switch in the Config editor (`#configModal`, index.html:779–787) for **tools** (global and project) and **project node types**, on the add and edit ops (including a global tool's "Save to" a repository).
2. The form is a **view over the YAML textarea**: every form input re-serialises the entry into `#configYaml`, and Save reads the textarea exactly as today. No new write route, no new request shape, no change to the dry run, the confirmation, `edit.dryYaml` (the "confirm covers only the text it checked" binding), the arming delay, or the read-only-while-busy rule.
3. The page model carries each tool's and node type's parsed mapping (`entry`) next to its `yaml`, so the browser never parses YAML (§2.2).
4. Pure, harness-tested functions: mapping → form state, form state → mapping, mapping → YAML text, advisory hints; a Python round-trip test that feeds the serialiser's output back through `yaml.safe_load` and the real validators; a drift guard that checks the form's field lists against the models' JSON Schema.

Deferred (recorded so they are not mistaken for gaps):
- **Relationships** keep the YAML editor. Their form needs `custom.params` (a free mapping of scalars) and the `provider`/`custom` coupling; the decisions for it are recorded in §3 Q11 so the follow-up starts from them.
- **Reading hand-edited YAML back into the form** (§2.4, Q3). It needs a YAML parser in the browser or a parse route; neither is in this PR.
- Reordering parameter / metadata rows (add and remove only).
- Client-side mirrors of the server's deeper checks (read-only Cypher keywords, `$name` usage vs declared parameters, generated constraint names, cross-repository conflicts). The server's dry run already reports them with the CLI's text.

---

## 2. Design

### 2.1 Principle: YAML stays the source of truth

The editor already has one text binding that every safety rule hangs off: `configSave` (index.html:3293) reads `#configYaml.value`, records it as `edit.dryYaml` for the dry run, refuses to write when the textarea changed while checking (index.html:3337), and drops a confirmation when the text differs from what was checked (index.html:3310). The form plugs in **in front of** that binding, not beside it:

- In form mode the textarea is hidden (`display: none`), not removed; each form `input`/`change` event runs `configFormChanged()`, which builds the mapping from the form state, serialises it (`configEntryYaml`), assigns it to `#configYaml.value`, and then runs the same reset the textarea's own `oninput` runs today (index.html:3279: clear `crossName`; drop `confirmed` and re-render). The textarea's handler is factored into `configTextChanged()` so both paths share it.
- Save, Reload, the 412 flow, the destination dropdown and the warning step are untouched. A form edit therefore produces exactly the request the same text typed into the textarea would, goes through the same dry run, and invalidates a confirmation exactly as typing does. A form edit can never reach the server without the review a YAML edit gets.
- Opening an entry does **not** re-serialise it: the textarea keeps the page model's `yaml` (the server's `dump_entry` output) until the first form change. Opening and saving without touching anything sends the same bytes as today.
- While `edit.busy` (a dry run or a write in flight), the whole form is disabled: the form lives in a `<fieldset id="configForm">` and `renderConfigModal` sets `fieldset.disabled = !!edit.busy`, next to the existing `readOnly` line (index.html:3225). The mode switch is disabled too.

### 2.2 Where the form's data comes from (no YAML parsing in the browser)

The browser has no YAML parser (index.html loads only cytoscape; there is no build step), and the G2a rule is to keep it that way. The server already has each entry as a mapping before it dumps it (`config_model.py`: `_global_entry` line 117, `_project_tool_entry` line 173, node types line 258). Each of those rows gains:

```python
"entry": form_entry(entry)   # the mapping, or None when JSON can't carry it faithfully
```

`form_entry(entry) -> dict | None` (new, `devgraph/dashboard/config_model.py`) returns the mapping only when it is plain JSON data that survives a trip into JavaScript unchanged: keys are strings; values are `str`, `bool`, `None`, `int` within ±(2^53 − 1), finite non-integral `float`, or lists/dicts of those. Anything else (a YAML date, `.nan`, a huge integer, a non-string key) gives `None`, and the editor opens that entry in YAML only. Relationship rows get no `entry` in this PR.

For **Add**, `CONFIG_SECTIONS.tools` and `.node_types` (index.html:2920) gain an `entry` mapping equal to `yaml.safe_load` of their existing `template` text (a Python test asserts the equality, §4). The relationships template is unchanged.

### 2.3 Which entries the form can show

`configFormFromEntry(section, entry) -> {ok: true, form} | {ok: false, reason}` decides. An entry the form cannot represent exactly opens in YAML with a notice, and the Form button is disabled with the reason as its description. The form never drops, rewrites or "fixes" a field it does not model; that is the whole round-trip guarantee (Q4).

**Tools** (`CypherTool`, devgraph/config/project_tools.py): representable when the keys are a subset of `name, description, cypher, parameters, max_rows, timeout_s`; `name`/`description`/`cypher` are strings when present; `max_rows`/`timeout_s` are integers (not booleans) when present; `parameters` is absent or a list of mappings whose keys are a subset of `name, type, required, default, description`, with `type` one of `PARAMETER_TYPES` when present, `required` a boolean when present, `default` a scalar or null, `description` a string or null.

**Node types** (`NodeTypeDecl`, devgraph/config/project_schema.py:173): keys a subset of `label, key, metadata, description, source, color`; `label`/`description`/`color` strings (or null for the optional two); `metadata` a list of mappings with keys a subset of `name, type, required, description`, `type` one of `METADATA_TYPES`, names unique; `key` a list of strings, each naming a metadata row, **in the same order as those rows appear in `metadata`** (the form expresses the key as ticks on metadata rows, Q7); `source` absent, null, or exactly `{provider: filesystem, kind: file|folder}`.

Values out of range (a `max_rows` of 5000, a bad label) are representable: the form shows them and the server's validator rejects them. Representability is about not losing data, not about validity.

An integral float (e.g. `3.0`) opens in YAML only, because the browser can't tell it from an integer. Anchors, aliases and merge keys are written out expanded when the form saves. A self-referencing anchor gives `entry: null` (YAML only).

Reason texts (shown as the notice; all `textContent`): "This entry has a field the form doesn't edit: `<path>`. Edit it as YAML." / "This entry's key order differs from its metadata order; the form can't show that. Edit it as YAML." / "This entry contains a value the page can't carry exactly (for example a date or a very large number). Edit it as YAML."

### 2.4 Switching between Form and YAML

- **Form → YAML**: always allowed. The textarea already holds the form's serialisation (or the untouched opening text). The editor remembers `edit.formText` (the last text the form produced, or the opening text) and `edit.formState`.
- **YAML → Form**: allowed only when the textarea still holds text whose mapping is known: `edit.formText` (restore `edit.formState`) or the opening text (rebuild from the opening `entry`). Otherwise the Form button is disabled and the panel says: "The YAML was edited by hand, and the form can only show text it produced. Keep editing as YAML, or discard the hand edits to return to the form." with a **Discard YAML edits** button that puts `edit.formText` back into the textarea (through `configTextChanged()`, so a pending confirmation is dropped) and switches to the form.
- **Confirmation across a switch**: a plain Form ↔ YAML switch with unchanged text keeps an existing warning confirmation. That is safe: the text is the same, and Save re-checks the text against the dry run's text before it writes. A hand edit of the textarea, or **Discard YAML edits**, goes through `configTextChanged()` and drops the confirmation.
- The opening mode is Form when the section has a form, the op is add or replace, and the entry is representable; otherwise YAML. The last mode the user picked is remembered for the page session (a module variable, not storage) and wins when it is available.
- Delete and Copy keep the read-only YAML view (G2b-2: a copy is the entry as shown). The global warning step (`edit.step === "warn"`) shows no editor at all, as today.

### 2.5 Serialisation: `configEntryYaml(section, mapping, order)`

A small block-style emitter, deliberately not a general YAML library:

- **Key order**: keys the opened entry had, in its order (`order`, from `Object.keys(entry)`), then newly set keys in model order (tools: `name, description, cypher, parameters, max_rows, timeout_s`; node types: `label, key, description, color, source, metadata`; parameters: `name, type, required, default, description`; metadata: `name, type, required, description`). Editing one field of a hand-ordered entry changes one line, not the layout.
- **Omission**: an empty optional field is omitted (an empty `description`, `color`, `default`, `max_rows`, `timeout_s`; an empty `parameters`/`metadata` list unless the entry had the key). A field equal to the model default is omitted unless the opened entry had it (parameter `type: string`, `required: true` for parameters, `required: false` for metadata), so the default is never silently written into an entry that did not spell it out, nor silently removed from one that did.
- **Strings**: plain only when they match `^[A-Za-z_][A-Za-z0-9_]*$` and are not a YAML 1.1 bool/null word (`y n yes no on off true false null`, any case). A string containing a line break is a literal block (`|` when it ends in exactly one `\n`, `|-` when it ends in none) provided its first line does not start with a space and it contains none of the line breaks `dump_entry` itself keeps out of blocks (`\r`, `\x85`, `\u2028`, `\u2029`; `_OTHER_LINE_BREAKS`, list_edit.py:37–48); anything else (including text ending in more than one line break) is a double-quoted scalar written with `JSON.stringify` (a JSON string literal is a valid YAML double-quoted scalar; PyYAML understands every escape `JSON.stringify` emits). So `cypher` reads as a `|` block like the server's dump, and `color: "#1f77b4"` is quoted.
- **Numbers / booleans / null**: integers as digits, finite floats with `String(n)`, `true`/`false`. `null` is never emitted (an empty field is omitted).
- **Lists**: `key: [a, b]` flow form only for `key` (all plain identifiers, matching the template's `key: [slug]`); `parameters` and `metadata` as block sequences of mappings (`- name: …` with two-space continuation).

The emitter only ever sees mappings built by `configEntryFromForm`, whose shapes are fixed, so it needs no general cases. The Python round-trip test (§4) is the guarantee: for every fixture, `yaml.safe_load(configEntryYaml(m)) == m`.

### 2.6 Form → mapping: `configEntryFromForm(section, form, order)`

Builds the mapping from the form state. Typed fields:
- `max_rows`, `timeout_s`, and integer defaults: text matching `^-?\d+$` becomes an integer; float defaults: a finite `Number(text)`; anything else is **kept as the string the user typed**, so the server's validator reports it (`… is not a integer`) rather than the form silently dropping it.
- Boolean parameter default: a select (`(none)`, `true`, `false`).
- `required` checkboxes become booleans.
- Node type `key`: the names of the metadata rows whose Key box is ticked, in row order.
- `source`: select `None` / `Filesystem — file` / `Filesystem — folder` → absent or `{provider: "filesystem", kind}`.

### 2.7 The form, field by field

**Tool**
| Field | Control | Notes |
|---|---|---|
| Name | text input | hint: pattern `[a-z][a-z0-9_]{0,63}`; a changed name on Edit is a rename, exactly as editing the YAML `name` is |
| Description | textarea (3 rows) | character count against 1024 |
| Cypher | textarea, monospace (`.cfg-yaml` style), `spellcheck=false`, 10 rows, resizable | Tab is **not** captured (keyboard users must be able to leave the field); help text: "Must filter on `$repo_id`; DevGraph supplies it. Read-only clauses only." |
| Parameters | one fieldset per parameter: Name, Type (select string/integer/float/boolean), Required (checkbox, default ticked), Default (input by type; disabled while Required is ticked, with help "A default makes the parameter optional"), Description; **Remove** per row; **Add parameter** after the list | the legend reads "Parameter 2: limit" |
| Max rows | text input (`inputmode=numeric`), kept as typed, placeholder "100 (default)" | empty = omitted |
| Timeout (s) | text input (`inputmode=numeric`), kept as typed, placeholder "10 (default)" | empty = omitted |

**Node type**
| Field | Control | Notes |
|---|---|---|
| Label | text input | hint: label pattern; built-in labels are refused by the server |
| Description | textarea (2 rows) | |
| Colour | text input `#rrggbb` plus a native `<input type="color">` swatch that writes into it, and a **Clear** button | the text is the value (a native colour input cannot be empty); the swatch follows the text when it is a valid colour |
| Source | select: None (declared only) / Filesystem — file / Filesystem — folder | hint when a filesystem source is chosen and the key is not exactly a string `path` row |
| Metadata | one fieldset per field: Name, Type (select), Required (checkbox, default unticked), **Key** (checkbox), Description; Remove; **Add field** | key order = row order of ticked rows |

### 2.8 Advisory hints: `configFormHints(section, form) -> [{field, text}]`

Shown under the field they concern and never blocking: Save stays enabled and the server decides. Only cheap structural checks that cannot disagree with the server in a confusing way:
- tools: name empty or not matching the pattern; description blank or over 1024 characters; cypher blank; cypher with no `$repo_id` (text search, worded "doesn't appear to filter on `$repo_id`"); duplicate parameter names; parameter name empty; a default on a required parameter; a default that does not read as its type; `max_rows`/`timeout_s` not an integer in range.
- node types: label empty or not matching the label pattern; no key ticked; a key or metadata name not matching the property-name pattern; duplicate metadata names; colour not `#rrggbb`; filesystem source without a key of exactly one string `path` row.

The patterns and limits are JS constants (`CONFIG_FORM_LIMITS`) checked against the Python constants by a test (§4), so a hint cannot drift from the validator it imitates.

### 2.9 Does a JSON Schema drive the form?

No. `project_tools_json_schema()` (project_tools.py:319) and `project_schema_json_schema()` (project_schema.py:672) exist, but a generic schema-driven form would render `cypher` as a one-line string, could not express "key = ticked metadata rows" or "default disabled while required", and both functions are documented as descriptive only (the real rules live in validators). Writing the form by hand for two entry kinds is less code than a generic renderer and reads better.

The schemas are used as a **drift guard** instead: a Python test loads the models' JSON Schema and asserts that the form's field lists (`CONFIG_FORM_FIELDS`, extracted from index.html) equal the `properties` of `CypherTool`, `ToolParameter`, `NodeTypeDecl`, `MetadataField` and `NodeSource`, and that the enum lists equal `PARAMETER_TYPES`, `METADATA_TYPES` and `FILESYSTEM_KINDS`. A field added to a model fails the test until the form either models it or is explicitly listed as YAML-only (which then makes such entries open in YAML).

### 2.10 Accessibility and escaping

- The switch is a `role="group"` labelled "Editor" with two buttons carrying `aria-pressed`; a disabled Form button has `aria-disabled="true"` and `aria-describedby` pointing at the reason text, and stays focusable so the reason can be read.
- Every control has a visible `<label for>`; repeated rows put the row's position and name in the fieldset `<legend>` ("Parameter 2: limit") and in the Remove button's accessible name ("Remove parameter limit"); ids come from a per-editor counter, never from entry data.
- Hints are linked with `aria-describedby`; no `aria-invalid` (they are advisory). After **Add parameter/field** focus moves to the new row's Name; after **Remove** it moves to the next row's Name, or the Add button.
- There is no `<form>` element, so Enter in a text input never submits; Save stays the explicit button. Escape keeps closing the modal (existing `configModalKey`).
- The arming focus move (index.html:3214–3220: focus leaves a button whose meaning changed) targets the textarea in YAML mode and the form's first enabled control in form mode (`configEditorFocusTarget()`).
- Every value is assigned through `.value`, `textContent` or `option.value`; no entry data reaches `innerHTML`. Select options come from constants.

### 2.11 Code organisation (index.html, inside the Config page block)

Pure (the harness grabs them, as with every Config function): `CONFIG_FORM_FIELDS`, `CONFIG_FORM_LIMITS`, `configFormFromEntry`, `configEntryFromForm`, `configEntryYaml`, `configYamlScalar`, `configFormHints`, `configFormSwitch(edit, text) -> {available, restore: "state"|"open"|null, reason}`.
DOM: `renderConfigForm(edit)` (builds the fieldset from `edit.formState`), `configFormChanged()`, `configTextChanged()`, `configSetEditorMode(mode)`, `configEditorFocusTarget()`. `openConfigEditor` gains `entry` in its options (from `item.entry`, or the section template's `entry` on Add) and sets `edit.mode`, `edit.formState`, `edit.formText`, `edit.openText`, `edit.openEntry`, `edit.order`. Markup: the switch and an empty `<fieldset id="configForm" class="cfg-form">` next to `#configYaml`; CSS for `.cfg-form` rows reusing the existing `.field` styles. The modal box keeps its width; the form scrolls inside it (`max-height` on the fieldset).

---

## 3. Design questions — recommended answers

1. **Which entry kinds first?** Tools (global and project) and node types. Tools are the most edited and the most error-prone in YAML (multi-line Cypher, the parameter list); node types are next (the key/metadata coupling is easier with ticks). Relationships stay YAML (Q11).
2. **Where does the form's data come from?** An `entry` mapping in the page model, produced by the server from the mapping it already dumps (§2.2). No browser YAML parser, no new route.
3. **Can hand-edited YAML go back to the form?** Not in this PR. YAML → Form is allowed only for text whose mapping is known (the opening text or the form's own last output); otherwise the user keeps editing YAML or discards the hand edits (§2.4). Supporting it needs either a vendored YAML parser (a new static file and a second YAML dialect next to PyYAML) or a parse-only route; both are larger than the rest of this PR and can follow if people miss it.
4. **Unknown fields: preserve or refuse?** Refuse to show them in the form: the entry opens in YAML with a notice naming the field. All models are `extra="forbid"`, so such a field is invalid anyway, and carrying keys the form does not show would mean a form save silently writes things the user cannot see.
5. **Opening mode?** Form for representable tool / node-type entries on add and edit; YAML otherwise; the user's last choice wins for the page session.
6. **Parameter list editing?** One fieldset per parameter, Add/Remove, no reordering (order is cosmetic for tools). Default input typed by the parameter's type and disabled while Required is ticked, matching the validator ("a default … must be required: false").
7. **How is a node type's key entered?** A Key checkbox on each metadata row; key order is row order. It makes "key component must be declared metadata" impossible to get wrong; an entry whose key order differs from its metadata order opens in YAML (§2.3).
8. **Cypher field?** A monospace, resizable textarea in the `.cfg-yaml` style, spellcheck off, Tab not captured. No syntax highlighting.
9. **Colour: picker or text?** Text `#rrggbb` as the value, with a native colour swatch that fills it and a Clear button (a native colour input cannot express "no colour").
10. **Client-side validation?** Advisory hints only (§2.8), never disabling Save; patterns and limits checked against the Python constants by a test. The server's dry run is authoritative and its 422 text is shown as today.
11. **Relationships (deferred) — decisions for the follow-up:** `from` as a text field of comma-separated labels, serialised as a string for one label and a list for several (preserving which form the entry used); `to` a text field; `provider` a select (builtin / custom / filesystem) that shows the custom name field only for `custom`; `color` as for node types; an entry with non-empty `custom.params` opens in YAML until a key/value row editor exists.
12. **Does a JSON Schema drive the form?** No; it guards the form's field lists against model drift (§2.9).
13. **What does Save send from form mode?** The textarea's text, which the form keeps current: the same request, dry run, confirmation and text binding as YAML mode (§2.1).

---

## 4. Test strategy

**`tests/dashboard/test_config_routes.py`** (where `config_model` is tested today): `GET /api/config` rows for global tools, project tools and node types carry `entry` equal to the file's mapping; relationship rows have none; an entry with a YAML date, `.nan` or a 2^60 integer has `entry: null`; `form_entry` unit cases (plain data kept; each non-plain kind gives `None`).

**`tests/dashboard/config_page_ui.js`** (pure functions):
- `configFormFromEntry`: representable tool (with parameters of each type, defaults, max_rows/timeout_s) and node type (metadata, key ticks, each source); refusals with the right reason for an unknown key, an unknown parameter key, `key` order differing from metadata order, a key naming no metadata row, a `source` with an extra key, a string `key`.
- `configEntryFromForm` ∘ `configFormFromEntry` is the identity on representable fixtures; integer/float text becomes numbers, other text stays a string; empty optionals omitted; defaults kept when the opened entry had them and not added when it did not; key order follows the opened entry, new keys in model order.
- `configEntryYaml`: plain vs quoted (`yes`, `null`, `#1f77b4`, `1e3`, `a: b`, leading space), `|` vs `|-` for Cypher, double-quoting for `\r`/`\u2028`, `key: [path]`.
- `configFormHints`: each hint, and none for a valid entry.
- `configFormSwitch`: available for the opening text and for the form's last output; unavailable with the hand-edit reason after a textarea edit; Discard restores the form text.

**`tests/dashboard/config_page_ui.js`** (DOM flow, stub DOM extended with `fieldset`/`input`/`label` and `aria-*` attributes):
- opens in form mode for a tool, YAML mode with the notice for an unrepresentable entry and for relationships, YAML read-only for delete/copy;
- a form edit writes the serialised text into `#configYaml`, clears `crossName`, and drops a pending confirmation exactly as a textarea edit does;
- Save from form mode sends the textarea's text in the dry run and the write (same URL, method, `If-Match`, body as YAML mode); a form edit after a warning confirmation forces a new dry run;
- opening and saving without changes sends the page model's `yaml` byte for byte;
- the fieldset and the switch are disabled while busy;
- labels/legends present for every control; Remove buttons' accessible names include the row name; focus moves to the new row after Add;
- hostile values (`<img onerror>` in name, description, Cypher) land only in `.value`/`textContent`.

**`tests/dashboard/test_config_form_roundtrip.py`** (new; skipped without node) with **`tests/dashboard/config_form_dump.js`** (new; grabs the pure functions out of index.html, reads JSON fixtures on stdin, prints `{yaml}` per fixture): for hostile and ordinary fixtures (multi-line Cypher with trailing spaces and no final newline, quotes, `#`, `: `, unicode, `yes`/`null`/numeric-looking strings, every parameter type and default), `yaml.safe_load(out) == mapping`, and valid fixtures pass `CypherTool.model_validate` / `NodeTypeDecl.model_validate`. Also: each section template's `entry` equals `yaml.safe_load(template)`.

**`tests/dashboard/test_config_form_drift.py`** (new; skipped without node): the form's field lists and enums equal the models' JSON Schema `properties` and the Python enum constants; `CONFIG_FORM_LIMITS` equals `NAME_PATTERN`, `LABEL_PATTERN`, `PROPERTY_NAME_PATTERN`, `COLOR_PATTERN`, `MAX_DESCRIPTION_LENGTH`, `MAX_ROWS_LIMIT`, `MAX_TIMEOUT_S`, `DEFAULT_MAX_ROWS`, `DEFAULT_TIMEOUT_S`.

**Live checks (before PR)**: headless agent + dev Neo4j, a throwaway registered repo: add a project tool through the form with two parameters → `devgraph config tools list --repo` shows it and a `devgraph mcp` session serves it within 2 s, `git status` shows the file unstaged; edit a node type's key through the ticks → the dry run's "key change" warning appears and needs the second click; open an entry with a hand-added unknown key → YAML mode with the notice; switch Form → YAML → edit → the Form button explains and Discard returns; keyboard-only pass through the tool form (Tab order, Add/Remove, Save). Screenshot for the PR.

Docs: README (Config page: the Form/YAML switch, which entries have a form, that the form writes the same YAML and goes through the same dry run; drop "Not in this page yet: a structured form editor", add relationships' form and hand-edit read-back as not yet), PROJECT_STATUS (G2b-3 done; relationship form and YAML read-back open).
