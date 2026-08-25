# DevGraph — Phase 4 Sketch: Enterprise Knowledge Federation (Optional)

Companion to `Design Brief #1.md` and `Implementation Plan #1.md`. This is a
**sketch, not a build-ready spec** — Implementation Plan #1's own Delivery
Order section is explicit that Phase 4 should get "no implementation planned
until real usage patterns emerge from Phases 1-3," and that further-out
phases "are more likely to shift once Phase 1 is in real use." Phases 1-3 are
now implemented (see `CLAUDE.md` Status); this document exists so the shape
of Phase 4 is written down before anyone needs it, not so it gets built next.

**Do not start implementing against this brief without a fresh design pass**
informed by actual multi-developer usage of Phases 1-3. Treat everything
below as a starting hypothesis, not a commitment.

## Why this might matter later

Phases 1-3 build a rich per-developer, per-workstation graph. Phase 4's
premise: once several developers on a team each have their own local
DevGraph instance, there may be value in merging graphs so one developer's
assistant can answer questions about a teammate's repository ("what does the
billing service's API look like?") without that developer mounting the repo
themselves. This is explicitly **optional** in the Design Brief — DevGraph
is fully useful and complete without it.

## Non-negotiables carried forward

Every constraint from Design Brief #1's Core Design Principles still applies
here; federation doesn't get a carve-out:

1. **Explicit registration only** — federation never causes a repo to be
   auto-discovered or auto-mounted on another developer's machine. A
   federated repo must still go through that developer's own `add_repo`.
2. **Local-first, opt-in network** — like Phase 3's PR/issue ingestion,
   federation is the second-ever component that talks to a network beyond
   `localhost`. It must default to fully off, per-repo (not a single global
   toggle), matching the `pr_source_enabled`/`issue_source_enabled` pattern
   already established in the registry.
3. **Repository isolation, extended** — `repo_id` scoping was already a hard
   boundary; federation must extend rather than weaken it. See namespacing
   below.

## Sketch: namespacing

Neo4j Community Edition still doesn't support multiple databases (this
hasn't changed and shouldn't be "fixed" by reaching for Enterprise — see
Implementation Plan #1's Watch Out section). The existing single-instance,
`repo_id`-scoped design continues; federation extends the key rather than
replacing the mechanism:

```
org_id/repo_id
```

e.g. a repo currently scoped as `repo_id: "rag-platform"` would become
`repo_id: "acme-corp/rag-platform"` once federation is enabled for it. Every
node/relationship already carries `repo_id` as a hard filter (Design Brief
Principle 3) — this is a value-shape change to that existing property, not a
new mechanism. Unfederated repos keep an unprefixed `repo_id` and are
unaffected.

**Open question, deliberately unresolved**: whether `org_id` prefixing
applies retroactively to a repo's existing `repo_id` (requiring a migration
of every existing node) or only to newly-created federated repos. This is
exactly the kind of decision that should wait for a real second-developer
use case rather than being guessed at now.

## Sketch: sync protocol

Push/pull of graph deltas between a developer's local instance and a shared
target (another developer's instance, or a shared central instance — both
should be supported by the same protocol so "shared central instance" isn't
a forced architecture change later):

- **Push**: a developer explicitly federates a specific mounted repo
  (`devgraph federate <repo_id> --to <target>`, sketch syntax). Only that
  repo's subgraph is pushed, filtered by its own `repo_id` — never a
  whole-instance dump.
- **Pull**: a developer explicitly subscribes to a federated repo from
  elsewhere, which merges into their local instance under the source's
  `org_id/repo_id` namespace, read-only (a developer's local instance should
  never let a pulled/federated subgraph be locally mutated and pushed back
  as if authoritative — provenance stays with the origin).
- **Delta, not snapshot**: reuses the same incremental-update posture as the
  rest of DevGraph (Phase 1's watcher-triggered incremental indexing, Phase
  3's `last_indexed_commit`-based incremental git walk) rather than
  re-syncing full graphs on every change.

None of this is designed in build-ready detail — deliberately. The actual
transport (a small sync service? direct Neo4j-to-Neo4j? something else?),
conflict resolution, and auth model all need their own pass once it's clear
federation is actually wanted.

## Opt-in gate

Extends the registry's existing opt-in-flag pattern (`pr_source_enabled`,
`issue_source_enabled` from Phase 3):

- `federation_enabled` (bool, default `false`) — per repo, not global.
- A federated repo also needs a target/subscription list, sketched as a
  small per-repo config blob rather than new registry columns per target
  (unlike the single-purpose Phase 3 flags, federation targets are
  open-ended, so this shouldn't grow the `repos` table's column count
  unboundedly).

## New MCP tools (sketch only — do not implement yet)

Following the plan's own phase pattern of adding tools alongside new node
types, federation would eventually need something like a
`search_component`/`find_callers` variant that can traverse into federated
namespaces when a developer explicitly asks — but this should be designed
against the Phase 1-3 tool surface as it exists in real use, not guessed at
here. No tool names are being committed to in this document.

## When to actually design this for real

Revisit this sketch (and expect to substantially rewrite it, likely as a
proper `Design Brief #3.md` or an update to this one) once there's a
concrete case of two or more developers wanting to share graph data — not
before. Until then, Phase 4 stays exactly what the Implementation Plan
called it: optional, and not scheduled.
