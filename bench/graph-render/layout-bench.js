// node layout-bench.js [data,comma]
// Times three force layouts on the same real graphs (data/*.json from export.js), all starting from the same
// random positions in a 1600x900 viewport:
//   app        index.html's forceDirectedSettle(), ported line for line (Map-based, object quadtree), stopped by
//              the same rules (180 steps or the converge threshold)
//   app-typed  the same physics on Float64Arrays with an array-backed quadtree (what a Worker would run)
//   d3-force   forceLink + forceManyBody (theta 0.9) + forceX/Y gravity, run for d3's default 300 ticks
// Reports ms/step, total ms, steps, and a compactness measure (mean edge length / mean random-pair distance:
// lower = connected nodes sit closer relative to the whole drawing).
const fs = require("fs"), path = require("path"), d3 = require("d3-force");
const sets = (process.argv[2] || "devgraph@1500,devgraph,all,all-x2").split(",");
const W = 1600, H = 900;

function rng(seed) { return () => { seed |= 0; seed = seed + 0x6D2B79F5 | 0; let t = Math.imul(seed ^ seed >>> 15, 1 | seed); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
function load(spec) {
  const [name, cap] = spec.split("@");
  const raw = JSON.parse(fs.readFileSync(path.join(__dirname, "data", name + ".json")));
  const n = cap ? Math.min(+cap, raw.nodes.length) : raw.nodes.length;
  const edges = raw.edges.filter(e => e[0] < n && e[1] < n);
  const r = rng(7), area = Math.max(W * H, 300000), spread = Math.sqrt(area) / 2;
  const start = new Float64Array(2 * n);
  for (let i = 0; i < n; i++) { start[2 * i] = (r() - .5) * spread; start[2 * i + 1] = (r() - .5) * spread; }
  return { n, edges, start, area };
}
function quality(xs, edges, n) {
  let el = 0; for (const [a, b] of edges) el += Math.hypot(xs[2 * a] - xs[2 * b], xs[2 * a + 1] - xs[2 * b + 1]);
  el /= Math.max(1, edges.length);
  const r = rng(3); let pd = 0; const S = 4000;
  for (let i = 0; i < S; i++) { const a = Math.floor(r() * n), b = Math.floor(r() * n); pd += Math.hypot(xs[2 * a] - xs[2 * b], xs[2 * a + 1] - xs[2 * b + 1]); }
  return +(el / (pd / S)).toFixed(3);
}

/* ── 1. the app's settle, verbatim logic ── */
const QUAD_THETA = 0.8;
const mk = (cx, cy, half) => ({ cx, cy, half, mass: 0, comX: 0, comY: 0, children: null, leafId: null });
const child = (node, p) => node.children[(p.y >= node.cy ? 2 : 0) + (p.x >= node.cx ? 1 : 0)];
function ins(node, id, p, depth) {
  if (node.mass === 0 && node.children === null) { node.mass = 1; node.comX = p.x; node.comY = p.y; node.leafId = id; return; }
  if (node.children === null) {
    if (depth >= 24) node.leafId = null;
    else {
      const oldId = node.leafId, oldP = { x: node.comX, y: node.comY }; node.leafId = null; const h = node.half / 2;
      node.children = [mk(node.cx - h, node.cy - h, h), mk(node.cx + h, node.cy - h, h), mk(node.cx - h, node.cy + h, h), mk(node.cx + h, node.cy + h, h)];
      ins(child(node, oldP), oldId, oldP, depth + 1);
    }
  }
  if (node.children !== null) ins(child(node, p), id, p, depth + 1);
  node.comX = (node.comX * node.mass + p.x) / (node.mass + 1); node.comY = (node.comY * node.mass + p.y) / (node.mass + 1); node.mass += 1;
}
function rep(node, id, p, i2, out) {
  if (node.mass === 0) return;
  if (node.children === null) {
    if (node.leafId === id) return;
    const dx = p.x - node.comX, dy = p.y - node.comY, dist = Math.hypot(dx, dy) || 0.01, f = node.mass * i2 / dist;
    out.x += dx / dist * f; out.y += dy / dist * f; return;
  }
  const dx = p.x - node.comX, dy = p.y - node.comY, dist = Math.hypot(dx, dy) || 0.01;
  if (node.half * 2 / dist < QUAD_THETA) { const f = node.mass * i2 / dist; out.x += dx / dist * f; out.y += dy / dist * f; }
  else for (const c of node.children) rep(c, id, p, i2, out);
}
function appSettle(g) {
  const ids = [...Array(g.n).keys()], positions = new Map(ids.map(i => [i, { x: g.start[2 * i], y: g.start[2 * i + 1] }]));
  const idealLength = Math.sqrt(g.area / g.n) * 0.5, i2 = idealLength * idealLength, ATT = 2.2, GRAV = 0.09;
  const deg = new Map(ids.map(i => [i, 0])); g.edges.forEach(([s, t]) => { deg.set(s, deg.get(s) + 1); deg.set(t, deg.get(t) + 1); });
  const gm = new Map(ids.map(i => [i, 1 + 16 / (1 + deg.get(i))]));
  let temp = Math.sqrt(g.area) / 10, iter = 0;
  const conv = idealLength * Math.sqrt(g.n) * 0.04;
  const step = () => {
    const disp = new Map(ids.map(i => [i, { x: 0, y: 0 }]));
    let minX = 1e18, minY = 1e18, maxX = -1e18, maxY = -1e18;
    positions.forEach(p => { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); });
    const root = mk((minX + maxX) / 2, (minY + maxY) / 2, Math.max(maxX - minX, maxY - minY, 1) / 2 * 1.01);
    ids.forEach(i => ins(root, i, positions.get(i), 0));
    ids.forEach(i => rep(root, i, positions.get(i), i2, disp.get(i)));
    g.edges.forEach(([s, t]) => {
      const a = positions.get(s), b = positions.get(t); let dx = a.x - b.x, dy = a.y - b.y;
      const dist = Math.hypot(dx, dy) || 0.01, f = ATT * dist * dist / idealLength; dx = dx / dist * f; dy = dy / dist * f;
      disp.get(s).x -= dx; disp.get(s).y -= dy; disp.get(t).x += dx; disp.get(t).y += dy;
    });
    let cx = 0, cy = 0; positions.forEach(p => { cx += p.x; cy += p.y; }); cx /= g.n; cy /= g.n;
    ids.forEach(i => { const p = positions.get(i), d = disp.get(i), gg = GRAV * gm.get(i); d.x += (cx - p.x) * gg; d.y += (cy - p.y) * gg; });
    let tot = 0;
    ids.forEach(i => { const p = positions.get(i), d = disp.get(i), dl = Math.hypot(d.x, d.y) || 0.01, c = Math.min(dl, temp); p.x += d.x / dl * c; p.y += d.y / dl * c; tot += dl; });
    temp *= 0.96; iter++; return tot / g.n;
  };
  const t0 = performance.now(); let avg = conv + 1;
  while (iter < 180 && avg > conv) avg = step();
  const ms = performance.now() - t0;
  const xs = new Float64Array(2 * g.n); positions.forEach((p, i) => { xs[2 * i] = p.x; xs[2 * i + 1] = p.y; });
  return { ms, steps: iter, xs, converged: avg <= conv };
}

/* ── 2. same physics, typed arrays + array quadtree ── */
function typedSettle(g) {
  const n = g.n, X = Float64Array.from(g.start), D = new Float64Array(2 * n);
  const idealLength = Math.sqrt(g.area / n) * 0.5, i2 = idealLength * idealLength, ATT = 2.2, GRAV = 0.09;
  const deg = new Uint32Array(n); for (const [s, t] of g.edges) { deg[s]++; deg[t]++; }
  const gm = Float64Array.from(deg, d => 1 + 16 / (1 + d));
  const es = Uint32Array.from(g.edges, e => e[0]), et = Uint32Array.from(g.edges, e => e[1]);
  const cap = 4 * n + 16; // node pool
  const cx = new Float64Array(cap), cyA = new Float64Array(cap), half = new Float64Array(cap), mass = new Float64Array(cap), comX = new Float64Array(cap), comY = new Float64Array(cap);
  const kids = new Int32Array(cap * 4), leaf = new Int32Array(cap);
  let used = 0;
  const alloc = (x, y, h) => { const k = used++; cx[k] = x; cyA[k] = y; half[k] = h; mass[k] = 0; comX[k] = 0; comY[k] = 0; kids[4 * k] = -1; leaf[k] = -1; return k; };
  const insert = (root, id) => {
    let k = root, depth = 0; const px = X[2 * id], py = X[2 * id + 1];
    for (;;) {
      if (mass[k] === 0 && kids[4 * k] === -1) { mass[k] = 1; comX[k] = px; comY[k] = py; leaf[k] = id; return; }
      if (kids[4 * k] === -1) {
        if (depth >= 24) leaf[k] = -1;
        else {
          const old = leaf[k], ox = comX[k], oy = comY[k], h = half[k] / 2; leaf[k] = -1;
          kids[4 * k] = alloc(cx[k] - h, cyA[k] - h, h); kids[4 * k + 1] = alloc(cx[k] + h, cyA[k] - h, h); kids[4 * k + 2] = alloc(cx[k] - h, cyA[k] + h, h); kids[4 * k + 3] = alloc(cx[k] + h, cyA[k] + h, h);
          const c = kids[4 * k + (oy >= cyA[k] ? 2 : 0) + (ox >= cx[k] ? 1 : 0)];
          // place the old point one level down (it can only collide again at max depth)
          let kk = c, d2 = depth + 1;
          for (;;) {
            if (mass[kk] === 0 && kids[4 * kk] === -1) { mass[kk] = 1; comX[kk] = ox; comY[kk] = oy; leaf[kk] = old; break; }
            if (kids[4 * kk] === -1) { if (d2 >= 24) { leaf[kk] = -1; mass[kk] += 1; comX[kk] = (comX[kk] * (mass[kk] - 1) + ox) / mass[kk]; comY[kk] = (comY[kk] * (mass[kk] - 1) + oy) / mass[kk]; break; } break; }
            break;
          }
        }
      }
      comX[k] = (comX[k] * mass[k] + px) / (mass[k] + 1); comY[k] = (comY[k] * mass[k] + py) / (mass[k] + 1); mass[k] += 1;
      if (kids[4 * k] === -1) return;
      k = kids[4 * k + (py >= cyA[k] ? 2 : 0) + (px >= cx[k] ? 1 : 0)]; depth++;
    }
  };
  const stack = new Int32Array(256);
  const repel = (id, root) => {
    const px = X[2 * id], py = X[2 * id + 1]; let fx = 0, fy = 0, sp = 0; stack[sp++] = root;
    while (sp) {
      const k = stack[--sp]; if (mass[k] === 0) continue;
      const dx = px - comX[k], dy = py - comY[k];
      if (kids[4 * k] === -1) { if (leaf[k] === id) continue; const dist = Math.hypot(dx, dy) || 0.01, f = mass[k] * i2 / dist; fx += dx / dist * f; fy += dy / dist * f; continue; }
      const dist = Math.hypot(dx, dy) || 0.01;
      if (half[k] * 2 / dist < QUAD_THETA) { const f = mass[k] * i2 / dist; fx += dx / dist * f; fy += dy / dist * f; }
      else { stack[sp++] = kids[4 * k]; stack[sp++] = kids[4 * k + 1]; stack[sp++] = kids[4 * k + 2]; stack[sp++] = kids[4 * k + 3]; }
    }
    D[2 * id] += fx; D[2 * id + 1] += fy;
  };
  let temp = Math.sqrt(g.area) / 10, iter = 0; const conv = idealLength * Math.sqrt(n) * 0.04;
  const step = () => {
    D.fill(0); let minX = 1e18, minY = 1e18, maxX = -1e18, maxY = -1e18;
    for (let i = 0; i < n; i++) { const x = X[2 * i], y = X[2 * i + 1]; if (x < minX) minX = x; if (x > maxX) maxX = x; if (y < minY) minY = y; if (y > maxY) maxY = y; }
    used = 0; const root = alloc((minX + maxX) / 2, (minY + maxY) / 2, Math.max(maxX - minX, maxY - minY, 1) / 2 * 1.01);
    for (let i = 0; i < n; i++) insert(root, i);
    for (let i = 0; i < n; i++) repel(i, root);
    for (let e = 0; e < es.length; e++) {
      const s = es[e], t = et[e]; let dx = X[2 * s] - X[2 * t], dy = X[2 * s + 1] - X[2 * t + 1];
      const dist = Math.hypot(dx, dy) || 0.01, f = ATT * dist * dist / idealLength; dx = dx / dist * f; dy = dy / dist * f;
      D[2 * s] -= dx; D[2 * s + 1] -= dy; D[2 * t] += dx; D[2 * t + 1] += dy;
    }
    let mx = 0, my = 0; for (let i = 0; i < n; i++) { mx += X[2 * i]; my += X[2 * i + 1]; } mx /= n; my /= n;
    let tot = 0;
    for (let i = 0; i < n; i++) {
      const gg = GRAV * gm[i]; D[2 * i] += (mx - X[2 * i]) * gg; D[2 * i + 1] += (my - X[2 * i + 1]) * gg;
      const dl = Math.hypot(D[2 * i], D[2 * i + 1]) || 0.01, c = Math.min(dl, temp);
      X[2 * i] += D[2 * i] / dl * c; X[2 * i + 1] += D[2 * i + 1] / dl * c; tot += dl;
    }
    temp *= 0.96; iter++; return tot / n;
  };
  const t0 = performance.now(); let avg = conv + 1;
  while (iter < 180 && avg > conv) avg = step();
  return { ms: performance.now() - t0, steps: iter, xs: X, converged: avg <= conv };
}

/* ── 3. d3-force ── */
function d3Settle(g) {
  const nodes = Array.from({ length: g.n }, (_, i) => ({ x: g.start[2 * i], y: g.start[2 * i + 1], vx: 0, vy: 0 }));
  const links = g.edges.map(([s, t]) => ({ source: s, target: t }));
  const ideal = Math.sqrt(g.area / g.n) * 0.5;
  const sim = d3.forceSimulation(nodes).stop()
    .force("link", d3.forceLink(links).distance(ideal).strength(0.5))
    .force("charge", d3.forceManyBody().strength(-ideal * 0.6).theta(0.9))
    .force("x", d3.forceX().strength(0.05)).force("y", d3.forceY().strength(0.05));
  const t0 = performance.now(); let ticks = 0;
  while (sim.alpha() >= sim.alphaMin() && ticks < 300) { sim.tick(); ticks++; }
  const xs = new Float64Array(2 * g.n); nodes.forEach((p, i) => { xs[2 * i] = p.x; xs[2 * i + 1] = p.y; });
  return { ms: performance.now() - t0, steps: ticks, xs, converged: sim.alpha() < sim.alphaMin() };
}

for (const spec of sets) {
  const g = load(spec);
  console.log(`\n${spec}: ${g.n} nodes / ${g.edges.length} edges   (start compactness ${quality(g.start, g.edges, g.n)})`);
  for (const [name, fn] of [["app", appSettle], ["app-typed", typedSettle], ["d3-force", d3Settle]]) {
    if (name === "app" && g.n > 30000) { console.log(`  ${name.padEnd(10)} skipped (>30k nodes: minutes)`); continue; }
    const r = fn(g);
    console.log(`  ${name.padEnd(10)} ${String(Math.round(r.ms)).padStart(7)} ms  ${String(r.steps).padStart(3)} steps  ${(r.ms / r.steps).toFixed(1).padStart(7)} ms/step  compactness ${quality(r.xs, g.edges, g.n)}  ${r.converged ? "converged" : "hit step cap"}`);
  }
}
