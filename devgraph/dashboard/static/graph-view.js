/* graph-view.js -- the dashboard's graph renderer.

   A small facade that gives index.html the slice of the Cytoscape.js API it
   was written against (collections, classes, data, positions, pan/zoom,
   events) on top of Sigma.js (WebGL) + graphology. Cytoscape's canvas
   renderer repaints the whole graph on every position write or viewport
   change, which is what made large repositories crawl (issue #54, and
   docs/graph-render-benchmark.md); Sigma draws from GPU buffers instead.

   What the page still owns: every colour, size and visibility decision. It
   passes nodeStyle(ele) / edgeStyle(ele) callbacks, and this module only
   decides when to re-run them (an element is restyled after a class or a
   style-relevant data change; cy.style().update() restyles everything).

   Coordinates match Cytoscape's: model space is y-down, zoom is pixels per
   model unit, pan is where the model origin sits on screen. Sigma's camera
   is normalised, so a fixed custom bounding box makes graph space map
   linearly onto it and setView()/readView() convert both ways. */
(function (global) {
  "use strict";

  /* Side of the fixed normalisation box. Any positive number works; the
     value only sets the scale of Sigma's internal 0..1 space. */
  const L = 1000;
  const CURVE_SEGMENTS = 10, CURVE_BEND = 0.15; /* bezier pieces per curved edge; control point offset as a fraction of the edge length */
  const NODE_SIZE_SCALE = 0.25; /* a quarter of the old Cytoscape node diameter (9 px, 15 px for repo/service at zoom 1); Sigma draws `size` as the diameter (see zoomToSizeRatioFunction) */

  /* ── node shapes ───────────────────────────────────────────────────────
     Sigma ships circles only. The dashboard draws repositories as hexagons
     and services as diamonds, so those are the circle program with a
     different signed-distance function in the fragment shader. */
  function makeShapeProgram(sdf) {
    const { NodeProgram } = global.Sigma.rendering;
    const { floatColor } = global.Sigma.utils;
    const VERT = `
      attribute vec4 a_id; attribute vec4 a_color; attribute vec2 a_position; attribute float a_size; attribute float a_angle;
      uniform mat3 u_matrix; uniform float u_sizeRatio; uniform float u_correctionRatio;
      varying vec4 v_color; varying vec2 v_diffVector; varying float v_radius;
      const float bias = 255.0 / 254.0;
      void main() {
        float size = a_size * u_correctionRatio / u_sizeRatio * 4.0;
        vec2 diffVector = size * vec2(cos(a_angle), sin(a_angle));
        vec2 position = a_position + diffVector;
        gl_Position = vec4((u_matrix * vec3(position, 1)).xy, 0, 1);
        v_diffVector = diffVector;
        v_radius = size / 2.0;
        #ifdef PICKING_MODE
        v_color = a_id;
        #else
        v_color = a_color;
        #endif
        v_color.a *= bias;
      }`;
    const FRAG = `
      precision highp float;
      varying vec4 v_color; varying vec2 v_diffVector; varying float v_radius;
      uniform float u_correctionRatio;
      const vec4 transparent = vec4(0.0, 0.0, 0.0, 0.0);
      void main(void) {
        float border = u_correctionRatio * 2.0;
        vec2 q = abs(v_diffVector);
        float r = v_radius * 0.94;
        float dist = ${sdf} + border;
        #ifdef PICKING_MODE
        if (dist > border) gl_FragColor = transparent; else gl_FragColor = v_color;
        #else
        float t = 0.0;
        if (dist > border) t = 1.0; else if (dist > 0.0) t = dist / border;
        gl_FragColor = mix(v_color, transparent, t);
        #endif
      }`;
    const GL = global.WebGLRenderingContext;
    return class extends NodeProgram {
      getDefinition() {
        return {
          VERTICES: 3, VERTEX_SHADER_SOURCE: VERT, FRAGMENT_SHADER_SOURCE: FRAG, METHOD: GL.TRIANGLES,
          UNIFORMS: ["u_sizeRatio", "u_correctionRatio", "u_matrix"],
          ATTRIBUTES: [
            { name: "a_position", size: 2, type: GL.FLOAT }, { name: "a_size", size: 1, type: GL.FLOAT },
            { name: "a_color", size: 4, type: GL.UNSIGNED_BYTE, normalized: true }, { name: "a_id", size: 4, type: GL.UNSIGNED_BYTE, normalized: true },
          ],
          CONSTANT_ATTRIBUTES: [{ name: "a_angle", size: 1, type: GL.FLOAT }],
          CONSTANT_DATA: [[0], [2 * Math.PI / 3], [4 * Math.PI / 3]],
        };
      }
      processVisibleItem(idColor, i, data) {
        const a = this.array;
        a[i++] = data.x; a[i++] = data.y; a[i++] = data.size; a[i++] = floatColor(data.color); a[i++] = idColor;
      }
      setUniforms(params, { gl, uniformLocations: u }) {
        gl.uniform1f(u.u_correctionRatio, params.correctionRatio);
        gl.uniform1f(u.u_sizeRatio, params.sizeRatio);
        gl.uniformMatrix3fv(u.u_matrix, false, params.matrix);
      }
    };
  }

  /* ── gradient edges, straight or curved ────────────────────────────────
     One GPU program draws an edge as a quadratic bezier cut into `segments`
     quads, colour mixed from the source end to the target end. segments = 1
     with curvature 0 is a plain straight gradient edge (a single quad, no
     more expensive than Sigma's own line); curved edges cost segments x 2
     triangles per edge, so the page only uses them on small graphs. The line
     stops short of the target by the node radius plus the arrow head, and a
     stock arrow head is drawn on the end of the curve's tangent. */
  function makeGradientEdgeProgram(segments, curvature) {
    const R = global.Sigma.rendering;
    const { floatColor } = global.Sigma.utils;
    const GL = global.WebGLRenderingContext;
    const VERT = `
      attribute vec4 a_id; attribute vec4 a_colorStart; attribute vec4 a_colorEnd;
      attribute vec2 a_positionStart; attribute vec2 a_positionEnd;
      attribute float a_thickness; attribute float a_radius;
      attribute float a_t; attribute float a_side; attribute float a_isEnd;
      uniform mat3 u_matrix; uniform float u_zoomRatio; uniform float u_sizeRatio; uniform float u_pixelRatio;
      uniform float u_correctionRatio; uniform float u_feather; uniform float u_minEdgeThickness; uniform float u_lengthToThicknessRatio;
      varying vec4 v_color; varying float v_side; varying float v_thickness; varying float v_feather;
      const float bias = 255.0 / 254.0;
      void main() {
        vec2 d = a_positionEnd - a_positionStart;
        vec2 ctrl = (a_positionStart + a_positionEnd) * 0.5 + vec2(-d.y, d.x) * ${curvature.toFixed(4)};
        float mt = 1.0 - a_t;
        vec2 p = mt * mt * a_positionStart + 2.0 * mt * a_t * ctrl + a_t * a_t * a_positionEnd;
        vec2 tangent = 2.0 * mt * (ctrl - a_positionStart) + 2.0 * a_t * (a_positionEnd - ctrl);
        float tl = length(tangent);
        tangent = tl > 1e-12 ? tangent / tl : vec2(1.0, 0.0);
        vec2 n = vec2(-tangent.y, tangent.x);
        float pixelsThickness = max(a_thickness, u_minEdgeThickness * u_sizeRatio);
        float wt = pixelsThickness * u_correctionRatio / u_sizeRatio;
        float stop = a_radius * 2.0 * u_correctionRatio / u_sizeRatio + wt * u_lengthToThicknessRatio * 2.0;
        p -= tangent * a_isEnd * stop;
        gl_Position = vec4((u_matrix * vec3(p + n * a_side * wt, 1)).xy, 0, 1);
        v_thickness = wt / u_zoomRatio;
        v_side = a_side;
        v_feather = u_feather * u_correctionRatio / u_zoomRatio / u_pixelRatio * 2.0;
        #ifdef PICKING_MODE
        v_color = a_id;
        #else
        v_color = mix(a_colorStart, a_colorEnd, a_t);
        #endif
        v_color.a *= bias;
      }`;
    const FRAG = `
      precision mediump float;
      varying vec4 v_color; varying float v_side; varying float v_thickness; varying float v_feather;
      const vec4 transparent = vec4(0.0, 0.0, 0.0, 0.0);
      void main(void) {
        #ifdef PICKING_MODE
        gl_FragColor = v_color;
        #else
        float dist = abs(v_side) * v_thickness;
        gl_FragColor = mix(v_color, transparent, smoothstep(v_thickness - v_feather, v_thickness, dist));
        #endif
      }`;
    const rows = [];
    for (let i = 0; i < segments; i++) {
      const t0 = i / segments, t1 = (i + 1) / segments, e1 = i === segments - 1 ? 1 : 0;
      rows.push([t0, 1, 0], [t0, -1, 0], [t1, 1, e1], [t1, 1, e1], [t0, -1, 0], [t1, -1, e1]);
    }
    class Line extends R.EdgeProgram {
      getDefinition() {
        return {
          VERTICES: rows.length, VERTEX_SHADER_SOURCE: VERT, FRAGMENT_SHADER_SOURCE: FRAG, METHOD: GL.TRIANGLES,
          UNIFORMS: ["u_matrix", "u_zoomRatio", "u_sizeRatio", "u_pixelRatio", "u_correctionRatio", "u_feather", "u_minEdgeThickness", "u_lengthToThicknessRatio"],
          ATTRIBUTES: [
            { name: "a_positionStart", size: 2, type: GL.FLOAT }, { name: "a_positionEnd", size: 2, type: GL.FLOAT },
            { name: "a_thickness", size: 1, type: GL.FLOAT },
            { name: "a_colorStart", size: 4, type: GL.UNSIGNED_BYTE, normalized: true }, { name: "a_colorEnd", size: 4, type: GL.UNSIGNED_BYTE, normalized: true },
            { name: "a_id", size: 4, type: GL.UNSIGNED_BYTE, normalized: true }, { name: "a_radius", size: 1, type: GL.FLOAT },
          ],
          CONSTANT_ATTRIBUTES: [{ name: "a_t", size: 1, type: GL.FLOAT }, { name: "a_side", size: 1, type: GL.FLOAT }, { name: "a_isEnd", size: 1, type: GL.FLOAT }],
          CONSTANT_DATA: rows,
        };
      }
      processVisibleItem(idColor, i, src, tgt, data) {
        const a = this.array;
        a[i++] = src.x; a[i++] = src.y; a[i++] = tgt.x; a[i++] = tgt.y; a[i++] = data.size || 1;
        a[i++] = floatColor(data.color); a[i++] = floatColor(data.colorEnd || data.color); a[i++] = idColor; a[i++] = tgt.size || 1;
      }
      setUniforms(params, { gl, uniformLocations: u }) {
        gl.uniformMatrix3fv(u.u_matrix, false, params.matrix);
        gl.uniform1f(u.u_zoomRatio, params.zoomRatio);
        gl.uniform1f(u.u_sizeRatio, params.sizeRatio);
        gl.uniform1f(u.u_pixelRatio, params.pixelRatio);
        gl.uniform1f(u.u_correctionRatio, params.correctionRatio);
        gl.uniform1f(u.u_feather, params.antiAliasingFeather);
        gl.uniform1f(u.u_minEdgeThickness, params.minEdgeThickness);
        gl.uniform1f(u.u_lengthToThicknessRatio, R.DEFAULT_EDGE_ARROW_HEAD_PROGRAM_OPTIONS.lengthToThicknessRatio);
      }
    }
    /* The stock head points from its "source" to the target, so hand it the curve's control point as the source. */
    const fakeSource = { x: 0, y: 0 }, fakeData = { color: "", size: 1 };
    class Head extends R.EdgeArrowHeadProgram {
      processVisibleItem(idColor, i, src, tgt, data) {
        fakeSource.x = (src.x + tgt.x) / 2 - (tgt.y - src.y) * curvature;
        fakeSource.y = (src.y + tgt.y) / 2 + (tgt.x - src.x) * curvature;
        fakeData.color = data.colorEnd || data.color; fakeData.size = data.size;
        return super.processVisibleItem(idColor, i, fakeSource, tgt, fakeData);
      }
    }
    return R.createEdgeCompoundProgram([Line, Head]);
  }

  /* ── elements and collections ──────────────────────────────────────── */
  const EMPTY_POS = Object.freeze({ x: 0, y: 0 });

  class Coll {
    constructor(cy, arr) { this._cy = cy; this._a = arr; }
    get length() { return this._a.length; }
    [Symbol.iterator]() { return this._a[Symbol.iterator](); }
    forEach(fn) { this._a.forEach(fn); return this; }
    map(fn) { return this._a.map(fn); }
    some(fn) { return this._a.some(fn); }
    filter(fn) {
      if (typeof fn === "string") { const m = selectorMatcher(fn); return new Coll(this._cy, this._a.filter(m)); }
      return new Coll(this._cy, this._a.filter(fn));
    }
    nonempty() { return this._a.length > 0; }
    empty() { return this._a.length === 0; }
    addClass(names) { this._a.forEach(e => e._setClasses(names, true)); return this; }
    removeClass(names) { this._a.forEach(e => e._setClasses(names, false)); return this; }
    toggleClass(names, on) { this._a.forEach(e => e.toggleClass(names, on)); return this; }
    remove() { this._cy._remove(this._a); return this; }
    boundingBox() {
      let x1 = Infinity, y1 = Infinity, x2 = -Infinity, y2 = -Infinity;
      this._a.forEach(e => { if (!e._isNode) return; const p = e.position(); if (p.x < x1) x1 = p.x; if (p.x > x2) x2 = p.x; if (p.y < y1) y1 = p.y; if (p.y > y2) y2 = p.y; });
      if (x1 === Infinity) return { x1: 0, y1: 0, x2: 0, y2: 0, w: 0, h: 0 };
      return { x1, y1, x2, y2, w: x2 - x1, h: y2 - y1 };
    }
  }

  /* The one selector the page uses: [label = "text"] on edges. */
  function selectorMatcher(sel) {
    const m = /^\[\s*(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"\s*\]$/.exec(sel);
    if (!m) throw new Error("graph-view: unsupported selector " + sel);
    const key = m[1], val = m[2].replace(/\\(.)/g, "$1");
    return e => e.data(key) === val;
  }

  class Ele {
    constructor(cy, isNode, data, attrs) {
      this._cy = cy; this._isNode = isNode; this._data = data; this._attrs = attrs; this._classes = new Set();
      this.length = 1;
    }
    id() { return this._data.id; }
    isNode() { return this._isNode; }
    isEdge() { return !this._isNode; }
    nonempty() { return true; }
    empty() { return false; }
    data(k, v) {
      if (k === undefined) return this._data;
      if (arguments.length === 1) return this._data[k];
      if (this._data[k] === v) return this;
      this._data[k] = v;
      /* Per-frame glow animation writes `glow` on every element; nodes ignore it, so only edges restyle for it. */
      if (k !== "glow" || !this._isNode) this._cy._dirty(this);
      return this;
    }
    hasClass(c) { return this._classes.has(c); }
    _setClasses(names, on) {
      let changed = false;
      for (const c of String(names).split(/\s+/)) {
        if (!c) continue;
        if (on) { if (!this._classes.has(c)) { this._classes.add(c); changed = true; } }
        else if (this._classes.delete(c)) changed = true;
      }
      if (changed) this._cy._dirty(this);
    }
    addClass(names) { this._setClasses(names, true); return this; }
    removeClass(names) { this._setClasses(names, false); return this; }
    toggleClass(names, on) {
      for (const c of String(names).split(/\s+/)) if (c) this._setClasses(c, on === undefined ? !this._classes.has(c) : !!on);
      return this;
    }
    remove() { this._cy._remove([this]); return this; }
    source() { return this._cy._byId.get(this._data.source) || EMPTY_COLL; }
    target() { return this._cy._byId.get(this._data.target) || EMPTY_COLL; }
    position(p) {
      const a = this._attrs;
      if (p === undefined) return { x: a.x, y: -a.y };
      a.x = p.x; a.y = -p.y;
      this._cy._moved();
      return this;
    }
    renderedPosition() {
      const v = this._cy._view, a = this._attrs;
      return { x: a.x * v.z + v.px, y: -a.y * v.z + v.py };
    }
  }
  const EMPTY_COLL = new Coll(null, []);

  /* ── the view ──────────────────────────────────────────────────────── */
  function create(opts) {
    const { Sigma, graphology } = global;
    const container = opts.container;
    const graph = new graphology.Graph({ multi: true, type: "directed" });
    const handlers = [];
    const byId = new Map();
    const nodes = [], edges = []; /* insertion-ordered; rebuilt lazily after removals */
    let listsStale = false;
    let minZoom = opts.minZoom ?? 0.2, maxZoom = opts.maxZoom ?? 3;
    let rafId = 0, dirtyAll = false, movedAll = false;
    const dirtyEles = new Set();
    let batchDepth = 0;
    const curvedLimit = opts.curvedEdgeLimit ?? 3000; /* curved edges cost ~10x the triangles; past this many nodes they go straight */
    let curved = true;
    let kPx = 1; /* see k(); the size function above reads it on every render */

    const cy = {
      _byId: byId,
      _view: { z: 1, px: 0, py: 0 },
      sigma: null,
    };

    const nodeProgramClasses = {
      circle: Sigma.rendering.NodeCircleProgram,
      hexagon: makeShapeProgram("max(q.x * 0.8660254 + q.y * 0.5, q.y) - r * 0.8660254"),
      diamond: makeShapeProgram("(q.x + q.y) * 0.7071068 - r * 0.7071068 * 1.2"),
    };
    const edgeProgramClasses = {
      straight: makeGradientEdgeProgram(1, 0),
      curved: makeGradientEdgeProgram(CURVE_SEGMENTS, CURVE_BEND),
    };
    const sigma = new Sigma(graph, container, {
      defaultEdgeType: "straight",
      edgeProgramClasses,
      defaultNodeType: "circle",
      nodeProgramClasses,
      renderLabels: true,
      labelRenderedSizeThreshold: 0,
      labelFont: opts.labelFont || "monospace",
      labelSize: 10,
      labelColor: { color: "#8a8f98" },
      defaultDrawNodeHover: () => {}, /* the page draws its own hover treatment; Sigma's default is a white label box */
      enableEdgeEvents: true,
      /* sizes are screen pixels at zoom 1 and scale linearly with the zoom, like Cytoscape's: Sigma divides by this */
      itemSizesReference: "screen",
      zoomToSizeRatioFunction: r => r * L / kPx,
      stagePadding: 0,
      autoRescale: false,
      hideEdgesOnMove: false,
      zIndex: true,
      allowInvalidContainer: true,
    });
    cy.sigma = sigma;
    sigma.setCustomBBox({ x: [0, L], y: [0, L] });
    const camera = sigma.getCamera();

    /* px per graph unit (== Cytoscape zoom) and where the model origin sits. */
    function readView() {
      const A = sigma.graphToViewport({ x: 0, y: 0 }), B = sigma.graphToViewport({ x: 1, y: 0 });
      cy._view = { z: Math.abs(B.x - A.x), px: A.x, py: A.y };
      return cy._view;
    }
    function k() { /* pixels-per-framed-unit times camera ratio: constant for a given viewport size */
      const A = sigma.graphToViewport({ x: 0, y: 0 }), B = sigma.graphToViewport({ x: L, y: 0 });
      return Math.abs(B.x - A.x) * camera.ratio;
    }
    function applyZoomLimits() {
      const kk = kPx = k();
      sigma.setSetting("minCameraRatio", kk / (maxZoom * L));
      sigma.setSetting("maxCameraRatio", kk / (minZoom * L));
    }
    function setView(z, px, py) {
      const { width: w, height: h } = sigma.getDimensions();
      z = Math.min(maxZoom, Math.max(minZoom, z));
      camera.setState({ x: (w / 2 - px) / (z * L), y: (py - h / 2) / (z * L), ratio: k() / (z * L), angle: 0 });
    }
    applyZoomLimits();
    readView();

    /* ── events ── */
    function emit(type, target, extra) {
      const evt = Object.assign({ type, target: target === undefined ? cy : target }, extra);
      for (const h of handlers.slice()) {
        if (!h.types.includes(type) && !(type === "pan" && h.types.includes("zoom"))) continue;
        if (h.sel === "node" && !(evt.target instanceof Ele && evt.target._isNode)) continue;
        if (h.sel === "edge" && !(evt.target instanceof Ele && !evt.target._isNode)) continue;
        if (h.once) handlers.splice(handlers.indexOf(h), 1);
        h.fn(evt);
      }
    }
    camera.on("updated", () => { readView(); emit("pan"); });
    sigma.on("afterRender", () => emit("render"));
    sigma.on("enterNode", ({ node }) => emit("mouseover", byId.get(node)));
    sigma.on("leaveNode", ({ node }) => emit("mouseout", byId.get(node)));
    sigma.on("clickNode", ({ node }) => emit("tap", byId.get(node)));
    sigma.on("clickEdge", ({ edge }) => emit("tap", byId.get(edge)));
    sigma.on("clickStage", () => emit("tap", cy));
    const down = () => emit("tapstart");
    sigma.on("downNode", down); sigma.on("downEdge", down); sigma.on("downStage", down);
    /* Scroll- and double-click-to-zoom stay off deliberately (use the +/- controls). Sigma's own
       switch for that also blocks programmatic zooming, so cancel just the gestures instead. */
    sigma.getMouseCaptor().on("wheel", e => e.preventSigmaDefault());
    sigma.getMouseCaptor().on("doubleClick", e => e.preventSigmaDefault());
    sigma.getMouseCaptor().on("mouseup", () => emit("tapend"));
    sigma.getTouchCaptor().on("touchup", () => emit("tapend"));
    sigma.getMouseCaptor().on("mouseleave", () => emit("tapend"));

    /* ── restyle + redraw scheduling ── */
    function applyStyle(e) {
      const s = e._isNode ? opts.nodeStyle(e) : opts.edgeStyle(e);
      const a = e._attrs;
      a.hidden = !!s.hidden;
      a.color = s.color;
      a.size = s.size;
      if (e._isNode) {
        a.type = s.shape || "circle";
        a.label = s.label || null;
        a.forceLabel = !!s.label;
        a.zIndex = s.z || 0;
        if (s.labelColor) a.labelColor = s.labelColor; else delete a.labelColor;
      } else {
        a.zIndex = s.z || 0;
        a.colorEnd = s.colorEnd || s.color;
        a.type = curved ? "curved" : "straight";
      }
    }
    function listsRebuild() {
      if (!listsStale) return;
      nodes.length = 0; edges.length = 0;
      byId.forEach(e => (e._isNode ? nodes : edges).push(e));
      listsStale = false;
    }
    function flush() {
      rafId = 0;
      listsRebuild();
      if (curved !== nodes.length <= curvedLimit) { curved = !curved; dirtyAll = true; }
      let partial = null;
      if (dirtyAll) { nodes.forEach(applyStyle); edges.forEach(applyStyle); }
      else {
        const pn = [], pe = [];
        dirtyEles.forEach(e => { if (!byId.has(e.id())) return; applyStyle(e); (e._isNode ? pn : pe).push(e.id()); });
        if (!movedAll && (pn.length + pe.length) < 0.1 * (nodes.length + edges.length + 1)) partial = { nodes: pn, edges: pe };
      }
      dirtyAll = false; movedAll = false; dirtyEles.clear();
      if (partial) sigma.refresh({ partialGraph: partial, skipIndexation: true });
      else sigma.refresh();
    }
    function schedule() { if (!rafId && !batchDepth) rafId = requestAnimationFrame(flush); }
    cy._dirty = e => { dirtyEles.add(e); schedule(); };
    cy._moved = () => { movedAll = true; schedule(); };

    /* ── element management ── */
    cy.add = function (els) {
      els = Array.isArray(els) ? els : [els];
      const added = [];
      for (const el of els) {
        const d = el.data;
        if (d.source !== undefined) continue; /* edges after nodes below */
        const attrs = { x: 0, y: 0, size: 1, color: "#888", label: null };
        graph.addNode(d.id, attrs);
        const e = new Ele(cy, true, d, graph.getNodeAttributes(d.id));
        byId.set(d.id, e); added.push(e); listsStale = true;
        if (el.position) e.position(el.position);
        dirtyEles.add(e);
      }
      for (const el of els) {
        const d = el.data;
        if (d.source === undefined) continue;
        if (!byId.has(d.source) || !byId.has(d.target)) continue; /* Cytoscape would throw; the page only adds edges between nodes it has */
        graph.addEdgeWithKey(d.id, d.source, d.target, { size: 1, color: "#888", type: curved ? "curved" : "straight" });
        const e = new Ele(cy, false, d, graph.getEdgeAttributes(d.id));
        byId.set(d.id, e); added.push(e); listsStale = true; dirtyEles.add(e);
      }
      movedAll = true; schedule();
      return new Coll(cy, added);
    };
    cy._remove = function (list) {
      for (const e of list) {
        if (!byId.has(e.id())) continue;
        if (e._isNode) {
          graph.forEachEdge(e.id(), edgeKey => byId.delete(edgeKey));
          graph.dropNode(e.id());
        } else graph.dropEdge(e.id());
        byId.delete(e.id());
      }
      listsStale = true; dirtyAll = false; movedAll = true; schedule();
    };
    cy.getElementById = id => byId.get(id) || EMPTY_COLL;
    cy.nodes = function (sel) { listsRebuild(); const c = new Coll(cy, nodes.slice()); return sel ? c.filter(sel) : c; };
    cy.edges = function (sel) { listsRebuild(); const c = new Coll(cy, edges.slice()); return sel ? c.filter(sel) : c; };
    cy.elements = function () { listsRebuild(); return new Coll(cy, nodes.concat(edges)); };
    cy.collection = function (arr) { return new Coll(cy, (arr || []).filter(e => e && e._cy === cy)); };
    cy.batch = function (fn) { batchDepth++; try { fn(); } finally { batchDepth--; if (!batchDepth && (dirtyEles.size || dirtyAll || movedAll)) schedule(); } };
    cy.style = () => ({ update() { dirtyAll = true; schedule(); } });

    /* ── events API ── */
    function on(types, sel, fn, once) {
      if (typeof sel === "function") { fn = sel; sel = null; }
      handlers.push({ types: types.split(/\s+/), sel, fn, once });
      return cy;
    }
    cy.on = (t, s, f) => on(t, s, f, false);
    cy.one = (t, s, f) => on(t, s, f, true);

    /* ── viewport ── */
    cy.container = () => container;
    cy.resize = function () { sigma.resize(); applyZoomLimits(); readView(); emit("pan"); };
    cy.zoom = () => readView().z;
    cy.pan = function (p) {
      const v = readView();
      if (p === undefined) return { x: v.px, y: v.py };
      setView(v.z, p.x, p.y);
      return cy;
    };
    cy.minZoom = () => minZoom;
    cy.maxZoom = () => maxZoom;
    cy.extent = function () {
      const v = readView(), { width: w, height: h } = sigma.getDimensions();
      const x1 = -v.px / v.z, y1 = -v.py / v.z;
      return { x1, y1, x2: x1 + w / v.z, y2: y1 + h / v.z, w: w / v.z, h: h / v.z };
    };
    cy.renderer = () => null;

    let animFrame = 0;
    /* Subset of cy.animate: { zoom: level | {level, renderedPosition}, pan: {x,y} } or { fit: {eles, padding} }. */
    cy.animate = function (target, o) {
      const { width: w, height: h } = sigma.getDimensions();
      const v0 = readView();
      let z1 = v0.z, p1 = { x: v0.px, y: v0.py };
      if (target.fit) {
        const bb = target.fit.eles.boundingBox(), pad = target.fit.padding || 0;
        z1 = Math.min(maxZoom, Math.max(minZoom, Math.min((w - 2 * pad) / Math.max(bb.w, 1), (h - 2 * pad) / Math.max(bb.h, 1))));
        p1 = { x: w / 2 - (bb.x1 + bb.w / 2) * z1, y: h / 2 - (bb.y1 + bb.h / 2) * z1 };
      } else {
        const zt = target.zoom;
        if (zt !== undefined) {
          z1 = Math.min(maxZoom, Math.max(minZoom, typeof zt === "object" ? zt.level : zt));
          if (typeof zt === "object" && zt.renderedPosition) { const rp = zt.renderedPosition; p1 = { x: rp.x - (rp.x - v0.px) / v0.z * z1, y: rp.y - (rp.y - v0.py) / v0.z * z1 }; }
        }
        if (target.pan) p1 = { x: target.pan.x, y: target.pan.y };
      }
      const dur = (o && o.duration) ?? 400, ease = o && o.easing === "ease-out" ? t => 1 - Math.pow(1 - t, 3) : t => t;
      cancelAnimationFrame(animFrame);
      const t0 = performance.now();
      const step = now => {
        const t = dur <= 0 ? 1 : Math.min(1, (now - t0) / dur), e = ease(t);
        setView(v0.z + (z1 - v0.z) * e, v0.px + (p1.x - v0.px) * e, v0.py + (p1.y - v0.py) * e);
        animFrame = t < 1 ? requestAnimationFrame(step) : 0;
      };
      animFrame = requestAnimationFrame(step);
      return cy;
    };

    /* ── layouts the page offers besides its own force-directed settle ── */
    cy.layout = function (o) {
      const ns = cy.nodes().map(n => n), listeners = [];
      const api = {
        one(type, fn) { if (type === "layoutstop") listeners.push(fn); return api; },
        run() {
          layoutPositions(cy, ns, o.name);
          cy.batch(() => {});
          setTimeout(() => listeners.forEach(fn => fn({ type: "layoutstop", target: cy })), 0);
          return api;
        },
      };
      return api;
    };

    /* Cytoscape emits layoutstop once after init (its default "random" layout runs then); the page hangs its first settle on it. */
    setTimeout(() => emit("layoutstop"), 0);

    cy.destroy = () => { cancelAnimationFrame(rafId); cancelAnimationFrame(animFrame); sigma.kill(); };
    return cy;
  }

  function layoutPositions(cy, ns, name) {
    const n = ns.length;
    if (!n) return;
    const gap = 36;
    if (name === "grid") {
      const cols = Math.ceil(Math.sqrt(n));
      ns.forEach((e, i) => e.position({ x: (i % cols) * gap, y: Math.floor(i / cols) * gap }));
    } else if (name === "concentric") {
      const deg = new Map(ns.map(e => [e.id(), 0]));
      cy.edges().forEach(e => { deg.set(e.data("source"), (deg.get(e.data("source")) || 0) + 1); deg.set(e.data("target"), (deg.get(e.data("target")) || 0) + 1); });
      const sorted = ns.slice().sort((a, b) => deg.get(b.id()) - deg.get(a.id()));
      let ring = 0, placed = 0, r = 0;
      while (placed < n) {
        const cap = ring === 0 ? 1 : Math.max(6, Math.floor((2 * Math.PI * ring * gap * 1.4) / gap));
        const slice = sorted.slice(placed, placed + cap);
        slice.forEach((e, i) => { const a = (2 * Math.PI * i) / slice.length; e.position({ x: Math.cos(a) * ring * gap * 1.4, y: Math.sin(a) * ring * gap * 1.4 }); });
        placed += slice.length; ring++; r++;
      }
    } else { /* breadthfirst, and anything unrecognised */
      const out = new Map(ns.map(e => [e.id(), []])), indeg = new Map(ns.map(e => [e.id(), 0]));
      cy.edges().forEach(e => { const s = e.data("source"), t = e.data("target"); if (out.has(s) && out.has(t)) { out.get(s).push(t); indeg.set(t, indeg.get(t) + 1); } });
      const level = new Map(), queue = [];
      ns.forEach(e => { if (!indeg.get(e.id())) { level.set(e.id(), 0); queue.push(e.id()); } });
      if (!queue.length && ns.length) { level.set(ns[0].id(), 0); queue.push(ns[0].id()); }
      for (let i = 0; i < queue.length; i++) out.get(queue[i]).forEach(t => { if (!level.has(t)) { level.set(t, level.get(queue[i]) + 1); queue.push(t); } });
      const rows = new Map();
      ns.forEach(e => { const l = level.get(e.id()) ?? 0; if (!rows.has(l)) rows.set(l, []); rows.get(l).push(e); });
      rows.forEach((row, l) => row.forEach((e, i) => e.position({ x: (i - (row.length - 1) / 2) * gap, y: l * gap * 1.6 })));
    }
  }

  global.GraphView = { create, NODE_SIZE_SCALE };
})(window);
