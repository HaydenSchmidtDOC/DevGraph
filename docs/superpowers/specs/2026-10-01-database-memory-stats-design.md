# Database & memory stats — design

Upstream issue: HaydenSchmidtDOC/DevGraph#3. PR #19 already delivered live JVM
heap via `GET /api/database-stats`. This spec covers the rest: the other stats a
stock Neo4j 5.26 Community container can actually provide, store size on disk,
and a short sampled history so the card can show trends.

## Goals

- Every number on the Database & memory card is a real reading or an explicit
  "unavailable" with a reason — never estimated (the card's existing rule).
- Show memory, CPU, GC and on-disk size for the Neo4j instance DevGraph uses.
- Keep roughly the last hour of samples so a user can see what an index run did
  even if no dashboard tab was open at the time.

## Non-goals

- Page cache hit ratio. Neo4j 5.26 Community exposes no `org.neo4j` JMX beans
  and its metrics subsystem is Enterprise-only; there is no source. The card
  says so instead of showing a dashed meter forever.
- A custom Neo4j image, APOC, or any plugin download (the stack is local-first:
  `NEO4J_PLUGINS=["apoc"]` would download at container start).
- Persisting history across restarts.

## What is available (verified against `neo4j:5.26-community`)

| Reading | Source |
| :--- | :--- |
| JVM heap used/max | `dbms.queryJmx("java.lang:type=Memory")` (existing) |
| System RAM total/free, swap total/free, process + system CPU load | `dbms.queryJmx("java.lang:type=OperatingSystem")` |
| GC collection count and total time | `dbms.queryJmx("java.lang:type=GarbageCollector,*")` |
| Configured page cache size | `dbms.listConfig()` → `server.memory.pagecache.size` |
| Store size (graph store vs. transaction logs) | Filesystem walk of the Neo4j data directory |

JMX composite values are nested as `attributes.<Name>.value` (scalars) or
`attributes.<Name>.value.properties.<field>` (composites), as #19 found.

## Architecture

### `devgraph/dashboard/db_metrics.py` (new)

- `collect_snapshot(engine, data_dir: Path | None) -> dict` — one reading, made
  of independent groups, each with its own `available` flag so one failing
  source never blanks the others:
  - `heap` — unchanged shape from #19 (`available`, `used_bytes`, `max_bytes`,
    `used_percent`). The parser moves here from `routes.py`.
  - `system` — `ram_total_bytes`, `ram_free_bytes`, `swap_total_bytes`,
    `swap_free_bytes`, `process_cpu_load`, `system_cpu_load` (0–1 fractions;
    a negative JVM "not available" value becomes `None`).
  - `gc` — `collection_count`, `collection_time_ms`, summed across collectors.
  - `pagecache` — `configured` (the config string as Neo4j reports it, e.g.
    `"512.00MiB"`), `hit_ratio_available: false`.
  - `store` — `graph_bytes` (sum under `databases/`), `tx_log_bytes` (sum under
    `transactions/`), `total_bytes`. Unavailable with reason `"not_configured"`
    when `data_dir` is `None`, `"unreadable"` when the walk fails.
  - `ts` — sample time (epoch seconds).
- Same defensive parsing rules as #19: fixed literal queries, `bool` rejected as
  a number, non-finite rejected, impossible values (used > max, zero totals)
  reported as unavailable.
- `MetricsHistory` — `deque(maxlen=240)` of compact samples (`ts`, heap used,
  RAM used, process CPU, store total), plus `start(interval_s=15)` / `stop()`
  running `collect_snapshot` on a daemon thread. `latest()` returns the most
  recent full snapshot; `since(seconds)` returns compact samples.

### Settings

`neo4j_data_dir: Path | None = None` (`DEVGRAPH_NEO4J_DATA_DIR`). Optional; when
unset, store size is unavailable and the card names the setting.

### Routes (`routes.py`)

- `GET /api/database-stats` returns the latest snapshot (collecting one on
  demand if the sampler has none yet). The `heap` key keeps its current shape,
  so the existing UI and tests keep working. Still always 200, never driver
  error text.
- `GET /api/database-stats/history?seconds=3600` (clamped 60–3600) returns
  `{"interval_s": 15, "samples": [...]}`.

### App lifecycle (`app.py`)

`build_app` creates one `MetricsHistory`, passes it to `build_router`, and
starts/stops it through a FastAPI lifespan handler — so both the tray and the
headless agent get sampling with no changes of their own.

### Deploy

`deploy/docker-compose.yml` mounts the Neo4j data volume read-only into the
agent at `/neo4j-data` and sets `DEVGRAPH_NEO4J_DATA_DIR=/neo4j-data`. No image
changes. `deploy/podman-compose.yml` runs only Neo4j (the agent is the host tray
app), so it gets a comment pointing `DEVGRAPH_NEO4J_DATA_DIR` at the volume's
host path (`podman volume inspect devgraph_neo4j_data --format '{{.Mountpoint}}'`);
the README documents the same.

## Dashboard (`static/index.html`)

The Database & memory card becomes:

- JVM heap meter + sparkline (existing meter, new sparkline).
- System RAM meter (used/total) + sparkline; swap as a text row.
- CPU (process / system) text row.
- GC: total collections and time.
- Page cache: configured size, with a tooltip that hit ratio needs Neo4j
  Enterprise metrics.
- Store size: total, with graph store vs. transaction logs split + sparkline.
- Note text names whatever is unavailable and why.

The card polls `/api/database-stats` and `/history` every 15 s. Each group is
rendered independently; any missing/invalid group stays dashed. Sparklines are
small canvases drawn with the same plain-canvas approach as the existing query
chart — no new library.

## Error handling

- Neo4j unreachable: every Neo4j-backed group is unavailable; store size can
  still be live (it reads the filesystem).
- If the first (heap) query raises, the remaining Neo4j queries in that
  snapshot are skipped, so an unreachable database costs one driver timeout per
  snapshot, not four.
- A file that disappears during the store walk (Neo4j rotates transaction logs)
  is skipped; any other walk error makes store size unavailable rather than
  reporting an undercount.
- A blank `DEVGRAPH_NEO4J_DATA_DIR` is treated as unset.
- Sampler exceptions are logged at debug and the sample is skipped; the thread
  never dies on a bad read.

## Testing

- `tests/dashboard/test_db_metrics.py`: parsers against recorded 5.26 rows plus
  malformed/denied/empty cases; store walk on a tmp dir (including missing
  dir); ring buffer limits and `since()`; sampler start/stop.
- `tests/dashboard/test_database_stats.py`: extend for the new keys and the
  history route; the existing heap assertions stay as-is.
- UI: extend `database_stats_ui.js` / `test_database_stats_ui.py` for the new
  rows, unavailable states, and sparkline rendering with empty/short history.
- Manual: run the dashboard against the local `neo4j:5.26-community` container.

## Docs

Update `PROJECT_STATUS.md` (dashboard section), `README.md` if it lists
settings, and the compose comments for the new mount.
