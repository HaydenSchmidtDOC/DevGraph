# DevGraph

Local-first developer knowledge graph platform. See [CLAUDE.md](CLAUDE.md) for repo status and working agreement, and [Blueprints/Design Brief #1.md](Blueprints/Design%20Brief%20%231.md) / [Blueprints/Implementation Plan #1.md](Blueprints/Implementation%20Plan%20%231.md) for the full design and build plan.

## Status

Pre-implementation — planning docs only, no source code yet. See [CLAUDE.md](CLAUDE.md).

## Prerequisites

- **Python 3.13+**
- **Git**
- **Podman** (project's container runtime — not Docker; needed once Neo4j/local services are stood up)
- **Neo4j** (Community Edition, local) — deployment mechanism (Podman container vs. native install) is an open question, see Implementation Plan's "Open questions" section

None of the above beyond Python and Git are installed on this machine as of the last environment check — install Podman and decide on the Neo4j deployment path before starting Phase 1 implementation.

## Getting started

There's nothing to build or run yet. Start by reading the Design Brief, then the Implementation Plan's Phase 1 section, before writing any code.
