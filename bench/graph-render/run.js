// node run.js [engines,comma] [data[@cap],comma] [--headless]
// Serves this folder over loopback, drives installed Chrome (real GPU unless --headless) through
// bench.html for each engine x dataset, writes out/results-<mode>.json.
const { chromium } = require("playwright-core"), http = require("http"), path = require("path"), fs = require("fs");
const engines = (process.argv[2] || "cy-canvas,cy-webgl,sigma,force-graph,canvas2d-batched").split(",");
const sets = (process.argv[3] || "devgraph@1500,devgraph,all,all-x2").split(",");
const headless = process.argv.includes("--headless");
const MIME = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json" };
const server = http.createServer((req, res) => {
  const p = path.join(__dirname, decodeURIComponent(req.url.split("?")[0]));
  if (!p.startsWith(__dirname) || !fs.existsSync(p) || fs.statSync(p).isDirectory()) { res.writeHead(404); return res.end(); }
  res.writeHead(200, { "content-type": MIME[path.extname(p)] || "application/octet-stream" }); fs.createReadStream(p).pipe(res);
});
(async () => {
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  const port = server.address().port;
  const browser = await chromium.launch({ channel: "chrome", headless, args: ["--ignore-gpu-blocklist", "--disable-renderer-backgrounding", "--disable-background-timer-throttling", "--window-size=1600,900"] });
  const results = [];
  for (const set of sets) for (const engine of engines) {
    const [data, cap] = set.split("@");
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } }), page = await ctx.newPage();
    const cdp = await ctx.newCDPSession(page); await cdp.send("Performance.enable");
    page.on("pageerror", e => console.error("pageerror", engine, set, e.message));
    await page.goto(`http://127.0.0.1:${port}/bench.html?engine=${engine}&data=${data}${cap ? "&cap=" + cap : ""}`);
    let r;
    try {
      const g = (m, k) => m.metrics.find(x => x.name === k).value;
      const m0 = await cdp.send("Performance.getMetrics");
      r = await Promise.race([page.evaluate(() => window.runBench()), new Promise((_, rej) => setTimeout(() => rej(new Error("timeout 240s")), 240000))]);
      const m1 = await cdp.send("Performance.getMetrics");
      r.mainThreadBusyPct = +(100 * (g(m1, "TaskDuration") - g(m0, "TaskDuration")) / (g(m1, "Timestamp") - g(m0, "Timestamp"))).toFixed(0);
      r.heapMB = Math.round(g(m1, "JSHeapUsedSize") / 1048576);
    } catch (e) { r = { engine, data, cap, error: String(e.message).slice(0, 200) }; }
    console.log(JSON.stringify(r)); results.push(r);
    await ctx.close();
  }
  fs.mkdirSync(path.join(__dirname, "out"), { recursive: true });
  fs.writeFileSync(path.join(__dirname, "out", `results-${headless ? "headless" : "gpu"}.json`), JSON.stringify(results, null, 1));
  await browser.close(); server.close();
})();
