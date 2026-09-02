// DevGraph dashboard. No framework, no build step -- direct DOM
// manipulation and one fetch-based data-layer function per /api endpoint,
// per Implementation Plan #5 Item 4. Kept small and readable over clever.

const LABEL_COLORS = {
  Service: "#5b8cff",
  Module: "#3fb950",
  Class: "#d29922",
  Function: "#e5534b",
  Endpoint: "#a371f7",
  Database: "#39c5cf",
  VectorStore: "#39c5cf",
  Queue: "#f778ba",
  Container: "#8b949e",
  Repository: "#eef0f2",
};
const DEFAULT_LABEL_COLOR = "#8b949e";

let activeRepoId = null;
let cy = null;

// --- Data layer -------------------------------------------------------

async function fetchRepos() {
  const res = await fetch("/api/repos");
  if (!res.ok) throw new Error(`GET /api/repos failed: ${res.status}`);
  return res.json();
}

async function fetchSummary(repoId) {
  const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/summary`);
  if (!res.ok) throw new Error(`GET summary failed: ${res.status}`);
  return res.json();
}

async function fetchGraph(repoId) {
  const res = await fetch(`/api/repos/${encodeURIComponent(repoId)}/graph`);
  if (!res.ok) throw new Error(`GET graph failed: ${res.status}`);
  return res.json();
}

async function fetchSearch(repoId, query) {
  const url = `/api/repos/${encodeURIComponent(repoId)}/search?q=${encodeURIComponent(query)}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`GET search failed: ${res.status}`);
  return res.json();
}

// --- Rendering ----------------------------------------------------------

function renderRepoList(repos) {
  const container = document.getElementById("repo-list");
  container.innerHTML = "";
  for (const repo of repos) {
    const btn = document.createElement("button");
    btn.className = "repo-item" + (repo.repo_id === activeRepoId ? " repo-item--active" : "");
    btn.innerHTML = `
      <div class="repo-item__name">${escapeHtml(repo.repo_id)}</div>
      <div class="repo-item__meta">${repo.node_count} nodes</div>
    `;
    btn.addEventListener("click", () => selectRepo(repo.repo_id));
    container.appendChild(btn);
  }
}

function renderSummary(summary) {
  const grid = document.getElementById("stat-grid");
  grid.innerHTML = "";
  const rows = Object.entries(summary.nodes_by_label || {});
  if (rows.length === 0) {
    grid.innerHTML = '<p class="empty-state">No nodes indexed yet.</p>';
    return;
  }
  for (const [label, count] of rows) {
    const tile = document.createElement("div");
    tile.className = "stat-tile";
    tile.innerHTML = `
      <div class="stat-tile__value">${count}</div>
      <div class="stat-tile__label">${escapeHtml(label)}</div>
    `;
    grid.appendChild(tile);
  }
}

function renderSearchResults(results) {
  const list = document.getElementById("search-results");
  list.innerHTML = "";
  for (const row of results) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="search-results__label">${escapeHtml(row.label)}</span>${escapeHtml(row.name)}`;
    list.appendChild(li);
  }
}

function renderGraph(graphData) {
  const container = document.getElementById("graph-canvas");
  if (cy) {
    cy.destroy();
  }
  cy = cytoscape({
    container,
    elements: {
      nodes: graphData.nodes,
      edges: graphData.edges,
    },
    style: [
      {
        selector: "node",
        style: {
          "background-color": (el) => LABEL_COLORS[el.data("label")] || DEFAULT_LABEL_COLOR,
          label: "data(name)",
          "font-size": "8px",
          color: "#eef0f2",
          "text-valign": "bottom",
          width: 14,
          height: 14,
        },
      },
      {
        selector: "edge",
        style: {
          width: 1,
          "line-color": "#26282c",
          "target-arrow-color": "#26282c",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
        },
      },
      {
        selector: ".highlighted",
        style: {
          "background-color": "#5b8cff",
          "line-color": "#5b8cff",
          "target-arrow-color": "#5b8cff",
        },
      },
    ],
    layout: { name: "cose", animate: false },
  });

  // Click-to-highlight-neighbors. Read-only -- no editing in v1.
  cy.on("tap", "node", (evt) => {
    cy.elements().removeClass("highlighted");
    const node = evt.target;
    node.closedNeighborhood().addClass("highlighted");
  });
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

// --- Interaction ----------------------------------------------------------

async function selectRepo(repoId) {
  activeRepoId = repoId;
  const [repos, summary, graphData] = await Promise.all([
    fetchRepos(),
    fetchSummary(repoId),
    fetchGraph(repoId),
  ]);
  renderRepoList(repos);
  renderSummary(summary);
  renderGraph(graphData);
}

async function refreshActiveRepo() {
  if (!activeRepoId) return;
  const [summary, graphData] = await Promise.all([fetchSummary(activeRepoId), fetchGraph(activeRepoId)]);
  renderSummary(summary);
  renderGraph(graphData);
}

function setLiveStatus(state) {
  const dot = document.querySelector(".live-status__dot");
  const text = document.getElementById("live-status-text");
  dot.className = "live-status__dot";
  if (state === "connected") {
    dot.classList.add("live-status__dot--connected");
    text.textContent = "live";
  } else if (state === "error") {
    dot.classList.add("live-status__dot--error");
    text.textContent = "disconnected";
  } else {
    text.textContent = "connecting…";
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onopen = () => setLiveStatus("connected");
  source.onerror = () => setLiveStatus("error");
  source.onmessage = (evt) => {
    let payload;
    try {
      payload = JSON.parse(evt.data);
    } catch (err) {
      return;
    }
    // Simple "refetch on signal" -- no client-side diffing/patching in v1.
    if (payload.type === "registry_changed") {
      fetchRepos().then(renderRepoList);
    } else if (payload.type === "reindexed" && payload.repo_id === activeRepoId) {
      refreshActiveRepo();
    }
  };
}

document.getElementById("search-box").addEventListener("input", async (evt) => {
  const query = evt.target.value.trim();
  if (!activeRepoId || query.length === 0) {
    renderSearchResults([]);
    return;
  }
  const { results } = await fetchSearch(activeRepoId, query);
  renderSearchResults(results);
});

async function init() {
  const repos = await fetchRepos();
  renderRepoList(repos);
  if (repos.length > 0) {
    await selectRepo(repos[0].repo_id);
  }
  connectEvents();
}

init();
