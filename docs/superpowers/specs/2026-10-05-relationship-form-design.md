# Dashboard Config page relationship form (G2b-4) — design addendum

Stacked on #45 (G2b-3, `epic1/g2b3-form-editor`).
Epic refs: §10 (one Config page; edit entries in place).
This is an **addendum** to `docs/superpowers/specs/2026-10-05-form-editor-design.md` (the G2b-3 spec, "the form spec" below). Everything there applies unchanged unless a section here says otherwise; section numbers like "§2.5" refer to the form spec. It settles the open details of the form spec's §3 Q11.

**Why a separate file, not a new section in the form spec:** the form spec describes #45, which is under review now; editing it would move that review's target. A short addendum keeps #45's document stable, keeps G2b-4's PR diff self-contained, and inherits every G2b-3 rule by reference rather than restating it.

Names and line numbers were checked on the tip of #45 (`e27f1e3`).

---

## 1. Scope

### PR G2b-4 (this PR): "A form view for project relationships in the Config editor"

In:
1. Project **relationships** get the Form | YAML switch on add and edit, exactly as tools and node types have it (form spec §1). Delete and Copy stay read-only YAML.
2. Relationship rows in the page model carry `entry` (form spec §2.2). The Add template gains an `entry`.
3. Pure functions extend to relationships. The round-trip test, the type-strict comparison and the drift guard all cover `RelationshipDecl` and `CustomProvider`.
4. Advisory hints, including label and provider cross-checks against the page model. They never block Save; the server decides.

Deferred:
- **`custom.params` editing.** A relationship whose `custom.params` is non-empty opens in YAML (Q11). An empty `params: {}` is shown, because it has nothing to edit, and kept as it was.
- Reading hand-edited YAML back into the form (form spec Q3, unchanged).
- Client-side mirrors of the document-level checks: "at most one filesystem relationship", duplicate relationship declarations, `extends: none` effects beyond the label list. The dry run reports them.

---

## 2. Design

### 2.1 Carried over unchanged

Each rule below is the form spec's and is not restated in detail:
- The browser never parses YAML. The form starts from the server's `entry` or the template's `entry` (§2.2).
- `entry` is `null` when JSON can't carry the mapping exactly (`form_entry`). An entry the form can't show exactly, including any unknown field, opens in YAML with a reason (§2.3, Q4).
- A single-line input refuses multi-line or CR values (`configFormLine`). Such an entry opens in YAML.
- The form only writes `#configYaml.value` and then runs `configTextChanged()`. Save, the dry run, the confirmation, `edit.dryYaml`, arming and the busy lock are untouched (§2.1).
- Opening does not re-serialise the entry. An untouched save sends the page model's `yaml` byte for byte.
- No control the browser may rewrite is read back. Text inputs write the state on `input`, and the state is the only source for serialising (index.html `configFormText_`).
- Values reach the page only through `.value`, `textContent` or `option.value`. Ids come from the per-editor counter.

### 2.2 Page model

`_schema_block` (config_model.py:265–274) adds `"entry": form_entry(entry)` to every relationship row, next to `yaml`, the same way node type rows have it (config_model.py:283). Rows with `editable: false` (declared more than once) get it too. They have no Edit button, so it is unused, but it keeps rows uniform.

`CONFIG_SECTIONS.relationships` (index.html:2953) gains `entry: {type: "DOCUMENTS", provider: "custom", custom: {name: "runbook_links"}, from: "Runbook", to: "Service"}`. That is `yaml.safe_load` of its existing template, and the existing template-equality test covers it once `config_form_dump.js` lists the section. The template text is unchanged. Its leading comment ("from and to must name node types that already exist…") stays visible in YAML mode. In form mode the same guidance is the From/To help text (§2.6). As with any opened text, the comment is gone from the textarea after the first form edit.

### 2.3 Which relationship entries the form can show

`configFormFromEntry("relationships", entry)` accepts an entry when all of the following hold:
- keys ⊆ `type, from, to, provider, custom, color`;
- `type` and `to` are single-line strings;
- `color` is null or a single-line string;
- `provider` is one of `PROVIDER_KINDS` when present;
- `custom` is null or a mapping whose keys ⊆ `name, params`, with `name` a single-line string when present and `params` an **empty** mapping when present;
- `from` is a "label list the form can write back exactly" (below);
- a non-null `custom` appears only with `provider: custom`.

**`from`:** either a string or a non-empty list of strings. Each label must be single-line, non-empty inside a list, contain no comma, and equal its own `trim()`. Anything else would come back different after a split on commas and a trim, so it opens in YAML. That includes a list item with a trailing space, a string `"A,B"` and a number in the list.

**`custom` with a non-custom provider:** this entry is invalid, because the validator rejects it. The form shows the custom name field only for `provider: custom`, so it would have nowhere to show this block. It opens in YAML instead, like the node type key-order refusals.

New reason texts, added to `CONFIG_FORM_REASONS` (all `textContent`):
- `fromList`: "This entry's `from` can't be shown as comma-separated labels exactly (a label with a comma, surrounding spaces or a non-text value). Edit it as YAML."
- `customProvider`: "This entry has a `custom` block but its provider isn't custom; the form can't show that. Edit it as YAML."

Non-empty `custom.params` uses the generic field reason, "…a field the form doesn't edit: `custom.params`…", the same as any other field the form doesn't model.

`CONFIG_FORM_REASONS.section` and the "Form: tools and node types only" quiet note (index.html:3283–3287) become unreachable. Every section with add/replace now has a form, so both are removed. They are not kept for a case that can't occur.

Form state: `{type, from, to, provider, custom, color, open}`. `from` is the label text, a string as-is or a list joined with `", "`. `provider` defaults to `"builtin"` when absent. `custom` is the custom name text. `open` is the opened entry.

### 2.4 Form → mapping

The `configFormPut` / `configFormPutChoice` rules apply as for the other sections. A control still showing the opened value keeps that exact value. An emptied field is omitted. A field at its model default is written only when the entry had it or the value differs from the default.

- `type`, `to`, `color`: `configFormPut`.
- `from`: if the entry had `from` and the text still equals its opening text, the opened value is kept as it was, string or list. Otherwise the text is split on `,`, each piece trimmed, and empty pieces dropped, so a trailing comma while typing writes nothing extra. Zero labels omit `from`. Several labels become a list. A single label becomes a list **if the opened `from` was a list**, otherwise a string. This is Q11's "kept as a string or a list the way the entry wrote it". A new entry writes a string for one label and a list for several.
- `provider`: `configFormPutChoice(…, "builtin")`.
- `custom` is written **only while the provider is `custom`**. It is built from the opened block (or `{}`) with `name` put from the field and an opened empty `params` kept, in the opened block's key order and then `name, params`. It is omitted when the name is empty and the entry had no block. An opened `custom: null` is kept while the name field stays empty. When the user switches the provider away from custom, the name stays in the form state but is not serialised. Switching back restores it. The YAML shows exactly what will be saved.

Model key order for new keys is `CONFIG_FORM_FIELDS.relationship`: `type, from, to, provider, custom, color`, the `RelationshipDecl` property order. `configEntryYaml` picks the model order through a section → field-list map instead of the current two-way ternary (index.html:4022). `from` is written with the existing scalar-list flow form (`from: [A, B]`), whose items `configYamlScalar(…, flow = true)` quotes when needed (`on`, `null`, `Y`). `custom` is written as a nested block mapping (`custom:\n  name: x`), with `params: {}` when kept. The emitter already handles both.

### 2.5 Advisory hints

`configFormHints(section, form, ctx)` gains an optional third argument. `ctx = configFormContext(model, scope)` is a new pure function built from the loaded page model:
- `labels`: the built-in labels (`model.global.node_types`) when the project's `schema.extends` is `"default"`, plus the labels of the project's `schema.node_types` rows;
- `relationship_types`: `model.global.relationship_types`;
- `filesystem`: `{file, folder}`, the label of the node type whose `entry.source.kind` is that kind, or null.

These come from the server's model, so no new JS constant can drift from `NODE_LABELS` or `RELATIONSHIP_TYPES`. Tools and node types ignore `ctx`.

Hints for relationships:
- `type`: empty, or not matching `RELATIONSHIP_TYPE_PATTERN`: "A relationship type is uppercase letters, digits and underscores, starting with a letter (at most 64 characters)."
- provider vs type: with `builtin` and a type not in `ctx.relationship_types`: "The builtin provider reuses one of DevGraph's relationship types; pick Custom or Filesystem to declare a new one." With `custom` or `filesystem` and a built-in type: "`CALLS` is built in: use the builtin provider to reuse it."
- `custom`: with provider custom, a name that is empty or doesn't match `PROPERTY_NAME_PATTERN`.
- `from`: no labels; a label not matching `LABEL_PATTERN`; a label listed twice.
- `to`: empty, or not matching `LABEL_PATTERN`.
- **labels that must exist:** a `from` or `to` label not in `ctx.labels`: "`Runbok` isn't a node type this repository has (built-in, or declared in this file as last loaded)." This is advisory only: the list is the last loaded file, and the server's `resolve_declaration` endpoint check decides.
- **filesystem:** `to` other than `ctx.filesystem.folder`: "A filesystem relationship points to the filesystem folder node type (`Folder`)." or "…, and this file declares none." A `from` label that is neither filesystem type: "`X` is not a filesystem node type."
- `color`: as for node types.

The label and filesystem hints are skipped when `ctx` is absent. `CONFIG_FORM_LIMITS` gains `RELATIONSHIP_TYPE_PATTERN`, and the drift test checks it against the Python constant.

### 2.6 The form, field by field

**Relationship**
| Field | Control | Notes |
|---|---|---|
| Type | text input | help: "Uppercase letters, digits and underscores." plus, on Edit, "Changing it renames the relationship." (the same rename semantics as editing `type` in YAML) |
| From | text input | help: "One or more node labels, separated by commas. Each must be a built-in node type or one declared in this file." |
| To | text input | help: "One node label: built-in, or declared in this file." |
| Provider | select from `CONFIG_FORM_FIELDS.relationship_providers`: "Built-in type" (`builtin`), "Custom provider" (`custom`), "Filesystem (file → parent folder)" (`filesystem`) | changing it rebuilds the form in place (§2.7) |
| Custom provider name | text input, shown only while Provider is `custom` | help: "Recorded as data; DevGraph does not run it." |
| Colour | the node type colour control: text `#rrggbb`, native swatch, **Clear** | factored out of `renderConfigForm` into `configFormColour(box, f)` and used by both sections; no behaviour change for node types |

The legend is "Relationship". The fields follow model order, with Custom provider name directly after Provider.

**No `<datalist>` for labels.** From holds several comma-separated labels, which a datalist can't suggest into. Datalist support in screen readers is also uneven. The unknown-label hint covers the same mistake.

### 2.7 View behaviour

- Provider is a structural change, like a parameter's Type. It shows or hides the custom name field in place, keeping focus on the select, and then runs `configFormChanged()`. The custom name control is added to or removed from `configFormEls.all`, so the busy lock and focus order follow.
- `configFormRefresh` passes `configFormContext(configModel, edit.scope)` to `configFormHints`.
- `renderConfigModal` loses the `section` / quiet-note branch (§2.3). A relationship opens in Form on add and edit when representable and in YAML with its reason otherwise. Delete and Copy keep the read-only YAML.
- Accessibility follows form spec §2.10: visible labels, hints linked by `aria-describedby`, no `aria-invalid`, Tab not captured, and the arming focus going to `configEditorFocusTarget()`.

---

## 3. Decisions — recommended answers

1. **`to` field?** A single text input. The model's `to` is a single `str` (`RelationshipDecl.to`), so a list is not representable there and a list value fails the `configFormLine` check (YAML only).
2. **`from` field?** Comma-separated labels. A string stays a string for one label. A list stays a list, even of one. A new entry writes a string for one label and a list for several. Labels that wouldn't survive split-and-trim open in YAML (§2.3–2.4).
3. **`provider` values?** A select of `PROVIDER_KINDS` (`builtin`, `custom`, `filesystem`) with explanatory option text. The drift guard checks it against `PROVIDER_KINDS`. An unknown provider value opens in YAML, matching how a parameter type outside the enum is refused.
4. **How does `custom` interact?** The name field is shown and serialised only for `provider: custom`. Switching away keeps the name in the form state, unserialised, so switching back restores it. An opened entry with a custom block under another provider is invalid and opens in YAML (`customProvider`). Non-empty `params` opens in YAML. Empty `params: {}` is kept.
5. **Cross-field validation?** Advisory hints only (§2.5): type vs provider, labels that must exist (built-in when `extends: default`, or declared in this file as last loaded), and filesystem endpoints. They are built from the page model, never from JS constants. Save stays enabled, and the dry run's 422 text is authoritative. Document-level checks (one filesystem relationship, duplicates) are left to the server.
6. **Colour?** The node type control, shared through `configFormColour`.
7. **Spec form?** This separate addendum (see the top of this document).

---

## 4. Test strategy

**`tests/dashboard/test_config_routes.py`:** relationship rows carry `entry` equal to the file's mapping, key order included, with `from` as a string in one fixture and a list in another. A relationship with a YAML date colour has `entry: null`. The G2b-3 assertion that relationship rows have no `entry` is replaced.

**`tests/dashboard/config_page_ui.js`** (pure):
- `configFormFromEntry("relationships", …)` accepts builtin, custom (with and without `params: {}`) and filesystem entries, and `from` as a string, a one-item list and a several-item list. It refuses with the right reason an unknown key, an unknown `custom` key, non-empty `custom.params`, a provider outside the enum, a custom block under `builtin`, a `from` item with a comma, edge spaces or a number, an empty list item, and a multi-line `type`/`to`/`from`.
- Form → mapping identity on representable fixtures. `from` shapes: an untouched list of one stays a list; editing a string to two labels gives a list; editing a list down to one keeps a list; a new entry's single label is a string; a trailing comma adds nothing; an emptied field omits `from`. Provider default omission and preservation. `custom` written only for the custom provider; a switch away and back restores the name; an opened `custom: null` is kept.
- `configEntryYaml`: `from: [A, B]`; quoted flow items (`on`, `null`, `Y`); the `custom` block with `params: {}`; key order (opened order, then `type, from, to, provider, custom, color`).
- `configFormHints` with a `ctx`: each relationship hint, none for a valid entry, no label hints without `ctx`, built-ins not known under `extends: none`; `configFormContext` from a model fixture.

**`tests/dashboard/config_page_ui.js`** (DOM):
- Add relationship opens in Form, its From/To help text carrying the template's guidance. This replaces the current "the Add relationship template says its endpoints must exist" check, which reads the YAML text.
- Edit opens in Form, or in YAML with the reason for a `params` entry. Delete and Copy keep the read-only YAML.
- Switching Provider shows and hides the custom name in place with focus kept, and the YAML follows.
- A form edit writes the textarea, clears `crossName` and drops a confirmation. Open-then-save sends the model's `yaml` byte for byte. The fieldset is disabled while busy.
- Labels on every control; hostile values only in `.value`/`textContent`; the node type colour control unchanged after the factoring.

**`tests/dashboard/test_config_form_roundtrip.py` + `config_form_dump.js`:**
- `VALID_RELATIONSHIPS` covers builtin (`CALLS`, `from` string), custom with `params: {}` and a colour, filesystem (`IS_CHILD_OF`, `from: [File, Folder]`), and a one-item list. The fixtures run through the type-strict `_ordered` comparison, directly and through the form. Valid fixtures pass `RelationshipDecl.model_validate`.
- Hostile fixtures put each `HOSTILE_STRINGS` value in `type`, `to`, `color`, `custom.name` and `from` (as a string and as a list item).
- `_form_refuses` learns the relationship single-line fields and the `from` rules, and returns the expected reason, so the "refused with the right reason" assertion holds for `fromList` as well as the field reason.
- `config_form_dump.js` lists the `relationships` template, so its `entry` is checked against `yaml.safe_load(template)`.

**`tests/dashboard/test_config_form_drift.py`:** `("relationship", RelationshipDecl)` and `("custom", CustomProvider)` join the property parametrisation (by alias, so `from` not `from_`). `relationship_providers == PROVIDER_KINDS`. `RELATIONSHIP_TYPE_PATTERN` joins the limits.

**Live checks (before PR):** a headless agent, the dev Neo4j and a throwaway registered repo with `File`/`Folder` filesystem node types and a declared `Runbook` type.
- Add `DOCUMENTS` (custom provider `runbook_links`, `from: Runbook, Service`, `to: Service`) through the form. `devgraph config schema list --repo` shows it with both `from` labels, and `git status` shows the file unstaged.
- Edit it to a single `from` label and check the file keeps a one-item list.
- A hand-added `custom.params` entry opens in YAML with the reason.
- An unknown label shows the hint, and Save's dry run returns the server's endpoint error.
- Switching an `IS_CHILD_OF` relationship from filesystem to builtin shows the provider hint, and the dry run refuses it.
- A keyboard-only pass through the relationship form.
- Screenshot for the PR.

**Docs:** README (Config page paragraph: relationships have a form; `custom.params` entries stay YAML; drop "a relationship form" from "Not in this page yet"); PROJECT_STATUS (G2b-4 done; YAML read-back and `custom.params` rows open).
