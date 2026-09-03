# DevGraph agent container: watcher + incremental indexer + MCP-ready graph
# + live dashboard, headless (no tray icon — see devgraph/agent/headless.py).
#
# Neo4j runs as its own container (deploy/docker-compose.yml); this image is
# just the Python side. Registered repos must be bind-mounted in (DevGraph
# never scans a path it wasn't explicitly given), and DEVGRAPH_NEO4J_URI must
# point at the Neo4j service/container, not 127.0.0.1.

FROM python:3.13-slim

WORKDIR /app

# git: needed by GitPython for repo history indexing.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY devgraph ./devgraph

RUN pip install --no-cache-dir .

ENV DEVGRAPH_DASHBOARD_HOST=0.0.0.0
EXPOSE 8765

ENTRYPOINT ["python", "-m", "devgraph.agent.headless"]
