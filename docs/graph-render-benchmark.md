# Graph render benchmark (issue #54)

Why the dashboard graph crawls on larger repositories, which render libraries could replace Cytoscape.js, and how they measured against it. Harness: `bench/graph-render/` (`npm install`, `node export.js ...`, `node run.js`).

## Method

- **Data:** real graphs from this machine's registered repositories, pulled through the dashboard's `/api/cypher` proxy, with a layout settled offline by d3-force so every engine draws identical geometry. Layout cost is not measured, only drawing.
  - `devgraph@1500`: first 1,500 nodes / 1,768 edges of the `devgraph` repo (the dashboard's default canvas cap, `CANVAS_NODE_CAP`).
  - `devgraph`: 9,699 nodes / 36,988 edges.
  - `all`: 14,198 nodes / 50,000 edges (the proxy caps one result at 50,000 rows).
  - `all-x2`: `all` tiled 2x2: 56,792 nodes / 200,000 edges, the size of the largest scope the dashboard anticipates.
- **Browser:** installed desktop Chrome, headed, real GPU, 1600x900, 120 Hz display (so 120 fps is the ceiling).
- **Scenarios** (3 s each, one step per animation frame): pan, zoom (1x to 5x and back), and rotate (every node position rewritten each frame, what `rotationTick` does today). Reported: achieved fps and p95 frame time.
- **Cytoscape baseline** is configured as `index.html` ships it: canvas renderer, same node/edge style, heavy-mode (flat colour, straight edges) past 1,600 nodes, `hideEdgesOnViewport`/`textureOnViewport` past 800, and the viewport-culling pass.

## Contenders

| Engine | Renderer | Notes |
|---|---|---|
| Cytoscape 3.33.1 canvas | Canvas2D | Current approach (app pins 3.30.2) |
| Cytoscape 3.33.1 WebGL | WebGL | Built-in `webgl: true` renderer, same API |
| Sigma.js 3.0.3 + graphology | WebGL | Purpose-built for large graphs; camera has a rotation angle |
| force-graph 1.52 | Canvas2D | d3-force based; unbatched per-node draw calls |
| Hand-rolled batched Canvas2D | Canvas2D | One path per colour, culled; the "d3-force + canvas" rewrite |

Not benchmarked (judged on design, not measured): Cosmograph/cosmos.gl (GPU force layout; would replace the layout code too), PixiJS (a general scene graph, so the same custom work as Sigma without its graph model), vis-network and G6 (Canvas2D, the same class of per-frame cost as the Cytoscape canvas rows).

## Results (fps, higher is better; p95 frame ms in brackets)

| Data | Engine | Pan | Zoom | Rotate (positions) | Build |
|---|---|---|---|---|---|
| 1.5k nodes | Cytoscape canvas | 115 (8) | 120 (8) | **5 (250)** | 1.0 s |
| | Cytoscape WebGL | 69 (25) | 72 (17) | 20 (67) | 0.8 s |
| | Sigma | 92 (25) | 119 (8) | 94 (25) | 0.16 s |
| | Batched Canvas2D | 112 (17) | 82 (42) | 120 (8) | 0.01 s |
| 9.7k nodes | Cytoscape canvas | 55 (9) | 120 (8) | **0.6 (2900)** | 8.3 s |
| | Cytoscape WebGL | 8 (159) | 7 (175) | 1.2 (1183) | 4.4 s |
| | Sigma | 91 (25) | 82 (25) | 13 (92); camera 75 (33) | 0.24 s |
| | force-graph | 1.6 (792) | 1.3 (1167) | 2.1 (558) | 0.2 s |
| | Batched Canvas2D | 34 (33) | 18 (50) | 30 (42) | 0.04 s |
| 14.2k nodes | Cytoscape canvas | 33 (33) | 120 (8) | **0.5 (2408)** | 6.8 s |
| | Sigma | 90 (17) | 104 (17) | 18 (75); camera 90 (17) | 0.32 s |
| | Batched Canvas2D | 14 (84) | 7 (967) | 15 (75) | 0.08 s |
| 56.8k nodes | Cytoscape canvas | **1.3 (2842)** | 120* | **0.1 (13817)** | 37 s |
| | Cytoscape WebGL | 3 (508) | 2.5 (550) | 0.5 (3500) | 16 s |
| | Sigma | 69 (33) | 80 (25) | 3 (433); camera 64 (25) | 1.1 s |
| | Batched Canvas2D | 7 (200) | 14 (175) | 7 (167) | 0.13 s |

\* Cytoscape's zoom figure is not trustworthy: with `textureOnViewport` it scales a cached bitmap while zooming and never repaints, so the frames are cheap but show a blurry picture. Its pan at the same size, which also repaints, is the honest number.

## Findings

1. **Cytoscape's cost is position writes and full repaints, not the idle graph.** At the default 1,500-node cap it pans at 115 fps but rotates at 5 fps, because every `position()` call invalidates its style/bounds caches and forces a full repaint. At 10k+ nodes any frame that repaints takes seconds. The earlier culling/heavy-mode work reduced the cost per frame but cannot change that scaling.
2. **Cytoscape's own WebGL renderer does not fix it.** It is faster only when nothing about the graph changes; with this graph's styling it loses to the canvas renderer at 9.7k nodes, and its position writes go through the same slow element API.
3. **Sigma.js is the only contender that stays interactive at every size**: 70-90 fps pan and zoom up to 57k nodes / 200k edges, builds in 0.2-1.1 s instead of 7-37 s, and uses 5-6x less memory (332 MB vs 1.7 GB at 57k). Rewriting every node position per frame (the current rotation approach) is still slow past 10k nodes, but Sigma's camera rotates the whole view for free (`camera.angle`: 64-90 fps at all sizes), so ambient rotation does not need position writes at all.
4. **A hand-rolled Canvas2D renderer and force-graph are not enough.** Even batched, 2D canvas fill/stroke of 50k+ edges per frame tops out around 15 fps at 14k nodes; force-graph's per-node draw calls are worse (1-2 fps at 10k).

## What a Sigma migration costs (against "keep 90% of the look")

Sigma has no built-in hexagon/diamond node shapes, gradient edges, or per-element style functions, and `index.html` uses the Cytoscape API at roughly 90 sites (selection/hover classes, culling, settle and rotation position writes, tap handlers, diffed updates, layout save/restore). Keeping the look needs custom node programs (hexagon, diamond, hover halo), the edge gradient replaced by a source-coloured edge with an arrowhead, and the glow canvas re-anchored to Sigma's camera. The force-directed settle (`forceDirectedSettle`, Barnes-Hut) can stay as it is: it only needs node positions.

## Layout: d3-force against the app's settle

d3-force is a layout library, not a renderer, so it only matters for `forceDirectedSettle`. `bench/graph-render/layout-bench.js` runs three layouts on the same real graphs from the same random start (Node, single thread): the app's settle ported line for line, the same physics on typed arrays, and d3-force (`forceLink` + `forceManyBody` at theta 0.9 + `forceX/Y`, d3's default 300 ticks). Compactness is mean edge length divided by mean random-pair distance (lower is tighter).

| Graph | Layout | Total | ms/step | Steps | Compactness |
|---|---|---|---|---|---|
| 1.5k | app | 1.4 s | 7.8 | 180 | 0.059 |
| | app, typed arrays | 1.1 s | 6.2 | 180 | 0.059 |
| | d3-force | 2.9 s | 9.7 | 300 | 0.065 |
| 9.7k | app | 16.0 s | 89 | 180 | 0.187 |
| | app, typed arrays | 12.1 s | 67 | 180 | 0.187 |
| | d3-force | 29.7 s | 99 | 300 | 0.190 |
| 14.2k | app | 29.0 s | 161 | 180 | 0.079 |
| | app, typed arrays | 18.6 s | 103 | 180 | 0.079 |
| | d3-force | 38.8 s | 129 | 300 | 0.088 |
| 56.8k | app, typed arrays | 91 s | 507 | 180 | 0.033 |
| | d3-force | 227 s | 757 | 300 | 0.035 |

- **d3-force is not an improvement.** It costs more per step and needs more steps, for a layout no tighter than the app's. Its Barnes-Hut is the same technique the app already uses. Switching would change the layout's character for no gain.
- **The real problem is where the settle runs.** The app runs 3 steps per animation frame on the main thread, so a frame is blocked for ~270 ms at 9.7k nodes and ~480 ms at 14.2k, before Cytoscape repaints anything. Typed arrays cut that by 25-35%, but the cost stays far above a frame budget at any size past a few thousand nodes.
- **Recommendation:** keep the app's physics (its tuning is the look), port it to typed arrays, and run it in a Web Worker that posts positions back, so the page stays responsive while it settles. The 180-step cap is hit at every size here, so larger graphs also never fully converge under the current limit.
