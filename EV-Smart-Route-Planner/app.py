import html
import logging
import math
import re

import pandas as pd
import streamlit as st

import ev_engine as eng
from ev_engine import EVPlannerError, VEHICLE_DATABASES

log = logging.getLogger("ev_planner")

# ==========================================
# PAGE CONFIG & MODERN CUSTOM CSS
# ==========================================

st.set_page_config(
    page_title="Smart EV Route Planner",
    page_icon="⚡",
    layout="centered",
    initial_sidebar_state="collapsed"
)

st.markdown(
    """
    <style>
    /* Global Page Styling */
    #MainMenu, footer, header { visibility: hidden; }
    .block-container {
        max-width: 850px;
        padding-top: 2rem;
        padding-bottom: 3rem;
    }

    /* Modern Header Card */
    .app-header {
        background: linear-gradient(135deg, #0f2027, #203a43, #2c5364);
        padding: 24px;
        border-radius: 16px;
        color: white;
        text-align: center;
        margin-bottom: 24px;
        box-shadow: 0px 10px 20px rgba(0, 0, 0, 0.15);
    }
    .app-header h1 {
        color: #00e676 !important;
        margin: 0;
        font-size: 2.2rem;
        font-weight: 700;
    }
    .app-header p {
        color: #cfd8dc;
        margin-top: 6px;
        font-size: 1rem;
    }
    @media (max-width: 600px) {
        .app-header { padding: 16px; }
        .app-header h1 { font-size: 1.5rem; }
        .app-header p { font-size: 0.85rem; }
    }

    /* Custom Metric Cards */
    div[data-testid="stMetricValue"] {
        font-size: 1.6rem !important;
        font-weight: 700 !important;
        color: #10b981 !important;
    }
    div[data-testid="stMetricLabel"] {
        font-weight: 600 !important;
    }

    /* Custom Radio Box Styling (explicit text colour so it stays readable in dark theme) */
    div[data-testid="stRadio"] div[role="radiogroup"] > label {
        background-color: #f8fafc;
        border: 2px solid #e2e8f0;
        border-radius: 12px;
        padding: 14px 18px;
        margin-bottom: 10px;
        transition: all 0.2s ease-in-out;
        cursor: pointer;
    }
    div[data-testid="stRadio"] div[role="radiogroup"] > label,
    div[data-testid="stRadio"] div[role="radiogroup"] > label * {
        color: #1f2937 !important;
    }
    div[data-testid="stRadio"] div[role="radiogroup"] > label:hover {
        border-color: #10b981;
        background-color: #f0fdf4;
    }

    /* Primary Buttons */
    div.stButton > button {
        width: 100%;
        height: 52px;
        border-radius: 12px;
        border: none;
        background: linear-gradient(135deg, #10b981, #059669);
        color: white;
        font-weight: 700;
        font-size: 16px;
        box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
        transition: all 0.2s ease;
    }
    div.stButton > button:hover {
        background: linear-gradient(135deg, #059669, #047857);
        box-shadow: 0 6px 16px rgba(16, 185, 129, 0.4);
        color: white;
    }

    /* Route details: ordered start -> cities/towns -> destination */
    .route-seq { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 4px; margin: 6px 0 10px 0; }
    .route-seq .stop { display: inline-flex; flex-direction: column; align-items: center; line-height: 1.15;
        padding: 6px 12px; border-radius: 10px; border: 1.5px solid #cbd5e1; background: #f8fafc; color: #1f2937; }
    .route-seq .stop b { font-size: 0.95rem; color: inherit; }
    .route-seq .stop small { font-size: 0.72rem; color: inherit; opacity: 0.75; }
    .route-seq .stop.start { background: #0f172a; border-color: #0f172a; color: #ffffff; }
    .route-seq .stop.dest { background: #dc2626; border-color: #dc2626; color: #ffffff; }
    .route-seq .stop.city { border-color: #0f172a; font-weight: 700; }
    .route-seq .arr { color: #64748b; font-size: 1.1rem; }
    </style>
    """,
    unsafe_allow_html=True
)

# ==========================================
# SMALL HELPERS
# ==========================================

def fmt_minutes(m):
    m = int(round(m))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def route_status(e):
    if not e["feasible"]:
        return "⛔ battery insufficient"
    if e["below_reserve"]:
        return "⚠️ below 10% reserve"
    if e["low_soc"]:
        return "⚠️ low arrival SOC"
    return ""


VEHICLE_ICONS = {"Electric Car (4-Wheeler)": "🚗", "Electric Two-Wheeler": "🛵", "Electric Three-Wheeler": "🛺"}

# Selected route is always GREEN on the map. Every other route keeps its own colour (by route number).
ROUTE_ALT_COLORS = [("#f97316", "orange"), ("#db2777", "pink"), ("#7c3aed", "violet"),
                    ("#92400e", "brown"), ("#0891b2", "teal"), ("#ca8a04", "gold")]


def _md_safe(text):
    return str(text).replace("*", "").replace("_", " ").replace("[", "(").replace("]", ")").replace("`", "'")

FRAME_FIT_JS = r"""
(function () {
  // Fit this frame to its own content: no empty strip underneath and no inner scrollbar, also when the content grows
  // (journey banner, arrival summary, narrow phone). If the host blocks access to the frame, nothing happens and the
  // height given by the page is used.
  var fe = null;
  try { fe = window.frameElement; } catch (e) { fe = null; }
  var timer = 0, last = -1;
  function fit() {
    timer = 0;
    try {
      var h = Math.min(2400, Math.max(120, Math.ceil(document.body.getBoundingClientRect().height) + 4));
      if (h !== last) {
        last = h;
        if (fe) {
          fe.style.height = h + "px"; fe.setAttribute("height", String(h));
          // Streamlit wraps the frame in containers that can keep the OLD height; then a taller frame would overlap the
          // next element (e.g. the checkbox below the map). Grow the nearest wrappers together with the frame.
          var el = fe.parentElement;
          for (var k = 0; k < 3 && el && el.tagName !== "BODY"; k++, el = el.parentElement) {
            el.style.height = h + "px"; el.style.minHeight = h + "px";
          }
        }
        // Also ask the host page (Streamlit) to size the frame, for hosts that do not allow direct access to it.
        try { window.parent.postMessage({isStreamlitMessage: true, type: "streamlit:setFrameHeight", height: h}, "*"); } catch (e) {}
      }
    } catch (e) {}
  }
  function queue() { if (!timer) timer = setTimeout(fit, 30); }
  try { new ResizeObserver(queue).observe(document.body); } catch (e) {}
  window.addEventListener("load", queue); window.addEventListener("resize", queue); queue();
})();
"""

MAP_HTML = r"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  *{box-sizing:border-box}
  html,body{background:transparent}
  body{margin:0;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color:#0f2137;-webkit-text-size-adjust:100%}
  .head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px;flex-wrap:wrap}
  .head b{font-size:16px;color:#0a1f3c}
  .pill{font-size:12px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:999px;padding:4px 11px;color:#334155}
  .pill.ok{background:#ecfdf5;border-color:#10b981;color:#065f46}
  .pill.bad{background:#fef2f2;border-color:#ef4444;color:#991b1b}
  .btns{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
  button{border:0;border-radius:12px;padding:0 16px;min-height:46px;font-weight:700;font-size:15px;cursor:pointer;color:#032b1f;background:#10b981;touch-action:manipulation}
  button.alt{background:#e2e8f0;color:#0a1f3c} button.danger{background:#dc2626;color:#fff} button.blue{background:#12315a;color:#fff}
  button:disabled{opacity:.5;cursor:default}
  .btns button{flex:1 1 140px}
  [hidden]{display:none!important}
  .banner{border-radius:10px;padding:10px 12px;margin-bottom:10px;font-size:14px;line-height:1.35;border:1px solid}
  .banner.info{background:#eff6ff;border-color:#93c5fd;color:#1e3a8a}
  .banner.warn{background:#fffbeb;border-color:#fbbf24;color:#92400e}
  .banner.err{background:#fef2f2;border-color:#f87171;color:#991b1b}
  .banner.ok{background:#ecfdf5;border-color:#34d399;color:#065f46}
  .banner a{color:inherit;font-weight:700}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:10px}
  @media (max-width:560px){.cards{grid-template-columns:repeat(2,1fr)} #map{height:400px!important} button{font-size:14px} .legend span{white-space:normal}}
  .card{background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;min-width:0}
  .card b{display:block;font-size:11px;color:#64748b;font-weight:700;text-transform:uppercase;letter-spacing:.05em}
  .card span{font-size:21px;font-weight:800;display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#0a1f3c}
  .card small{display:block;font-size:11px;color:#64748b;min-height:14px}
  .bar{height:7px;background:#e2e8f0;border-radius:4px;margin-top:3px;overflow:hidden}
  .bar i{display:block;height:100%;width:0;background:#10b981;transition:width .3s,background .3s}
  .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px;font-size:13px}
  details{margin-bottom:2px;font-size:13px}
  summary{cursor:pointer;font-weight:600;padding:6px 0;color:#0a1f3c}
  input[type=number]{width:80px;padding:8px;border-radius:8px;border:1px solid #cbd5e1;font-size:15px}
  select{padding:8px;border-radius:8px;border:1px solid #cbd5e1}
  .mapbox{position:relative}
  #map{height:560px;border-radius:14px;border:1px solid #e2e8f0;box-shadow:0 1px 2px rgba(10,31,60,.06)}
  #b_recenter{position:absolute;right:10px;bottom:28px;z-index:1000;min-height:42px;box-shadow:0 2px 6px rgba(0,0,0,.35)}
  .me{position:relative;width:22px;height:22px}
  .me .dot{position:absolute;inset:3px;border-radius:50%;background:#2563eb;border:3px solid #fff;box-shadow:0 0 0 2px rgba(37,99,235,.45),0 1px 5px rgba(0,0,0,.5)}
  .me .arrow{position:absolute;left:5px;top:-9px;width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-bottom:12px solid #2563eb;transform-origin:6px 20px;display:none}
  /* Live navigation shows only the journey controls: the figures repeat what the page already shows (still updated in the background) */
  .cards,#calib,.row label{display:none!important}
  .row:not(:has(#b_recalc:not([hidden]))){display:none}
  .legend{font-size:12.5px;margin:10px 2px 0;color:#334155;line-height:1.75}
  .legend span{margin-right:14px;white-space:nowrap}
  .dot2{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:4px;vertical-align:-1px;border:2px solid #fff;box-shadow:0 0 0 1px #9ca3af}
  .lbl{background:#fff;border:2px solid #7c3aed;border-radius:6px;padding:1px 5px;font-weight:700;font-size:11px}
  .lbl.town{border:1.5px solid #0f172a;color:#111827;font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.35)}
  .badge{width:30px;height:30px;border-radius:50%;color:#fff;font-weight:800;font-size:15px;line-height:24px;text-align:center;border:3px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.6)}
  .sw{display:inline-block;width:22px;height:6px;border-radius:3px;margin-right:5px;vertical-align:middle}
  .panel{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:14px 16px 10px;margin-top:14px;box-shadow:0 1px 2px rgba(10,31,60,.05)}
  #summary{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:12px 16px;margin-top:10px}
  #summary h4{margin:0 0 6px;color:#0a1f3c}
  #summary td{padding:2px 12px 2px 0;font-size:14px}
</style>

<div class="mapbox"><div id="map"></div><button id="b_recenter" class="blue" hidden>🎯 Recenter</button></div>
<div class="legend" id="legend"></div>

<div class="panel">
<div class="head"><b>🧭 Live navigation</b><span id="gps_pill" class="pill">GPS idle</span></div>
<div class="btns">
  <button id="b_start">▶ Start Journey</button>
  <button id="b_pause" class="alt" hidden>⏸ Pause</button>
  <button id="b_resume" hidden>▶ Resume</button>
  <button id="b_end" class="danger" hidden>■ End Journey</button>
</div>
<div id="banner" class="banner info" hidden></div>
<div class="cards">
  <div class="card"><b>Remaining distance</b><span id="n_rem">–</span><small id="n_rem2"></small></div>
  <div class="card"><b>Remaining time</b><span id="n_time">–</span><small id="n_eta"></small></div>
  <div class="card"><b>Battery now (est.)</b><span id="n_soc">–</span><div class="bar"><i id="n_bar"></i></div><small id="n_soc2"></small></div>
  <div class="card"><b>Arrival SOC (est.)</b><span id="n_arr">–</span><small id="n_arr2"></small></div>
  <div class="card"><b>Speed (GPS)</b><span id="n_spd">–</span><small id="n_trav"></small></div>
  <div class="card"><b>Route status</b><span id="n_state">Not started</span><small id="n_dev"></small></div>
</div>
<div class="row">
  <button id="b_recalc" class="blue" hidden style="min-height:40px">⟳ Recalculate route</button>
  <label><input type="checkbox" id="c_auto" checked> Auto-recalculate if off-route</label>
</div>
<details id="calib">
  <summary>Battery % different on your dashboard? Set the actual value</summary>
  <input type="number" id="i_soc" min="1" max="100" step="1"> %
  <button id="b_setsoc" class="alt" style="min-height:38px">Set</button>
  <small style="display:block;color:#6b7280;margin-top:4px">The app has no live link to the vehicle, so battery use is a model estimate. Setting the real value re-bases the estimate from here.</small>
</details>
</div>
<div id="summary" hidden></div>

<script>
const D = __DATA__;
// ==PURE-START==
const RE = 6371000, rad = x => x * Math.PI / 180;
function hav(a1, o1, a2, o2) {
  const p1 = rad(a1), p2 = rad(a2), dl = rad(a2 - a1), dn = rad(o2 - o1);
  const a = Math.sin(dl / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dn / 2) ** 2;
  return 2 * RE * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}
function cumDist(path, totalM) {            // path: [[lat,lon],...] -> cumulative metres, scaled to totalM
  const c = [0];
  for (let i = 1; i < path.length; i++) c.push(c[i - 1] + hav(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1]));
  const k = (totalM && c[c.length - 1] > 0) ? totalM / c[c.length - 1] : 1;
  return c.map(x => x * k);
}
function segProj(lat0, lon0, a, b) {        // distance (m) from point to segment a-b, local planar approximation
  const ky = RE * Math.PI / 180, kx = Math.cos(rad(lat0)) * ky;
  const ax = (a[1] - lon0) * kx, ay = (a[0] - lat0) * ky, bx = (b[1] - lon0) * kx, by = (b[0] - lat0) * ky;
  const dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
  let t = L2 > 0 ? (-(ax * dx + ay * dy)) / L2 : 0;
  t = Math.max(0, Math.min(1, t));
  return {d: Math.hypot(ax + t * dx, ay + t * dy), t};
}
function project(path, cum, lat, lon, i0, i1) {
  let best = {d: Infinity, s: 0, i: 0};
  i0 = Math.max(0, i0); i1 = Math.min(path.length - 2, i1);
  for (let i = i0; i <= i1; i++) {
    const r = segProj(lat, lon, path[i], path[i + 1]);
    if (r.d < best.d) best = {d: r.d, s: cum[i] + r.t * (cum[i + 1] - cum[i]), i};
  }
  return best;
}
function socAt(trace, sM) {                 // unclamped SOC (%) at route distance sM from the energy trace
  const km = sM / 1000;
  if (km <= trace[0].distance_km) return trace[0].soc_raw;
  const last = trace[trace.length - 1];
  if (km >= last.distance_km) return last.soc_raw;
  let lo = 0, hi = trace.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (trace[m].distance_km <= km) lo = m; else hi = m; }
  const a = trace[lo], b = trace[hi], span = b.distance_km - a.distance_km;
  const u = span > 0 ? (km - a.distance_km) / span : 0;
  return a.soc_raw + (b.soc_raw - a.soc_raw) * u;
}
function samplePoints(path, distM, M) {     // port of ev_engine.sample_route_points
  const cum = [0];
  for (let i = 1; i < path.length; i++) cum.push(cum[i - 1] + hav(path[i - 1][0], path[i - 1][1], path[i][0], path[i][1]));
  const total = cum[cum.length - 1];
  const nSeg = Math.max(1, Math.min(M.maxPoints - 1, Math.floor(total / M.minSpacing)));
  const spacing = total / nSeg, scale = distM ? distM / total : 1;
  const pts = []; let j = 0;
  for (let k = 0; k <= nSeg; k++) {
    const target = k * spacing;
    while (j < cum.length - 2 && cum[j + 1] < target) j++;
    const seg = cum[j + 1] - cum[j];
    const t = seg <= 0 ? 0 : Math.min(1, Math.max(0, (target - cum[j]) / seg));
    pts.push({lat: path[j][0] + t * (path[j + 1][0] - path[j][0]), lon: path[j][1] + t * (path[j + 1][1] - path[j][1]), d: target * scale});
  }
  return pts;
}
function fillMissing(v) {                   // port of ev_engine.fill_missing; returns null if nothing usable
  const known = []; v.forEach((x, i) => { if (x !== null && x !== undefined && isFinite(x)) known.push(i); });
  if (!known.length) return null;
  return v.map((x, i) => {
    if (known.includes(i)) return x;
    let l = null, r = null;
    for (const k of known) { if (k < i) l = k; else if (k > i && r === null) r = k; }
    if (l === null) return v[r];
    if (r === null) return v[l];
    return v[l] + (i - l) / (r - l) * (v[r] - v[l]);
  });
}
function smooth(v, w) {                     // port of ev_engine.smooth
  if (w <= 1 || v.length < 3) return v.slice();
  const h = Math.floor(w / 2);
  return v.map((_, i) => { const s = v.slice(Math.max(0, i - h), i + h + 1); return s.reduce((a, b) => a + b, 0) / s.length; });
}
function calcEnergy(pts, M, socStart, speedKmh) {   // port of ev_engine.calculate_energy (same equations/constants)
  const sp = Math.min(Math.max(speedKmh || M.defaultCruise, M.minCruise), M.maxCruise), v = sp / 3.6;
  const elevOk = pts.every(p => p.el !== null && p.el !== undefined);
  let socKwh = M.usable * socStart / 100, minKwh = socKwh, trac = 0, regen = 0, aux = 0, depKm = null;
  const trace = [{distance_km: pts[0].d / 1000, soc_pct: socStart, soc_raw: socStart}];
  for (let i = 1; i < pts.length; i++) {
    const d = pts[i].d - pts[i - 1].d;
    if (d <= 1e-6) continue;
    let grade = 0;
    if (elevOk) grade = Math.max(-M.maxGrade, Math.min(M.maxGrade, (pts[i].el - pts[i - 1].el) / d));
    const th = Math.atan(grade);
    const frr = M.crr * M.mass * M.g * Math.cos(th), fa = 0.5 * M.rho * M.cd * M.area * v * v, fg = M.mass * M.g * Math.sin(th);
    const w = (frr + fa + fg) * d, tH = d / v / 3600;
    let eb;
    if (w >= 0) { eb = w / M.etaM / 3.6e6; trac += eb; }
    else {
      let rec = Math.abs(w) * M.etaR / 3.6e6;
      if (socKwh / M.usable * 100 >= M.regenCutoff) rec = 0;
      else rec = Math.min(rec, M.regenCapKw * tH, Math.max(0, M.usable - socKwh));
      eb = -rec; regen += rec;
    }
    const ea = M.auxKw * tH; aux += ea;
    socKwh -= (eb + ea); minKwh = Math.min(minKwh, socKwh);
    const pct = socKwh / M.usable * 100;
    if (depKm === null && socKwh <= 0) depKm = pts[i].d / 1000;
    trace.push({distance_km: pts[i].d / 1000, soc_pct: Math.max(0, pct), soc_raw: pct});
  }
  return {total: trac - regen + aux, traction: trac, regen, aux, finalRaw: socKwh / M.usable * 100,
          feasible: depKm === null, depletedKm: depKm, shortfallKwh: Math.max(0, -minKwh), elevOk, trace, speedKmh: sp};
}
// ==PURE-END==

const M = D.model;
const $ = id => document.getElementById(id);
const fmtKm = m => (m >= 10000 ? (m / 1000).toFixed(0) : (m / 1000).toFixed(1)) + " km";
const fmtMin = mi => { mi = Math.max(0, Math.round(mi)); return mi >= 60 ? Math.floor(mi / 60) + " h " + String(mi % 60).padStart(2, "0") + " min" : mi + " min"; };
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

if (typeof L === "undefined") {
  $("map").innerHTML = "<p style='padding:20px'>Map library could not load (internet or firewall blocking unpkg.com).</p>";
} else {
// ---------------- map & planned routes ----------------
const map = L.map("map");
// Basemap: CARTO raster tiles need an API key since 25 Sep 2026 (they show "API KEY REQUIRED"), so the default is
// OpenStreetMap's own keyless tile server; the layer switcher (top right) offers alternatives.
const TILE_SETS = {
  "OpenStreetMap": L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    {maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"}),
  "Esri Streets": L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
    {maxZoom: 19, attribution: "Tiles &copy; Esri, HERE, Garmin, OpenStreetMap contributors"}),
  "OpenTopoMap": L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    {maxZoom: 17, subdomains: "abc", attribution: "&copy; OpenStreetMap contributors, SRTM | &copy; OpenTopoMap (CC-BY-SA)"})
};
TILE_SETS["OpenStreetMap"].addTo(map);
L.control.layers(TILE_SETS, null, {position: "topright"}).addTo(map);

let bounds = null, selLine = null;
const routeTip = r => "<b>" + esc(r.name) + (r.sel ? " (selected)" : "") + "</b><br>" + r.km.toFixed(1) + " km · " + fmtMin(r.min) + " · " + r.kwh.toFixed(1) + " kWh";
// alternatives first (each in its own colour), the selected route last so it is drawn on top, in green
D.routes.forEach(r => { if (!r.sel) L.polyline(r.path, {color: r.color, weight: 5, opacity: .9}).bindTooltip(routeTip(r), {sticky: true}).addTo(map); });
D.routes.forEach(r => { if (r.sel) { selLine = L.polyline(r.path, {color: "#10b981", weight: 7, opacity: 1}).bindTooltip(routeTip(r), {sticky: true}).addTo(map); bounds = selLine.getBounds(); } });
map.fitBounds(bounds, {padding: [20, 20]});

function pin(lat, lon, color, r, tip, perm) {
  const m = L.circleMarker([lat, lon], {radius: r, color: "#fff", weight: 3, fillColor: color, fillOpacity: 1}).addTo(map);
  m.bindTooltip(tip, perm ? {permanent: true, direction: "top", className: "lbl", offset: [0, -6]} : {});
  return m;
}
function badge(lat, lon, txt, bg, label) {
  const m = L.marker([lat, lon], {icon: L.divIcon({html: "<div class='badge' style='background:" + bg + "'>" + txt + "</div>", className: "", iconSize: [30, 30], iconAnchor: [15, 15]}), zIndexOffset: 1000}).addTo(map);
  m.bindTooltip(label, {permanent: true, direction: "right", offset: [16, 0], className: "lbl town"});
  return m;
}
const legend = [];
badge(D.start.lat, D.start.lon, "A", "#0f172a", "A · " + esc(D.start.name));
badge(D.dest.lat, D.dest.lon, "B", "#dc2626", "B · " + esc(D.dest.name));
// cities / towns on the selected route, numbered in driving order
(D.places || []).forEach((p, i) => {
  L.circleMarker([p.lat, p.lon], {radius: 6, color: "#fff", weight: 2, fillColor: "#0f172a", fillOpacity: 1}).addTo(map)
    .bindTooltip((i + 1) + " · " + esc(p.name) + " (km " + p.km.toFixed(0) + ")", {permanent: true, direction: "right", offset: [8, 0], className: "lbl town"});
});
if (D.low) { pin(D.low.lat, D.low.lon, "#7c3aed", 11, "Battery drops below 20% (km " + D.low.km.toFixed(0) + ")", true); legend.push(["#7c3aed", "Battery below 20%"]); }
if (D.empty) { pin(D.empty.lat, D.empty.lon, "#111827", 11, "Battery EMPTY (km " + D.empty.km.toFixed(0) + ")", true); legend.push(["#111827", "Battery empty"]); }
if (D.stations.length) {
  const stBounds = L.latLngBounds(bounds.getSouthWest(), bounds.getNorthEast());
  D.stations.forEach(s => {
    const info = "<b>" + esc(s.name) + "</b>" + (s.addr ? "<br>" + esc(s.addr) : "") + (s.op ? "<br>Operator: " + esc(s.op) : "") +
      (s.sockets ? "<br>Connectors: " + esc(s.sockets) : "") + (s.cap ? "<br>Charging points: " + esc(s.cap) : "") +
      "<br>" + s.dist.toFixed(1) + " km from the search point" +
      (s.est != null ? "<br>Est. battery on arrival: " + Math.max(0, s.est).toFixed(1) + "% (" + ({yes: "reachable", low: "reachable, low battery", no: "not reachable on this charge"}[s.reach] || "") + ")" : "");
    L.marker([s.lat, s.lon], {icon: L.divIcon({html: "<div class='badge' style='background:#f59e0b;color:#111827'>&#9889;</div>", className: "", iconSize: [30, 30], iconAnchor: [15, 15]}), zIndexOffset: 800})
      .addTo(map).bindTooltip(esc(s.name), {direction: "top", offset: [0, -14]}).bindPopup(info);
    stBounds.extend([s.lat, s.lon]);
  });
  map.fitBounds(stBounds, {padding: [30, 30]});   // make sure the chargers are actually in view
  legend.push(["#f59e0b", "Charging station (" + D.stations.length + ", click for details)"]);
}
const swatch = (c, t) => "<span><i class='dot2' style='background:" + c + "'></i>" + t + "</span>";
$("legend").innerHTML =
  D.routes.map(r => "<span><i class='sw' style='background:" + (r.sel ? "#10b981" : r.color) + "'></i>" + esc(r.name) + (r.sel ? " (selected)" : "") + ": " + r.km.toFixed(0) + " km, " + fmtMin(r.min) + "</span>").join("") +
  "<br>" + swatch("#0f172a", "A = start") + swatch("#dc2626", "B = destination") + swatch("#2563eb", "blue dot with ring = your live GPS position") +
  ((D.places || []).length ? swatch("#0f172a", "numbered dots = cities/towns on the selected route, in order") : "") +
  legend.map(l => swatch(l[0], l[1])).join("") +
  (D.placesNote ? "<br><i>" + esc(D.placesNote) + "</i>" : "");

// ---------------- live navigation (real GPS only) ----------------
const NAV = {state: "idle"};
const planned = {path: D.nav.path, cum: cumDist(D.nav.path, D.nav.distM), distM: D.nav.distM, durMin: D.durationMin, trace: D.trace, rerouted: false};
let route = planned;
let watchId = null, wakeLock = null, userMarker = null, accCircle = null, navLine = null, following = true;
let lastFix = null, travM = 0, movingS = 0, activeMs = 0, tickT = null, hint = 0, progressM = 0;
let firstFix = false, socOffset = D.plannedSoc - D.trace[0].soc_raw, offCount = 0, isOff = false, lastDev = 0, reroutes = 0;
let recalcBusy = false, lastRecalc = 0, lastPos = null, journeyStartSoc = null, lastSpeedKmh = null, arrived = false;
const SIG = "evnav|" + D.start.lat.toFixed(4) + "," + D.start.lon.toFixed(4) + "|" + D.dest.lat.toFixed(4) + "," + D.dest.lon.toFixed(4) + "|" + M.name + "|r" + D.selIdx + "|" + Math.round(D.nav.distM);
$("i_soc").value = Math.round(D.plannedSoc);

let stickUntil = 0;
function banner(msg, kind, stick) { const b = $("banner"); b.className = "banner " + (kind || "info"); b.innerHTML = msg; b.hidden = !msg; stickUntil = Date.now() + (stick || 0); }
function pill(txt, cls) { const p = $("gps_pill"); p.textContent = txt; p.className = "pill " + (cls || ""); }
function setState(s) {
  NAV.state = s;
  $("b_start").hidden = s !== "idle";
  $("b_pause").hidden = s !== "active";
  $("b_resume").hidden = s !== "paused";
  $("b_end").hidden = !(s === "active" || s === "paused" || s === "arrived");
  $("b_end").textContent = s === "arrived" ? "■ Finish" : "■ End Journey";
  $("b_recalc").hidden = s !== "active";
}
function socNowRaw() { return socAt(route.trace, progressM) + socOffset; }
function socEndRaw() { return socAt(route.trace, route.distM) + socOffset; }

function render() {
  const rem = Math.max(0, route.distM - progressM) + (isOff ? lastDev : 0);
  $("n_rem").textContent = fmtKm(rem);
  $("n_rem2").textContent = "of " + fmtKm(route.distM) + (route.rerouted ? " (new route)" : "");
  let remMin = route.durMin * Math.max(0, route.distM - progressM) / route.distM, basis = "planned pace";
  if (travM >= 2000 && movingS >= 180) {
    const pace = clamp((travM / 1000) / (movingS / 3600), 10, 130);
    remMin = (Math.max(0, route.distM - progressM) / 1000) / pace * 60; basis = "your pace " + pace.toFixed(0) + " km/h";
  }
  $("n_time").textContent = fmtMin(remMin);
  $("n_eta").textContent = "arrive " + new Date(Date.now() + remMin * 60000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}) + " · " + basis;
  const sNow = socNowRaw(), sEnd = socEndRaw();
  $("n_soc").textContent = clamp(sNow, 0, 100).toFixed(1) + "%";
  const bar = $("n_bar"); bar.style.width = clamp(sNow, 0, 100) + "%"; bar.style.background = sNow < M.reserve ? "#dc2626" : sNow < M.lowSoc ? "#f59e0b" : "#10b981";
  const usedKwh = journeyStartSoc === null ? 0 : (journeyStartSoc - sNow) / 100 * M.usable;
  $("n_soc2").textContent = "used so far ≈ " + Math.max(0, usedKwh).toFixed(2) + " kWh";
  $("n_arr").textContent = clamp(sEnd, 0, 100).toFixed(1) + "%";
  $("n_arr").style.color = sEnd < 0 ? "#dc2626" : sEnd < M.reserve ? "#dc2626" : sEnd < M.lowSoc ? "#d97706" : "";
  $("n_arr2").textContent = "needs ≈ " + Math.max(0, (sNow - sEnd) / 100 * M.usable).toFixed(2) + " kWh more";
  $("n_spd").textContent = lastSpeedKmh === null ? "–" : lastSpeedKmh.toFixed(0) + " km/h";
  $("n_trav").textContent = "driven " + fmtKm(travM) + " (GPS)";
  $("n_state").textContent = NAV.state === "idle" ? "Not started" : NAV.state === "paused" ? "Paused" : arrived ? "Arrived" : isOff ? "OFF ROUTE" : "On route";
  $("n_state").style.color = isOff ? "#dc2626" : "";
  $("n_dev").textContent = NAV.state === "idle" ? "" : "from route: " + Math.round(lastDev) + " m";
}

function userIcon() {
  return L.divIcon({html: "<div class='me'><div class='arrow' id='me_arrow'></div><div class='dot'></div></div>", className: "", iconSize: [22, 22], iconAnchor: [11, 11]});
}
function showUser(lat, lon, acc, heading) {
  if (!userMarker) { userMarker = L.marker([lat, lon], {icon: userIcon(), zIndexOffset: 2000}).addTo(map); accCircle = L.circle([lat, lon], {radius: acc, color: "#2563eb", weight: 1, fillOpacity: .12}).addTo(map); }
  else { userMarker.setLatLng([lat, lon]); accCircle.setLatLng([lat, lon]); accCircle.setRadius(acc); }
  const el = document.getElementById("me_arrow");
  if (el) { if (heading !== null && heading !== undefined && isFinite(heading)) { el.style.display = "block"; el.style.transform = "rotate(" + heading + "deg)"; } else el.style.display = "none"; }
}
function follow(lat, lon) {
  if (!following) return;
  const z = map.getZoom();
  map.setView([lat, lon], z < 15 ? 16 : z, {animate: true});
}
map.on("dragstart", () => { if (NAV.state === "active") { following = false; $("b_recenter").hidden = false; } });
$("b_recenter").onclick = () => { following = true; $("b_recenter").hidden = true; if (lastPos) map.setView([lastPos.lat, lastPos.lon], Math.max(map.getZoom(), 16)); };

function geoError(e) {
  if (e.code === 1) banner("Location permission was denied. Allow location for this site in your browser/site settings (and make sure device location is on), then press Start again.", "err");
  else if (Date.now() > stickUntil && !isOff && !recalcBusy && $("banner").className.indexOf("err") < 0)   // transient errors never hide important messages
    banner(e.code === 2 ? "Your position is unavailable (no GPS signal). Move to an open area; waiting…" : "Waiting for GPS (timed out). Still trying…", "warn");
  pill(e.code === 1 ? "GPS blocked" : "GPS: no fix", "bad");
}
function startWatch() {
  if (!("geolocation" in navigator)) { banner("This browser has no GPS/geolocation support.", "err"); return false; }
  if (window.isSecureContext === false) {
    banner("GPS needs a secure page. Open the app over <b>https://</b> (e.g. Streamlit Community Cloud or an https tunnel) or on <b>localhost</b>. Plain http://192.168.x.x addresses are blocked by the phone's browser.", "err");
    return false;
  }
  pill("GPS: waiting for fix…");
  watchId = navigator.geolocation.watchPosition(onFix, geoError, {enableHighAccuracy: true, maximumAge: 1000, timeout: 20000});
  getWake();
  return true;
}
function stopWatch() { if (watchId !== null) { navigator.geolocation.clearWatch(watchId); watchId = null; } releaseWake(); }
async function getWake() { try { if ("wakeLock" in navigator) wakeLock = await navigator.wakeLock.request("screen"); } catch (e) {} }
function releaseWake() { try { if (wakeLock) { wakeLock.release(); wakeLock = null; } } catch (e) {} }
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && NAV.state === "active") getWake(); });

function onFix(p) {
  if (NAV.state !== "active") return;
  const c = p.coords, lat = c.latitude, lon = c.longitude, acc = c.accuracy || 999, t = p.timestamp || Date.now();
  lastPos = {lat, lon};
  showUser(lat, lon, acc, c.heading);
  if (acc > 200) { pill("GPS weak ±" + Math.round(acc) + " m", "bad"); banner("GPS signal is weak (±" + Math.round(acc) + " m). Navigation will update when it improves.", "warn"); return; }
  pill("GPS ±" + Math.round(acc) + " m", "ok");
  if ($("banner").className.indexOf("err") < 0 && !isOff && !recalcBusy && Date.now() > stickUntil) banner("", "info");

  if (lastFix) {
    const dM = hav(lastFix.lat, lastFix.lon, lat, lon), dt = (t - lastFix.t) / 1000;
    if (dt > 0 && dM / dt > 70) {                    // > 250 km/h between fixes = GPS glitch: re-base, don't count it
      lastFix = {lat, lon, t};
    } else if (dM > Math.max(5, acc * 0.5)) {
      travM += dM; if (dt > 0 && dt < 120 && dM / dt > 1) movingS += dt;
      if (c.speed === null || c.speed === undefined || !isFinite(c.speed)) lastSpeedKmh = dt > 0 ? dM / dt * 3.6 : null;
      lastFix = {lat, lon, t};
    }
  } else lastFix = {lat, lon, t};
  if (c.speed !== null && c.speed !== undefined && isFinite(c.speed)) lastSpeedKmh = c.speed * 3.6;

  const thr = Math.max(60, 2 * acc);
  let pr = project(route.path, route.cum, lat, lon, hint - 20, hint + 600);
  if (pr.d > thr) { const full = project(route.path, route.cum, lat, lon, 0, route.path.length - 2); if (full.d < pr.d) pr = full; }
  lastDev = pr.d;
  if (firstFix) {                                    // Start pressed: begin from where you actually are
    firstFix = false;
    if (pr.d > Math.max(150, thr)) {                 // not on the planned route (e.g. not at the planned start): plan from here right away
      banner("You are not on the planned route. Planning a route from your current location…", "info");
      recalc(lat, lon, "start"); return;
    }
  }
  if (pr.d <= thr) {
    offCount = 0;
    if (isOff) { isOff = false; banner("Back on route.", "ok", 6000); }
    let s = pr.s; if (s < progressM && progressM - s < 50) s = progressM;
    progressM = s; hint = pr.i;
  } else { offCount++; }

  if (!isOff && offCount >= 3) {
    isOff = true;
    banner("⚠️ You are off route (" + Math.round(pr.d) + " m away). " + ($("c_auto").checked ? "Recalculating…" : "Press <b>Recalculate route</b>."), "warn");
  }
  if (isOff && $("c_auto").checked && !recalcBusy && Date.now() - lastRecalc > 30000) recalc(lat, lon, "off-route");

  const toDest = hav(lat, lon, D.dest.lat, D.dest.lon);
  if (!arrived && (route.distM - progressM < 40 || toDest < 40)) { arrived = true; progressM = route.distM; finish(true); return; }
  following && follow(lat, lon);
  render(); save();
}

// ---------------- recalculation (OSRM + Open-Meteo from the browser, same energy model) ----------------
async function fetchJson(url, ms) {
  const ctl = new AbortController(), id = setTimeout(() => ctl.abort(), ms || 15000);
  try { const r = await fetch(url, {signal: ctl.signal}); if (!r.ok) throw new Error("HTTP " + r.status); return await r.json(); }
  finally { clearTimeout(id); }
}
async function fetchElev(pts) {
  const out = [];
  for (let s = 0; s < pts.length; s += M.chunk) {
    const ch = pts.slice(s, s + M.chunk);
    try {
      const j = await fetchJson("https://api.open-meteo.com/v1/elevation?latitude=" + ch.map(p => p.lat.toFixed(5)).join(",") + "&longitude=" + ch.map(p => p.lon.toFixed(5)).join(","), 12000);
      if (!Array.isArray(j.elevation) || j.elevation.length !== ch.length) throw new Error("bad elevation response");
      j.elevation.forEach(v => out.push(typeof v === "number" && isFinite(v) ? v : null));
    } catch (e) { ch.forEach(() => out.push(null)); }
  }
  return out;
}
async function recalc(lat, lon, reason) {
  if (recalcBusy) return;
  recalcBusy = true; lastRecalc = Date.now();
  banner("⟳ Recalculating route from your position…", "info");
  try {
    const j = await fetchJson("https://router.project-osrm.org/route/v1/driving/" + lon + "," + lat + ";" + D.dest.lon + "," + D.dest.lat + "?overview=full&geometries=geojson", 15000);
    if (j.code !== "Ok" || !j.routes || !j.routes.length) throw new Error(j.code === "NoRoute" ? "no drivable route from here" : "routing service error");
    const r = j.routes[0], path = r.geometry.coordinates.map(c => [c[1], c[0]]);
    if (path.length < 2 || !(r.distance > 0)) throw new Error("empty route");
    const pts = samplePoints(path, r.distance, M);
    const raw = await fetchElev(pts), filled = fillMissing(raw), sm = filled ? smooth(filled, M.smooth) : null;
    pts.forEach((p, i) => { p.el = sm ? sm[i] : null; });
    const socHere = clamp(socNowRaw(), 0.01, 100);
    const cruise = (r.distance / 1000) / (r.duration / 3600);
    const en = calcEnergy(pts, M, socHere, cruise);
    route = {path, cum: cumDist(path, r.distance), distM: r.distance, durMin: Math.round(r.duration / 60 * M.travelFactor), trace: en.trace, rerouted: true};
    socOffset = 0; progressM = 0; hint = 0; offCount = 0; isOff = false; reroutes++;
    if (selLine) selLine.setStyle({color: "#9ca3af", weight: 4, dashArray: "6 8"});
    if (navLine) map.removeLayer(navLine);
    navLine = L.polyline(path, {color: "#10b981", weight: 6}).bindTooltip("Recalculated route").addTo(map);
    banner("✅ New route found: " + fmtKm(r.distance) + ", " + fmtMin(route.durMin) + ", arrival ≈ " + clamp(en.finalRaw, 0, 100).toFixed(0) + "% battery" +
      (en.elevOk ? "" : " (terrain unavailable: flat-road estimate)") + (en.feasible ? "" : " ⛔ battery will not reach the destination") + ".", en.feasible ? "ok" : "err", 10000);
    render(); save();
  } catch (e) {
    banner("Could not recalculate (" + (e.name === "AbortError" ? "timed out" : e.message) + "). You are still off the old route — <a href='#' id='retry'>retry now</a>.", "err");
    const a = document.getElementById("retry"); if (a) a.onclick = ev => { ev.preventDefault(); if (lastPos) recalc(lastPos.lat, lastPos.lon, "retry"); };
  } finally { recalcBusy = false; }
}

// ---------------- journey control ----------------
function startTicker() { stopTicker(); let prev = Date.now(); tickT = setInterval(() => { const n = Date.now(); if (NAV.state === "active") activeMs += n - prev; prev = n; if (NAV.state === "active") render(); }, 1000); }
function stopTicker() { if (tickT) { clearInterval(tickT); tickT = null; } }
$("b_start").onclick = () => {
  route = planned; if (navLine) { map.removeLayer(navLine); navLine = null; } if (selLine) selLine.setStyle({color: "#10b981", weight: 6, dashArray: null});
  const actual = parseFloat($("i_soc").value);
  if (isFinite(actual) && actual > 0 && actual <= 100) socOffset = actual - socAt(route.trace, 0);
  journeyStartSoc = socNowRaw(); travM = movingS = activeMs = 0; progressM = 0; hint = 0; offCount = 0; isOff = false; arrived = false; lastFix = null; reroutes = 0;
  $("summary").hidden = true; following = true; $("b_recenter").hidden = true;
  setState("active"); firstFix = true;
  if (!startWatch()) { setState("idle"); firstFix = false; return; }
  banner("Getting your GPS position… allow the location prompt if your browser asks.", "info");
  startTicker(); render(); save();
};
$("b_pause").onclick = () => { stopWatch(); setState("paused"); lastFix = null; pill("GPS paused"); banner("Journey paused — GPS tracking is off. Press Resume to continue.", "info"); render(); save(); };
$("b_resume").onclick = () => { setState("active"); lastFix = null; if (!startWatch()) { setState("paused"); return; } banner("Resumed — waiting for GPS…", "info"); render(); save(); };
$("b_end").onclick = () => { if (NAV.state === "arrived") { setState("idle"); render(); } else finish(false); };
$("b_recalc").onclick = () => { if (lastPos) recalc(lastPos.lat, lastPos.lon, "manual"); else banner("No GPS position yet — wait for the first fix.", "warn"); };
$("b_setsoc").onclick = () => {
  const v = parseFloat($("i_soc").value);
  if (!(isFinite(v) && v > 0 && v <= 100)) { banner("Enter a battery % between 1 and 100.", "err"); return; }
  socOffset = v - socAt(route.trace, progressM);
  if (journeyStartSoc === null) journeyStartSoc = v;
  banner("Battery set to " + v.toFixed(0) + "%. Estimates now count from this value.", "ok"); render(); save();
};
function finish(isArrival) {
  stopWatch(); stopTicker(); setState(isArrival ? "arrived" : "idle");
  const sNow = clamp(socNowRaw(), 0, 100), usedPct = journeyStartSoc === null ? 0 : journeyStartSoc - socNowRaw();
  const mins = activeMs / 60000;
  $("summary").innerHTML = "<h4>" + (isArrival ? "🏁 You have arrived" : "Journey ended") + "</h4><table>" +
    "<tr><td>Distance driven (GPS)</td><td><b>" + fmtKm(travM) + "</b></td></tr>" +
    "<tr><td>Journey time</td><td><b>" + fmtMin(mins) + "</b></td></tr>" +
    "<tr><td>Average speed</td><td><b>" + (mins > 0 ? (travM / 1000 / (mins / 60)).toFixed(0) + " km/h" : "–") + "</b></td></tr>" +
    "<tr><td>Estimated energy used</td><td><b>" + Math.max(0, usedPct / 100 * M.usable).toFixed(2) + " kWh</b></td></tr>" +
    "<tr><td>Estimated battery now</td><td><b>" + sNow.toFixed(1) + "%</b></td></tr>" +
    "<tr><td>Route recalculations</td><td><b>" + reroutes + "</b></td></tr></table>" +
    "<small>Battery figures are model estimates, not vehicle telemetry.</small>";
  $("summary").hidden = false; pill("GPS off"); banner(isArrival ? "🏁 Destination reached." : "", "ok");
  try { localStorage.removeItem(SIG); } catch (e) {}
  render();
}

// ---------------- survive page reloads (Streamlit reruns re-create this frame) ----------------
let lastSave = 0;
function save() {
  if (NAV.state !== "active" && NAV.state !== "paused") return;
  const n = Date.now(); if (n - lastSave < 2000 && NAV.state === "active") return; lastSave = n;
  try { localStorage.setItem(SIG, JSON.stringify({ts: n, state: NAV.state, travM, movingS, activeMs, progressM, hint, socOffset, journeyStartSoc, reroutes, isOff,
    route: route.rerouted ? {path: route.path, distM: route.distM, durMin: route.durMin, trace: route.trace} : null})); } catch (e) {}
}
(function restore() {
  try {
    const s = JSON.parse(localStorage.getItem(SIG) || "null");
    if (!s || (s.state !== "active" && s.state !== "paused") || Date.now() - s.ts > 6 * 3600 * 1000) return;
    if (s.route) { route = {path: s.route.path, cum: cumDist(s.route.path, s.route.distM), distM: s.route.distM, durMin: s.route.durMin, trace: s.route.trace, rerouted: true};
      if (selLine) selLine.setStyle({color: "#9ca3af", weight: 4, dashArray: "6 8"}); navLine = L.polyline(route.path, {color: "#10b981", weight: 6}).addTo(map); }
    travM = s.travM; movingS = s.movingS; activeMs = s.activeMs; progressM = s.progressM; hint = s.hint; socOffset = s.socOffset; journeyStartSoc = s.journeyStartSoc; reroutes = s.reroutes;
    setState(s.state); startTicker();
    if (s.state === "active") { startWatch(); banner("Journey restored after the page refreshed.", "info", 8000); } else banner("Journey paused. Press Resume to continue.", "info");
    render();
  } catch (e) {}
})();

setState(NAV.state);
if (NAV.state === "idle") { render(); }
}
/*__FIT__*/
</script>
"""


def build_map_payload(analyzed, selected_idx, start, dest, stations, vehicle_category, ev_model, initial_soc):
    sel = analyzed[selected_idx]
    energy, route = sel["energy"], sel["route"]
    routes = []
    for i, a in enumerate(analyzed):
        coords = a["route"]["geometry"]["coordinates"]
        step = max(1, len(coords) // 1200)
        path = [[c[1], c[0]] for c in coords[::step]]
        if coords[-1] != coords[::step][-1]:
            path.append([coords[-1][1], coords[-1][0]])
        routes.append({
            "name": a["route"]["corridor_name"], "path": path, "sel": i == selected_idx,
            "color": ROUTE_ALT_COLORS[i % len(ROUTE_ALT_COLORS)][0],
            "km": a["route"]["distance_km"], "min": a["route"]["duration_minutes"],
            "kwh": a["energy"]["total_energy_kwh"],
        })
    coords = route["geometry"]["coordinates"]
    step = max(1, len(coords) // 4000)
    nav_path = [[c[1], c[0]] for c in coords[::step]]
    if coords[-1] != coords[::step][-1]:
        nav_path.append([coords[-1][1], coords[-1][0]])
    pt = lambda p: None if not p else {"lat": p["lat"], "lon": p["lon"], "km": p["distance_km"]}
    return {
        "routes": routes,
        "start": {"lat": start["latitude"], "lon": start["longitude"], "name": start["name"]},
        "dest": {"lat": dest["latitude"], "lon": dest["longitude"], "name": dest["name"]},
        "low": pt(energy.get("low_soc_point")), "empty": pt(energy.get("depleted_point")),
        "stations": [{"lat": s["lat"], "lon": s["lon"], "name": s["name"], "addr": s.get("address", ""), "op": s.get("operator", ""), "sockets": s.get("sockets", ""), "cap": s.get("capacity", ""), "est": s.get("est_soc"), "reach": s.get("reach", ""), "dist": s["distance_km"]} for s in (stations or [])],
        "trace": energy["trace"],
        "durationMin": route["duration_minutes"],
        "icon": VEHICLE_ICONS.get(vehicle_category, "🚗"),
        "nav": {"path": nav_path, "distM": route["distance_km"] * 1000.0},
        "plannedSoc": float(initial_soc),
        "model": eng.model_params(ev_model, vehicle_category),
        "selIdx": selected_idx,
        "places": [{"lat": p["lat"], "lon": p["lon"], "name": p["name"], "km": p["route_km"]} for p in (sel.get("places") or [])],
        "placesNote": ("" if sel.get("places_error") else
                       ("No cities/towns found within 6 km of this route." if sel.get("places") == [] else "")),
    }


MAP_FRAME_HEIGHT = 770   # measured content height is ~745 px (idle). The frame also resizes itself to its content (see FRAME_FIT_JS);
                         # where a host blocks that, 770 leaves no big empty gap and the panel scrolls inside the frame while navigating


def build_map_html(data):
    import json
    return MAP_HTML.replace("/*__FIT__*/", FRAME_FIT_JS).replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


def render_route_map(analyzed, selected_idx, start, dest, stations, vehicle_category, ev_model, initial_soc):
    """Leaflet map with all routes, markers and real-GPS live navigation."""
    import streamlit.components.v1 as components

    sel = analyzed[selected_idx]
    energy = sel["energy"]
    if getattr(eng, "ENGINE_VERSION", 0) < 6:
        st.error("`ev_engine.py` in your folder is an OLD version. Replace it with the latest `ev_engine.py`, "
                 "restart Streamlit (Ctrl+C, then run again) and calculate a new trip.")
        return
    if not energy.get("trace"):
        st.info("This result was calculated with an older version of the app. "
                "Press **← NEW TRIP** and calculate again.")
        return
    data = build_map_payload(analyzed, selected_idx, start, dest, stations, vehicle_category, ev_model, initial_soc)
    components.html(build_map_html(data), height=MAP_FRAME_HEIGHT, scrolling=True)


def cached_charging_stations(lat, lon, radius_km):
    # No st.cache_data here on purpose: the engine already caches live answers for 1 h (disk, shared by all sessions) and
    # merges identical requests in flight. A second cache would also keep a "saved answer from earlier" for an hour,
    # and Retry could then never reach the servers again.
    return eng.find_charging_stations(lat, lon, radius_km)


# ==========================================
# PAGE 2 (RESULTS) DESIGN SYSTEM
# Everything below is used ONLY by the results page. The first page and its CSS are not touched: this stylesheet is
# injected only while the results page is shown, so it disappears again when you go back.
# ==========================================

RESULTS_CSS = r'''<style>
:root{--ev-font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--ev-navy:#0a1f3c;--ev-navy-2:#12315a;--ev-accent:#10b981;--ev-bright:#2ee6a6;--ev-deep:#047857;
  --ev-ink:#0f2137;--ev-muted:#64748b;--ev-line:#e2e8f0;--ev-soft:#f3f6fb}
.stApp,[data-testid="stAppViewContainer"]{background:var(--ev-soft)}
.stApp{color:var(--ev-ink)}
.stApp,.stApp button,.stApp summary,.stApp input,.stApp textarea{font-family:var(--ev-font)}
.stApp .block-container{max-width:1080px;padding-top:1.25rem;padding-bottom:3rem}
.stApp [data-testid="stMarkdownContainer"] :where(p,li){color:var(--ev-ink)}
.stApp [data-testid="stCaptionContainer"]{color:var(--ev-muted)}

/* 0. Page header */
.ev-hero{position:relative;overflow:hidden;border-radius:18px;padding:26px 30px 24px;color:#fff;
  background:linear-gradient(135deg,#0a1f3c 0%,#12315a 58%,#0d4a5c 100%);box-shadow:0 10px 28px rgba(10,31,60,.22)}
.ev-hero:before{content:"";position:absolute;right:-70px;top:-90px;width:300px;height:300px;border-radius:50%;
  background:radial-gradient(circle,rgba(46,230,166,.30),rgba(46,230,166,0) 70%)}
.ev-hero:after{content:"";position:absolute;left:0;top:0;bottom:0;width:6px;background:linear-gradient(180deg,#2ee6a6,#10b981)}
.ev-hero>*{position:relative}
.ev-hero-brand{font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ev-bright);font-weight:700}
.ev-hero-title{font-size:2rem;font-weight:800;line-height:1.15;margin:8px 0 6px;color:#fff}
.ev-hero-route{display:flex;flex-wrap:wrap;align-items:center;gap:10px;font-size:1.25rem;font-weight:600;color:#e6f0fa}
.ev-hero-route .arrow{color:var(--ev-bright)}
.ev-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}
.ev-chip{padding:6px 13px;border-radius:999px;background:rgba(255,255,255,.10);border:1px solid rgba(255,255,255,.20);
  font-size:.85rem;color:#dbe7f3}
.ev-chip b{color:#fff}

/* Section headings: identical block and identical spacing for every section */
.ev-sec{display:flex;align-items:center;gap:12px;margin:34px 0 2px;padding-bottom:12px;border-bottom:2px solid var(--ev-line)}
.ev-sec-num{flex:0 0 38px;height:38px;border-radius:11px;background-color:var(--ev-navy);background-repeat:no-repeat;
  background-position:center;background-size:21px 21px;box-shadow:inset 0 0 0 1px rgba(46,230,166,.25)}
/*__ICONS__*/
.ev-sec-title{font-size:1.35rem;font-weight:800;color:var(--ev-navy);line-height:1.2}
.ev-sec-sub{font-size:.9rem;color:var(--ev-muted);margin-top:2px;line-height:1.35}
.ev-subhead{font-size:1rem;font-weight:700;color:var(--ev-navy);margin:6px 0 0}
.ev-subhead small{font-weight:500;color:var(--ev-muted);font-size:.85rem}

/* Route overview cards */
.ev-rcard{margin:0!important;background:#fff;border:1px solid var(--ev-line);border-left:6px solid var(--rc,#10b981);border-radius:14px;padding:8px 18px;height:78px;overflow:hidden;
  box-shadow:0 1px 2px rgba(10,31,60,.05);display:grid;grid-template-columns:minmax(0,1fr) 350px;align-items:center;gap:6px 18px}
.ev-rcard.sel{border-color:var(--ev-accent);box-shadow:0 0 0 2px rgba(16,185,129,.35),0 8px 20px rgba(16,185,129,.14)}
.ev-rcard-main{min-width:0;display:flex;flex-direction:column;gap:5px}
.ev-rcard-via{font-size:.82rem;color:var(--ev-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding-left:20px}
.ev-rcard-top{display:flex;flex-wrap:nowrap;align-items:center;margin:0;gap:10px;overflow:hidden}
.ev-rcard-name{font-size:1.05rem;font-weight:800;color:var(--ev-navy);display:flex;align-items:center;gap:8px;white-space:nowrap}
.ev-dot{width:12px;height:12px;border-radius:50%;display:inline-block;flex:0 0 12px}
.ev-selbadge{font-size:.68rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;background:var(--ev-accent);color:#032b1f;border-radius:999px;padding:3px 9px}
.ev-rtags{display:flex;flex-wrap:nowrap;gap:6px;height:auto;overflow:hidden}
.ev-pill{font-size:.72rem;font-weight:700;border-radius:999px;padding:3px 9px;border:1px solid;white-space:nowrap;height:24px;line-height:16px}
.ev-pill.teal{background:#e6faf4;border-color:#99e6cf;color:#047857}
.ev-pill.navy{background:#e8eef7;border-color:#bccbe2;color:#12315a}
.ev-pill.green{background:#ecfdf5;border-color:#86efac;color:#166534}
.ev-pill.warn{background:#fffbeb;border-color:#fcd34d;color:#92400e}
.ev-pill.bad{background:#fef2f2;border-color:#fca5a5;color:#991b1b}
.ev-pill.plain{background:#f1f5f9;border-color:#cbd5e1;color:#475569}
.ev-via{font-size:.82rem;color:var(--ev-muted);max-height:36px;line-height:18px;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;margin:0}
.ev-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px 14px}
.ev-stat-l{white-space:nowrap;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:var(--ev-muted);font-weight:700}
.ev-stat-v{font-size:1.02rem;font-weight:800;color:var(--ev-navy);white-space:nowrap}
.ev-stat-v.warn{color:#b45309}.ev-stat-v.bad{color:#b91c1c}

/* KPI cards */
.ev-kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.ev-kpi{background:#fff;border:1px solid var(--ev-line);border-radius:14px;padding:16px 18px 14px;box-shadow:0 1px 2px rgba(10,31,60,.05);
  border-left:5px solid var(--ev-accent)}
.ev-kpi.navy{border-left-color:var(--ev-navy)}
.ev-kpi.warn{border-left-color:#f59e0b}.ev-kpi.bad{border-left-color:#ef4444}
.ev-kpi-label{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--ev-muted);font-weight:700}
.ev-kpi-value{font-size:1.9rem;font-weight:800;color:var(--ev-navy);line-height:1.15;margin-top:6px;white-space:nowrap}
.ev-kpi.warn .ev-kpi-value{color:#b45309}.ev-kpi.bad .ev-kpi-value{color:#b91c1c}
.ev-kpi-unit{font-size:.95rem;font-weight:600;color:var(--ev-muted);margin-left:5px}
.ev-kpi-note{font-size:.8rem;color:var(--ev-muted);margin-top:4px;min-height:1.1em}

/* Battery bar */
.ev-batt{background:#fff;border:1px solid var(--ev-line);border-radius:14px;padding:14px 18px 12px;box-shadow:0 1px 2px rgba(10,31,60,.05)}
.ev-batt-head{display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;font-size:.9rem;color:var(--ev-muted);margin-bottom:10px}
.ev-batt-head b{color:var(--ev-navy)}
.ev-batt-track{position:relative;height:16px;border-radius:999px;background:#e8eef5;overflow:visible}
.ev-batt-fill{position:absolute;left:0;top:0;bottom:0;border-radius:999px 0 0 999px;background:linear-gradient(90deg,#10b981,#2ee6a6)}
.ev-batt-fill.warn{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.ev-batt-fill.bad{background:linear-gradient(90deg,#dc2626,#f87171)}
.ev-batt-used{position:absolute;top:0;bottom:0;background:repeating-linear-gradient(135deg,#cbd5e1 0 5px,#e2e8f0 5px 10px)}
.ev-batt-mark{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--ev-navy)}
.ev-batt-leg{display:flex;flex-wrap:wrap;gap:6px 18px;margin-top:12px;font-size:.78rem;color:var(--ev-muted)}
.ev-batt-leg i{display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:6px;vertical-align:-2px}
.ev-batt-leg i.f{background:linear-gradient(90deg,#10b981,#2ee6a6)}
.ev-batt-leg i.u{background:repeating-linear-gradient(135deg,#cbd5e1 0 3px,#e2e8f0 3px 6px)}
.ev-batt-leg i.m{width:3px;border-radius:1px;background:var(--ev-navy);margin-right:7px}

/* Alerts and notes */
.ev-alert{display:flex;gap:10px;align-items:flex-start;padding:12px 16px;border-radius:12px;border:1px solid;font-size:.95rem;line-height:1.45}
.ev-alert-ic{flex:0 0 auto;line-height:1.45}
.ev-alert.ok{background:#ecfdf5;border-color:#a7f3d0;color:#065f46}
.ev-alert.warn{background:#fffbeb;border-color:#fde68a;color:#92400e}
.ev-alert.err{background:#fef2f2;border-color:#fecaca;color:#991b1b}
.ev-alert.info{background:#eff6ff;border-color:#bfdbfe;color:#1e3a8a}
.ev-note{font-size:.82rem;color:var(--ev-muted);line-height:1.5}

/* Panels, route stops */
.ev-panel{background:#fff;border:1px solid var(--ev-line);border-radius:14px;padding:14px 18px 14px;box-shadow:0 1px 2px rgba(10,31,60,.05)}
.ev-panel-head{font-size:.95rem;color:var(--ev-muted);margin-bottom:10px}
.ev-panel-head b{color:var(--ev-navy);font-size:1.02rem}
.ev-seq{display:flex;flex-wrap:wrap;align-items:center;gap:8px 6px}
.ev-seq-arr{color:#94a3b8;font-size:1rem}
.ev-stop{display:inline-flex;align-items:center;gap:8px;padding:6px 12px 6px 8px;border-radius:999px;border:1.5px solid #cbd5e1;background:#f8fafc;color:var(--ev-ink);line-height:1.15}
.ev-stop b{font-size:.9rem;color:inherit}
.ev-stop small{font-size:.72rem;opacity:.7}
.ev-stop i{font-style:normal;font-weight:800;font-size:.72rem;width:20px;height:20px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;background:var(--ev-navy);color:#fff;flex:0 0 20px}
.ev-stop.start{background:var(--ev-navy);border-color:var(--ev-navy);color:#fff}
.ev-stop.start i{background:var(--ev-bright);color:var(--ev-navy)}
.ev-stop.dest{background:#dc2626;border-color:#dc2626;color:#fff}
.ev-stop.dest i{background:#fff;color:#dc2626}
.ev-panel .ev-note{margin-top:10px}

/* Tables */
.ev-table-wrap{background:#fff;border:1px solid var(--ev-line);border-radius:14px;overflow-x:auto;box-shadow:0 1px 2px rgba(10,31,60,.05)}
.ev-table{width:100%;border-collapse:collapse;font-size:.88rem}
.ev-table th{background:var(--ev-navy);color:#fff;text-align:left;padding:10px 10px;font-weight:700;font-size:.7rem;text-transform:uppercase;letter-spacing:.05em;line-height:1.25;vertical-align:bottom}
.ev-table td{padding:10px 10px;border-top:1px solid var(--ev-line);color:var(--ev-ink);white-space:nowrap;vertical-align:middle}
.ev-table td.wrap{white-space:normal;min-width:104px;line-height:1.35}
.ev-table .num{text-align:right}
.ev-table tbody tr:nth-child(even) td{background:#f8fafc}
.ev-table tbody tr.sel td{background:#e8faf3;font-weight:700}
.ev-table tbody tr.total td{background:#eef3f9;font-weight:800;color:var(--ev-navy)}
.ev-sw{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:7px;vertical-align:-1px}
.ev-chip2{display:inline-block;font-size:.74rem;font-weight:700;padding:2px 9px;border-radius:999px;border:1px solid}
.ev-chip2.yes{background:#ecfdf5;border-color:#86efac;color:#166534}
.ev-chip2.low{background:#fffbeb;border-color:#fcd34d;color:#92400e}
.ev-chip2.no{background:#fef2f2;border-color:#fca5a5;color:#991b1b}

/* Streamlit widgets used on this page: buttons and expanders */
.stApp div.stButton>button{width:100%;min-height:46px;height:auto;border-radius:12px;background:#fff;color:var(--ev-navy);
  border:1.5px solid #cbd5e1;font-weight:700;font-size:.95rem;box-shadow:none;transition:all .15s ease}
.stApp div.stButton>button:hover{background:#f0fdf8;border-color:var(--ev-accent);color:var(--ev-deep);box-shadow:0 2px 8px rgba(16,185,129,.18)}
.stApp div.stButton>button[kind="primary"],.stApp div.stButton>button[data-testid="stBaseButton-primary"]{background:var(--ev-navy);border-color:var(--ev-navy);color:#fff}
.stApp div.stButton>button[kind="primary"]:hover,.stApp div.stButton>button[data-testid="stBaseButton-primary"]:hover{background:var(--ev-navy-2);border-color:var(--ev-navy-2);color:var(--ev-bright)}
.stApp div.stButton>button:disabled{background:var(--ev-accent);border-color:var(--ev-accent);color:#032b1f;opacity:1;cursor:default;box-shadow:none}
.stApp div.stButton>button p,.stApp div.stButton>button span,.stApp div.stButton>button div{color:inherit}
.stApp [data-testid="stExpander"]{background:#fff;border:1px solid var(--ev-line)!important;border-radius:14px;overflow:hidden;box-shadow:0 1px 2px rgba(10,31,60,.05)}
.stApp [data-testid="stExpander"] summary,.stApp [data-testid="stExpander"] summary *{color:var(--ev-navy);font-weight:700}

.stApp [data-testid="stCheckbox"]{margin:14px 0 -6px}
.stApp [data-testid="stCheckbox"] label,.stApp [data-testid="stCheckbox"] label>div{background:transparent!important}
.stApp [data-testid="stCheckbox"] label p{color:var(--ev-navy);font-weight:700;background:transparent}
.stApp [data-testid="stHorizontalBlock"]:has(.ev-rcard) div.stButton{width:100%}
.stApp [data-testid="stHorizontalBlock"]:has(.ev-rcard) div.stButton>button{height:78px;min-height:78px}
@media (max-width:860px){.ev-rcard{grid-template-columns:1fr;height:auto;padding:12px 16px}.ev-rcard .ev-stats{grid-column:1/-1}
  .stApp [data-testid="stHorizontalBlock"]:has(.ev-rcard) div.stButton>button{height:auto;min-height:46px}.ev-rcard-via{white-space:normal}}
@media (max-width:640px){.ev-rcard{grid-template-columns:1fr}.ev-rcard .ev-stats{grid-template-columns:1fr 1fr}}
@media (max-width:900px){.ev-kpis{grid-template-columns:repeat(2,1fr)}}
@media (max-width:640px){
  .ev-hero{padding:20px 20px 18px}.ev-hero-title{font-size:1.55rem}.ev-hero-route{font-size:1.05rem}
  .ev-sec-title{font-size:1.15rem}.ev-kpi-value{font-size:1.55rem}.ev-kpi{padding:14px 14px 12px}.ev-kpi-note{min-height:2.7em}.ev-kpi-label{min-height:2.5em}
}
</style>'''


_SEC_ICONS = {
    "route": '<circle cx="6" cy="19" r="2.6"/><circle cx="18" cy="5" r="2.6"/><path d="M8.6 19H17a3.5 3.5 0 0 0 0-7H7a3.5 3.5 0 0 1 0-7h8.4"/>',
    "map": '<path d="M3 6.5l6-2.5 6 2.5 6-2.5v13.5l-6 2.5-6-2.5-6 2.5z"/><path d="M9 4v13.5M15 6.5V20"/>',
    "energy": '<path d="M13 2.5L4.5 13.5H11l-1 8 8.5-11H12z"/>',
    "elevation": '<path d="M2.5 20l6.5-11.5 4 7 3-4.5 5.5 9z"/><path d="M8.2 11.2l1.6 1.4 1.4-1.5"/>',
    "charging": '<path d="M9 2.5v5M15 2.5v5"/><path d="M6 7.5h12v3.5a6 6 0 0 1-12 0z"/><path d="M12 17v4.5"/>',
    "compare": '<path d="M5 20v-8M12 20V5M19 20v-12"/><path d="M3 21h18"/>',
    "back": '<path d="M19 12H5"/><path d="M11.5 5.5L5 12l6.5 6.5"/>',
}


def _icon_css():
    from urllib.parse import quote
    rules = []
    for name, body in _SEC_ICONS.items():
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='#2ee6a6' stroke-width='2' "
               f"stroke-linecap='round' stroke-linejoin='round'>{body}</svg>")
        data = quote(svg, safe="/:=' ,.-")
        rules.append('.ev-sec-num.ic-' + name + '{background-image:url("data:image/svg+xml;utf8,' + data + '")}')
    return "\n".join(rules)


RESULTS_CSS = RESULTS_CSS.replace("/*__ICONS__*/", _icon_css())


def _compact_css(css):
    """
    CSS for st.markdown(unsafe_allow_html=True): remove comments and ALL blank lines/indentation.
    Markdown ends an HTML block at the first blank line, so on some Streamlit versions (e.g. the one used when deployed) every
    rule after a blank line inside <style> was treated as plain text and silently lost - the route-stop chips then showed
    unstyled ("AMysuru0 km..."). One compact block has no blank line, so it behaves the same on every version.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return "\n".join(line.strip() for line in css.splitlines() if line.strip())


def _h(value):
    """HTML-escape any text shown on the results page ($ is escaped so Streamlit never treats it as maths)."""
    return html.escape(str(value)).replace("$", "&#36;")


class _Raw(str):
    """A table cell that already contains safe HTML."""


def ev_section(icon, title, subtitle=""):
    sub = f'<div class="ev-sec-sub">{_h(subtitle)}</div>' if subtitle else ""
    return (f'<div class="ev-sec"><span class="ev-sec-num ic-{icon}" aria-hidden="true"></span><div>'
            f'<div class="ev-sec-title" role="heading" aria-level="2">{_h(title)}</div>{sub}</div></div>')


def ev_alert(kind, text_html):
    icon = {"ok": "✅", "warn": "⚠️", "err": "⛔", "info": "ℹ️"}[kind]
    return f'<div class="ev-alert {kind}"><span class="ev-alert-ic">{icon}</span><div>{text_html}</div></div>'


def ev_note(text):
    return f'<div class="ev-note">{_h(text)}</div>'


def ev_hero(start_text, dest_text, ev_model, category, soc, n_routes, selected=""):
    icon = VEHICLE_ICONS.get(category, "🚗")
    chips = (f'<span class="ev-chip">{icon} <b>{_h(ev_model)}</b> · {_h(category)}</span>'
             f'<span class="ev-chip">🔋 Start battery <b>{_h(soc)}%</b></span>'
             f'<span class="ev-chip">🔀 <b>{n_routes}</b> route{"s" if n_routes != 1 else ""} found</span>'
             + (f'<span class="ev-chip">🛣️ {selected}</span>' if selected else ""))
    return ('<div class="ev-hero"><div class="ev-hero-brand">⚡ Smart EV Route Planner</div>'
            '<div class="ev-hero-title" role="heading" aria-level="1">EV Route Analysis</div>'
            f'<div class="ev-hero-route"><span>{_h(start_text)}</span><span class="arrow">➔</span><span>{_h(dest_text)}</span></div>'
            f'<div class="ev-chips">{chips}</div></div>')


def _tag_tone(text):
    if "⛔" in text:
        return "bad"
    if "⚠" in text:
        return "warn"
    return {"Fastest": "teal", "Shortest": "navy", "Lowest modelled energy": "green"}.get(text, "navy")


def ev_route_card(i, a, selected, tags):
    r, e = a["route"], a["energy"]
    color = "#10b981" if selected else ROUTE_ALT_COLORS[i % len(ROUTE_ALT_COLORS)][0]
    pills = "".join(f'<span class="ev-pill {_tag_tone(t)}">{_h(t)}</span>' for t in tags)
    if not pills:
        pills = '<span class="ev-pill plain">Alternative route</span>'
    via = eng.route_via_text(a.get("places"))
    via_html = f"via {_h(via)}" if via else "Direct road (no major towns listed)"
    arr_tone = "bad" if (not e["feasible"] or e["below_reserve"]) else ("warn" if e["low_soc"] else "")
    stat = lambda label, val, tone="": (f'<div><div class="ev-stat-l">{label}</div><div class="ev-stat-v {tone}">{val}</div></div>')
    return (f'<div class="ev-rcard{" sel" if selected else ""}" style="--rc:{color}">'
            '<div class="ev-rcard-main">'
            f'<div class="ev-rcard-top"><span class="ev-rcard-name"><span class="ev-dot" style="background:{color}"></span>{_h(r["corridor_name"])}</span>'
            f'{"<span class=ev-selbadge>Selected</span>" if selected else ""}<div class="ev-rtags">{pills}</div></div>'
            f'<div class="ev-rcard-via">{via_html}</div></div>'
            '<div class="ev-stats">'
            + stat("Distance", f'{r["distance_km"]:.1f} km')
            + stat("Time", _h(fmt_minutes(r["duration_minutes"])))
            + stat("Energy", f'{e["total_energy_kwh"]:.1f} kWh')
            + stat("Arrival SOC", f'{e["final_soc"]:.0f}%', arr_tone)
            + '</div></div>')


def ev_kpi(label, value, unit="", note="", tone=""):
    u = f'<span class="ev-kpi-unit">{_h(unit)}</span>' if unit else ""
    return (f'<div class="ev-kpi {tone}"><div class="ev-kpi-label">{_h(label)}</div>'
            f'<div class="ev-kpi-value">{_h(value)}{u}</div><div class="ev-kpi-note">{_h(note) if note else "&nbsp;"}</div></div>')


def ev_kpis(*cards):
    return '<div class="ev-kpis">' + "".join(cards) + "</div>"


def ev_battery(initial, arrival, tone=""):
    i = max(0.0, min(100.0, float(initial)))
    a = max(0.0, min(100.0, float(arrival)))
    used = (f'<div class="ev-batt-used" style="left:{a:.2f}%;width:{i - a:.2f}%"></div>' if i > a else "")
    return ('<div class="ev-batt"><div class="ev-batt-head"><span>Battery level along the trip</span>'
            f'<span><b>{i:.0f}%</b> at start &nbsp;➔&nbsp; <b>{a:.1f}%</b> on arrival</span></div>'
            f'<div class="ev-batt-track"><div class="ev-batt-fill {tone}" style="width:{a:.2f}%"></div>{used}'
            f'<span class="ev-batt-mark" style="left:{eng.LOW_SOC_WARNING_PCT:.0f}%"></span></div>'
            '<div class="ev-batt-leg"><span><i class="f"></i>Remaining on arrival</span><span><i class="u"></i>Energy used</span>'
            f'<span><i class="m"></i>{eng.LOW_SOC_WARNING_PCT:.0f}% low-battery level</span></div></div>')


def ev_sequence(seq):
    chips, n = [], 0
    for s in seq:
        if s["kind"] == "start":
            badge = "A"
        elif s["kind"] == "dest":
            badge = "B"
        else:
            n += 1
            badge = str(n)
        chips.append(f'<span class="ev-stop {_h(s["kind"])}"><i>{badge}</i><b>{_h(s["name"])}</b><small>{s["km"]:.0f} km</small></span>')
    return '<div class="ev-seq">' + '<span class="ev-seq-arr">➔</span>'.join(chips) + "</div>"


def ev_table(headers, rows, num_cols=(), sel_row=None, total_row=None, min_width=640, wrap_cols=()):
    th = "".join(f'<th class="{"num" if k in num_cols else ""}">{_h(t)}</th>' for k, t in enumerate(headers))
    body = []
    for ri, row in enumerate(rows):
        cls = "sel" if ri == sel_row else ("total" if ri == total_row else "")
        tds = "".join(f'<td class="{"num" if k in num_cols else ""}{" wrap" if k in wrap_cols else ""}">{c if isinstance(c, _Raw) else _h(c)}</td>'
                      for k, c in enumerate(row))
        body.append(f'<tr class="{cls}">{tds}</tr>')
    return (f'<div class="ev-table-wrap"><table class="ev-table" style="min-width:{min_width}px"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def _choose_route(i):
    """Callback of the 'Select route' buttons: runs before the page is redrawn, so every section shows route i."""
    st.session_state.route_choice = i


ELEV_CHART_HEIGHT = 322

ELEV_HTML = r"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  *{box-sizing:border-box}
  html,body{margin:0;background:transparent;font-family:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;color:#0f2137}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:14px 16px 8px;box-shadow:0 1px 2px rgba(10,31,60,.05)}
  .top{display:flex;justify-content:space-between;align-items:baseline;gap:4px 12px;flex-wrap:wrap;margin-bottom:6px}
  .top b{font-size:15px;color:#0a1f3c}
  .top span{font-size:12.5px;color:#64748b}
  #c{position:relative;width:100%;height:250px}
  svg{display:block;width:100%;height:250px;touch-action:pan-y}
  .tip{position:absolute;pointer-events:none;background:#0a1f3c;color:#fff;border-radius:8px;padding:6px 10px;font-size:12px;line-height:1.4;white-space:nowrap;transform:translate(-50%,-115%);display:none;box-shadow:0 4px 12px rgba(0,0,0,.25);z-index:2}
  .tip b{color:#2ee6a6}
</style>
<div class="card"><div class="top"><b>Elevation profile (smoothed)</b><span id="rng"></span></div>
<div id="c"><svg id="s" role="img" aria-label="Elevation profile along the selected route"></svg><div class="tip" id="tip"></div></div></div>
<script>
const D = __DATA__;
const pts = D.pts, N = pts.length, total = pts[N - 1][0] || D.km || 1;
const svg = document.getElementById("s"), box = document.getElementById("c"), tip = document.getElementById("tip");
const NS = "http://www.w3.org/2000/svg";
const fmt = v => Math.round(v).toLocaleString("en-US");
document.getElementById("rng").textContent = D.start + " ➔ " + D.dest + "  ·  " + fmt(D.lo) + "–" + fmt(D.hi) + " m";
function el(n, a, p) { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; }
function nice(r, n) { const raw = r / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p; return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p; }
let X, Y, M;
function draw() {
  const W = box.clientWidth || 600, H = 250;
  M = {l: 56, r: 14, t: 10, b: 30};
  svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.innerHTML = "";
  let lo = Infinity, hi = -Infinity;
  for (const p of pts) { if (p[1] < lo) lo = p[1]; if (p[1] > hi) hi = p[1]; }
  if (hi - lo < 20) { const mid = (hi + lo) / 2; lo = mid - 10; hi = mid + 10; }
  const pad = (hi - lo) * 0.06; lo -= pad; hi += pad;
  const ys = nice(hi - lo, 4); lo = Math.floor(lo / ys) * ys; hi = Math.ceil(hi / ys) * ys;
  X = km => M.l + (km / total) * (W - M.l - M.r);
  Y = e => M.t + (1 - (e - lo) / (hi - lo)) * (H - M.t - M.b);
  const defs = el("defs", {}, svg), g = el("linearGradient", {id: "g", x1: 0, y1: 0, x2: 0, y2: 1}, defs);
  el("stop", {offset: "0%", "stop-color": "#10b981", "stop-opacity": ".38"}, g);
  el("stop", {offset: "100%", "stop-color": "#10b981", "stop-opacity": ".03"}, g);
  for (let v = lo; v <= hi + 1e-9; v += ys) {
    el("line", {x1: M.l, x2: W - M.r, y1: Y(v), y2: Y(v), stroke: "#e2e8f0", "stroke-width": 1}, svg);
    const t = el("text", {x: M.l - 8, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "#64748b"}, svg); t.textContent = fmt(v) + " m";
  }
  const xs = nice(total, 6);
  for (let k = 0; k <= total + 1e-9; k += xs) {
    el("line", {x1: X(k), x2: X(k), y1: H - M.b, y2: H - M.b + 4, stroke: "#94a3b8"}, svg);
    const t = el("text", {x: X(k), y: H - 10, "text-anchor": k === 0 ? "start" : "middle", "font-size": 11, fill: "#64748b"}, svg); t.textContent = (Math.round(k * 10) / 10) + (k === 0 ? " km" : "");
  }
  let line = "", area = "";
  pts.forEach((p, i) => { line += (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1); });
  area = line + "L" + X(pts[N - 1][0]).toFixed(1) + "," + (H - M.b) + "L" + X(pts[0][0]).toFixed(1) + "," + (H - M.b) + "Z";
  el("path", {d: area, fill: "url(#g)"}, svg);
  el("path", {d: line, fill: "none", stroke: "#10b981", "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round"}, svg);
  el("line", {x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, stroke: "#94a3b8"}, svg);
  window._cross = el("line", {y1: M.t, y2: H - M.b, stroke: "#0a1f3c", "stroke-width": 1, "stroke-dasharray": "3 3", visibility: "hidden"}, svg);
  window._dot = el("circle", {r: 5, fill: "#fff", stroke: "#0a1f3c", "stroke-width": 2.5, visibility: "hidden"}, svg);
}
function show(clientX) {
  const r = svg.getBoundingClientRect(), W = r.width;
  const km = Math.max(0, Math.min(total, ((clientX - r.left) - M.l) / (W - M.l - M.r) * total));
  let a = 0, b = N - 1;
  while (b - a > 1) { const m = (a + b) >> 1; if (pts[m][0] <= km) a = m; else b = m; }
  const i = (km - pts[a][0] <= pts[b][0] - km) ? a : b, p = pts[i];
  const p0 = pts[Math.max(0, i - 1)], p1 = pts[Math.min(N - 1, i + 1)];
  const dk = (p1[0] - p0[0]) * 1000, grade = dk > 0 ? (p1[1] - p0[1]) / dk * 100 : 0;
  _cross.setAttribute("x1", X(p[0])); _cross.setAttribute("x2", X(p[0])); _cross.setAttribute("visibility", "visible");
  _dot.setAttribute("cx", X(p[0])); _dot.setAttribute("cy", Y(p[1])); _dot.setAttribute("visibility", "visible");
  tip.innerHTML = "<b>" + p[0].toFixed(1) + " km</b> · " + fmt(p[1]) + " m · " + (grade >= 0 ? "+" : "") + grade.toFixed(1) + "%";
  tip.style.display = "block";
  tip.style.left = Math.max(70, Math.min(W - 70, X(p[0]))) + "px"; tip.style.top = Y(p[1]) + "px";
}
function hide() { tip.style.display = "none"; if (window._cross) { _cross.setAttribute("visibility", "hidden"); _dot.setAttribute("visibility", "hidden"); } }
svg.addEventListener("mousemove", e => show(e.clientX));
svg.addEventListener("mouseleave", hide);
svg.addEventListener("touchstart", e => show(e.touches[0].clientX), {passive: true});
svg.addEventListener("touchmove", e => show(e.touches[0].clientX), {passive: true});
svg.addEventListener("touchend", hide);
draw(); window.addEventListener("resize", () => { draw(); hide(); });
/*__FIT__*/
</script>"""


def build_elevation_html(profile, start_name, dest_name, route_km):
    """Interactive elevation chart (inline SVG, no external libraries) from the EXISTING smoothed profile points."""
    import json
    pts = [[round(p["distance_km"], 3), round(p["elevation_m"], 1)] for p in profile["points"] if p.get("elevation_m") is not None]
    ys = [p[1] for p in pts]
    data = {"pts": pts, "km": route_km, "start": start_name, "dest": dest_name, "lo": min(ys), "hi": max(ys)}
    return ELEV_HTML.replace("/*__FIT__*/", FRAME_FIT_JS).replace("__DATA__", json.dumps(data).replace("</", "<\\/"))


def render_elevation_chart(profile, start_name, dest_name, route_km):
    import streamlit.components.v1 as components
    components.html(build_elevation_html(profile, start_name, dest_name, route_km), height=ELEV_CHART_HEIGHT)


# ==========================================
# SESSION STATE INITIALIZATION
# ==========================================

if "page" not in st.session_state:
    st.session_state.page = "home"
if "all_routes" not in st.session_state:
    st.session_state.all_routes = []
if "selected_route_idx" not in st.session_state:
    st.session_state.selected_route_idx = 0

# ==========================================
# PAGE 1 (HOME) THEME — same navy / white / electric-green look as the results page. Styling only.
# ==========================================
HOME_CSS = r'''<style>
:root{--ev-font:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;--ev-navy:#0a1f3c;--ev-navy-2:#12315a;--ev-accent:#10b981;--ev-bright:#2ee6a6;--ev-deep:#047857;
  --ev-ink:#0f2137;--ev-muted:#64748b;--ev-line:#e2e8f0;--ev-soft:#f3f6fb}
.stApp,[data-testid="stAppViewContainer"]{background:var(--ev-soft)}
.stApp{color:var(--ev-ink)}
.stApp,.stApp button,.stApp input,.stApp textarea{font-family:var(--ev-font)}
.stApp .block-container{max-width:880px;padding-top:1.25rem;padding-bottom:3rem}
.stApp [data-testid="stMarkdownContainer"] :where(p,li){color:var(--ev-ink)}
.ev-hero{position:relative;overflow:hidden;border-radius:18px;padding:28px 30px 26px;margin-bottom:26px;color:#fff;
  background:linear-gradient(135deg,#0a1f3c 0%,#12315a 58%,#0d4a5c 100%);box-shadow:0 10px 28px rgba(10,31,60,.22)}
.ev-hero:before{content:"";position:absolute;right:-70px;top:-90px;width:300px;height:300px;border-radius:50%;background:radial-gradient(circle,rgba(46,230,166,.30),rgba(46,230,166,0) 70%)}
.ev-hero:after{content:"";position:absolute;left:0;top:0;bottom:0;width:6px;background:linear-gradient(180deg,#2ee6a6,#10b981)}
.ev-hero>*{position:relative}
.ev-hero-brand{font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ev-bright);font-weight:700}
.ev-hero-title{font-size:2.1rem;font-weight:800;line-height:1.15;margin:8px 0 6px;color:#fff}
.ev-hero-sub{font-size:1rem;color:#cfe0f2}
.stApp h3{color:var(--ev-navy);font-weight:800;font-size:1.25rem;border-left:5px solid var(--ev-accent);padding:2px 0 2px 12px;margin:6px 0 10px}
.stApp hr{border:0;border-top:1px solid var(--ev-line);margin:1.6rem 0}
.stApp [data-testid="stWidgetLabel"] p,.stApp label p{color:var(--ev-navy);font-weight:700}
.stApp [data-baseweb="input"],.stApp [data-baseweb="select"]>div{border-radius:12px;border:1.5px solid #cbd5e1;background:#fff}
.stApp [data-baseweb="input"]:focus-within,.stApp [data-baseweb="select"]>div:focus-within{border-color:var(--ev-accent);box-shadow:0 0 0 3px rgba(16,185,129,.18)}
.stApp [data-baseweb="input"] input,.stApp [data-baseweb="select"] *{color:var(--ev-ink)}
.stApp [data-testid="stSlider"] [role="slider"]{background:var(--ev-accent);border-color:var(--ev-accent)}
.stApp [data-testid="stSlider"] [data-testid="stSliderThumbValue"]{color:var(--ev-deep);font-weight:700}
.stApp div.stButton>button{width:100%;height:54px;border-radius:12px;border:1.5px solid var(--ev-navy);background:var(--ev-navy);color:#fff;
  font-weight:800;font-size:1rem;letter-spacing:.02em;box-shadow:0 6px 16px rgba(10,31,60,.22);transition:all .15s ease}
.stApp div.stButton>button:hover{background:var(--ev-navy-2);border-color:var(--ev-navy-2);color:var(--ev-bright);box-shadow:0 8px 20px rgba(10,31,60,.28)}
.stApp div.stButton>button p,.stApp div.stButton>button span,.stApp div.stButton>button div{color:inherit}
@media (max-width:640px){.ev-hero{padding:20px 20px 18px}.ev-hero-title{font-size:1.55rem}}
</style>'''

# ==========================================
# PAGE 1 — HOME
# ==========================================

if st.session_state.page == "home":
    st.markdown(_compact_css(HOME_CSS), unsafe_allow_html=True)
    st.markdown(
        '<div class="ev-hero"><div class="ev-hero-brand">EV Dashboard</div>'
        '<div class="ev-hero-title">⚡ Smart EV Route Planner</div>'
        '<div class="ev-hero-sub">Elevation-aware, physics-based battery energy estimation</div></div>',
        unsafe_allow_html=True
    )

    with st.container():
        st.subheader("📍 Trip Details")
        c1, c2 = st.columns(2)
        with c1:
            from_loc = st.text_input("Starting Location", value="Mysuru")
        with c2:
            to_loc = st.text_input("Destination", value="Bengaluru")

        st.markdown("---")
        st.subheader("🚗 EV Setup")
        vehicle_category = st.selectbox("Vehicle Type", list(VEHICLE_DATABASES.keys()))
        vehicle_database = VEHICLE_DATABASES[vehicle_category]

        ev_choice = st.selectbox("EV Model", list(vehicle_database.keys()))
        init_soc = st.slider("Starting Battery (% SOC)", min_value=10, max_value=100, value=100)

        st.markdown(" ")

        if st.button("⚡ CALCULATE ROUTES & ENERGY"):
            try:
                if not from_loc.strip() or not to_loc.strip():
                    raise eng.ValidationError("Please enter both a starting location and a destination.")
                # Live progress: plan_trip reports each stage from this (the calling) thread, so it is safe to draw.
                box = st.empty()
                bar = box.progress(0.0, text="Starting…")

                def on_progress(frac, msg):
                    bar.progress(min(max(float(frac), 0.0), 1.0), text=msg)

                try:
                    plan = eng.plan_trip(from_loc, to_loc, ev_choice, init_soc, vehicle_category, progress=on_progress)
                finally:
                    box.empty()
                start, dest, analyzed = plan["start"], plan["dest"], plan["analyzed"]
                st.session_state.plan_info = {k: plan[k] for k in ("timings", "requests", "cache_hits", "notes")}

                st.session_state.all_routes = analyzed
                st.session_state.selected_route_idx = 0
                st.session_state.route_choice = 0
                st.session_state.start = start
                st.session_state.dest = dest
                st.session_state.ev = ev_choice
                st.session_state.vehicle_category = vehicle_category
                st.session_state.soc = init_soc
                st.session_state.user_start_text = from_loc.strip()
                st.session_state.user_dest_text = to_loc.strip()
                st.session_state.charging = {}
                st.session_state.charging_error = ""
                st.session_state.charging_stale = ""
                st.session_state.page = "results"
                st.rerun()

            except EVPlannerError as e:
                st.error(str(e))
            except Exception:
                log.exception("Unexpected failure while planning route")
                st.error("Something went wrong while planning the route. Please try again; if it keeps happening, check your internet connection.")

# ==========================================
# PAGE 2 — RESULTS
# Layout (top to bottom): 1 Page Header · 2 Route Overview · 3 Interactive Map · 4 Energy Analysis · 5 Elevation Analysis
# · 6 Charging Stations · 7 Navigation (Energy breakdown, assumptions, planning time and Route Comparison are hidden). All numbers come from the same calculations as before.
# ==========================================

else:
    if not st.session_state.all_routes:
        st.session_state.page = "home"
        st.rerun()

    all_routes = st.session_state.all_routes
    start = st.session_state.start
    dest = st.session_state.dest
    ev = st.session_state.ev
    soc = st.session_state.soc
    category = st.session_state.vehicle_category
    user_start_text = st.session_state.get("user_start_text", start["name"])
    user_dest_text = st.session_state.get("user_dest_text", dest["name"])

    st.markdown(_compact_css(RESULTS_CSS), unsafe_allow_html=True)

    # The selected route lives in session state under a fixed key; the "Select route" buttons below change it
    # (callback runs before this script is re-run, so every section below always shows the same route).
    if not (0 <= st.session_state.get("route_choice", 0) < len(all_routes)):
        st.session_state.route_choice = 0
    selected_idx = st.session_state.route_choice
    st.session_state.selected_route_idx = selected_idx

    badges, spread = eng.route_badges(all_routes)
    curr = all_routes[selected_idx]
    route, energy, profile = curr["route"], curr["energy"], curr["profile"]
    places = curr.get("places")
    problems = eng.route_consistency_problems(curr)

    # ------------------------------------------------------------------ Page header
    st.markdown(ev_hero(user_start_text, user_dest_text, ev, category, soc, len(all_routes),
                        f'<b>{_h(route["corridor_name"])}</b> · {route["distance_km"]:.1f} km · {_h(fmt_minutes(route["duration_minutes"]))}'),
                unsafe_allow_html=True)

    # ------------------------------------------------------------------ 1. Route Overview
    st.markdown(ev_section("route", "Route Overview", "Alternative routes for this trip. Select one to update the map and charging stations."),
                unsafe_allow_html=True)
    for i, a_route in enumerate(all_routes):                # one compact row per route, stacked top to bottom
        row_card, row_btn = st.columns([5.6, 1.4])
        with row_card:
            tags = badges[i] + ([route_status(a_route["energy"])] if route_status(a_route["energy"]) else [])
            st.markdown(ev_route_card(i, a_route, i == selected_idx, tags), unsafe_allow_html=True)
        with row_btn:
            st.button("✓ Selected route" if i == selected_idx else "Select route", key=f"select_route_{i}",
                      on_click=_choose_route, args=(i,), disabled=(i == selected_idx),
                      type="primary" if i == selected_idx else "secondary")
    if problems:
        st.markdown(ev_alert("err", "<b>Internal consistency check failed for this route:</b> " + _h("; ".join(problems))
                             + ". Please calculate the trip again."), unsafe_allow_html=True)
    if len(all_routes) == 1:
        st.markdown(ev_alert("info", "Only one distinct road route was found for this trip."), unsafe_allow_html=True)
    elif spread <= 2.0:
        st.markdown(ev_alert("info", "Routes differ by less than 2% in modelled energy — treat them as equivalent; the difference is inside the model's uncertainty."),
                    unsafe_allow_html=True)

    # ------------------------------------------------------------------ 2. Interactive Map
    st.markdown(ev_section("map", "Interactive Map", "All alternative routes. The selected route is drawn in green; A is the start, B the destination."),
                unsafe_allow_html=True)
    if curr.get("places_error"):
        # No warning and no button: try the town-name lookup once more in the background (it normally finishes from the
        # saved cache) and move on. Without names the route cards simply show no "via ..." text.
        _tried = st.session_state.setdefault("places_retried", set())
        if (id(curr), selected_idx) not in _tried:
            _tried.add((id(curr), selected_idx))
            try:
                eng.attach_places([curr], start["name"], dest["name"])
                st.rerun()
            except EVPlannerError:
                pass

    charging = {}
    for _k, _v in st.session_state.get("charging", {}).items():
        # estimated battery on arrival at each station: from the arrival SOC (destination) or from the 20% point
        charging[_k] = eng.annotate_station_reachability(
            _v, energy["final_soc_raw"] if _k == "dest" else eng.LOW_SOC_WARNING_PCT, energy, route["distance_km"])
    stations_shown, _seen = [], set()
    for _s in charging.get("dest", []) + charging.get(f"low_{selected_idx}", []):  # one marker per station
        if (round(_s["lat"], 4), round(_s["lon"], 4)) not in _seen:
            _seen.add((round(_s["lat"], 4), round(_s["lon"], 4)))
            stations_shown.append(_s)
    render_route_map(all_routes, selected_idx, start, dest, stations_shown, category, ev, soc)

    # ------------------------------------------------------------------ Optional elevation analysis (one tick box)
    show_details = st.checkbox("Show elevation analysis", value=False, key="show_details")
    if show_details:
        # ------------------------------------------------------------------ 4. Elevation Analysis
        st.markdown(ev_section("elevation", "Elevation Analysis", f"Terrain along the selected route ({route['corridor_name']})"), unsafe_allow_html=True)
        if not energy["elevation_available"]:
            st.markdown(ev_alert("warn", "<b>Elevation service unavailable:</b> terrain was NOT included. Energy is a flat-road estimate; hills would change it."),
                        unsafe_allow_html=True)
            st.markdown(ev_kpis(ev_kpi("Elevation gain", "n/a", "", "terrain not available", "navy"),
                                ev_kpi("Elevation loss", "n/a", "", "terrain not available", "navy")), unsafe_allow_html=True)
        else:
            st.markdown(ev_kpis(
                ev_kpi("Elevation gain", f"{energy['ascent_m']:,.0f}", "m", "total climbing"),
                ev_kpi("Elevation loss", f"{energy['descent_m']:,.0f}", "m", "total descending", "navy"),
                ev_kpi("Highest point", f"{profile['max_elev_m']:,.0f}", "m", f"lowest {profile['min_elev_m']:,.0f} m", "navy"),
                ev_kpi("Steepest gradient", f"{energy['max_grade_pct']:.1f}", "%", "between samples", "navy"),
            ), unsafe_allow_html=True)
            render_elevation_chart(profile, start["name"], dest["name"], route["distance_km"])
            terrain_note = (f"Terrain: Copernicus GLO-90 (~90 m resolution) via Open-Meteo, sampled every ~{profile['spacing_m']:.0f} m and "
                            f"3-point smoothed. Max gradient {energy['max_grade_pct']:.1f}%. Bridges, flyovers and cuttings are not captured.")
            if profile.get("missing_count", 0) > 0:
                terrain_note += (f" {profile['missing_count']} of {len(profile['points'])} elevation samples were unavailable "
                                 "(slow or busy elevation service) and were interpolated from their neighbours.")
            st.markdown(ev_note(terrain_note), unsafe_allow_html=True)

    # ------------------------------------------------------------------ 5. Charging Stations
    st.markdown(ev_section("charging", "Charging Stations", "Chargers near your destination and low-battery point."),
                unsafe_allow_html=True)

    def search_chargers():
        """Fetch OSM chargers near the destination (and the <20% point); store result or error, then redraw."""
        ch, err = {}, ""
        with st.spinner("Searching nearby charging stations (OpenStreetMap)..."):
            try:
                ch["dest"] = cached_charging_stations(dest["latitude"], dest["longitude"], 10.0)
                lp = energy["low_soc_point"]
                if lp and lp["distance_km"] < route["distance_km"] - 5:
                    try:
                        ch[f"low_{selected_idx}"] = cached_charging_stations(lp["lat"], lp["lon"], 10.0)
                    except Exception as ex:  # destination chargers are still useful
                        err = f"Chargers near the 20% point could not be loaded ({ex})"
            except Exception as ex:  # Overpass down / timeout / blocked
                if not isinstance(ex, EVPlannerError):
                    log.exception("Charging-station search failed")
                err = str(ex) or type(ex).__name__
        if "dest" in ch:
            st.session_state.charging = ch
        ages = [x["stale_h"] for lst in ch.values() for x in lst if x.get("stale_h") is not None]
        st.session_state.charging_stale = (
            f"The live OpenStreetMap servers did not answer, so these are saved results from {max(ages):.1f} h ago." if ages else "")
        st.session_state.charging_error = err
        st.rerun()

    _fc, _ = st.columns([1, 2])
    with _fc:
        if st.button("Find charging stations (OpenStreetMap)", key="find_chargers", type="primary"):
            search_chargers()
    if st.session_state.get("charging_error"):
        st.markdown(ev_alert("err", "Charging-station data from OpenStreetMap is unavailable right now "
                                    "(the server is busy or timed out). Please try again.")
                    + ev_note(f"Details: {st.session_state.charging_error}"), unsafe_allow_html=True)
    if st.session_state.get("charging_stale") and not st.session_state.get("charging_error"):
        st.markdown(ev_alert("warn", _h(st.session_state.charging_stale) + " Press Retry to ask the servers again."), unsafe_allow_html=True)
    if st.session_state.get("charging_error") or st.session_state.get("charging_stale"):
        _rc, _ = st.columns([1, 2])
        with _rc:
            if st.button("🔄 Retry", key="retry_chargers"):
                search_chargers()
    reach_chip = {"yes": _Raw('<span class="ev-chip2 yes">Yes</span>'), "low": _Raw('<span class="ev-chip2 low">Low battery</span>'),
                  "no": _Raw('<span class="ev-chip2 no">No</span>')}
    for key, title in (("dest", "Near destination"), (f"low_{selected_idx}", "Near where battery drops below 20% (purple marker)")):
        if key in charging:
            if charging[key]:
                st.markdown(f'<div class="ev-subhead">{_h(title)} <small>— {len(charging[key])} station(s), nearest first</small></div>',
                            unsafe_allow_html=True)
                has_conn = any(s.get("sockets") for s in charging[key])     # show the column only when OpenStreetMap has connector data
                cols = ["Charging station", "Distance (km)"] + (["Connector type"] if has_conn else []) + ["Est. battery on arrival", "Reachable?"]
                recs = [[s["name"], round(s["distance_km"], 1)]
                        + ([s.get("sockets") or "Not listed"] if has_conn else [])
                        + [(f'{max(0.0, s["est_soc"]):.1f}%' if s.get("est_soc") is not None else "–"),
                           reach_chip.get(s.get("reach"), "–")] for s in charging[key]]
                n_c = len(cols)
                st.markdown(ev_table(cols, recs, num_cols=(1, n_c - 2), wrap_cols=(0, 2) if has_conn else (0,), min_width=480), unsafe_allow_html=True)
            else:
                st.markdown(ev_alert("info", f"{_h(title)}: no charging stations found within 10 km."), unsafe_allow_html=True)
    if any(charging.get(k) for k in ("dest", f"low_{selected_idx}")):
        st.markdown(ev_note("Battery on arrival is an estimate using the straight-line distance, so real roads use a little more. "
                            "Chargers come from OpenStreetMap: details may be missing or out of date, and live availability is not shown."),
                    unsafe_allow_html=True)

    # ------------------------------------------------------------------ 6. Navigation
    st.markdown(ev_section("back", "Navigation", "Start a new journey."), unsafe_allow_html=True)
    _nv, _ = st.columns([1, 2])
    with _nv:
        if st.button("← NEW TRIP", key="new_trip", type="primary"):
            st.session_state.page = "home"
            st.rerun()
