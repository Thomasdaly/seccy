// Dependency-free SVG charts. Every label goes in via textContent: series
// names, step names and hosts come from upstream data and are untrusted.

const NS = "http://www.w3.org/2000/svg";

export function s(tag, attrs = {}, parent) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "style") Object.assign(el.style, v);
    else if (k === "text") el.textContent = v;
    else el.setAttribute(k, v);
  }
  if (parent) parent.appendChild(el);
  return el;
}

// ---- tooltip ---------------------------------------------------------------

function tipEl() { return document.getElementById("tooltip"); }

export function showTip(evt, title, rows = [], total) {
  const t = tipEl();
  t.replaceChildren();
  if (title) {
    const d = document.createElement("div");
    d.className = "tt-title";
    d.textContent = title;
    t.appendChild(d);
  }
  for (const r of rows) {
    const row = document.createElement("div");
    row.className = "tt-row";
    const key = document.createElement("span");
    key.className = r.kind === "line" ? "key-line" : "key-rect";
    key.style.background = r.color;
    const v = document.createElement("strong");
    v.textContent = r.value;
    const l = document.createElement("span");
    l.textContent = r.label;
    row.append(key, v, l);
    t.appendChild(row);
  }
  if (total) {
    const d = document.createElement("div");
    d.className = "tt-total";
    d.textContent = total;
    t.appendChild(d);
  }
  t.hidden = false;
  let x, y;
  if (evt && evt.clientX != null && evt.type !== "focus") { x = evt.clientX; y = evt.clientY; }
  else {
    const r = (evt?.currentTarget || evt?.target)?.getBoundingClientRect?.();
    x = r ? r.left + r.width / 2 : 0; y = r ? r.top : 0;
  }
  const w = t.offsetWidth, hgt = t.offsetHeight;
  let left = x + 14, top = y + 14;
  if (left + w > window.innerWidth - 8) left = x - w - 14;
  if (top + hgt > window.innerHeight - 8) top = y - hgt - 14;
  t.style.left = Math.max(8, left) + "px";
  t.style.top = Math.max(8, top) + "px";
}

export function hideTip() { tipEl().hidden = true; }

// ---- scales / frame --------------------------------------------------------

export function niceMax(max, count = 4) {
  if (!(max > 0)) return { max: 1, step: 0.25 };
  const raw = max / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  return { max: Math.ceil(max / step - 1e-9) * step, step };
}

function frame(el, o = {}) {
  el.replaceChildren();
  const W = Math.max(260, Math.round(el.clientWidth || 600));
  const H = o.height || 200;
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": o.label || "Chart" }, el);
  return { el, svg, W, H, x0: o.left ?? 52, x1: W - (o.right ?? 12), y0: o.top ?? 8, y1: H - (o.bottom ?? 24) };
}

function empty(f, text) {
  s("line", { class: "base-line", x1: f.x0, x2: f.x1, y1: f.y1, y2: f.y1 }, f.svg);
  s("text", { class: "empty", x: (f.x0 + f.x1) / 2, y: (f.y0 + f.y1) / 2, "text-anchor": "middle", text }, f.svg);
}

function yAxis(f, max, step, fmt) {
  const Y = (v) => f.y1 - (v / max) * (f.y1 - f.y0);
  for (let v = 0; v <= max + step / 1000; v += step) {
    const y = Math.round(Y(v)) + 0.5;
    s("line", { class: v === 0 ? "base-line" : "grid-line", x1: f.x0, x2: f.x1, y1: y, y2: y }, f.svg);
    s("text", { x: f.x0 - 8, y, dy: "0.32em", "text-anchor": "end", text: fmt(v) }, f.svg);
  }
  return Y;
}

function xLabels(f, buckets, X, fmtX) {
  const n = buckets.length;
  const fit = Math.max(2, Math.floor((f.x1 - f.x0) / 64));
  const every = Math.ceil(n / fit);
  for (let i = 0; i < n; i++) {
    if ((n - 1 - i) % every !== 0) continue; // anchor labels on the latest bucket
    s("text", { x: X(i), y: f.y1 + 16, "text-anchor": "middle", text: fmtX(buckets[i]) }, f.svg);
  }
}

function svgX(f, evt) {
  const r = f.svg.getBoundingClientRect();
  return (evt.clientX - r.left) * (f.W / r.width);
}

function roundedTop(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

// ---- stacked columns -------------------------------------------------------
// opt: { buckets, series:[{name,color,values}], fmt, fmtX, titleX, height, emptyText }

export function columns(el, opt) {
  const { buckets, series, fmt, fmtX, titleX } = opt;
  const f = frame(el, opt);
  const n = buckets.length;
  const totals = buckets.map((_, i) => series.reduce((a, sr) => a + (sr.values[i] || 0), 0));
  const maxT = Math.max(0, ...totals);
  if (!(maxT > 0)) return empty(f, opt.emptyText || "No data in this range");
  f.svg.setAttribute("role", "group");
  const { max, step } = niceMax(maxT);
  const Y = yAxis(f, max, step, fmt);
  const band = (f.x1 - f.x0) / n;
  const bw = Math.max(2, Math.min(24, band * 0.66));
  const X = (i) => f.x0 + band * i + band / 2;
  const hover = s("rect", { class: "band-hover", y: f.y0, height: f.y1 - f.y0, width: band, visibility: "hidden" }, f.svg);
  const cols = [];
  for (let i = 0; i < n; i++) {
    const g = s("g", {}, f.svg);
    cols.push(g);
    const segs = series.filter((sr) => sr.values[i] > 0);
    let acc = 0;
    segs.forEach((sr, k) => {
      const v = sr.values[i];
      const yTop = Y(acc + v), yBot = Y(acc) - (k > 0 ? 2 : 0); // 2px surface gap between segments
      acc += v;
      const hh = yBot - yTop;
      if (hh < 0.75) return;
      const x = X(i) - bw / 2;
      const isTop = k === segs.length - 1;
      s("path", { d: isTop ? roundedTop(x, yTop, bw, hh, 4) : `M${x},${yTop}h${bw}v${hh}h${-bw}Z`, style: { fill: sr.color } }, g);
    });
  }
  xLabels(f, buckets, X, fmtX);
  const hits = [];
  for (let i = 0; i < n; i++) {
    const hit = s("rect", {
      class: "hit", x: f.x0 + band * i, y: f.y0, width: band, height: f.y1 - f.y0 + 20,
      role: "img", tabindex: i === n - 1 ? 0 : -1,
      "aria-label": `${titleX(buckets[i])}: ${series.map((sr) => `${sr.name} ${fmt(sr.values[i] || 0)}`).join(", ")}`,
    }, f.svg);
    hits.push(hit);
    const show = (e) => {
      hover.setAttribute("x", f.x0 + band * i);
      hover.setAttribute("visibility", "visible");
      cols.forEach((g, j) => g.classList.toggle("dim", j !== i));
      const rows = series.map((sr) => ({ color: sr.color, value: fmt(sr.values[i] || 0), label: sr.name }));
      showTip(e, titleX(buckets[i]), rows, series.length > 1 ? `Total ${fmt(totals[i])}` : null);
    };
    const hide = () => { hover.setAttribute("visibility", "hidden"); cols.forEach((g) => g.classList.remove("dim")); hideTip(); };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("pointerleave", hide);
    hit.addEventListener("blur", hide);
    hit.addEventListener("keydown", (e) => {
      const targets = { ArrowLeft: Math.max(0, i - 1), ArrowRight: Math.min(n - 1, i + 1), Home: 0, End: n - 1 };
      if (!Object.hasOwn(targets, e.key)) return;
      e.preventDefault();
      hit.setAttribute("tabindex", -1);
      const next = hits[targets[e.key]];
      next.setAttribute("tabindex", 0);
      next.focus();
    });
  }
}

// ---- lines -----------------------------------------------------------------
// opt: { buckets, series:[{name,color,values(null = gap)}], fmt, fmtX, titleX, height }

export function lines(el, opt) {
  const { buckets, series, fmt, fmtX, titleX } = opt;
  const f = frame(el, opt);
  const n = buckets.length;
  const all = series.flatMap((sr) => sr.values.filter((v) => v != null));
  if (!all.length) return empty(f, opt.emptyText || "No data in this range");
  f.svg.setAttribute("role", "group");
  const { max, step } = niceMax(Math.max(...all));
  const Y = yAxis(f, max, step, fmt);
  const X = (i) => (n === 1 ? (f.x0 + f.x1) / 2 : f.x0 + ((f.x1 - f.x0) * i) / (n - 1));
  const cross = s("line", { class: "crosshair", y1: f.y0, y2: f.y1, visibility: "hidden" }, f.svg);
  for (const sr of series) {
    let d = "", run = [];
    const flush = () => {
      if (run.length === 1) s("circle", { cx: X(run[0]), cy: Y(sr.values[run[0]]), r: 3, style: { fill: sr.color } }, f.svg);
      run = [];
    };
    sr.values.forEach((v, i) => {
      if (v == null) { flush(); return; }
      d += `${run.length ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`;
      run.push(i);
    });
    flush();
    if (d) s("path", { d, fill: "none", style: { stroke: sr.color, strokeWidth: 2, strokeLinejoin: "round", strokeLinecap: "round" } }, f.svg);
    const last = sr.values.map((v, i) => [v, i]).filter(([v]) => v != null).pop();
    if (last) s("circle", { cx: X(last[1]), cy: Y(last[0]), r: 4, style: { fill: sr.color, stroke: "var(--surface)", strokeWidth: 2 } }, f.svg);
  }
  xLabels(f, buckets, X, fmtX);
  const dots = series.map((sr) => s("circle", { r: 4, visibility: "hidden", style: { fill: sr.color, stroke: "var(--surface)", strokeWidth: 2, pointerEvents: "none" } }, f.svg));
  const hit = s("rect", { class: "hit", x: f.x0 - 6, y: f.y0, width: f.x1 - f.x0 + 12, height: f.y1 - f.y0 + 20,
    role: "img", tabindex: 0, "aria-label": `${opt.label || "Line chart"}. Use left and right arrow keys to explore values.` }, f.svg);
  let cur = n - 1;
  const showAt = (i, e) => {
    cur = Math.max(0, Math.min(n - 1, i));
    const x = X(cur);
    cross.setAttribute("x1", x); cross.setAttribute("x2", x); cross.setAttribute("visibility", "visible");
    series.forEach((sr, k) => {
      const v = sr.values[cur];
      if (v == null) { dots[k].setAttribute("visibility", "hidden"); return; }
      dots[k].setAttribute("cx", x); dots[k].setAttribute("cy", Y(v)); dots[k].setAttribute("visibility", "visible");
    });
    const rows = series.map((sr) => ({ kind: "line", color: sr.color, value: sr.values[cur] == null ? "—" : fmt(sr.values[cur]), label: sr.name }));
    hit.setAttribute("aria-label", `${titleX(buckets[cur])}: ${rows.map((row) => `${row.label} ${row.value}`).join(", ")}`);
    showTip(e, titleX(buckets[cur]), rows);
  };
  const hide = () => { cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); hideTip(); };
  hit.addEventListener("pointermove", (e) => {
    const x = svgX(f, e);
    showAt(n === 1 ? 0 : Math.round(((x - f.x0) / (f.x1 - f.x0)) * (n - 1)), e);
  });
  hit.addEventListener("pointerleave", hide);
  hit.addEventListener("blur", hide);
  hit.addEventListener("focus", (e) => showAt(cur, e));
  hit.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      showAt(cur + (e.key === "ArrowLeft" ? -1 : 1), { currentTarget: hit, type: "focus" });
    }
  });
}

// ---- flow: stacked share rows joined by ribbons ----------------------------
// opt: { rows:[{title, total, values:{key:number}, fmt}], keys:[{key,name,color,ink}] }

export function flowRows(el, opt) {
  const { rows, keys } = opt;
  const barH = 28, gapH = 52, labelW = 92;
  const H = rows.length * barH + (rows.length - 1) * gapH + 4;
  const f = frame(el, { height: H, left: labelW, right: 4, top: 2, bottom: 2, label: opt.label });
  const width = f.x1 - f.x0;
  const layout = rows.map((row, ri) => {
    const tot = keys.reduce((a, k) => a + (row.values[k.key] || 0), 0);
    const parts = keys.filter((k) => (row.values[k.key] || 0) > 0);
    const gaps = Math.max(0, parts.length - 1) * 2;
    let x = f.x0;
    const pos = {};
    for (const k of parts) {
      const w = tot ? ((row.values[k.key] / tot) * (width - gaps)) : 0;
      pos[k.key] = { x, w, share: tot ? row.values[k.key] / tot : 0 };
      x += w + 2;
    }
    return { y: f.y0 + ri * (barH + gapH), tot, pos };
  });
  const tipFor = (k) => (e) => {
    showTip(e, k.name, rows.map((row) => {
      const tot = keys.reduce((a, kk) => a + (row.values[kk.key] || 0), 0);
      const v = row.values[k.key] || 0;
      return { color: k.color, value: row.fmt(v), label: `${row.title} · ${tot ? Math.round((100 * v) / tot) : 0}%` };
    }));
  };
  // ribbons first so blocks sit on top
  for (let ri = 0; ri < layout.length - 1; ri++) {
    const a = layout[ri], b = layout[ri + 1];
    const ya = a.y + barH, yb = b.y, ym = (ya + yb) / 2;
    for (const k of keys) {
      const pa = a.pos[k.key], pb = b.pos[k.key];
      if (!pa || !pb) continue;
      const d = `M${pa.x},${ya}C${pa.x},${ym} ${pb.x},${ym} ${pb.x},${yb}H${pb.x + pb.w}C${pb.x + pb.w},${ym} ${pa.x + pa.w},${ym} ${pa.x + pa.w},${ya}Z`;
      const p = s("path", { d, style: { fill: k.color, opacity: 0.16 } }, f.svg);
      p.addEventListener("pointermove", tipFor(k));
      p.addEventListener("pointerleave", hideTip);
    }
  }
  rows.forEach((row, ri) => {
    const L = layout[ri];
    s("text", { class: "strong", x: 0, y: L.y + 12, text: row.title }, f.svg);
    s("text", { x: 0, y: L.y + 26, text: row.fmt(L.tot) }, f.svg);
    if (!L.tot) {
      s("rect", { x: f.x0, y: L.y, width, height: barH, rx: 4, style: { fill: "var(--grid)" } }, f.svg);
      s("text", { x: f.x0 + 8, y: L.y + barH / 2, dy: "0.32em", text: "nothing in range" }, f.svg);
      return;
    }
    for (const k of keys) {
      const p = L.pos[k.key];
      if (!p || p.w <= 0) continue;
      const r = s("rect", {
        x: p.x, y: L.y, width: Math.max(1, p.w), height: barH, rx: Math.min(4, p.w / 2),
        style: { fill: k.color, cursor: "default" },
      }, f.svg);
      r.addEventListener("pointermove", tipFor(k));
      r.addEventListener("pointerleave", hideTip);
      const label = `${Math.round(p.share * 100)}%`;
      if (p.w >= label.length * 7 + 14) {
        s("text", { x: p.x + 8, y: L.y + barH / 2, dy: "0.32em", text: label, style: { fill: k.ink, fontWeight: 600, pointerEvents: "none" } }, f.svg);
      }
    }
  });
}

// ---- uptime strip ----------------------------------------------------------
// cells: [{t, up (0..1|null), ms}], 15-minute buckets

export function uptimeStrip(el, cells, fmtTitle) {
  const f = frame(el, { height: 30, left: 0, right: 0, top: 0, bottom: 12, label: "24 hour uptime" });
  const n = cells.length;
  const cw = (f.x1 - f.x0) / n;
  cells.forEach((c, i) => {
    const color = c.up == null ? "var(--grid)" : c.up >= 0.999 ? "var(--good)" : c.up >= 0.9 ? "var(--warning)" : "var(--critical)";
    const r = s("rect", { x: f.x0 + i * cw, y: 0, width: Math.max(1, cw - 1), height: 16, rx: 1, style: { fill: color } }, f.svg);
    r.addEventListener("pointermove", (e) => showTip(e, fmtTitle(c.t), [{
      color, value: c.up == null ? "no samples" : `${(c.up * 100).toFixed(c.up >= 0.999 || c.up === 0 ? 0 : 1)}% up`,
      label: c.ms == null ? "" : `avg ${Math.round(c.ms)} ms`,
    }]));
    r.addEventListener("pointerleave", hideTip);
  });
  s("text", { x: 0, y: 28, text: "24h ago" }, f.svg);
  s("text", { x: f.x1, y: 28, "text-anchor": "end", text: "now" }, f.svg);
}
