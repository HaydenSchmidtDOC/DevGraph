# Dashboard Config Page Relationship Form (G2b-4) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Project relationships get the Config editor's Form | YAML switch, as tools and node types have it. Type, From (comma-separated labels, kept as a string or a list the way the entry wrote it), To, Provider (with the custom provider name for `custom`) and Colour. The form stays a view over `#configYaml`, and relationship entries the form can't show exactly, including any with non-empty `custom.params`, open in YAML with a reason.

**Spec:** `docs/superpowers/specs/2026-10-05-relationship-form-design.md` (the addendum; every task implements the sections it names), on top of `docs/superpowers/specs/2026-10-05-form-editor-design.md` (G2b-3), whose rules all carry over. The G2a spec `docs/superpowers/specs/2026-10-05-config-page-design.md` §2.2–2.3 still governs every write.

**Working directory:** this worktree, branch `epic1/g2b4-relationship-form` (stacked on #45, `epic1/g2b3-form-editor`). Live tests: Neo4j `bolt://127.0.0.1:7687` (`neo4j`/`devgraph-local-dev`), unique repo_ids, cleanup in teardown; never touch `~/.devgraph`. Dashboard test clients use `base_url="http://127.0.0.1"` (Host guard).

## Global Constraints

- YAML is the source of truth. The form only ever writes `#configYaml.value`, then runs `configTextChanged()`. `configSave`, `configWriteRequest`, `configEditTarget`, the dry run, `edit.dryYaml`, the confirmation, `CONFIG_ARM_MS` and the 412 flow are not modified. No new route, no new request shape.
- No YAML parsing in the browser. The relationship form is built from the row's `entry` or the template's `entry`. YAML → Form only for text whose mapping is known (form spec §2.4, unchanged).
- `entry` is `form_entry(...)`: null when JSON can't carry the mapping exactly. Unknown fields, non-empty `custom.params`, a custom block under a non-custom provider and a `from` that split-and-trim would change all refuse into YAML-only with a reason. The form never writes a key it doesn't show and never drops one it does.
- Multi-line or CR values in a single-line input (`type`, `from` labels, `to`, `color`, `custom.name`) are refused into YAML (`configFormLine`).
- Nothing is read back from a control that may rewrite its value. Inputs write the form state on `input`, and the state alone is serialised.
- Client-side checks are advisory hints only and never disable Save. Label and provider cross-checks come from the loaded page model (`configFormContext`), never from new JS copies of `NODE_LABELS`/`RELATIONSHIP_TYPES`. The server's dry run is authoritative.
- The drift guard covers `RelationshipDecl` and `CustomProvider` (properties by alias), `PROVIDER_KINDS` and `RELATIONSHIP_TYPE_PATTERN`. The round-trip test compares with the type-strict `_ordered` helper (key order and leaf types).
- Every entry value goes through `.value`, `textContent` or `option.value`. Ids come from the per-editor counter. Every control is labelled; Tab is never captured.
- All browser code lives in the Config page block of `devgraph/dashboard/static/index.html`; no new static file.
- TDD: each task starts with failing tests, then the implementation, then the full suite.
- Commit messages: plain imperative; no `Co-Authored-By` trailer, no AI attribution. Never commit `uv.lock`, real names or personal paths.

## Review Focus

Reviewers should check these first. They are where this slice can silently lose data or loosen a G2b-3 guarantee:
1. **`from` shape preservation** (spec §2.4). An untouched list of one stays a list. A one-label edit of a list stays a list. A string only becomes a list when it gains a second label. Opened values that split-and-trim would alter are refused (§2.3), not normalised.
2. **`custom` coupling.** `custom` is serialised only for `provider: custom`. A switch away and back restores the name without it ever being written. An opened `custom: null` and `params: {}` are kept as they were. A custom block under another provider is refused, not dropped.
3. **Removed `section` reason.** The quiet "tools and node types only" branch is deleted, not left reachable through some other path. Delete and Copy still get the read-only YAML.
4. **Hints stay advisory.** `configFormContext` reads only the loaded model. No hint disables Save. Label hints are skipped without a context, and built-ins are not "known" under `extends: none`.
5. **Shared colour control.** The `configFormColour` extraction leaves the node type form's behaviour and DOM order unchanged.
6. **Round-trip and drift coverage.** Relationship fixtures go through both the direct and the through-the-form paths. The refusal assertion accepts the new `fromList` reason only where expected.

### Task 1: Parsed entries on relationship rows

**Files:** `devgraph/dashboard/config_model.py` (`_schema_block`: `"entry": form_entry(entry)` on each relationship row), `tests/dashboard/test_config_routes.py`.

- [ ] Write failing tests (spec §4, routes). `GET /api/config` relationship rows carry `entry` equal to the file's mapping, key order included: one with `from` as a string, one as a list, one custom with `params: {}`. An ambiguous (`editable: false`) row carries it too. A relationship with a YAML date `color` has `entry: null`. Replace the G2b-3 assertion that relationship rows have no `entry`.
- [ ] Implement spec §2.2 (server half).
- [ ] `uv run pytest -q tests/dashboard`, then `uv run pytest -q`; commit "Carry parsed entries on Config page relationship rows".

### Task 2: Relationship pure functions, round-trip and drift guard

**Files:** `devgraph/dashboard/static/index.html` (Config page block):
- `CONFIG_FORM_FIELDS.relationship`, `.custom` and `.relationship_providers`;
- `CONFIG_FORM_LIMITS.RELATIONSHIP_TYPE_PATTERN`;
- `CONFIG_FORM_CHECKS.relationship` and `.custom`;
- `CONFIG_FORM_REASONS.fromList` and `.customProvider`, with `.section` removed;
- `entry` on `CONFIG_SECTIONS.relationships`;
- the relationship branches of `configFormFromEntry`, `configEntryFromForm` and `configFormHints` (optional `ctx`);
- a section → field-list map in `configEntryYaml`;
- the new `configFormContext`.

Tests: `tests/dashboard/config_page_ui.js` (pure cases; the harness's grabbed-name list gains `configFormContext`), `tests/dashboard/config_form_dump.js` (`relationships` in the template list), `tests/dashboard/test_config_form_roundtrip.py`, `tests/dashboard/test_config_form_drift.py`.

- [ ] Write failing tests (spec §4, pure functions):
  - Representability: accepted builtin, custom and filesystem entries, and each refusal with its reason text.
  - Form ↔ mapping identity on representable fixtures.
  - Every `from` shape rule; provider default omission and preservation; the `custom` rules, including switch-away-and-back.
  - `configEntryYaml` flow lists with quoted items, the nested `custom` block, and key order.
  - Every relationship hint with a `ctx`, none for a valid entry, and no label hints without `ctx`.
  - `configFormContext` from a model fixture, under both `extends` values.
  - In the round-trip test: `VALID_RELATIONSHIPS` (each passing `RelationshipDecl.model_validate`, directly and through the form) and hostile relationship fixtures; `_SINGLE_LINE`/`_form_refuses` taught the relationship fields and `from` rules, returning the expected reason; the `relationships` template entry equal to `yaml.safe_load(template)`.
  - In the drift test: `RelationshipDecl` and `CustomProvider` in the property parametrisation; `relationship_providers == PROVIDER_KINDS`; `RELATIONSHIP_TYPE_PATTERN` in the limits.
  - Update the existing pure checks that expect relationships to be refused with `CONFIG_FORM_REASONS.section`.
- [ ] Implement spec §2.3, §2.4, §2.5 and the template `entry` of §2.2.
- [ ] `uv run pytest -q tests/dashboard` (node must run), then `uv run pytest -q`; commit "Add relationship form functions with round-trip and drift checks".

### Task 3: Relationship form view

**Files:**
- `devgraph/dashboard/static/index.html`:
  - `renderConfigForm` relationship branch, with legend "Relationship";
  - `configFormColour(box, f)` extracted from the node type branch and used by both;
  - Provider select that shows and hides the custom name in place;
  - `configFormRefresh` passing `configFormContext(configModel, edit.scope)`;
  - `renderConfigModal` without the quiet-note branch;
  - `configEntryRows` already passes `item.entry` for relationships, so check it, don't change it.
- `tests/dashboard/config_page_ui.js` (DOM flow).
- `tests/dashboard/test_config_page_ui.py` (docstring, if it lists the sections with a form).

- [ ] Write failing harness tests (spec §4, DOM). Replace the template-comment check with the Add-in-Form check (From/To help text).
  - Add relationship opens in Form.
  - Edit opens in Form, or in YAML with the reason for a `custom.params` entry. Delete and Copy keep the read-only YAML.
  - Provider switching shows and hides the custom name with focus kept on the select, and the textarea follows.
  - A form edit writes the textarea, clears `crossName` and drops a pending confirmation.
  - Open-then-save sends the model's `yaml` byte for byte. Save from form mode sends the textarea's text.
  - The fieldset and switch are disabled while busy.
  - Every control is labelled. Hints are linked by `aria-describedby`. Hostile values land only in `.value`/`textContent`.
  - The node type colour control behaves as before.
- [ ] Implement spec §2.6 and §2.7.
- [ ] `uv run pytest -q` (node must run); commit "Add the relationship form to the Config editor".

### Task 4: Docs and live verification

**Files:**
- `README.md`, the Config page form paragraph: relationships open in the form; From takes comma-separated labels and keeps the entry's string/list form; the custom provider name appears for `custom`; entries with `custom.params` stay YAML; "Not in this page yet" keeps only loading hand-edited YAML back into the form, plus editing `custom.params` in the form.
- `PROJECT_STATUS.md`: G2b-4 done; YAML read-back and `custom.params` rows open.

- [ ] Run the live checks in spec §4 against a throwaway registry and repo:
  - add a two-label `from` relationship through the form, and confirm it with `devgraph config schema list --repo` and `git status` (file unstaged);
  - a one-label edit keeps the list;
  - a `custom.params` entry opens in YAML with the reason;
  - an unknown-label hint is followed by the server's endpoint error;
  - a filesystem → builtin switch shows the provider hint and the dry run refuses it;
  - a keyboard-only pass;
  - a browser screenshot if a browser is available.
- [ ] Update the docs; full `uv run pytest -q`; commit "Document the relationship form".
