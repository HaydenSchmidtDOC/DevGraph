DevGraph - Personal Developer Knowledge Graph Platform
Design Brief v1.0

Target Audience: Internal Daifuku Development Teams
 Primary Use Case: Local-first architecture intelligence, repository understanding, system navigation and AI augmentation for developers working across multiple isolated repositories.

Executive Summary

DevGraph is a lightweight, local-first developer platform that continuously builds and maintains a structured knowledge graph of repositories explicitly mounted by a developer.

The system serves as a personal architectural memory layer between the developer's repositories and their coding assistant (Claude Code, GitHub Copilot, Bionic, etc.).

Unlike conventional RAG systems, DevGraph prioritises:

Relationship understanding
Architecture discovery
Dependency analysis
Design traceability
Cross-repository intelligence

rather than pure semantic document retrieval.

The system operates entirely on the developer's workstation.

No source-code monitoring occurs outside explicitly registered repositories.

No automatic machine-wide scanning is permitted.

Core Design Principles
Principle 1 - Explicit Repository Registration

The platform must never inspect arbitrary locations.

The developer (or AI assistant acting on their behalf) must explicitly mount a repository.

Example:

devgraph add d:\projects\rag-platform

devgraph add d:\projects\airport-tools


Only registered repositories will be indexed.

Only registered repositories will be watched.

No machine-wide discovery.

No recursive filesystem crawling.

This is a deliberate security and privacy requirement.

Principle 2 - Local First

All processing occurs locally.

Components:

Developer Workstation
│
├── Neo4j
├── DevGraph Agent
├── Repository Registry
├── Graph Indexer
├── Git Watcher
└── MCP Server


No cloud dependencies.

No telemetry by default.

No external API requirements.

Principle 3 - Repository Isolation

Repositories remain logically isolated.

Every graph object carries:

repo_id


Example:

{
  "name": "RetrievalService",
  "repo_id": "rag-platform"
}


This enables:

Repository-specific queries
Repository filtering
Cross-repo analysis when desired

without requiring separate Neo4j databases.

Principle 4 - AI Optimised

The graph exists primarily for AI consumption.

Design should favour:

Relationship discovery
Impact analysis
Dependency tracing
Architecture summarisation

rather than human-driven Cypher exploration.

The MCP layer should expose high-level tools.

High Level Architecture
+----------------------------------------------------+
|                    Developer                       |
+-------------------------+--------------------------+
                          |
                          v
+----------------------------------------------------+
|                   VS Code / IDE                    |
+----------------------------------------------------+
                          |
                          v
+----------------------------------------------------+
|              AI Assistant (Copilot / Claude)       |
+----------------------------------------------------+
                          |
                          v
+----------------------------------------------------+
|                  DevGraph MCP Layer                |
+----------------------------------------------------+
                          |
                          v
+-------------------+--------------+-----------------+
|                   |              |                 |
v                   v              v                 v
Query Engine   Graph Engine   Repo Registry   Index Manager
                          |
                          v
+----------------------------------------------------+
|                      Neo4j                         |
+----------------------------------------------------+
                          |
                          v
+----------------------------------------------------+
|                 Mounted Repositories               |
+----------------------------------------------------+

Core Components
1. DevGraph Agent

Primary orchestrator.

Responsibilities:

Startup management
Repository registration
Index scheduling
Git watcher management
MCP management
Health monitoring

Runs as:

Windows Service
or
System Tray Application

2. Repository Registry

Tracks mounted repositories.

Example:

{
  "repo_id": "rag-platform",
  "path": "D:\\Projects\\rag-platform",
  "active": true,
  "watch_enabled": true,
  "last_indexed": "timestamp"
}


Functions:

add_repo()
remove_repo()
enable_watch()
disable_watch()
rescan()

3. Git Watcher

Monitors only registered repositories.

Never scans beyond mounted paths.

Example:

WATCH:
D:\Projects\rag-platform

WATCH:
D:\Projects\airport-tools

DO NOT WATCH:
Entire D: drive
Entire user profile
Entire machine


Events:

git commit
file save
branch change
merge
pull
checkout


Trigger:

Incremental Index Job

4. Incremental Indexer

Central intelligence component.

Must support:

Python

Extract:

module
class
function
decorator
import
inheritance

Containers

Extract:

container
image
network
volume
service


Sources:

Podman
Podman Compose

APIs

Extract:

endpoint
route
method
dependency


Sources:

FastAPI
Flask
Django
REST

Datastores

Extract:

database
vectorstore
queue
cache


Examples:

Postgres
Qdrant
Redis
Valkey
Neo4j
RabbitMQ
Kafka

Graph Schema
Repository
Repository


Properties:

repo_id
name
path

Container
Container


Properties:

name
image
repo_id

Service
Service


Examples:

Retrieval Service
Embedding Service
API Gateway

Module
Module


Examples:

retrieval.py
vectors.py
chunking.py

Class
Class

Function
Function

Endpoint
Endpoint

Database
Database

VectorStore
VectorStore

Queue
Queue

Relationships

Core relationships:

CONTAINS
CALLS
IMPORTS
USES
RUNS
WRITES_TO
READS_FROM
IMPLEMENTS
DEPENDS_ON
EXTENDS


Example:

Retrieval Service
        |
        USES
        |
        v
Qdrant

Retrieval Service
        |
        CALLS
        |
        v
Embedding Service

MCP Layer

The AI should never need to write Cypher.

Expose:

search_component

trace_request_flow

get_service_dependencies

find_callers

find_related_files

summarise_repository

compare_branches

impact_analysis

explain_architecture

list_services


Cypher remains an advanced tool only.

Cross Repository Strategy

Default Behaviour:

Workspace Scoped


When a developer opens:

rag-platform


All MCP queries automatically filter:

repo_id=rag-platform


No noise from other mounted repositories.

Optional Mode:

Cross Repository Intelligence


Example:

show all repositories using Redis

show repositories using FastAPI

show reusable embedding components


Developer must opt-in.

Performance Requirements

Initial indexing:

Full scan of mounted repo


Subsequent indexing:

Changed files only


No full rebuilds.

Target behaviour:

Developer saves file
      ↓
Indexer updates graph
      ↓
Graph refreshed
      ↓
AI sees new architecture

Security Requirements
Mandatory

No telemetry enabled by default.

No repository auto-discovery.

No cloud dependencies.

No outbound transmission of source code.

No scanning outside mounted repositories.

Access Control

Developer owns graph.

Graph stored locally.

Repository removal must support:

Delete from graph
Delete indexes
Delete metadata

Future Roadmap

Phase 1

Repository Graph
Code Structure
Container Structure
MCP Access


Phase 2

Requirements
Design Decisions
Architecture Notes


Phase 3

Git History
PR Knowledge
Issue Tracking


Phase 4

Enterprise Knowledge Federation
(Optional)

Recommended Tech Stack
Python 3.13+

Neo4j Community Edition

Official Neo4j MCP Server

Tree-sitter

Watchdog

Typer CLI

SQLite

Pydantic

Podman parsers

Success Criteria

A developer can:

Mount a repository.
Allow DevGraph to build an architecture graph.
Open VS Code.
Ask:
What services depend on retrieval.py?

Which containers call the embedding service?

What breaks if I replace Qdrant?

Show the request path from API to vector store.

Receive accurate graph-backed answers in seconds without the AI re-reading the entire repository.

This should be treated as a personal architecture intelligence platform, not a traditional RAG system or code search engine. The graph is the source of truth; AI is the interface.