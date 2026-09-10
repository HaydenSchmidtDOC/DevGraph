# DevGraph — Implementation Plan #9: CoDeSys V2.3 / TwinCAT 2 `.pro` extraction

**Status: scoping — no extractor code written yet.** This plan is built from
research only; the actual `.pro` file structure is being reverse-engineered
by hand (byte-diffing controlled edits in CoDeSys) outside this repo, in a
separate, non-committed workflow, because sample project files may contain
sensitive/proprietary logic. See "Handling the source samples" below before
touching any real `.pro` file in this repo.

## Context

`.claude/skills/adding-a-language/SKILL.md` covers adding a Tree-sitter-based
extractor for a mainstream programming language. This is not that: TwinCAT 2's
System Manager `.pro` project file and CoDeSys V2.3's `.pro` project file are
the same format (Beckhoff licensed 3S's CoDeSys V2.3 engine for TwinCAT 2), and
neither is a language with an existing Tree-sitter grammar or any other
maintained open-source parser — confirmed by research (see "Prior art" below).
The format itself is undocumented outside vendor tooling.

The file mixes two genuinely different encodings in one container:

- **Structured Text (ST) POUs** — plain text, IEC 61131-3 dialect (CODESYS
  extensions). Straightforward to parse once section boundaries are known.
- **Ladder Logic (LD) / Function Block Diagram (FBD) POUs** — stored as
  compiled graphical-element records (contacts, coils, gates, wiring/
  connection data), not text. Extracting these requires reverse-engineered
  byte-level record layout: element type tags, position/wiring fields, record
  boundaries. This is the part currently being hand-derived via controlled
  edit + byte-diff in CoDeSys itself.

This means DevGraph's usual "one grammar package, one extractor loop" shape
does not apply directly. The extractor will need two independent decoders
behind one dispatch entry: a text-section parser for ST POUs, and a binary
record decoder for LD/FBD POUs, sharing only the common node/relationship
output shape (`GraphNode`/`GraphRelationship`/`ExtractionResult` from
`devgraph/indexer/common.py`).

## Prior art (researched, none directly usable)

- CODESYS Forge community thread: confirms the `.pro` container has a
  text-editable header/structure for ST content, but does not document the
  LD/FBD binary encoding.
- `pytwincatparser` (PyPI), `chunkhound`'s `twincat_parser.py`: both target
  TwinCAT **3** XML files (`.TcPOU`/`.TcGVL`/`.TcDUT`), a structurally
  unrelated, XML-based format. Not applicable to TC2/CoDeSys V2.3 `.pro`.
- `greenforge-labs/codescribe`: exports CODESYS **V3** binary projects to
  plaintext, but only by driving a running CODESYS install's scripting
  engine — not a standalone parser, and V3 not V2.3.
- `ironplc`: parses IEC 61131-3 Structured Text (including a CODESYS
  dialect) — i.e. the language *inside* a POU, not the `.pro` project
  container or the LD/FBD binary encoding.
- `momalab/ICSREF`: reverse-engineers compiled CODESYS V2 **runtime
  binaries** (deployed bytecode on a PLC) — a different artifact from the
  source-side `.pro` project file.

No dependency to pull in for either half of this extractor. Both the ST
section parser and the LD/FBD record decoder are original work, built from
the manual reverse-engineering findings.

## Handling the source samples (hard rule for this plan)

The `.pro` samples used to derive the binary record layout are sensitive and
must never enter this repo's git history:

1. Real sample text/bytes are only ever held in an out-of-repo scratch
   location (session temp dir) or in memory during a working session — never
   written under `C:\Dev\projects\active\devgraph`.
2. Fixtures committed to this repo (`tests/indexer/pro/fixtures/...`) are
   **fabricated look-alikes** that exercise the same structural patterns
   (same record shapes, same ST syntax constructs) with placeholder names/
   values — never a redacted copy of a real file. Redaction is easy to get
   wrong (stray metadata, unredacted neighbouring bytes); fabrication has no
   such failure mode.
3. Before any commit that touches this feature, `git status`/`git diff`
   review must confirm no real project file, no raw byte dump, and no
   findings notes derived verbatim from a real file are staged.
4. Any scratch file holding real sample bytes gets deleted at the end of the
   working session it was created in.

## Proposed shape (subject to change as the binary format is derived)

1. **Prerequisite check**: confirm `devgraph/indexer/common.py`'s shared
   dataclasses are unchanged/sufficient (they are, per Implementation Plan
   #8 — six languages already depend on them).
2. **Section splitter**: a small pre-pass that walks the `.pro` container
   and splits it into its POU sections (by whatever delimiter/header pattern
   the reverse-engineering finds), tagging each as ST or LD/FBD before
   either decoder runs. This is new — no existing extractor needs this,
   since every current language is one encoding per file.
3. **ST decoder** (`devgraph/indexer/pro/st_decoder.py`): text/regex-based
   parse of each ST-tagged section into `Module`/`Class`(POU)/`Function`
   nodes + `CALLS` edges, philosophically identical to the existing
   extractors' name-based CALLS resolution (see `adding-a-language`
   skill's step 4) — no need to invent a new approach here once section
   boundaries are known.
4. **LD/FBD decoder** (`devgraph/indexer/pro/ldfbd_decoder.py`): binary
   record parser built directly from the reverse-engineered layout. Scope
   for v1: recover element type + wiring/connection topology per rung
   (enough to represent gate/contact/coil structure as graph nodes/edges),
   not full round-trip fidelity (no requirement to reconstruct byte-for-byte
   editable output). Decide this ceiling explicitly once the record layout
   is further along — don't let "full fidelity" become an unstated scope
   creep.
5. **Node/edge mapping** (needs a decision once both decoders exist): POU
   (any kind) → `Class`-equivalent; for LD/FBD, individual rungs/gates are
   candidates for `Function`-equivalent or a dedicated relationship shape —
   this repo's existing five-language precedent (structs/traits as
   Class-equivalent judgment calls) applies, but LD/FBD's graphical nature
   may need a node shape none of the existing six languages have needed.
   Write the decision down in this file once made, per the `adding-a-language`
   skill's Prerequisites #3 pattern.
6. **IMPORTS ceiling**: CODESYS V2.3 projects are typically single-file
   (the whole project is one `.pro`), so cross-file IMPORTS in the sense the
   other six extractors use (file-to-file) likely doesn't apply — the
   in-project-file library/POU-reference structure is the closer analogue if
   one exists. Decide explicitly once the container structure is confirmed;
   default to structural-parity-only (no IMPORTS edges) unless the format
   gives a clear same-project reference to resolve.
7. **Dispatch wiring**: add `.pro` to `devgraph/indexer/dispatch.py`'s
   routing once the decoder produces something real — trivial, same shape
   as the existing `elif resolved.suffix == ...` branches.
8. **Tests**: `tests/indexer/pro/test_st_decoder.py` and
   `test_ldfbd_decoder.py`, against fabricated fixtures only (see handling
   rule above). No golden-*repo* test is possible here (no public CODESYS
   V2.3 repos to shallow-clone, and real samples can't be committed) — the
   fabricated-fixture suite is the actual parity check for this language,
   not a substitute for one.

## Open items (blocking full plan write-up until resolved)

- LD/FBD binary record layout is not yet fully derived — steps 4-6 above
  stay provisional until enough element types are identified via the
  ongoing byte-diff process.
- Whether the `.pro` container has any true cross-reference structure worth
  modeling as `IMPORTS` (step 6) is unknown until the container's top-level
  structure is mapped.
- No decision yet on whether LD/FBD gates get their own graph node label or
  reuse `Function` — deferred to step 5.
