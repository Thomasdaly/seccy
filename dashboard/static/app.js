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
const dfUTC = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
const dfw = new Intl.DateTimeFormat(undefined, { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" });
const tf = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const fmtWhen = (t) => (t ? dtf.format(new Date(t * 1000)) : "—");
const fmtDur = (a, b) => {
  if (!a || !b) return "—";
  const d = b - a;
  return d < 3600 ? `${Math.max(1, Math.round(d / 60))} min` : `${(d / 3600).toFixed(1)} h`;
};
const bucketLabel = (step) => (t) => (step < 86400 ? tf.format(new Date(t * 1000)) : dfUTC.format(new Date(t * 1000)));
const bucketTitle = (step) => (t) => {
  const d = new Date(t * 1000);
  if (step < 86400) return `${df.format(d)}, ${tf.format(d)}–${tf.format(new Date((t + step) * 1000))}`;
  if (step === 86400) return dfw.format(d) + " (UTC day)";
  return `Week of ${dfUTC.format(d)} (UTC)`;
};
const RANGE_LABEL = { "24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days", "90d": "last 13 weeks" };

// ---------------------------------------------------------------- colour
let ROUTES = ["glue", "planner", "judge", "embed", "other"];
const slot = (route) => {
  const i = ROUTES.indexOf(route);
  return route === "other" || i < 0 ? 0 : (i % 8) + 1;
};
const colorOf = (route) => (slot(route) ? `var(--s${slot(route)})` : "var(--s-other)");
const inkOn = (route) => (slot(route) === 4 ? "#16251f" : "var(--flow-label)");
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
const state = { tab: "health", range: "7d", raw: "generations", page: 1, table: null, collection: null, cache: {}, timer: null, sql: "" };
const VIEWS = {
  health: ["Stack health", "Service availability, stack checks and disk capacity."],
  trends: ["Usage trends", "Spend, volume, errors and latency across model routes."],
  flows: ["Workflow activity", "Route usage, run activity and guardrail events."],
  findings: ["Findings", "Review verdicts, evidence references and ground-truth matches."],
  eval: ["Lab evaluation", "Recall, false positives and coverage across scored runs."],
  raw: ["Raw data", "Browse traces, generations, database records and vector-store metadata."],
};
const TABS = Object.keys(VIEWS);
const tabButtons = [...document.querySelectorAll(".tabs button")];

function readHash() {
  const [tab, q] = location.hash.replace(/^#\/?/, "").split("?");
  state.tab = TABS.includes(tab) ? tab : "health";
  const p = new URLSearchParams(q || "");
  state.range = Object.hasOwn(RANGE_LABEL, p.get("range")) ? p.get("range") : "7d";
  state.raw = RAW_TABS.some(([key]) => key === p.get("raw")) ? p.get("raw") : "generations";
  state.run = p.get("run") || null;
}
function writeHash({ replace = false } = {}) {
  const q = new URLSearchParams({ range: state.range });
  if (state.tab === "raw") q.set("raw", state.raw);
  if (state.run) q.set("run", state.run);
  const hash = `#/${state.tab}?${q}`;
  if (hash !== location.hash) history[replace ? "replaceState" : "pushState"](null, "", hash);
}

async function api(path, opts) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  let j = null;
  try { j = await r.json(); } catch { /* non-JSON */ }
  if (!r.ok) throw new Error(j?.error || (typeof j?.detail === "string" ? j.detail : JSON.stringify(j?.detail)) || `HTTP ${r.status}`);
  return j;
}

// ---------------------------------------------------------------- charts mount + resize
const drawers = new WeakMap();
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
    btn.addEventListener("click", () => {
      const all = table(cols, rows, { onRow });
      wrap.replaceChildren(all);
      (all.querySelectorAll(".row-action")[cap] || all).focus();
    });
    wrap.append(btn);
    return wrap;
  }
  return h("div", { class: "table-wrap", tabindex: 0, role: "group", "aria-label": "Scrollable data table" }, h("table", { class: "data" },
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", { scope: "col", class: c.align === "r" ? "r" : null }, c.label || h("span", { class: "sr-only" }, "Links"))))),
    h("tbody", {}, rows.map((r) => {
      return h("tr", onRow ? { class: "clickable", onclick: () => onRow(r) } : {}, cols.map((c, index) => {
        const value = (c.render ? c.render(r) : c.fmt ? c.fmt(r[c.key]) : r[c.key]) ?? "—";
        const content = onRow && index === 0
          ? h("button", { type: "button", class: "row-action", onclick: (event) => { event.stopPropagation(); onRow(r); } },
            value, h("span", { class: "sr-only" }, " — View details"))
          : value;
        return h("td", { class: [c.align === "r" ? "r" : "", c.cls || ""].join(" ").trim() || null, title: c.title ? c.title(r) : null }, content);
      }));
    }))));
}

function chartCard({ title, sub, draw, tableView, legendItems, legendKind, cls, heading = "h2" }) {
  const body = mount(h("div", { class: "chart" }), draw);
  const tbl = h("div", { hidden: true });
  const btn = h("button", { class: "link-btn", "aria-pressed": "false", "aria-label": `Table for ${title}` }, "Table");
  btn.addEventListener("click", () => {
    const on = tbl.hidden;
    tbl.hidden = !on;
    body.hidden = on;
    btn.setAttribute("aria-pressed", String(on));
    if (on) tbl.replaceChildren(tableView());
    else drawers.get(body)?.();
  });
  return h("div", { class: "card" + (cls ? " " + cls : "") },
    h("div", { class: "card-head" }, h("div", {}, h(heading, {}, title), sub ? h("p", { class: "sub" }, sub) : null), tableView ? h("div", { class: "actions" }, btn) : null),
    legendItems ? legend(legendItems, legendKind) : null, body, tbl);
}

function card(title, sub, ...kids) {
  return h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", {}, h("h2", {}, title), sub ? h("p", { class: "sub" }, sub) : null)), ...kids);
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

function updatePill({ services, at }) {
  const down = services.filter((service) => service.ok === false).length;
  const degraded = services.some((service) => ["warning", "serious"].includes(service.status));
  const worst = down ? "critical" : degraded ? "warning" : services.length ? "good" : "unknown";
  const label = { good: "Services up", critical: `${down} down`, warning: "Degraded", unknown: "No service data" }[worst];
  const pill = $("#stack-pill");
  pill.hidden = false;
  pill.title = at ? `Service probes at ${fmtWhen(at)}` : "Service probes";
  pill.replaceChildren(statusIcon(worst), label);
}

const CHECK_PRIORITY = { critical: 0, serious: 1, warning: 2, unknown: 3, good: 4 };

function renderHealth(d) {
  const orderedChecks = [...d.checks].sort((a, b) => CHECK_PRIORITY[a.status] - CHECK_PRIORITY[b.status]);
  const checks = card("Stack checks", "Failures and warnings appear first.",
    h("div", { class: "checks" }, orderedChecks.map((c) => h("div", { class: "check", "data-tone": c.status },
      statusIcon(c.status), h("span", { class: "name" }, h("span", { class: "sr-only" }, `${STATUS_LABEL[c.status]} — `), c.label), h("span", { class: "val" }, c.value),
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

  const up = d.services.filter((service) => service.ok).length;
  const degraded = d.services.filter((service) => service.status === "warning").length;
  const failures = orderedChecks.filter((check) => check.status === "critical").length;
  const warnings = orderedChecks.filter((check) => ["warning", "serious"].includes(check.status)).length;
  const unknown = orderedChecks.filter((check) => check.status === "unknown").length;
  const langfuse = d.services.find((service) => service.name === "langfuse" && service.ok)?.detail;
  const spend = langfuse?.cost_24h;
  const summary = h("section", { class: "tiles health-summary", "aria-label": "Health summary" },
    tile("Services reachable", d.services.length ? `${up} / ${d.services.length}` : "—",
      d.services.length ? `${d.services.length - up} down · ${degraded} degraded` : "No service probes", false,
      up < d.services.length ? "critical" : degraded ? "warning" : up ? "good" : null),
    tile("Checks needing attention", fmtInt(failures + warnings),
      `${failures} failing · ${warnings} warning · ${unknown} unknown`, false, failures ? "critical" : warnings ? "warning" : null),
    tile("LLM spend · 24h", fmtUSD(spend),
      spend == null ? "Langfuse data unavailable" : `${fmtInt(langfuse.gens_24h)} traced calls`),
    tile("Docker disk free", `${dk.free_pct.toFixed(0)}%`, `${fmtBytes(dk.free)} of ${fmtBytes(dk.total)}`, false, diskStatus));

  return [
    summary,
    checks,
    h("h2", { class: "section-title" }, "Services"),
    h("p", { class: "lede small" }, "Probes run every 30 seconds. Uptime strips show 15-minute intervals and reset when the dashboard restarts."),
    h("div", { class: "grid g-auto" }, svcCards),
    pipeCards.length ? [h("h2", { class: "section-title" }, "Langfuse v4 pipeline"),
      h("p", { class: "lede small" }, "LiteLLM exports over OTLP to Langfuse; the worker drains Redis into ClickHouse and MinIO. If any of these stop, calls still succeed but spend and traces go dark."),
      h("div", { class: "grid g-auto" }, pipeCards)] : null,
    h("h2", { class: "section-title" }, "Models & stores"),
    h("div", { class: "stack" }, routesCard, h("div", { class: "grid g2" }, ollamaCard, qdrantCard)),
  ];
}

// ================================================================= TRENDS

function tile(label, value, hint, hero, tone) {
  return h("div", { class: "tile" + (hero ? " hero" : ""), "data-tone": tone },
    h("div", { class: "label" }, label), h("div", { class: "value num" }, value), hint ? h("div", { class: "hint" }, hint) : null);
}

function renderTrends(d) {
  const t = d.totals;
  const routes = d.routes;
  const fx = bucketLabel(d.step), tx = bucketTitle(d.step);
  const base = { buckets: d.buckets, fmtX: fx, titleX: tx };
  const usageAvailable = !d.notes?.length;
  const usage = (value, format) => usageAvailable ? format(value) : "—";
  const usageHint = (hint) => usageAvailable ? hint : "Usage data unavailable";
  const costPerThousand = t.calls > 0 ? `${fmtUSD(1000 * t.cost / t.calls)} per 1,000 calls` : "No traced calls";

  const judgeP95 = t.p95?.judge;
  const tiles = h("div", { class: "tiles" },
    tile("LLM spend", usage(t.cost, fmtUSD), usageHint(costPerThousand), true),
    tile("Calls", usage(t.calls, fmtInt), usageHint("Traced generations")),
    tile("Tokens", usage(t.tokens, fmtInt), usageHint("Input + output")),
    tile("Error rate", usage(t.error_rate, (value) => fmtPct(value, 1)), usageHint(`${fmtInt(t.errors)} errored generations`)),
    tile("Judge p95", usage(judgeP95, fmtMs), usageHint("Frontier verdict latency")),
    tile("Findings", t.findings == null ? "—" : fmtInt(t.findings), d.harness ? "unique, evidenced" : "findings DB not set up"),
    tile("Cost per finding", usage(t.cost_per_finding, fmtUSD), usageHint("Spend ÷ unique evidenced findings")));

  const mk = (metric, title, sub, fmt, kind = "columns") => {
    if (!usageAvailable) return card(title, sub, h("p", { class: "muted small" }, "Usage data unavailable."));
    const series = routeSeries(d[metric], routes);
    const draw = (el) => (kind === "lines" ? lines : columns)(el, { ...base, series, fmt, label: title });
    return chartCard({ title, sub, draw, legendItems: series, legendKind: kind === "lines" ? "line" : "rect", tableView: () => seriesTable(d.buckets, d.step, series, fmt) });
  };

  const findSeries = [{ name: "Unique findings", color: "var(--s7)", values: d.findings }];
  const findings = chartCard({
    title: "Unique evidenced findings", sub: d.harness ? "Confirmed verdicts with evidence, excluding duplicates." : "Findings database unavailable.",
    draw: (el) => columns(el, { ...base, series: findSeries, fmt: fmtInt, label: "Unique evidenced findings", emptyText: d.harness ? "No findings in this range" : "Findings database unavailable" }),
    tableView: d.harness ? () => seriesTable(d.buckets, d.step, findSeries, fmtInt) : null,
  });

  return [
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
      const track = h("div", { "aria-hidden": "true", style: { minWidth: 0 } }, bar);
      const show = (e) => showTip(e, r.label, [{ color: r.color || color, value: fmt(r.value), label: r.note || "" }]);
      track.addEventListener("pointermove", show);
      track.addEventListener("pointerleave", hideTip);
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
      verdictTotal ? h("div", { class: "funnel-note" }, statusIcon("unknown"), h("span", {}, `Judge verdicts: ${fmtInt(f.confirmed)} confirmed, ${fmtInt(f.needs_review)} need review, ${fmtInt(f.rejected)} rejected (${fmtPct(f.rejected / verdictTotal)} of verdicts).`)) : null);

    const g = hs.guardrails;
    const ks = hs.kill_switch;
    guardCard = card("Guardrails", `Scope allowlist, rate limit and kill-switch events, ${RANGE_LABEL[d.range]}.`,
      h("div", { class: "tiles", style: { gridTemplateColumns: "repeat(3, minmax(0, 1fr))" } },
        tile("Scope denied", fmtInt(g.by_kind.scope_denied || 0), "out-of-scope requests dropped"),
        tile("Rate limited", fmtInt(g.by_kind.rate_limited || 0), "per-target ceiling hits"),
        h("div", { class: "tile" }, h("div", { class: "label" }, "Shared stop"), h("div", { class: "value", style: { fontSize: "18px", marginTop: "6px" } }, badge(!ks ? "unknown" : ks.state === "engaged" ? "warning" : "good", ks ? ks.state : "unavailable")), h("div", { class: "hint" }, ks ? `Checked ${fmtWhen(ks.at)}` : ""))),
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
    ], d.runs, { onRow: (r) => (r.tracked ? openRun(r.id) : openDrawer(`Session ${r.id}`, r)), empty: "No runs in this range.", cap: 15 }));

  return [
    notices(d.notes),
    h("div", { class: "stack" }, flowCard, h("div", { class: "grid g2" }, funnelCard, guardCard), stepsCard, runsCard),
  ];
}

// ================================================================= shared: verdicts, runs

const OUTCOME = {
  tp: ["good", "true positive", "Matches a known authz bug in the ground truth."],
  fp: ["critical", "false positive", "Flagged a negative endpoint, or a flag-gated bug on the secure control twin."],
  other: ["unknown", "non-authz bug", "Matches a real bug outside an authz tester's remit (SQLi, SSRF, …). Not counted."],
  unmatched: ["warning", "not in ground truth", "Novel or false: a human needs to triage it."],
};
const SEV_ORDER = ["critical", "high", "medium", "low", "info", "unrated"];
const SEV_COLOR = { critical: "var(--critical)", high: "var(--serious)", medium: "var(--warning)", low: "var(--s1)", info: "var(--s-other)", unrated: "var(--s-other)" };

const endpoint = (r) => h("span", { class: "mono" }, h("strong", {}, r.method || "?"), " ", r.path || "");
const gtCell = (r) => {
  if (!r.lab) return h("span", { class: "muted" }, "—");
  if (!r.outcome) return h("span", { class: "muted" }, r.lab);
  const [st, label] = OUTCOME[r.outcome];
  const id = r.outcome === "tp" || r.outcome === "other" ? r.gt_id : null;
  return h("span", { class: "cell-key", title: `${label}${r.gt_id ? ` · ${r.gt_id}` : ""} (${r.lab} ${r.twin} twin)` },
    statusIcon(st), id ? h("span", { class: "mono" }, id) : h("span", { class: "ink2" }, label));
};
const labelBadge = (r) => {
  if (r.label === "needs_review") return badge("warning", "needs review");
  if (r.label === "rejected") return badge("unknown", "rejected");
  if (!r.evidence_ref) return badge("critical", "no evidence");
  if (r.is_duplicate) return badge("unknown", "duplicate");
  return badge("good", "finding");
};
const sevCell = (r) => (r.severity ? h("span", { class: "cell-key" }, h("span", { class: "key-dot", style: { background: SEV_COLOR[r.severity.toLowerCase()] || "var(--s-other)" } }), r.severity) : h("span", { class: "muted" }, "—"));
const linkTo = (url, text) => (url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation() }, text) : null);

function drawerSection(title, ...kids) {
  return h("section", { class: "d-sec" }, h("h3", {}, title), ...kids);
}

function openFinding(r) {
  runRequest?.abort();
  if (state.run) { state.run = null; writeHash({ replace: true }); }
  const body = h("div", {},
    h("div", { class: "d-badges" }, labelBadge(r), r.lab ? gtCell(r) : null),
    drawerSection("Where", kv([["Endpoint", `${r.method} ${r.path}`], ["Target", r.target], ["Run", r.run_id], ["Run status", r.run_status]]),
      r.run_id ? h("button", { class: "btn", onclick: () => openRun(r.run_id) }, "Open run") : null),
    drawerSection("Probe", kv([["Identity", r.identity], ["Tenant", r.tenant], ["Role", r.role], ["Intent", r.intent], ["Probe id", r.probe_id]])),
    drawerSection("Verdict", kv([["Label", r.label], ["Severity", r.severity], ["Judged", fmtWhen(r.judged_at)], ["Duplicate", r.is_duplicate ? "yes (Qdrant similarity hit)" : "no"], ["Verdict id", r.id]]),
      linkTo(r.trace_link, "Judge trace in Langfuse ↗")),
    drawerSection("Evidence", r.evidence_ref
      ? kv([["Replayable pair", r.evidence_ref], ["Last status", r.status_code], ["Request", r.request_blob], ["Response", r.response_blob]])
      : h("div", { class: "notice" }, statusIcon("critical"), h("span", {}, "No evidence_ref. No evidence, no report: this can't become a finding until the exchange is replayed and stored."))),
    r.outcome ? drawerSection("Ground truth", h("p", { class: "small ink2" }, OUTCOME[r.outcome][2]), kv([["Lab", `${r.lab} (${r.twin} twin)`], ["Entry", r.gt_id], ["Category", r.gt_category]])) : null);
  openDrawer(`Verdict ${r.id} · ${r.method} ${r.path}`, body);
}

let runRequest;
async function openRun(id) {
  runRequest?.abort();
  const request = new AbortController();
  runRequest = request;
  state.run = id;
  writeHash({ replace: true }); // include the run in a shared URL
  openDrawer(`Run ${id}`, h("p", { class: "muted small drawer-message", role: "status" }, "Loading run…"));
  let d;
  try {
    d = await api(`/api/run/${encodeURIComponent(id)}`, { signal: request.signal });
    request.signal.throwIfAborted();
  } catch (e) {
    if (request.signal.aborted || !$("#drawer").open) return;
    openDrawer(`Run ${id}`, h("div", { class: "notice" }, statusIcon("critical"), h("span", { class: "error" }, e.message)));
    return;
  }
  const r = d.run, sc = d.score;
  const verdicts = d.verdicts;
  const vc = (pred) => verdicts.filter(pred).length;
  const body = h("div", {},
    notices(d.notes),
    h("div", { class: "tiles d-tiles" },
      tile("Status", badge(r.status === "done" ? "good" : r.status === "running" ? "unknown" : "warning", r.status), fmtDur(r.started_at, r.ended_at)),
      tile("LLM spend", fmtUSD(r.cost), `${fmtInt(r.calls)} calls`),
      tile("Findings", fmtInt(r.findings), r.cost_per_finding ? `${fmtUSD(r.cost_per_finding)} each` : "unique, evidenced"),
      sc && sc.recall != null ? tile("Recall", fmtPct(sc.recall), `${sc.hit} of ${sc.of} authz bugs`) : null,
      sc ? tile("False positives", fmtInt(sc.counts.fp), `${sc.lab} ${sc.twin} twin`) : null),
    drawerSection("Run", kv([["Target", r.target], ["Program", r.program], ["Started", fmtWhen(r.started_at)], ["Ended", r.ended_at ? fmtWhen(r.ended_at) : "—"]]),
      linkTo(r.session_link, "Session in Langfuse ↗")),
    drawerSection("Funnel", hbarList([
      { label: "Operations", value: r.operations || 0 },
      { label: "Probes", value: r.probes || 0 },
      { label: "Executed", value: r.executed || 0 },
      { label: "Interesting", value: r.interesting || 0 },
      { label: "Judged", value: verdicts.length },
      { label: "Confirmed", value: vc((v) => v.label === "confirmed") },
      { label: "Findings", value: r.findings || 0 },
    ])),
    sc ? drawerSection(`Ground truth · ${sc.lab} (${sc.twin})`,
      sc.missed?.length ? h("div", {}, h("p", { class: "small ink2" }, `Missed ${sc.missed.length}:`), h("ul", { class: "d-list" }, sc.missed.map((f) => h("li", {}, h("span", { class: "mono" }, f.id), h("span", { class: "muted" }, ` ${f.endpoint}`))))) : sc.of ? h("p", { class: "small ink2" }, "Every scorable authz bug found.") : null,
      Object.keys(sc.fps).length ? h("div", {}, h("p", { class: "small ink2" }, "False positives:"), h("ul", { class: "d-list" }, Object.entries(sc.fps).map(([k, n]) => h("li", {}, h("span", { class: "mono" }, k), h("span", { class: "muted" }, ` ×${n}`))))) : h("p", { class: "small ink2" }, "No false positives."),
      sc.unscorable?.length ? h("p", { class: "small muted" }, `Not auto-scorable (no fixed endpoint): ${sc.unscorable.map((f) => f.id).join(", ")}.`) : null) : null,
    drawerSection("Spend by step", table([
      { label: "Step", render: (x) => h("span", { class: "mono" }, x.name) },
      { label: "Route", render: (x) => routeKey(x.route) },
      { label: "Calls", key: "calls", fmt: fmtInt, align: "r" },
      { label: "Tokens", key: "tokens", fmt: fmtInt, align: "r" },
      { label: "Spend", key: "cost", fmt: fmtUSD, align: "r" },
    ], d.steps, { empty: "No traced calls for this run (pass the run id as LiteLLM metadata session_id)." })),
    drawerSection(`Verdicts (${verdicts.length})`, table([
      { label: "Status", render: labelBadge },
      { label: "Endpoint", render: endpoint },
      { label: "Sev", render: sevCell },
      { label: "Ground truth", render: gtCell },
    ], verdicts, { onRow: openFinding, empty: "No verdicts.", cap: 25 })),
    d.guardrails.length ? drawerSection("Guardrail events", table([
      { label: "When", key: "t", fmt: fmtWhen, cls: "nw" },
      { label: "Kind", key: "kind" },
      { label: "Host", render: (x) => h("span", { class: "mono" }, x.host || "—") },
      { label: "Detail", key: "detail", cls: "clip", title: (x) => x.detail || "" },
    ], d.guardrails, { cap: 10 })) : null);
  openDrawer(`Run ${id}`, body);
}

// ================================================================= FINDINGS

const FILTERS = [
  ["findings", "Findings", (r) => r.finding],
  ["review", "Needs review", (r) => r.label === "needs_review"],
  ["noevidence", "No evidence", (r) => r.label === "confirmed" && !r.evidence_ref],
  ["dups", "Duplicates", (r) => r.label === "confirmed" && r.is_duplicate],
  ["fp", "Lab false positives", (r) => r.outcome === "fp"],
  ["unmatched", "Not in ground truth", (r) => r.outcome === "unmatched"],
  ["all", "Everything", () => true],
];

const CSV_COLS = ["id", "judged_at", "label", "severity", "is_duplicate", "evidence_ref", "method", "path", "target", "run_id",
  "identity", "tenant", "role", "intent", "status_code", "lab", "twin", "outcome", "gt_id", "trace_link"];
function csv(rows, cols) {
  const esc = (v) => {
    let s = v == null ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // untrusted strings: don't let a spreadsheet run them as formulas
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\r\n");
}
function download(name, text, type) {
  const a = h("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

function renderFindings(d) {
  if (!d.available) return h("div", { class: "notice" }, statusIcon("unknown"), h("span", {}, d.error || "Findings DB unavailable."));
  const t = d.totals;
  const tiles = h("div", { class: "tiles" },
    tile("Findings", fmtInt(t.findings), `confirmed, evidenced, unique · ${RANGE_LABEL[d.range]}`, true),
    tile("Needs review", fmtInt(t.needs_review), "judge unsure: human call"),
    tile("No evidence", fmtInt(t.no_evidence), t.no_evidence ? "blocked by the evidence gate" : "evidence gate clean"),
    tile("Duplicates", fmtInt(t.duplicates), "folded by Qdrant dedup"),
    tile("Lab false positives", fmtInt(t.lab_fp), "against ground truth"),
    tile("Not in ground truth", fmtInt(t.unmatched), "lab: novel or false"));

  const sevRows = SEV_ORDER.filter((k) => d.severity[k]).map((k) => ({ label: k, value: d.severity[k], color: SEV_COLOR[k] }));
  const sevCard = card("Findings by severity", "As the judge rated them.", sevRows.length ? hbarList(sevRows) : h("p", { class: "muted small" }, "No findings in this range."));

  const byEp = new Map();
  for (const r of d.rows.filter((x) => x.finding)) {
    const k = `${r.method} ${r.path}`;
    byEp.set(k, (byEp.get(k) || 0) + 1);
  }
  const epRows = [...byEp].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([label, value]) => ({ label, value }));
  const epCard = card("Most-hit endpoints", "Findings per METHOD + path template.", epRows.length ? hbarList(epRows) : h("p", { class: "muted small" }, "No findings in this range."));

  state.ffilter = state.ffilter || "findings";
  const holder = h("div", {});
  const search = h("input", { type: "search", class: "search", placeholder: "Filter by path, target, run, identity…", "aria-label": "Filter verdicts", value: state.fq || "" });
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Verdict filters" });
  const resultCount = h("p", { class: "result-count small muted", role: "status" });
  const visible = () => {
    const pred = (FILTERS.find((f) => f[0] === state.ffilter) || FILTERS[0])[2];
    const q = (state.fq || "").toLowerCase().trim();
    return d.rows.filter(pred).filter((r) => !q || [r.method, r.path, r.target, r.run_id, r.identity, r.tenant, r.role, r.intent, r.gt_id, r.severity].some((v) => v && String(v).toLowerCase().includes(q)));
  };
  const draw = () => {
    for (const chip of chips.children) chip.setAttribute("aria-pressed", String(chip.dataset.filter === state.ffilter));
    const rows = visible();
    resultCount.textContent = `${fmtInt(rows.length)} matching verdict${rows.length === 1 ? "" : "s"}`;
    exportBtn.disabled = !rows.length;
    holder.replaceChildren(table([
      { label: "Judged", key: "judged_at", fmt: fmtWhen, cls: "nw" },
      { label: "Status", render: labelBadge },
      { label: "Sev", render: sevCell },
      { label: "Endpoint", render: endpoint },
      { label: "Target", render: (r) => h("span", { class: "mono" }, r.target || "—") },
      { label: "As", render: (r) => [r.identity, r.role, r.tenant].filter(Boolean).join(" · ") || "—", cls: "nw" },
      { label: "Intent", key: "intent", cls: "clip", title: (r) => r.intent || "" },
      { label: "Ground truth", render: gtCell },
      { label: "", cls: "nw", render: (r) => linkTo(r.trace_link, "Trace ↗") || "—" },
    ], rows, { onRow: openFinding, empty: "No verdicts match.", cap: 50 }));
  };
  chips.append(...FILTERS.map(([k, label, pred]) => h("button", { class: "chip", "data-filter": k, onclick: () => { state.ffilter = k; draw(); } }, `${label} · ${fmtInt(d.rows.filter(pred).length)}`)));
  search.addEventListener("input", () => { state.fq = search.value; draw(); });
  const exportBtn = h("button", { class: "btn", onclick: () => download(`harness-verdicts-${d.range}.csv`,
    csv(visible().map((r) => ({ ...r, judged_at: r.judged_at ? new Date(r.judged_at * 1000).toISOString() : "" })), CSV_COLS), "text/csv") }, "Export CSV");
  draw();

  const list = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("div", {}, h("h2", {}, "Verdicts"), h("p", { class: "sub" }, "Newest first. Open a row for evidence and run details.")), h("div", { class: "actions" }, exportBtn)),
    h("div", { class: "toolbar" }, search), chips, resultCount, holder);
  return [tiles, h("div", { class: "stack" }, h("div", { class: "grid g2" }, sevCard, epCard), list)];
}

// ================================================================= EVAL

function matrix(t) {
  const cols = [...t.columns].reverse(); // oldest → newest, left → right
  const rows = [
    ...t.findings.filter((f) => f.scope === "authz").map((f) => ({ ...f, group: "authz" })),
    ...t.findings.filter((f) => f.scope !== "authz").map((f) => ({ ...f, group: "other" })),
    ...t.negatives.map((f) => ({ ...f, group: "negative" })),
  ];
  const cell = (f, c) => {
    if (c.found[f.id]) return h("td", { class: "mx good", title: `${c.id}: confirmed ×${c.found[f.id]}` }, "✓");
    if (c.fps[f.id]) return h("td", { class: "mx bad", title: `${c.id}: false positive ×${c.fps[f.id]}` }, "✗");
    if (f.group === "negative" || (c.twin === "control" && f.gated)) return h("td", { class: "mx na", title: "should stay clean" }, "·");
    if (!f.matchable) return h("td", { class: "mx na", title: "not auto-scorable" }, "?");
    return h("td", { class: "mx " + (f.group === "authz" ? "miss" : "na"), title: `${c.id}: not found` }, f.group === "authz" ? "○" : "·");
  };
  const unmatched = (c) => c.counts.unmatched + Object.keys(c.fps).filter((k) => !rows.some((r) => r.id === k)).reduce((a, k) => a + c.fps[k], 0);
  return h("div", { class: "table-wrap", tabindex: 0, role: "group", "aria-label": "Scrollable coverage matrix" }, h("table", { class: "data matrix" },
    h("thead", {}, h("tr", {}, h("th", {}, "Ground truth"), cols.map((c) => h("th", { class: "mx-h" },
      h("button", { class: "link-btn", title: `${c.id} · ${c.twin} twin · ${fmtWhen(c.started_at)}`, onclick: () => openRun(c.id) }, df.format(new Date((c.started_at || 0) * 1000)), c.twin === "control" ? h("span", { class: "tag" }, "ctl") : null))))),
    h("tbody", {},
      rows.map((f) => h("tr", { class: f.group === "authz" ? "" : "muted-row" },
        h("th", { scope: "row", class: "nw" }, h("span", { class: "mono" }, f.group === "negative" ? f.endpoint : f.id), f.group !== "authz" ? h("span", { class: "tag" }, f.group === "negative" ? "negative" : "other") : null, !f.matchable && f.group !== "negative" ? h("span", { class: "tag" }, "manual") : null),
        cols.map((c) => cell(f, c)))),
      h("tr", { class: "muted-row" }, h("td", {}, "Unmatched / other FPs"), cols.map((c) => { const n = unmatched(c); return h("td", { class: "mx " + (n ? (c.twin === "control" ? "bad" : "warn") : "na") }, n ? String(n) : "·"); })))));
}

function renderEval(d) {
  if (!d.available) {
    return [h("div", { class: "notice" }, statusIcon("unknown"), h("span", {}, d.error || "Unavailable.")),
      ...(d.errors || []).map((e) => h("div", { class: "notice" }, statusIcon("critical"), h("span", { class: "error" }, e)))];
  }
  const out = [notices(d.notes), ...(d.errors || []).map((e) => h("div", { class: "notice" }, statusIcon("critical"), h("span", { class: "error" }, `Ground truth: ${e}`)))];
  const cards = d.targets.map((t) => {
    const L = t.latest;
    const head = h("div", { class: "card-head" }, h("div", {}, h("h2", {}, t.name), h("p", { class: "sub mono" }, [t.source, t.base_url, t.control_url ? `control ${t.control_url}` : null].filter(Boolean).join(" · "))),
      h("div", { class: "actions" }, h("span", { class: "muted small" }, `${t.scorable} of ${t.authz} authz bugs auto-scorable`)));
    if (!t.runs) {
      return h("div", { class: "card" }, head, h("p", { class: "muted small" }, `No runs against ${t.name} in this range. Write harness.runs.target as its base URL (e.g. ${t.base_url}) or a name like "${t.name}"${t.control_url ? ` / "${t.name}-control"` : ""}.`));
    }
    // The orchestrator replays each probe on the secure twin inside the same run; that
    // control result lives in the evidence file, not the DB, so only separate control runs count here.
    const fpHint = t.latest_control ? "latest run + latest control run"
      : t.control_url ? "latest run · in-run twin checks are in its evidence files" : "latest run";
    const tiles = h("div", { class: "tiles" },
      tile("Recall", L?.recall == null ? "—" : fmtPct(L.recall), L ? `${L.hit} of ${L.of} authz bugs · latest run` : "no finished run", true),
      tile("False positives", t.fp_latest == null ? "—" : fmtInt(t.fp_latest), fpHint),
      tile("Cost per confirmed bug", fmtUSD(t.cost_per_bug), `${RANGE_LABEL[d.range]}`),
      tile("Runs scored", fmtInt(t.runs), t.control_url ? "vulnerable + control" : "vulnerable"));
    const series = t.recall_series.filter((x) => x.recall != null);
    const chart = series.length > 1 ? chartCard({
      title: "Recall per run", heading: "h3", sub: "Vulnerable twin, oldest to newest.",
      draw: (el) => lines(el, { buckets: series.map((x) => x.t), series: [{ name: "Recall", color: "var(--s3)", values: series.map((x) => x.recall) }], fmt: (v) => fmtPct(v), fmtX: (x) => df.format(new Date(x * 1000)), titleX: (x) => fmtWhen(x), label: `${t.name} recall per run`, height: 160 }),
      tableView: () => table([{ label: "Run", render: (x) => h("span", { class: "mono" }, x.id) }, { label: "Started", key: "t", fmt: fmtWhen }, { label: "Recall", key: "recall", fmt: (v) => fmtPct(v), align: "r" }], [...series].reverse(), { onRow: (x) => openRun(x.id) }),
    }) : null;
    return h("div", { class: "card" }, head, tiles,
      chart ? h("div", { class: "eval-chart" }, chart) : null,
      h("h3", { class: "mx-title" }, "Coverage, last ", String(t.columns.length), " runs"),
      h("p", { class: "sub small muted" }, "✓ found · ○ missed authz bug · ✗ false positive · · should stay clean / not counted · ? needs manual scoring. Click a date to open the run."),
      matrix(t));
  });

  const runsCard = card("Scored runs", "Every lab run in range. Recall counts distinct authz bugs; spend comes from the run's Langfuse session.",
    table([
      { label: "Run", render: (r) => h("span", { class: "mono" }, r.id) },
      { label: "Lab", render: (r) => [r.lab, r.twin === "control" ? h("span", { class: "tag" }, "control") : null] },
      { label: "Started", key: "started_at", fmt: fmtWhen, cls: "nw" },
      { label: "Recall", align: "r", render: (r) => (r.recall == null ? "—" : h("span", {}, `${fmtPct(r.recall)} `, h("span", { class: "inline-bar", style: { width: `${Math.round(48 * r.recall)}px`, background: "var(--s3)" } }))) },
      { label: "TP", align: "r", render: (r) => fmtInt(r.counts.tp) },
      { label: "FP", align: "r", render: (r) => (r.counts.fp ? h("span", { class: "error" }, fmtInt(r.counts.fp)) : "0") },
      { label: "Other", align: "r", render: (r) => fmtInt(r.counts.other) },
      { label: "Unmatched", align: "r", render: (r) => fmtInt(r.counts.unmatched) },
      { label: "Spend", key: "cost", fmt: fmtUSD, align: "r" },
      { label: "Per bug", key: "cost_per_bug", fmt: fmtUSD, align: "r" },
    ], d.runs, { onRow: (r) => openRun(r.id), empty: "No lab runs in this range.", cap: 15 }));
  out.push(h("div", { class: "stack" }, ...cards, runsCard));
  return out;
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

let drawerTrigger;
function openDrawer(title, obj) {
  const drawer = $("#drawer");
  if (!drawer.open) drawerTrigger = document.activeElement;
  $("#drawer-title").textContent = title;
  const body = $("#drawer-body");
  body.scrollTop = 0;
  body.replaceChildren(obj instanceof Node ? obj : h("pre", {}, typeof obj === "string" ? obj : JSON.stringify(obj, null, 2)));
  hideTip();
  if (!drawer.open) drawer.showModal();
  else if (!drawer.contains(document.activeElement)) $("#drawer-close").focus();
}

function pager(page, pages, go, more) {
  const hasNext = more ?? page < pages;
  return h("div", { class: "pager" },
    h("span", {}, pages ? `Page ${page} of ${fmtInt(pages)}` : `Page ${page}`),
    h("button", { class: "btn", disabled: page <= 1, onclick: () => go(page - 1) }, "Previous"),
    h("button", { class: "btn", disabled: !hasNext, onclick: () => go(page + 1) }, "Next"));
}

const ext = (url) => (url ? h("a", { href: url, target: "_blank", rel: "noopener noreferrer", onclick: (e) => e.stopPropagation() }, "Langfuse ↗") : "—");

async function renderRaw(get) {
  const sub = h("div", { class: "subnav", role: "group", "aria-label": "Raw data source" }, RAW_TABS.map(([k, label]) =>
    h("button", { "data-raw": k, "aria-pressed": String(state.raw === k), onclick: () => { state.raw = k; resetPages(); writeHash(); render({ focusRaw: true }); } }, label)));
  const out = [sub];
  const range = state.range;
  const go = (p) => { state.page = p; render(); };
  // Langfuse v2 pages by cursor: remember the cursor that opens each page
  state.cursors = state.cursors || [null];
  const cur = state.cursors[state.page - 1] ?? null;
  const cq = cur ? `&cursor=${encodeURIComponent(cur)}` : "";
  const cursorPager = (d) => { state.cursors[state.page] = d.next; return pager(state.page, null, go, d.next != null); };
  const emptyPage = (d) => (d.next ? "No rows on this page; Langfuse pages can be sparse. Try Next." : "Nothing in this range.");

  if (state.raw === "generations") {
    const d = await get(`/api/raw/generations?range=${range}${cq}`);
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
        { label: "", render: (r) => ext(r.link) },
      ], d.rows, { onRow: (r) => openDrawer(`Generation ${r.id}`, r.raw), empty: emptyPage(d) }),
      cursorPager(d)));
  } else if (state.raw === "traces") {
    const d = await get(`/api/raw/traces?range=${range}${cq}`);
    out.push(card(`Traces · ${RANGE_LABEL[range]}`, "Root observation of each trace (Langfuse v4 has no trace list). Session = harness run id.",
      table([
        { label: "Time", key: "ts", fmt: fmtWhen, cls: "nw" },
        { label: "Name", render: (r) => h("span", { class: "mono" }, r.name || "—") },
        { label: "Session", render: (r) => h("span", { class: "mono" }, r.session_id || "—") },
        { label: "Type", key: "type" },
        { label: "Latency", key: "latency_ms", fmt: fmtMs, align: "r" },
        { label: "Cost", key: "cost", fmt: fmtUSD, align: "r" },
        { label: "", render: (r) => ext(r.link) },
      ], d.rows, { onRow: (r) => openDrawer(`Trace ${r.id}`, r.raw), empty: emptyPage(d) }),
      cursorPager(d)));
  } else if (state.raw === "tables") {
    const t = await get("/api/raw/tables");
    if (!t.available || !t.tables?.length) {
      out.push(card("Harness tables", null, h("div", { class: "notice" }, statusIcon("unknown"), h("span", {}, t.error || "No database tables available."))));
    } else {
      if (!state.table) state.table = t.tables[0].name;
      const d = await get(`/api/raw/table/${encodeURIComponent(state.table)}?page=${state.page}`);
      const cols = d.columns.map((c) => ({ label: c, cls: "clip", render: (r) => { const v = r[c]; return v == null ? h("span", { class: "muted" }, "null") : typeof v === "object" ? JSON.stringify(v) : String(v); }, title: (r) => (r[c] == null ? "" : typeof r[c] === "object" ? JSON.stringify(r[c]) : String(r[c])) }));
      out.push(h("div", { class: "raw-split" },
        h("div", { class: "card" }, h("nav", { class: "list-nav", "aria-label": "Tables" }, t.tables.map((x) =>
          h("button", { "aria-current": String(x.name === state.table), onclick: () => { state.table = x.name; state.page = 1; state.cursors = [null]; render(); } }, h("span", { class: "mono" }, x.name), h("span", { class: "muted num" }, fmtInt(x.rows)))))),
        card(`harness.${state.table}`, "Newest first, 50 per page.",
          table(cols, d.rows, { onRow: (r) => openDrawer(`harness.${state.table}`, r), empty: "Empty table." }),
          pager(d.page, null, go, d.more))));
    }
  } else if (state.raw === "qdrant") {
    const c = await get("/api/raw/qdrant");
    if (!c.collections.length) {
      out.push(card("Qdrant", null, h("p", { class: "muted small" }, "No collections yet.")));
    } else {
      if (!state.collection || !c.collections.some((x) => x.name === state.collection)) state.collection = c.collections[0].name;
      state.qOffsets = state.qOffsets || [null];
      const off = state.qOffsets[state.page - 1] ?? null;
      const d = await get(`/api/raw/qdrant/${encodeURIComponent(state.collection)}${off != null ? `?offset=${encodeURIComponent(off)}` : ""}`);
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

const RENDERERS = { health: renderHealth, trends: renderTrends, flows: renderFlows, findings: renderFindings, eval: renderEval };
const snapshots = new WeakMap();
let viewRequest;

function replaceView(section, ...nodes) {
  for (const chart of section.querySelectorAll(".chart")) ro.unobserve(chart);
  section.replaceChildren(...nodes.flat(Infinity).filter(Boolean));
  for (const chart of section.querySelectorAll(".chart")) {
    const width = chart.clientWidth;
    if (width) {
      widths.set(chart, width);
      drawers.get(chart)?.();
    }
    ro.observe(chart);
  }
}

function syncControls() {
  const [title, description] = VIEWS[state.tab];
  $("#view-title").textContent = title;
  $("#view-description").textContent = description;
  document.title = title + " · Seccy";
  for (const button of tabButtons) {
    const selected = button.dataset.tab === state.tab;
    const panel = document.getElementById(button.getAttribute("aria-controls"));
    panel.hidden = !selected;
    if (!selected) panel.removeAttribute("aria-busy");
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  }
  $("#range").hidden = state.tab === "health" || (state.tab === "raw" && !["generations", "traces"].includes(state.raw));
  $("#auto-control").hidden = state.tab === "raw";
  $("#stack-pill").hidden = state.tab !== "health" || !snapshots.has($("#tab-health"));
  for (const input of $("#range").querySelectorAll("input")) input.checked = input.value === state.range;
}

function resetPages() {
  state.page = 1;
  state.cursors = [null];
  state.qOffsets = [null];
}

async function render({ background = false, focusRaw = false } = {}) {
  viewRequest?.abort();
  const request = new AbortController();
  viewRequest = request;
  const { signal } = request;
  const { tab, range, raw, page, table, collection } = state;
  const section = $(`#tab-${tab}`);
  const key = JSON.stringify(tab === "health" ? [tab] : tab === "raw" ? [tab, range, raw, page, table, collection] : [tab, range]);
  const previous = snapshots.get(section);
  const stale = previous?.key === key;
  const status = $("#updated");
  const loadState = $("#load-state");
  const restoreRawFocus = focusRaw && section.contains(document.activeElement);
  syncControls();
  hideTip();
  loadState.replaceChildren();
  section.setAttribute("aria-busy", "true");
  if (!stale) {
    snapshots.delete(section);
    replaceView(section);
  }
  if (!background || !stale) status.textContent = stale ? "Refreshing…" : "Loading…";
  const get = async (path) => {
    const result = await api(path, { signal });
    signal.throwIfAborted();
    return result;
  };
  try {
    let nodes;
    let at;
    if (tab === "raw") {
      await metadata;
      signal.throwIfAborted();
      nodes = await renderRaw(get);
    } else {
      const [data] = await Promise.all([get(tab === "health" ? "/api/health" : `/api/${tab}?range=${range}`), metadata]);
      signal.throwIfAborted();
      nodes = RENDERERS[tab](data);
      if (tab === "health") {
        at = data.at;
        updatePill(data);
      }
    }
    signal.throwIfAborted();
    const loaded = Date.now() / 1000;
    snapshots.set(section, {
      key: tab === "raw" ? JSON.stringify([tab, range, raw, page, state.table, state.collection]) : key,
      loaded,
    });
    replaceView(section, nodes);
    status.textContent = at ? `Service snapshot · ${fmtWhen(at)}` : `Loaded ${tf.format(new Date(loaded * 1000))}`;
    if (restoreRawFocus) section.querySelector(`[data-raw="${raw}"]`)?.focus();
  } catch (e) {
    if (signal.aborted) return;
    status.textContent = stale ? `Showing last successful load · ${fmtWhen(previous.loaded)}` : "Data unavailable";
    const retry = h("button", { class: "btn", onclick: () => { $("#refresh").focus(); render(); } }, "Retry");
    loadState.replaceChildren(h("div", { class: "notice load-notice" },
      h("span", { class: "error", role: "alert" }, stale ? "Refresh failed. " : "Could not load this view. ", e.message), retry));
    if (restoreRawFocus) retry.focus();
  } finally {
    if (!signal.aborted) {
      section.removeAttribute("aria-busy");
      viewRequest = null;
    }
  }
}

function schedule() {
  clearInterval(state.timer);
  if (!$("#auto").checked) return;
  state.timer = setInterval(() => {
    const interacting = document.activeElement?.closest(".tab");
    if (state.tab !== "raw" && document.visibilityState === "visible" && !interacting
        && !$("#drawer").open && !viewRequest && $("#tooltip").hidden) {
      render({ background: true });
    }
  }, 30000);
}

function closeDrawer({ restoreFocus = true, updateHash = true } = {}) {
  const drawer = $("#drawer");
  if (!drawer.open) return;
  runRequest?.abort();
  if (updateHash && state.run) {
    state.run = null;
    writeHash({ replace: true });
  }
  drawer.close();
  $("#drawer-body").replaceChildren();
  if (restoreFocus) {
    const trigger = drawerTrigger?.isConnected && !drawerTrigger.closest("[hidden]") ? drawerTrigger : $(`#nav-${state.tab}`);
    trigger.focus();
  }
  drawerTrigger = null;
}

let metadata;
function init() {
  readHash();
  metadata = api("/api/meta", { signal: AbortSignal.timeout(10000) }).then((meta) => {
    if (Array.isArray(meta.routes)) ROUTES = meta.routes;
    $("#demo-banner").hidden = !meta.demo;
  }).catch(() => { /* View-specific requests still report their own errors. */ });

  const seg = $("#range");
  for (const r of Object.keys(RANGE_LABEL)) {
    seg.append(
      h("input", { type: "radio", name: "range", id: `range-${r}`, value: r, onchange: () => {
        state.range = r;
        resetPages();
        writeHash();
        render();
      } }),
      h("label", { for: `range-${r}` }, r));
  }

  for (const button of tabButtons) {
    button.addEventListener("click", () => {
      if (state.tab === button.dataset.tab) return;
      state.tab = button.dataset.tab;
      resetPages();
      writeHash();
      render();
    });
  }
  $(".tabs").addEventListener("keydown", (e) => {
    const i = tabButtons.indexOf(e.target);
    if (i < 0) return;
    const targets = { ArrowLeft: (i - 1 + tabButtons.length) % tabButtons.length,
      ArrowRight: (i + 1) % tabButtons.length, Home: 0, End: tabButtons.length - 1 };
    if (!Object.hasOwn(targets, e.key)) return;
    e.preventDefault();
    tabButtons[i].tabIndex = -1;
    const next = tabButtons[targets[e.key]];
    next.tabIndex = 0;
    next.focus();
  });
  $(".tabs").addEventListener("focusout", (e) => {
    if (e.currentTarget.contains(e.relatedTarget)) return;
    for (const button of tabButtons) button.tabIndex = button.dataset.tab === state.tab ? 0 : -1;
  });

  $(".skip-link").addEventListener("click", (e) => {
    e.preventDefault();
    $("#view-title").focus();
  });
  $("#refresh").addEventListener("click", () => render());
  $("#auto").addEventListener("change", schedule);
  $("#drawer-close").addEventListener("click", () => closeDrawer());
  $("#drawer").addEventListener("cancel", (e) => { e.preventDefault(); closeDrawer(); });
  $("#drawer").addEventListener("keydown", (e) => {
    if (e.key !== "Tab") return;
    const focusable = [...e.currentTarget.querySelectorAll("button, a[href], [tabindex], input, textarea, select")]
      .filter((el) => el.tabIndex >= 0 && !el.disabled && el.getClientRects().length);
    const edge = e.shiftKey ? focusable[0] : focusable.at(-1);
    if (document.activeElement !== edge) return;
    e.preventDefault();
    (e.shiftKey ? focusable.at(-1) : focusable[0]).focus();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hideTip(); });
  window.addEventListener("hashchange", () => {
    closeDrawer({ restoreFocus: false, updateHash: false });
    readHash();
    resetPages();
    render();
    if (state.run) openRun(state.run);
  });

  writeHash({ replace: true });
  schedule();
  render();
  if (state.run) openRun(state.run);
}

init();
