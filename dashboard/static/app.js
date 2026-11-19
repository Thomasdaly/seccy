import { columns, lines, flowRows, uptimeStrip, showTip, hideTip, s } from "./charts.js";

// ---------------------------------------------------------------- DOM helper
// Never innerHTML: generations, payloads and hosts are untrusted upstream data.
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "style") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (sel) => document.querySelector(sel);

// ---------------------------------------------------------------- formatting
const nf = new Intl.NumberFormat();
const cf = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const fmtInt = (n) => (n == null ? "—" : Math.abs(n) >= 10000 ? cf.format(n) : nf.format(Math.round(n)));
const fmtUSD = (v) => {
  if (v == null) return "—";
  if (v === 0) return "$0";
  const a = Math.abs(v);
  if (a < 0.01) return "$" + v.toFixed(4);
  if (a < 100) return "$" + v.toFixed(2);
  if (a < 10000) return "$" + nf.format(Math.round(v));
  return "$" + cf.format(v);
};
const fmtMs = (ms) => (ms == null ? "—" : ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${(ms / 60000).toFixed(1)} min`);
const fmtPct = (x, d = 0) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const fmtBytes = (b) => {
  if (b == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (b >= 1000 && i < u.length - 1) { b /= 1000; i++; }
  return `${b.toFixed(b >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};
const fmtAgo = (t) => {
  if (!t) return "—";
  const d = Date.now() / 1000 - t;
  if (d < 60) return "just now";
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
};
const fmtIn = (t) => {
  if (!t) return "—";
  const d = t - Date.now() / 1000;
  if (d <= 60) return "within a minute";
  if (d < 3600) return `in ${Math.round(d / 60)}m`;
  if (d < 86400) return `in ${Math.round(d / 3600)}h`;
  return "not soon";
};
const dtf = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const df = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const dfw = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric" });
const tf = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const fmtWhen = (t) => (t ? dtf.format(new Date(t * 1000)) : "—");
const fmtDur = (a, b) => {
  if (!a || !b) return "—";
  const d = b - a;
  return d < 3600 ? `${Math.max(1, Math.round(d / 60))} min` : `${(d / 3600).toFixed(1)} h`;
};
const bucketLabel = (step) => (t) => (step < 86400 ? tf.format(new Date(t * 1000)) : df.format(new Date(t * 1000)));
const bucketTitle = (step) => (t) => {
  const d = new Date(t * 1000);
  if (step < 86400) return `${df.format(d)}, ${tf.format(d)}–${tf.format(new Date((t + step) * 1000))}`;
  if (step === 86400) return dfw.format(d) + " (UTC day)";
  return "Week of " + df.format(d);
};
const RANGE_LABEL = { "24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days", "90d": "last 13 weeks" };

// ---------------------------------------------------------------- colour
let ROUTES = ["glue", "planner", "judge", "embed", "other"];
const slot = (route) => {
  const i = ROUTES.indexOf(route);
  return route === "other" || i < 0 ? 0 : (i % 8) + 1;
};
const colorOf = (route) => (slot(route) ? `var(--s${slot(route)})` : "var(--s-other)");
const inkOn = (route) => (slot(route) === 4 ? "#0b0b0b" : "#ffffff");
const ROUTE_ROLE = { glue: "local · classify/normalise", embed: "local · dedup vectors", planner: "hosted open · probe planning", judge: "frontier · final verdict", other: "unmapped model" };

// ---------------------------------------------------------------- icons
const STATUS_LABEL = { good: "OK", warning: "Warning", serious: "Serious", critical: "Failing", unknown: "Unknown" };
function statusIcon(status, cls = "icon") {
  const svg = s("svg", { viewBox: "0 0 16 16", class: cls, "aria-hidden": "true" });
  if (status === "good") {
    s("circle", { cx: 8, cy: 8, r: 8, style: { fill: "var(--good)" } }, svg);
    s("path", { d: "M4.5 8.2l2.3 2.3 4.7-4.9", fill: "none", stroke: "#fff", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }, svg);
  } else if (status === "warning" || status === "serious") {
    s("path", { d: "M8 1L15.5 14.5H.5Z", "stroke-linejoin": "round", style: { fill: status === "warning" ? "var(--warning)" : "var(--serious)" } }, svg);
    s("path", { d: "M8 6v4M8 12.2v.1", stroke: "#0b0b0b", "stroke-width": 1.8, "stroke-linecap": "round" }, svg);
  } else if (status === "critical") {
    s("circle", { cx: 8, cy: 8, r: 8, style: { fill: "var(--critical)" } }, svg);
    s("path", { d: "M5.3 5.3l5.4 5.4m0-5.4l-5.4 5.4", stroke: "#fff", "stroke-width": 1.8, "stroke-linecap": "round" }, svg);
  } else {
    s("circle", { cx: 8, cy: 8, r: 7, fill: "none", style: { stroke: "var(--muted)" }, "stroke-width": 1.5 }, svg);
    s("path", { d: "M5 8h6", style: { stroke: "var(--muted)" }, "stroke-width": 1.5, "stroke-linecap": "round" }, svg);
  }
  return svg;
}
const badge = (status, text) => h("span", { class: "badge" }, statusIcon(status), text ?? STATUS_LABEL[status]);
const routeKey = (route, kind = "dot") => h("span", { class: "cell-key" }, h("span", { class: `key-${kind}`, style: { background: colorOf(route) } }), route);

// ---------------------------------------------------------------- state
const state = { tab: "health", range: "7d", raw: "generations", page: 1, table: null, collection: null, meta: null, cache: {}, timer: null, sql: "" };
const TABS = ["health", "trends", "flows", "raw"];

function readHash() {
  const [tab, q] = location.hash.replace(/^#\/?/, "").split("?");
  if (TABS.includes(tab)) state.tab = tab;
  const p = new URLSearchParams(q || "");
  if (p.get("range") && ["24h", "7d", "30d", "90d"].includes(p.get("range"))) state.range = p.get("range");
  if (p.get("raw")) state.raw = p.get("raw");
}
function writeHash() {
  const q = new URLSearchParams({ range: state.range });
  if (state.tab === "raw") q.set("raw", state.raw);
  history.replaceState(null, "", `#/${state.tab}?${q}`);
}

async function api(path, opts) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let j = null;
  try { j = await r.json(); } catch { /* non-JSON */ }
  if (!r.ok) throw new Error(j?.error || (typeof j?.detail === "string" ? j.detail : JSON.stringify(j?.detail)) || `HTTP ${r.status}`);
  return j;
}

// ---------------------------------------------------------------- charts mount + resize
const drawers = new Map();
const widths = new WeakMap();
const ro = new ResizeObserver((entries) => {
  for (const e of entries) {
    const w = Math.round(e.contentRect.width);
    if (!w || widths.get(e.target) === w) continue;
    widths.set(e.target, w);
    drawers.get(e.target)?.();
  }
});
function mount(el, draw) {
  drawers.set(el, () => draw(el));
  ro.observe(el);
  return el;
}

function legend(items, kind = "rect") {
  if (items.length < 2) return null;
  return h("div", { class: "legend" }, items.map((it) => h("span", {}, h("span", { class: `key-${kind}`, style: { background: it.color } }), it.name)));
}

function table(cols, rows, { onRow, empty = "Nothing here yet.", cap } = {}) {
  if (!rows.length) return h("p", { class: "muted small" }, empty);
  if (cap && rows.length > cap) {
    const wrap = h("div", {}, table(cols, rows.slice(0, cap), { onRow }));
    const btn = h("button", { class: "link-btn more-row" }, `Show all ${rows.length}`);
    btn.addEventListener("click", () => wrap.replaceChildren(table(cols, rows, { onRow })));
    wrap.append(btn);
    return wrap;
  }
  return h("div", { class: "table-wrap" }, h("table", { class: "data" },
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.align === "r" ? "r" : null }, c.label)))),
    h("tbody", {}, rows.map((r) => {
      const tr = h("tr", onRow ? { class: "clickable", tabindex: 0, onclick: () => onRow(r), onkeydown: (e) => { if (e.key === "Enter") onRow(r); } } : {},
        cols.map((c) => {
          const v = c.render ? c.render(r) : c.fmt ? c.fmt(r[c.key]) : r[c.key];
          return h("td", { class: [c.align === "r" ? "r" : "", c.cls || ""].join(" ").trim() || null, title: c.title ? c.title(r) : null }, v ?? "—");
        }));
      return tr;
    }))));
}

function chartCard({ title, sub, draw, tableView, legendItems, legendKind, cls }) {
  const body = mount(h("div", { class: "chart" }), draw);
  const tbl = h("div", { hidden: true });
  const btn = h("button", { class: "link-btn", "aria-pressed": "false" }, "Table");
  btn.addEventListener("click", () => {
    const on = tbl.hidden;
    tbl.hidden = !on;
    body.hidden = on;
    btn.textContent = on ? "Chart" : "Table";
    btn.setAttribute("aria-pressed", String(on));
    if (on) tbl.replaceChildren(tableView());
    else drawers.get(body)?.();
  });
  return h("div", { class: "card" + (cls ? " " + cls : "") },
    h("div", { class: "card-head" }, h("div", {}, h("h3", {}, title), sub ? h("p", { class: "sub" }, sub) : null), tableView ? h("div", { class: "actions" }, btn) : null),
    legendItems ? legend(legendItems, legendKind) : null, body, tbl);
}

function card(title, sub, ...kids) {
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", {}, h("h3", {}, title), sub ? h("p", { class: "sub" }, sub) : null)), ...kids);
}

function seriesTable(buckets, step, series, fmt) {
  const t = bucketTitle(step);
  return table(
    [{ label: "Bucket", key: "t", fmt: t, cls: "nw" }, ...series.map((sr, i) => ({ label: sr.name, align: "r", render: (r) => fmt(r.v[i]) }))],
    buckets.map((b, bi) => ({ t: b, v: series.map((sr) => sr.values[bi]) })).reverse());
}

function routeSeries(obj, routes, { keepEmpty = false } = {}) {
  return routes
    .filter((r) => keepEmpty || (obj[r] || []).some((v) => v))
    .map((r) => ({ key: r, name: r, color: colorOf(r), values: obj[r] || [] }));
}

function notices(list) {
  return (list || []).map((n) => h("div", { class: "notice" }, statusIcon("unknown"), h("span", {}, n)));
}

// ================================================================= HEALTH

const SVC = {
  litellm: { title: "LiteLLM", role: "Model router · :4000", kv: (d) => [["Version", d.version], ["Routes", d.routes], ["Langfuse callback", d.langfuse_callback == null ? "—" : d.langfuse_callback ? "on" : "off"]] },
  ollama: { title: "Ollama", role: "Local inference · RTX 2060", kv: (d) => [["Version", d.version], ["Installed models", d.installed?.length], ["Resident", (d.loaded || []).map((m) => `${m.name} · ${m.gpu_pct ?? "?"}% GPU`).join(", ") || "none (cold)"]] },
  qdrant: { title: "Qdrant", role: "Vectors · finding dedup · :6333", kv: (d) => [["Version", d.version], ["Collections", d.collections?.length], ["Points", d.collections ? fmtInt(d.collections.reduce((a, c) => a + (c.points || 0), 0)) : null]] },
  langfuse: { title: "Langfuse", role: "Traces · token + USD cost · :3000", kv: (d) => [["Version", d.version], ["Calls (24h)", fmtInt(d.gens_24h)], ["Spend (24h)", d.cost_24h == null ? null : fmtUSD(d.cost_24h)], ["Last trace", d.last_obs_at ? fmtAgo(d.last_obs_at) : "—"]] },
  "langfuse-worker": { title: "Langfuse worker", role: "Ingestion queue → ClickHouse", kv: (d) => [["Health endpoint", d.http ? `HTTP ${d.http}` : null]] },
  clickhouse: { title: "ClickHouse", role: "Langfuse trace store", kv: (d) => [["Ping", d.http ? `HTTP ${d.http}` : null]] },
  redis: { title: "Redis", role: "Langfuse queue + cache", kv: (d) => [["Reply", d.reply]] },
  minio: { title: "MinIO", role: "Langfuse event + media blobs", kv: (d) => [["Liveness", d.http ? `HTTP ${d.http}` : null]] },
  postgres: { title: "Postgres", role: "Harness findings DB · Langfuse metadata · :5432", kv: (d) => [["Version", d.version], ["DB size", fmtBytes(d.size)], ["Connections", d.conns != null ? `${d.conns} / ${d.max_conns}` : null], ["Harness schema", d.harness == null ? null : d.harness ? "applied" : "missing"]] },
};

function kv(pairs) {
  return h("dl", { class: "kv" }, pairs.filter(([, v]) => v != null && v !== "").map(([k, v]) => [h("dt", {}, k), h("dd", {}, String(v))]));
}

function updatePill(services) {
  const worst = services.some((x) => x.status === "critical") ? "critical" : services.some((x) => x.status === "warning") ? "warning" : "good";
  const down = services.filter((x) => x.status === "critical").length;
  const pill = $("#stack-pill");
  pill.hidden = false;
  pill.replaceChildren(statusIcon(worst), worst === "good" ? "All services up" : worst === "critical" ? `${down} service${down > 1 ? "s" : ""} down` : "Degraded");
}

function renderHealth(d) {
  updatePill(d.services);

  const checks = card("Stack checks", "The harness design principles, checked against live state.",
    h("div", { class: "checks" }, d.checks.map((c) => h("div", { class: "check" },
      statusIcon(c.status), h("span", { class: "name" }, c.label), h("span", { class: "val" }, c.value),
      h("span", { class: "det" }, c.detail + (c.at ? ` Last trace ${fmtAgo(c.at)}.` : ""))))));

  const PIPE = ["langfuse-worker", "clickhouse", "redis", "minio"];
  const svcCard = (sv) => {
    const meta = SVC[sv.name] || { title: sv.name, role: "", kv: () => [] };
    const strip = mount(h("div", { class: "chart" }), (el) => uptimeStrip(el, sv.history, (t) => `${fmtWhen(t)} (15 min)`));
    return h("div", { class: "card svc" },
      h("div", { class: "card-head" }, h("div", {}, h("h3", {}, meta.title), h("p", { class: "sub" }, meta.role)), h("div", { class: "actions" }, badge(sv.status))),
      h("div", { class: "metric-row" },
        h("div", { class: "metric" }, h("div", { class: "m-label" }, "Probe"), h("div", { class: "m-value num" }, sv.ok ? fmtMs(sv.ms) : "—")),
        h("div", { class: "metric" }, h("div", { class: "m-label" }, "Uptime (since start, ≤24h)"), h("div", { class: "m-value num" }, fmtPct(sv.uptime, sv.uptime === 1 ? 0 : 2)))),
      strip,
      sv.reasons?.length ? h("ul", { class: "reasons" }, sv.reasons.map((r) => h("li", {}, statusIcon(sv.status), h("span", {}, r)))) : null,
      sv.ok ? kv(meta.kv(sv.detail || {})) : null);
  };
  const svcCards = d.services.filter((x) => !PIPE.includes(x.name)).map(svcCard);
  const pipeCards = d.services.filter((x) => PIPE.includes(x.name)).map(svcCard);

  const dk = d.disk;
  const usedPct = dk.used / dk.total;
  const diskStatus = dk.free_pct > 20 ? "good" : dk.free_pct > 10 ? "warning" : "critical";
  svcCards.push(h("div", { class: "card svc" },
    h("div", { class: "card-head" }, h("div", {}, h("h3", {}, "Docker disk"), h("p", { class: "sub" }, "WSL2 volume: pgdata, qdrant, images")), h("div", { class: "actions" }, badge(diskStatus))),
    h("div", { class: "metric-row" }, h("div", { class: "metric" }, h("div", { class: "m-label" }, "Free"), h("div", { class: "m-value num" }, `${dk.free_pct.toFixed(0)}%`))),
    h("div", { class: "meter" + (diskStatus === "good" ? "" : " " + diskStatus), role: "meter", "aria-valuenow": Math.round(usedPct * 100), "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": "Disk used" },
      h("span", { style: { width: `${(usedPct * 100).toFixed(1)}%` } })),
    kv([["Used", fmtBytes(dk.used)], ["Free", fmtBytes(dk.free)], ["Total", fmtBytes(dk.total)]])));

  // routes + probe
  const probeOut = h("div", {});
  const probed = state.cache.probe;
  const routeRows = d.routes.map((r) => {
    const p = probed?.results?.find((x) => x.route === r.route);
    return { ...r, probe: p };
  });
  const routesTable = () => table([
    { label: "Route", render: (r) => routeKey(r.route) },
    { label: "Role", render: (r) => ROUTE_ROLE[r.route] || "custom" },
    { label: "Model", render: (r) => h("span", { class: "mono" }, r.model || "—") },
    { label: "Config", render: (r) => (r.placeholder ? badge("critical", "placeholder") : badge("good", "set")) },
    { label: probed ? `Probe (${fmtAgo(probed.at)})` : "Probe", render: (r) => (!r.probe ? h("span", { class: "muted" }, "not run") : r.probe.ok ? badge("good", "answered") : h("span", { title: r.probe.error || "" }, badge("critical", "failed"), h("div", { class: "small muted" }, r.probe.error || ""))) },
  ], routeRows);
  probeOut.append(routesTable());
  const probeBtn = h("button", { class: "btn" }, "Probe routes");
  probeBtn.addEventListener("click", async () => {
    probeBtn.disabled = true;
    probeBtn.textContent = "Probing… (up to 3 min)";
    try { state.cache.probe = await api("/api/probe-routes", { method: "POST" }); render(); }
    catch (e) { probeOut.prepend(h("p", { class: "error small" }, e.message)); probeBtn.disabled = false; probeBtn.textContent = "Probe routes"; }
  });
  const routesCard = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h3", {}, "Model routes"), h("p", { class: "sub" }, "Probing sends one tiny real request per route through LiteLLM; hosted routes cost a fraction of a cent.")), h("div", { class: "actions" }, probeBtn)),
    probeOut);

  const ol = d.services.find((x) => x.name === "ollama")?.detail || {};
  const loaded = new Map((ol.loaded || []).map((m) => [m.name, m]));
  const ollamaCard = card("Ollama models", "Resident = loaded now. qwen3:8b overflows the 6 GB card, so partial GPU is normal; 0% on a GPU model means CUDA fallback.",
    table([
      { label: "Model", render: (m) => h("span", { class: "mono" }, m.name) },
      { label: "Params", key: "params" }, { label: "Quant", key: "quant" },
      { label: "Size", key: "size", fmt: fmtBytes, align: "r" },
      { label: "Resident", render: (m) => { const l = loaded.get(m.name); if (!l) return h("span", { class: "muted" }, "no"); const cpu = /-cpu(:|$)/.test(m.name); return badge(cpu || l.gpu_pct >= 50 ? "good" : l.gpu_pct === 0 ? "critical" : "warning", `${cpu ? "CPU by design" : `${l.gpu_pct}% GPU`} · ${l.expires_at && l.expires_at - Date.now() / 1000 < 30 * 86400 ? "unloads " + fmtIn(l.expires_at) : "pinned"}`); } },
    ], ol.installed || [], { empty: "Ollama unreachable or no models pulled." }));

  const qd = d.services.find((x) => x.name === "qdrant")?.detail || {};
  const qdrantCard = card("Qdrant collections", "Dedup store: one point per confirmed finding.",
    table([
      { label: "Collection", render: (c) => h("span", { class: "mono" }, c.name) },
      { label: "Status", render: (c) => badge(c.status === "green" ? "good" : c.status === "yellow" ? "warning" : "critical", c.status) },
      { label: "Points", key: "points", fmt: fmtInt, align: "r" },
      { label: "Vectors", key: "vectors" },
    ], qd.collections || [], { empty: "No collections yet. The orchestrator creates one when it first stores a finding embedding." }));

  return [
    h("p", { class: "lede" }, h("strong", {}, "Is the stack up right now? "), "The dashboard container probes every service every 30 s. Strips show 15-minute cells; history resets when the dashboard restarts."),
    checks,
    h("h2", { class: "section-title" }, "Services"),
    h("div", { class: "grid g-auto" }, svcCards),
    pipeCards.length ? [h("h2", { class: "section-title" }, "Langfuse v4 pipeline"),
      h("p", { class: "lede small" }, "LiteLLM exports over OTLP to Langfuse; the worker drains Redis into ClickHouse and MinIO. If any of these stop, calls still succeed but spend and traces go dark."),
      h("div", { class: "grid g-auto" }, pipeCards)] : null,
    h("h2", { class: "section-title" }, "Models & stores"),
    h("div", { class: "stack" }, routesCard, h("div", { class: "grid g2" }, ollamaCard, qdrantCard)),
  ];
}

// ================================================================= TRENDS

function tile(label, value, hint, hero) {
  return h("div", { class: "tile" + (hero ? " hero" : "") }, h("div", { class: "label" }, label), h("div", { class: "value" }, value), hint ? h("div", { class: "hint" }, hint) : null);
}

function renderTrends(d) {
  const t = d.totals;
  const routes = d.routes;
  const fx = bucketLabel(d.step), tx = bucketTitle(d.step);
  const base = { buckets: d.buckets, fmtX: fx, titleX: tx };

  const judgeP95 = t.p95?.judge;
  const tiles = h("div", { class: "tiles" },
    tile("LLM spend", fmtUSD(t.cost), RANGE_LABEL[d.range], true),
    tile("Calls", fmtInt(t.calls), "generations"),
    tile("Tokens", fmtInt(t.tokens), "input + output"),
    tile("Error rate", fmtPct(t.error_rate, 1), `${fmtInt(t.errors)} errored generations`),
    tile("Judge p95", fmtMs(judgeP95), "frontier verdict latency"),
    tile("Findings", t.findings == null ? "—" : fmtInt(t.findings), d.harness ? "unique, evidenced" : "findings DB not set up"),
    tile("Cost per finding", fmtUSD(t.cost_per_finding), "the number to drive down"));

  const mk = (metric, title, sub, fmt, kind = "columns") => {
    const series = routeSeries(d[metric], routes);
    const draw = (el) => (kind === "lines" ? lines : columns)(el, { ...base, series, fmt, label: title });
    return chartCard({ title, sub, draw, legendItems: series, legendKind: kind === "lines" ? "line" : "rect", tableView: () => seriesTable(d.buckets, d.step, series, fmt) });
  };

  const findSeries = [{ name: "Unique findings", color: "var(--s7)", values: d.findings }];
  const findings = chartCard({
    title: "Unique evidenced findings", sub: d.harness ? `Confirmed by the judge, with a replayable exchange, not a duplicate. ${fmtUSD(t.cost_per_finding)} of LLM spend each.` : "Needs the harness findings DB (db/harness.sql).",
    draw: (el) => columns(el, { ...base, series: findSeries, fmt: fmtInt, emptyText: d.harness ? "No findings in this range" : "Findings DB not set up" }),
    tableView: () => seriesTable(d.buckets, d.step, findSeries, fmtInt),
  });

  return [
    h("p", { class: "lede" }, h("strong", {}, "Is the harness getting cheaper and better? "), "Spend and volume per route tier, latency and errors per route, and the outcome they buy: unique findings. Watch cost per finding fall as the glue tier gets sharper."),
    notices(d.notes),
    tiles,
    h("div", { class: "grid g2" },
      mk("cost", "Spend by route", "USD from Langfuse. Local routes are free; spend should sit on planner and judge.", fmtUSD),
      mk("calls", "Calls by route", "Glue should carry most of the volume.", fmtInt),
      mk("p95", "p95 latency by route", "Per bucket, computed in ClickHouse.", fmtMs, "lines"),
      mk("errors", "Errors by route", "Generations Langfuse marked ERROR: timeouts, 429s, provider faults.", fmtInt),
      mk("tokens", "Tokens by route", "Input + output.", fmtInt),
      findings),
  ];
}

// ================================================================= FLOWS

function hbarList(rows, { fmt = fmtInt, color = "var(--accent)" } = {}) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return h("div", { class: "hbars", style: { display: "grid", gridTemplateColumns: "minmax(120px, auto) 1fr auto", gap: "8px 12px", alignItems: "center" } },
    rows.map((r) => {
      const bar = h("div", { style: { height: "16px", width: `${Math.max(r.value ? 0.5 : 0, (100 * r.value) / max)}%`, background: r.color || color, borderRadius: "0 4px 4px 0" } });
      const track = h("div", { tabindex: 0, "aria-label": `${r.label}: ${fmt(r.value)}${r.note ? ", " + r.note : ""}`, style: { minWidth: 0 } }, bar);
      const show = (e) => showTip(e, r.label, [{ color: r.color || color, value: fmt(r.value), label: r.note || "" }]);
      track.addEventListener("pointermove", show);
      track.addEventListener("focus", show);
      track.addEventListener("pointerleave", hideTip);
      track.addEventListener("blur", hideTip);
      return [h("div", { class: "ink2", style: { fontSize: "13px" } }, r.label), track, h("div", { class: "num", style: { textAlign: "right", fontSize: "13px", whiteSpace: "nowrap" } }, fmt(r.value), r.note ? h("span", { class: "muted small" }, "  " + r.note) : null)];
    }));
}

function renderFlows(d) {
  const routes = d.routes.filter((r) => d.split[r] && (d.split[r].calls || d.split[r].cost || d.split[r].tokens));
  const keys = routes.map((r) => ({ key: r, name: r, color: colorOf(r), ink: inkOn(r) }));
  const val = (m) => Object.fromEntries(routes.map((r) => [r, d.split[r][m]]));
  const flowCard = chartCard({
    title: "Where calls, tokens and spend go",
    sub: "Each row splits the range's total across routes; ribbons follow a route down. A healthy harness is wide blue on top and narrow at the bottom: cheap local calls do the volume, the frontier judge is rare but owns the spend.",
    legendItems: keys, legendKind: "rect",
    draw: (el) => flowRows(el, { label: "Share of calls, tokens and spend by route", keys, rows: [
      { title: "Calls", values: val("calls"), fmt: fmtInt },
      { title: "Tokens", values: val("tokens"), fmt: fmtInt },
      { title: "Spend", values: val("cost"), fmt: fmtUSD }] }),
    tableView: () => {
      const tot = (m) => routes.reduce((a, r) => a + d.split[r][m], 0) || 1;
      return table([
        { label: "Route", render: (r) => routeKey(r, "rect") },
        { label: "Calls", align: "r", render: (r) => `${fmtInt(d.split[r].calls)} · ${fmtPct(d.split[r].calls / tot("calls"))}` },
        { label: "Tokens", align: "r", render: (r) => `${fmtInt(d.split[r].tokens)} · ${fmtPct(d.split[r].tokens / tot("tokens"))}` },
        { label: "Spend", align: "r", render: (r) => `${fmtUSD(d.split[r].cost)} · ${fmtPct(d.split[r].cost / tot("cost"))}` },
      ], routes);
    },
  });

  const hs = d.harness || {};
  let funnelCard, guardCard;
  if (!hs.available) {
    const setup = [h("div", { class: "notice" }, statusIcon("unknown"), h("span", {}, hs.error || "Findings DB unavailable.", " Run ", h("code", {}, "sudo bash scripts/setup-2060.sh"), " to apply ", h("code", {}, "db/harness.sql"), ", then have the orchestrator write runs, operations, probes, exchanges, verdicts and guardrail_events."))];
    funnelCard = card("Agent funnel", "plan → probe → observe → judge → report", ...setup);
    guardCard = card("Guardrails", "Scope allowlist, rate limits, kill switch", h("p", { class: "muted small" }, "Needs the findings DB."));
  } else {
    const f = hs.funnel;
    const stages = [
      ["Operations mapped", f.operations, null],
      ["Probes planned", f.planned, "planner"],
      ["Probes executed", f.executed, "sandbox replay"],
      ["Looked interesting", f.interesting, "glue"],
      ["Judged", f.judged, "judge"],
      ["Confirmed", f.confirmed, null],
      ["With evidence", f.evidenced, "replayable pair"],
      ["Unique findings", f.unique_findings, "Qdrant dedup"],
    ];
    const rows = stages.map(([label, value, who], i) => {
      const prev = i > 1 ? stages[i - 1][1] : null;
      const conv = prev ? `${fmtPct(value / prev, value / prev < 0.1 ? 1 : 0)} of prev` : null;
      return { label, value, note: [who, conv].filter(Boolean).join(" · ") };
    });
    const verdictTotal = f.confirmed + f.rejected + f.needs_review;
    funnelCard = card("Agent funnel", `Each stage of plan → probe → observe → judge → report, ${RANGE_LABEL[d.range]}. Big drops are expected; watch the glue→judge ratio and the evidence gate.`,
      hbarList(rows),
      f.confirmed_no_evidence
        ? h("div", { class: "funnel-note" }, statusIcon("critical"), h("span", {}, h("strong", {}, `${f.confirmed_no_evidence} confirmed without evidence. `), "No evidence, no report: these are blocked from findings until replayed."))
        : h("div", { class: "funnel-note" }, statusIcon("good"), h("span", {}, "Evidence gate clean: every confirmation has a replayable request/response pair.")),
      verdictTotal ? h("div", { class: "funnel-note" }, statusIcon("unknown"), h("span", {}, `Judge verdicts: ${fmtInt(f.confirmed)} confirmed, ${fmtInt(f.needs_review)} need review, ${fmtInt(f.rejected)} rejected (${fmtPct(f.rejected / verdictTotal)} false-positive escalations from glue).`)) : null);

    const g = hs.guardrails;
    const ks = hs.kill_switch;
    guardCard = card("Guardrails", `Scope allowlist, rate limit and kill-switch events, ${RANGE_LABEL[d.range]}.`,
      h("div", { class: "tiles", style: { gridTemplateColumns: "repeat(3, minmax(0, 1fr))" } },
        tile("Scope denied", fmtInt(g.by_kind.scope_denied || 0), "out-of-scope requests dropped"),
        tile("Rate limited", fmtInt(g.by_kind.rate_limited || 0), "per-target ceiling hits"),
        h("div", { class: "tile" }, h("div", { class: "label" }, "Kill switch"), h("div", { class: "value", style: { fontSize: "18px", marginTop: "6px" } }, badge(!ks ? "unknown" : ks.state === "engaged" ? "warning" : "good", ks ? ks.state : "never used")), h("div", { class: "hint" }, ks ? fmtWhen(ks.at) : ""))),
      table([
        { label: "When", key: "t", fmt: fmtWhen, cls: "nw" },
        { label: "Kind", key: "kind" },
        { label: "Host", render: (r) => h("span", { class: "mono" }, r.host || "—") },
        { label: "Detail", key: "detail", cls: "clip", title: (r) => r.detail || "" },
        { label: "Run", render: (r) => h("span", { class: "mono" }, r.run_id || "—") },
      ], g.recent, { empty: "No guardrail events in this range.", cap: 8 }));
  }

  const maxStepCost = Math.max(0, ...d.steps.map((x) => x.cost));
  const stepsCard = card("Steps", "Generations grouped by name, exact from Langfuse metrics. Name each loop step via LiteLLM metadata generation_name. Sorted by spend.",
    table([
      { label: "Step", render: (r) => h("span", { class: "mono" }, r.name) },
      { label: "Route", render: (r) => routeKey(r.route) },
      { label: "Calls", key: "calls", fmt: fmtInt, align: "r" },
      { label: "Spend", align: "r", render: (r) => h("span", {}, fmtUSD(r.cost), " ", h("span", { class: "inline-bar", style: { width: `${maxStepCost ? Math.round((48 * r.cost) / maxStepCost) : 0}px`, background: colorOf(r.route) } })) },
      { label: "Tokens", key: "tokens", fmt: fmtInt, align: "r" },
      { label: "p50", key: "p50", fmt: fmtMs, align: "r" },
      { label: "p95", key: "p95", fmt: fmtMs, align: "r" },
      { label: "Errors", align: "r", render: (r) => (r.errors ? `${fmtInt(r.errors)} · ${fmtPct(r.errors / r.calls, 1)}` : "0") },
    ], d.steps, { empty: "No generations in this range." }));

  const runsCard = card("Runs", "Harness runs joined to Langfuse sessions (pass the run id as LiteLLM metadata session_id). Untracked = traced session with no harness.runs row.",
    table([
      { label: "Run", render: (r) => h("span", { class: "mono" }, r.id) },
      { label: "Target", render: (r) => r.target || h("span", { class: "muted" }, "—") },
      { label: "Status", render: (r) => badge(r.status === "done" ? "good" : r.status === "running" ? "unknown" : r.status === "untracked" ? "unknown" : "warning", r.status) },
      { label: "Started", key: "started_at", fmt: fmtWhen, cls: "nw" },
      { label: "Duration", align: "r", render: (r) => fmtDur(r.started_at, r.ended_at) },
      { label: "Ops", key: "operations", fmt: fmtInt, align: "r" },
      { label: "Probes", key: "probes", fmt: fmtInt, align: "r" },
      { label: "Findings", key: "findings", fmt: fmtInt, align: "r" },
      { label: "LLM calls", key: "calls", fmt: fmtInt, align: "r" },
      { label: "LLM spend", key: "cost", fmt: fmtUSD, align: "r" },
      { label: "Per finding", key: "cost_per_finding", fmt: fmtUSD, align: "r" },
    ], d.runs, { empty: "No runs in this range.", cap: 15 }));

  return [
    h("p", { class: "lede" }, h("strong", {}, "Where does the work and money go? "), "Route tiers from glue to judge, the agent loop's funnel from mapped operations to unique findings, and the guardrails around it."),
    notices(d.notes),
    h("div", { class: "stack" }, flowCard, h("div", { class: "grid g2" }, funnelCard, guardCard), stepsCard, runsCard),
  ];
}

// ================================================================= RAW

const RAW_TABS = [["generations", "Generations"], ["traces", "Traces"], ["tables", "Harness tables"], ["qdrant", "Qdrant"], ["sql", "SQL"]];
const SQL_EXAMPLES = [
  ["Findings by severity", "select severity, count(*) from harness.verdicts\nwhere label = 'confirmed' and evidence_ref is not null and not is_duplicate\ngroup by 1 order by 2 desc"],
  ["Interesting rate per run", "select o.run_id, count(*) as probes,\n  round(100.0 * avg(p.interesting::int), 1) as pct_interesting\nfrom harness.probes p join harness.operations o on o.id = p.op_id\ngroup by 1 order by 1 desc limit 20"],
  ["Unevidenced confirmations", "select v.id, v.judged_at, v.severity, o.method, o.path, p.identity, p.tenant\nfrom harness.verdicts v\njoin harness.probes p on p.id = v.probe_id\njoin harness.operations o on o.id = p.op_id\nwhere v.label = 'confirmed' and v.evidence_ref is null\norder by v.judged_at desc"],
  ["Top denied hosts", "select host, count(*) from harness.guardrail_events\nwhere kind = 'scope_denied' group by 1 order by 2 desc limit 20"],
  ["Paths with most confirmations", "select o.method, o.path, count(*) as confirmed\nfrom harness.verdicts v\njoin harness.probes p on p.id = v.probe_id\njoin harness.operations o on o.id = p.op_id\nwhere v.label = 'confirmed' group by 1, 2 order by 3 desc limit 20"],
];

function openDrawer(title, obj) {
  $("#drawer-title").textContent = title;
  $("#drawer-body").textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  $("#drawer").hidden = false;
  $("#drawer-close").focus();
}

function pager(page, pages, go, more) {
  const hasNext = more ?? page < pages;
  return h("div", { class: "pager" },
    h("span", {}, pages ? `Page ${page} of ${fmtInt(pages)}` : `Page ${page}`),
    h("button", { class: "btn", disabled: page <= 1, onclick: () => go(page - 1) }, "Previous"),
    h("button", { class: "btn", disabled: !hasNext, onclick: () => go(page + 1) }, "Next"));
}

const ext = (url) => (url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation() }, "Langfuse ↗") : "—");

async function renderRaw() {
  const sub = h("div", { class: "subnav", role: "toolbar", "aria-label": "Raw data source" }, RAW_TABS.map(([k, label]) =>
    h("button", { "aria-pressed": String(state.raw === k), onclick: () => { state.raw = k; state.page = 1; state.cursors = [null]; writeHash(); render(); } }, label)));
  const out = [h("p", { class: "lede" }, h("strong", {}, "The records behind every chart. "), "Read-only. Click a row for the full record. Long bodies are trimmed; Langfuse holds the originals."), sub];
  const range = state.range;
  const go = (p) => { state.page = p; render(); };
  // Langfuse v2 pages by cursor: remember the cursor that opens each page
  state.cursors = state.cursors || [null];
  const cur = state.cursors[state.page - 1] ?? null;
  const cq = cur ? `&cursor=${encodeURIComponent(cur)}` : "";
  const cursorPager = (d) => { state.cursors[state.page] = d.next; return pager(state.page, null, go, d.next != null); };
  const emptyPage = (d) => (d.next ? "No rows on this page; Langfuse pages can be sparse. Try Next." : "Nothing in this range.");

  if (state.raw === "generations") {
    const d = await api(`/api/raw/generations?range=${range}${cq}`);
    out.push(card(`Generations · ${RANGE_LABEL[range]}`, "Every LLM call LiteLLM exported to Langfuse, newest first, 50 per page.",
      table([
        { label: "Time", key: "ts", fmt: fmtWhen, cls: "nw" },
        { label: "Step", render: (r) => h("span", { class: "mono" }, r.name) },
        { label: "Route", render: (r) => routeKey(r.route) },
        { label: "Model", render: (r) => h("span", { class: "mono" }, r.model) },
        { label: "Latency", key: "latency_ms", fmt: fmtMs, align: "r" },
        { label: "In", key: "in_tok", fmt: fmtInt, align: "r" },
        { label: "Out", key: "out_tok", fmt: fmtInt, align: "r" },
        { label: "Cost", key: "cost", fmt: fmtUSD, align: "r" },
        { label: "Level", render: (r) => (r.level === "ERROR" ? badge("critical", "error") : r.level === "WARNING" ? badge("warning", "warning") : h("span", { class: "muted" }, "ok")) },
        { label: "Input", key: "input", cls: "clip", title: (r) => r.input },
        { label: "Output", key: "output", cls: "clip", title: (r) => r.output || r.status || "" },
        { label: "", render: (r) => ext(r.link) },
      ], d.rows, { onRow: (r) => openDrawer(`Generation ${r.id}`, r.raw), empty: emptyPage(d) }),
      cursorPager(d)));
  } else if (state.raw === "traces") {
    const d = await api(`/api/raw/traces?range=${range}${cq}`);
    out.push(card(`Traces · ${RANGE_LABEL[range]}`, "Root observation of each trace (Langfuse v4 has no trace list). Session = harness run id.",
      table([
        { label: "Time", key: "ts", fmt: fmtWhen, cls: "nw" },
        { label: "Name", render: (r) => h("span", { class: "mono" }, r.name || "—") },
        { label: "Session", render: (r) => h("span", { class: "mono" }, r.session_id || "—") },
        { label: "Type", key: "type" },
        { label: "Latency", key: "latency_ms", fmt: fmtMs, align: "r" },
        { label: "Cost", key: "cost", fmt: fmtUSD, align: "r" },
        { label: "Input", key: "input", cls: "clip", title: (r) => r.input },
        { label: "", render: (r) => ext(r.link) },
      ], d.rows, { onRow: (r) => openDrawer(`Trace ${r.id}`, r.raw), empty: emptyPage(d) }),
      cursorPager(d)));
  } else if (state.raw === "tables") {
    const t = await api("/api/raw/tables");
    if (!t.available) {
      out.push(card("Harness tables", null, h("div", { class: "notice" }, statusIcon("unknown"), h("span", {}, t.error))));
    } else {
      if (!state.table) state.table = t.tables[0].name;
      const d = await api(`/api/raw/table/${encodeURIComponent(state.table)}?page=${state.page}`);
      const cols = d.columns.map((c) => ({ label: c, cls: "clip", render: (r) => { const v = r[c]; return v == null ? h("span", { class: "muted" }, "null") : typeof v === "object" ? JSON.stringify(v) : String(v); }, title: (r) => (r[c] == null ? "" : typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c])) }));
      out.push(h("div", { class: "raw-split" },
        h("div", { class: "card" }, h("nav", { class: "list-nav", "aria-label": "Tables" }, t.tables.map((x) =>
          h("button", { "aria-current": String(x.name === state.table), onclick: () => { state.table = x.name; state.page = 1; state.cursors = [null]; render(); } }, h("span", { class: "mono" }, x.name), h("span", { class: "muted num" }, fmtInt(x.rows)))))),
        card(`harness.${state.table}`, "Newest first, 50 per page.",
          table(cols, d.rows, { onRow: (r) => openDrawer(`harness.${state.table}`, r), empty: "Empty table." }),
          pager(d.page, null, go, d.more))));
    }
  } else if (state.raw === "qdrant") {
    const c = await api("/api/raw/qdrant");
    if (!c.collections.length) {
      out.push(card("Qdrant", null, h("p", { class: "muted small" }, "No collections yet.")));
    } else {
      if (!state.collection || !c.collections.some((x) => x.name === state.collection)) state.collection = c.collections[0].name;
      state.qOffsets = state.qOffsets || [null];
      const off = state.qOffsets[state.page - 1] ?? null;
      const d = await api(`/api/raw/qdrant/${encodeURIComponent(state.collection)}${off != null ? `?offset=${encodeURIComponent(off)}` : ""}`);
      state.qOffsets[state.page] = d.next;
      out.push(h("div", { class: "raw-split" },
        h("div", { class: "card" }, h("nav", { class: "list-nav", "aria-label": "Collections" }, c.collections.map((x) =>
          h("button", { "aria-current": String(x.name === state.collection), onclick: () => { state.collection = x.name; state.page = 1; state.cursors = [null]; state.qOffsets = [null]; render(); } }, h("span", { class: "mono" }, x.name), h("span", { class: "muted num" }, fmtInt(x.points)))))),
        card(state.collection, "Payloads only; vectors omitted.",
          table([
            { label: "Point id", render: (r) => h("span", { class: "mono" }, String(r.id)) },
            { label: "Payload", cls: "clip", render: (r) => h("span", { class: "mono" }, JSON.stringify(r.payload)), title: (r) => JSON.stringify(r.payload) },
          ], d.points, { onRow: (r) => openDrawer(`Point ${r.id}`, r) }),
          pager(state.page, null, go, d.next != null))));
    }
  } else if (state.raw === "sql") {
    const ta = h("textarea", { class: "sql", spellcheck: "false", "aria-label": "SQL query" });
    ta.value = state.sql || SQL_EXAMPLES[0][1];
    const res = h("div", {});
    const run = async () => {
      state.sql = ta.value;
      res.replaceChildren(h("p", { class: "muted small" }, "Running…"));
      try {
        const r = await api("/api/raw/sql", { method: "POST", body: JSON.stringify({ sql: ta.value }) });
        const cols = r.columns.map((c, i) => ({ label: c, cls: "clip", render: (row) => (row[i] == null ? h("span", { class: "muted" }, "null") : typeof row[i] === "object" ? JSON.stringify(row[i]) : String(row[i])), title: (row) => (row[i] == null ? "" : String(typeof row[i] === "object" ? JSON.stringify(row[i]) : row[i])) }));
        res.replaceChildren(
          h("p", { class: "muted small" }, r.status ? r.status : `${fmtInt(r.rows.length)} rows${r.truncated ? " (first 500)" : ""} · ${fmtMs(r.ms)}`),
          table(cols, r.rows, { onRow: (row) => openDrawer("Row", Object.fromEntries(r.columns.map((c, i) => [c, row[i]]))), empty: "No rows." }));
      } catch (e) {
        res.replaceChildren(h("p", { class: "error small" }, e.message));
      }
    };
    ta.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); run(); } });
    out.push(card("SQL", "Read-only transaction as dashboard_ro (SELECT on schema harness only), 5 s timeout, 500 rows. ⌘/Ctrl+Enter runs.",
      h("div", { class: "chips" }, SQL_EXAMPLES.map(([label, q]) => h("button", { class: "chip", onclick: () => { ta.value = q; run(); } }, label))),
      ta,
      h("div", { style: { display: "flex", justifyContent: "flex-end", margin: "8px 0 12px" } }, h("button", { class: "btn primary", onclick: run }, "Run")),
      res));
  }
  return out;
}

// ================================================================= render loop

let seq = 0;
async function render() {
  const my = ++seq;
  for (const t of TABS) {
    $(`#tab-${t}`).hidden = t !== state.tab;
    document.querySelector(`.tabs [data-tab="${t}"]`).setAttribute("aria-selected", String(t === state.tab));
  }
  $("#range").hidden = state.tab === "health";
  for (const b of $("#range").children) b.setAttribute("aria-checked", String(b.dataset.range === state.range));
  const sec = $(`#tab-${state.tab}`);
  $("#main").classList.add("loading");
  try {
    let nodes;
    if (state.tab === "health") nodes = renderHealth(await api("/api/health"));
    else if (state.tab === "trends") nodes = renderTrends(await api(`/api/trends?range=${state.range}`));
    else if (state.tab === "flows") nodes = renderFlows(await api(`/api/flows?range=${state.range}`));
    else nodes = await renderRaw();
    if (my !== seq) return;
    for (const el of sec.querySelectorAll(".chart")) { ro.unobserve(el); drawers.delete(el); }
    sec.replaceChildren(...[nodes].flat(Infinity).filter(Boolean));
    $("#updated").textContent = `Updated ${tf.format(new Date())}`;
  } catch (e) {
    if (my !== seq) return;
    sec.replaceChildren(h("div", { class: "notice" }, statusIcon("critical"), h("span", { class: "error" }, `Could not load: ${e.message}`)));
  } finally {
    if (my === seq) $("#main").classList.remove("loading");
  }
  if (state.tab !== "health") api("/api/health").then((d) => updatePill(d.services)).catch(() => {});
}

function schedule() {
  clearInterval(state.timer);
  if ($("#auto").checked) state.timer = setInterval(() => { if (state.tab !== "raw" && document.visibilityState === "visible") render(); }, 30000);
}

async function init() {
  readHash();
  try {
    state.meta = await api("/api/meta");
    ROUTES = state.meta.routes;
    $("#demo-banner").hidden = !state.meta.demo;
  } catch { /* render() will surface the error */ }
  const seg = $("#range");
  for (const r of ["24h", "7d", "30d", "90d"]) {
    seg.append(h("button", { role: "radio", "data-range": r, onclick: () => { state.range = r; state.page = 1; state.cursors = [null]; state.qOffsets = [null]; writeHash(); render(); } }, r));
  }
  for (const b of document.querySelectorAll(".tabs button")) {
    b.addEventListener("click", () => { state.tab = b.dataset.tab; state.page = 1; state.cursors = [null]; writeHash(); render(); });
  }
  $("#refresh").addEventListener("click", render);
  $("#auto").addEventListener("change", schedule);
  $("#drawer-close").addEventListener("click", () => { $("#drawer").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drawer").hidden = true; });
  window.addEventListener("hashchange", () => { readHash(); render(); });
  writeHash();
  schedule();
  render();
}

init();
