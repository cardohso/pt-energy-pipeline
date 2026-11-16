"use strict";
// Reads data/dashboard.json (written by `pt-energy dashboard build`) and draws everything.
// No libraries, no network calls except that one file. Text is always set with textContent.

const GEN_COLORS = ["--series-1", "--series-2", "--series-3", "--series-4", "--series-5", "--series-6"];
const NS = "http://www.w3.org/2000/svg";
const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function s(tag, attrs) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const pad = (n) => String(n).padStart(2, "0");
const utc = (t) => new Date(t * 1000);
const fmtDay = (t) => { const d = utc(t); return `${DAYS[d.getUTCDay()]} ${pad(d.getUTCDate())}`; };
const fmtHour = (t) => { const d = utc(t); return `${fmtDay(t)} ${pad(d.getUTCHours())}:00`; };
const fmtStamp = (t) => { const d = utc(t); return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`; };
const fmtNum = (v, digits = 0) => v == null ? "–" : v.toLocaleString("en-GB", { minimumFractionDigits: digits, maximumFractionDigits: digits });

function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v || 1));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

// ---------------------------------------------------------------- charts

/** series: [{name, color, values[]}] aligned with `t`. mode: "stack" | "line". */
function drawChart(host, { t, series, mode, unit, digits = 0, label, signed = false }) {
  const W = 880, H = 300, m = { l: 52, r: 12, t: 10, b: 26 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const n = t.length;
  const x = (i) => m.l + (n <= 1 ? 0 : (i / (n - 1)) * iw);

  const totals = t.map((_, i) => series.reduce((a, sr) => a + (sr.values[i] ?? 0), 0));
  let lo = 0, hi;
  if (mode === "stack") hi = niceMax(Math.max(...totals));
  else {
    const all = series.flatMap((sr) => sr.values.filter((v) => v != null));
    hi = Math.max(...all);
    lo = signed ? Math.min(0, Math.min(...all)) : 0;
    hi = niceMax(hi);
    if (lo < 0) lo = -niceMax(-lo);
  }
  const y = (v) => m.t + ih - ((v - lo) / (hi - lo || 1)) * ih;

  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", tabindex: "0", "aria-label": label });
  const grid = s("g", { class: "grid" }), axis = s("g", { class: "axis" });
  for (let k = 0; k <= 4; k++) {
    const v = lo + ((hi - lo) * k) / 4;
    grid.append(s("line", { x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    const tx = s("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" });
    tx.textContent = fmtNum(v, hi < 10 ? 1 : 0);
    axis.append(tx);
  }
  // one tick per UTC midnight
  t.forEach((tt, i) => {
    if (utc(tt).getUTCHours() === 0) {
      grid.append(s("line", { x1: x(i), x2: x(i), y1: m.t, y2: m.t + ih }));
      const tx = s("text", { x: x(i), y: H - 8, "text-anchor": "middle" });
      tx.textContent = fmtDay(tt);
      axis.append(tx);
    }
  });
  svg.append(grid);

  if (mode === "stack") {
    const base = new Array(n).fill(0);
    series.forEach((sr) => {
      const top = sr.values.map((v, i) => base[i] + (v ?? 0));
      let d = t.map((_, i) => `${i ? "L" : "M"}${x(i)},${y(top[i])}`).join("");
      d += t.map((_, i) => `L${x(n - 1 - i)},${y(base[n - 1 - i])}`).join("") + "Z";
      svg.append(s("path", { d, fill: css(sr.color), stroke: css("--surface-1"), "stroke-width": 1 }));
      top.forEach((v, i) => (base[i] = v));
    });
  } else {
    series.forEach((sr) => {
      let d = "", pen = false;
      sr.values.forEach((v, i) => {
        if (v == null) { pen = false; return; }
        d += `${pen ? "L" : "M"}${x(i)},${y(v)}`;
        pen = true;
      });
      svg.append(s("path", { d, fill: "none", stroke: css(sr.color), "stroke-width": 2, "stroke-linejoin": "round" }));
    });
  }
  svg.append(s("line", { class: "baseline", x1: m.l, x2: W - m.r, y1: y(Math.max(lo, 0)), y2: y(Math.max(lo, 0)) }));
  svg.append(axis);

  const cross = s("line", { class: "cross", y1: m.t, y2: m.t + ih, visibility: "hidden" });
  svg.append(cross);
  const tip = h("div", { class: "tip", hidden: true, role: "status" });
  host.replaceChildren(svg, tip);

  const show = (i) => {
    i = Math.max(0, Math.min(n - 1, i));
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i));
    cross.setAttribute("visibility", "visible");
    tip.hidden = false;
    const rows = [...series].reverse().map((sr) =>
      h("div", { class: "row" },
        h("span", { class: "swatch", style: `background:${css(sr.color)}` }),
        h("span", {}, sr.name),
        h("span", {}, sr.values[i] == null ? "–" : `${fmtNum(sr.values[i], digits)} ${unit}`)));
    const extra = mode === "stack"
      ? h("div", { class: "row" }, h("span", {}), h("span", {}, "Total"), h("span", {}, `${fmtNum(totals[i], digits)} ${unit}`))
      : null;
    tip.replaceChildren(h("div", { class: "t" }, fmtHour(t[i])), ...rows, ...(extra ? [extra] : []));
    const box = host.getBoundingClientRect();
    const px = (x(i) / W) * box.width;
    tip.style.left = `${Math.min(Math.max(px + 12, 0), box.width - tip.offsetWidth)}px`;
    tip.style.top = "8px";
    return i;
  };
  let cur = n - 1;
  svg.addEventListener("pointermove", (e) => {
    const box = svg.getBoundingClientRect();
    const vx = ((e.clientX - box.left) / box.width) * W;
    cur = show(Math.round(((vx - m.l) / iw) * (n - 1)));
  });
  svg.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  svg.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft") { cur = show(cur - 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { cur = show(cur + 1); e.preventDefault(); }
    if (e.key === "Escape") { tip.hidden = true; cross.setAttribute("visibility", "hidden"); }
  });
  svg.addEventListener("focus", () => { cur = show(cur); });
  svg.addEventListener("blur", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });

  // legend (always present for >= 2 series) shows the latest value, so no colour is read alone
  const last = n - 1;
  const legend = series.length > 1
    ? h("ul", { class: "legend" }, series.map((sr) =>
        h("li", {}, h("span", { class: "swatch", style: `background:${css(sr.color)}` }), sr.name, " ",
          h("b", {}, sr.values[last] == null ? "–" : fmtNum(sr.values[last], digits)))))
    : null;

  // table view of the same numbers
  const table = h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Hour (UTC)"), series.map((sr) => h("th", { class: "num" }, `${sr.name} (${unit})`)))),
    h("tbody", {}, [...t.keys()].reverse().map((i) =>
      h("tr", {}, h("td", {}, fmtHour(t[i])), series.map((sr) => h("td", { class: "num" }, sr.values[i] == null ? "–" : fmtNum(sr.values[i], digits)))))));
  if (legend) host.append(legend);
  host.append(h("details", { class: "tableview" }, h("summary", {}, "View as table"), h("div", { class: "scroll" }, table)));
}

// ---------------------------------------------------------------- KPIs and trace

function openTrace(k) {
  const dlg = document.getElementById("trace");
  document.getElementById("trace-title").textContent = `Trace: ${k.label}`;
  const files = k.trace;
  const body = document.getElementById("trace-body");
  body.replaceChildren(
    h("p", {}, h("b", {}, `${fmtNum(k.value, 1)} ${k.unit}`), `, computed from ${fmtNum(files.reduce((a, f) => a + f.rows_used, 0))} model rows between ${fmtStamp(k.from)} and ${fmtStamp(k.to)}.`),
    h("p", { class: "muted" }, k.formula),
    h("h3", {}, `${files.length} raw file${files.length === 1 ? "" : "s"}`),
    h("div", { class: "scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, ["Window", "Rows", "Fetched", "Contract", "File in the data repository", "SHA-256 of the file bytes"].map((c) => h("th", {}, c)))),
      h("tbody", {}, files.map((f) => h("tr", {},
        h("td", {}, f.window), h("td", { class: "num" }, f.rows_used), h("td", {}, fmtStamp(f.fetched_at)),
        h("td", {}, f.contract_version), h("td", {}, h("code", {}, f.path)), h("td", {}, h("code", {}, f.response_sha256))))))),
    h("p", { class: "muted" }, "Files are in the private data repository, kept exactly as received. The file name carries a content key (for ENTSO-E it ignores the per-request document id and timestamp, so an identical answer is not stored twice); the last column is the plain SHA-256 of the stored bytes, which anyone with the file can check. The content key is stored on every model row.")
  );
  dlg.showModal();
}

function renderKpis(doc) {
  document.getElementById("kpis").replaceChildren(...doc.kpis.map((k) =>
    h("div", { class: "kpi" },
      h("div", { class: "label" }, k.label),
      h("div", { class: "value" }, fmtNum(k.value, k.unit === "MW" ? 0 : 1), h("span", { class: "unit" }, k.unit)),
      h("button", { type: "button", onclick: null }, "Trace to raw files"))));
  document.querySelectorAll("#kpis button").forEach((b, i) => b.addEventListener("click", () => openTrace(doc.kpis[i])));
}

// ---------------------------------------------------------------- health

const STATUS = {
  pass: { cls: "good", icon: "✓", text: "Passed" },
  warning: { cls: "warning", icon: "▲", text: "Warning" },
  blocking: { cls: "critical", icon: "✕", text: "Blocking" },
  known: { cls: "known", icon: "i", text: "Known issue" },
};
function checkState(c) {
  if (c.status === "pass") return STATUS.pass;
  if (c.status === "known") return STATUS.known;
  return c.severity === "blocking" ? STATUS.blocking : STATUS.warning;
}
const pill = (st) => h("span", { class: `pill ${st.cls}` }, st.icon, " ", st.text);

function renderHealth(doc) {
  const hl = doc.health, host = document.getElementById("health-body");
  const blocking = hl.checks.filter((c) => checkState(c) === STATUS.blocking);
  const warns = hl.checks.filter((c) => checkState(c) === STATUS.warning);
  const known = hl.checks.filter((c) => checkState(c) === STATUS.known);

  let banner;
  if (!hl.published) {
    banner = h("div", { class: "banner critical" }, h("span", { class: "ic" }, "✕"),
      h("div", {}, "Latest build was refused by the quality gate", h("small", {}, "This page shows the previous good build.")));
  } else if (warns.length) {
    banner = h("div", { class: "banner warning" }, h("span", { class: "ic" }, "▲"),
      h("div", {}, `Published, with ${warns.length} warning${warns.length === 1 ? "" : "s"}`, h("small", {}, "Warnings do not stop publishing; they are listed below.")));
  } else {
    banner = h("div", { class: "banner good" }, h("span", { class: "ic" }, "✓"), h("div", {}, "Published, all checks passed"));
  }

  const counts = h("div", { class: "counts" },
    [["checks", hl.counts.checks], ["passed", hl.counts.passed], ["blocking", hl.counts.blocking_failures], ["warnings", hl.counts.warnings], ["known issues", hl.counts.known]]
      .map(([label, v]) => h("div", {}, h("b", {}, v), label)));

  const stateRow = (c) => h("tr", {}, h("td", {}, pill(checkState(c))), h("td", {}, c.dataset), h("td", {}, c.name),
    h("td", {}, c.message, Array.isArray(c.known) && c.known.length ? ` (known: ${c.known.join(", ")})` : ""));
  const issues = [...blocking, ...warns, ...known];
  const issueTable = issues.length
    ? h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Status", "Dataset", "Check", "What it says"].map((x) => h("th", {}, x)))), h("tbody", {}, issues.map(stateRow))))
    : h("p", { class: "muted" }, "Nothing to report.");

  const fresh = hl.checks.filter((c) => c.name === "freshness");
  const freshTable = h("div", { class: "scroll" }, h("table", {},
    h("thead", {}, h("tr", {}, ["Dataset", "Age of newest data", "Allowed", "Last fetched", "Status"].map((x) => h("th", {}, x)))),
    h("tbody", {}, fresh.map((c) => h("tr", {}, h("td", {}, c.dataset), h("td", { class: "num" }, c.observed), h("td", {}, c.threshold),
      h("td", {}, hl.last_fetch[c.dataset] ? fmtStamp(hl.last_fetch[c.dataset]) : "–"), h("td", {}, pill(checkState(c))))))));

  const runs = h("ul", { class: "runs", "aria-label": "Recent quality-gate runs" }, hl.history.map((r) => {
    const st = !r.published ? STATUS.blocking : r.warnings ? STATUS.warning : STATUS.pass;
    return h("li", { class: st.cls, title: `${r.batch_id}: ${r.passed}/${r.checks} passed, ${r.blocking} blocking, ${r.warnings} warnings` },
      `${st.icon} ${r.batch_id.slice(4, 6)}-${r.batch_id.slice(6, 8)}`);
  }));

  const totalRev = hl.revisions.reduce((a, r) => ({ added: a.added + r.added, changed: a.changed + r.changed, removed: a.removed + r.removed }), { added: 0, changed: 0, removed: 0 });
  const revTable = h("div", { class: "scroll" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Dataset"), ["New rows", "Changed values", "Removed rows"].map((x) => h("th", { class: "num" }, x)))),
    h("tbody", {}, hl.revisions.map((r) => h("tr", {}, h("td", {}, r.dataset), h("td", { class: "num" }, fmtNum(r.added)), h("td", { class: "num" }, fmtNum(r.changed)), h("td", { class: "num" }, fmtNum(r.removed)))))));

  const rej = hl.rejected.length
    ? h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["Dataset", "Reason", "Rows"].map((x) => h("th", {}, x)))),
        h("tbody", {}, hl.rejected.map((r) => h("tr", {}, h("td", {}, r.dataset), h("td", {}, r.reason), h("td", { class: "num" }, r.rows))))))
    : h("p", { class: "muted" }, "No rows rejected.");

  host.replaceChildren(
    banner, counts,
    h("p", { class: "muted" }, `Batch ${hl.batch_id}, checked ${fmtStamp(Date.parse(hl.checked_at) / 1000)}. Raw state `, h("code", {}, hl.raw_state_sha256.slice(0, 16)), "."),
    h("h3", {}, "Checks that need attention"), issueTable,
    h("h3", {}, "Freshness"), freshTable,
    h("h3", {}, `Recent runs (${hl.history.length})`), runs,
    h("p", { class: "muted" }, "Each box is one pipeline run. ✓ published clean, ▲ published with warnings, ✕ refused."),
    h("h3", {}, "Revisions"),
    h("p", { class: "muted" }, `Publishers sometimes change values after the fact. Every version is kept. In the whole history: ${fmtNum(totalRev.changed)} changed values, ${fmtNum(totalRev.removed)} removed rows, ${fmtNum(totalRev.added)} rows that arrived late or for a later day.`),
    revTable,
    h("h3", {}, "Rejected rows"), rej);
}

// ---------------------------------------------------------------- page

function render(doc) {
  const latest = doc.window.to;
  document.getElementById("lede").textContent =
    `Data up to ${fmtStamp(latest + 3600)}. Last build ${fmtStamp(doc.batch.built_at)}, batch ${doc.batch.id}.`;
  renderKpis(doc);
  const g = doc.generation;
  drawChart(document.getElementById("chart-gen"), {
    t: g.t, mode: "stack", unit: "MW", label: "Hourly generation by source, stacked area chart",
    series: g.groups.map(({ name, values }, i) => ({ name, values, color: GEN_COLORS[i] })),
  });
  drawChart(document.getElementById("chart-dem"), {
    t: doc.demand.t, mode: "line", unit: "MW", label: "Hourly demand, line chart",
    series: [{ name: "Demand", values: doc.demand.v, color: "--series-1" }],
  });
  drawChart(document.getElementById("chart-pri"), {
    t: doc.price.t, mode: "line", unit: "EUR/MWh", digits: 1, signed: true, label: "Hourly day-ahead price, line chart",
    series: [{ name: "OMIE price", values: doc.price.v, color: "--series-2" }],
  });
  renderHealth(doc);
  document.getElementById("credits").replaceChildren(...doc.credits.map((c) =>
    h("li", {}, h("a", { href: c.url }, c.name), `: ${c.use}. ${c.licence}`)));
}

fetch("data/dashboard.json", { cache: "no-cache" })
  .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); })
  .then(render)
  .catch((e) => { document.getElementById("lede").textContent = `Could not load the data: ${e.message}`; });
