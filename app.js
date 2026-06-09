// UI controller. Data access goes through window.DataSource (live API or static export).

const fmt = (v, d = 1) => (v === null || v === undefined || isNaN(v) ? "—" : Number(v).toFixed(d));
const fmtInt = (v) => (v === null || v === undefined || isNaN(v) ? "—" : Math.round(v));
const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
const fmtTimeShort = (iso) => (iso ? new Date(iso).toLocaleTimeString() : "—");
const fmtBucketLabel = (bucket, str) => {
  if (!str) return "—";
  if (bucket === "day") return str;
  if (bucket === "hour") return str.slice(5, 16);
  return new Date(str).toLocaleString([], { hour: "2-digit", minute: "2-digit" });
};

function ccFlag(cc) {
  if (!cc || cc.length !== 2) return "📡";
  return String.fromCodePoint(...[...cc.toUpperCase()].map((c) => 0x1f1e6 + c.charCodeAt(0) - 65));
}

const PERIOD_LABEL = { day: "jour", week: "semaine", month: "mois", all: "tout", range: "plage" };

const state = {
  meta: null,
  scope: { university: null, device: null },
  nav: { period: "week" },
  metric: "temperature",
  bucket: "hour",
};

// --- theming ---

const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function chartTheme() {
  return { tick: cssVar("--muted") || "#8a93a6", grid: cssVar("--border") || "#262b35", text: cssVar("--text") || "#e6e8ee" };
}

function applyChartTheme() {
  const t = chartTheme();
  const all = [charts.co2, charts.temp, charts.hum, charts.pres, compareChart];
  for (const c of all) {
    c.options.plugins.legend.labels.color = t.text;
    c.options.scales.x.ticks.color = t.tick;
    c.options.scales.x.grid.color = t.grid;
    c.options.scales.y.ticks.color = t.tick;
    c.options.scales.y.grid.color = t.grid;
    if (c.options.scales.y.title) c.options.scales.y.title.color = t.tick;
    c.update("none");
  }
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("theme", theme); } catch (e) { /* ignore */ }
  applyChartTheme();
  setMapTheme(theme);
}

// --- charts ---

function makeChart(ctx, label, color) {
  const t = chartTheme();
  return new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets: [{
      label, data: [], borderColor: color, backgroundColor: color + "33",
      fill: true, tension: 0.25, pointRadius: 0, borderWidth: 2,
    }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { labels: { color: t.text } }, tooltip: { mode: "index", intersect: false } },
      scales: {
        x: { ticks: { color: t.tick, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { color: t.grid } },
        y: { ticks: { color: t.tick }, grid: { color: t.grid } },
      },
    },
  });
}

const charts = {
  co2: makeChart(document.getElementById("chart-co2"), "CO2 (ppm)", "#4cc9f0"),
  temp: makeChart(document.getElementById("chart-temp"), "Température (°C)", "#f6a26b"),
  hum: makeChart(document.getElementById("chart-hum"), "Humidité (%)", "#7be495"),
  pres: makeChart(document.getElementById("chart-pres"), "Pression (hPa)", "#c792ea"),
};

let _strongFlags = [];
const compareChart = new Chart(document.getElementById("chart-compare"), {
  type: "line",
  data: { labels: [], datasets: [
    { label: "Mesuré (°C)", data: [], borderColor: "#f6a26b", backgroundColor: "#f6a26b22", fill: true, tension: 0.25, borderWidth: 2,
      pointRadius: (c) => (_strongFlags[c.dataIndex] ? 4 : 0), pointBackgroundColor: "#ef476f", pointBorderColor: "#ef476f" },
    { label: "Normale nationale (°C)", data: [], borderColor: "#8a93a6", borderDash: [6, 5], fill: false, tension: 0, borderWidth: 2, pointRadius: 0 },
  ] },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { labels: { color: "#e6e8ee" } }, tooltip: { mode: "index", intersect: false } },
    scales: {
      x: { ticks: { color: "#8a93a6", maxRotation: 0, autoSkip: true, maxTicksLimit: 10 }, grid: { color: "#262b35" } },
      y: { ticks: { color: "#8a93a6" }, grid: { color: "#262b35" }, title: { display: true, text: "°C", color: "#8a93a6" } },
    },
  },
});

function setSeries(points, bucket) {
  const labels = points.map((p) => fmtBucketLabel(bucket, p.bucket));
  charts.co2.data.labels = labels; charts.co2.data.datasets[0].data = points.map((p) => p.co2);
  charts.temp.data.labels = labels; charts.temp.data.datasets[0].data = points.map((p) => p.temperature);
  charts.hum.data.labels = labels; charts.hum.data.datasets[0].data = points.map((p) => p.humidity);
  charts.pres.data.labels = labels; charts.pres.data.datasets[0].data = points.map((p) => p.air_pressure);
  Object.values(charts).forEach((c) => c.update("none"));
}

// --- map ---

let _map = null;
let _markers = {};
let _tileLayer = null;
const TILES = {
  dark: { url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png" },
  light: { url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png" },
};
const TILE_ATTR = '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> · © <a href="https://carto.com/attributions">CARTO</a>';

function setMapTheme(theme) {
  if (!_map) return;
  if (_tileLayer) _map.removeLayer(_tileLayer);
  _tileLayer = L.tileLayer(TILES[theme] ? TILES[theme].url : TILES.dark.url, { attribution: TILE_ATTR, maxZoom: 18 });
  _tileLayer.addTo(_map);
}

function initMap() {
  if (typeof L === "undefined") return;
  _map = L.map("map", { scrollWheelZoom: false, attributionControl: true });
  setMapTheme(document.documentElement.dataset.theme || "dark");
  const pts = [];
  for (const u of state.meta.universities) {
    if (u.latitude == null || u.longitude == null) continue;
    const real = !u.is_demo;
    const m = L.circleMarker([u.latitude, u.longitude], {
      radius: real ? 10 : 8,
      color: real ? "#4cc9f0" : "#4361ee",
      fillColor: real ? "#4cc9f0" : "#4361ee",
      fillOpacity: 0.85, weight: 2,
    }).addTo(_map);
    m.bindPopup(
      `<b>${ccFlag(u.country_code)} ${u.name}</b><br>${u.city || ""}, ${u.country || ""}<br>` +
      `${u.device_count || (u.devices || []).length} capteur(s) · ${Number(u.message_count || 0).toLocaleString()} mesures` +
      `${u.is_demo ? "<br><i>site de démonstration</i>" : ""}`
    );
    m.on("click", () => applyUniversitySelection(u.slug));
    _markers[u.slug] = m;
    pts.push([u.latitude, u.longitude]);
  }
  if (pts.length) _map.fitBounds(pts, { padding: [40, 40], maxZoom: 6 });
}

function highlightMarker(slug) {
  for (const [s, m] of Object.entries(_markers)) {
    m.setStyle({ weight: s === slug ? 4 : 2 });
    if (s === slug) m.bringToFront();
  }
}

// --- cards / table ---

function setCards(r) {
  if (!r) return;
  document.getElementById("v-co2").textContent = fmtInt(r.co2);
  document.getElementById("v-temp").textContent = fmt(r.temperature, 1);
  document.getElementById("v-hum").textContent = fmt(r.humidity, 1);
  document.getElementById("v-pres").textContent = fmt(r.air_pressure, 1);
  document.getElementById("v-batt").textContent = fmt(r.battery_percentage, 1);
  document.getElementById("v-batv").textContent = fmt(r.bat_v, 3);
  document.getElementById("v-rssi").textContent = fmtInt(r.rssi);
  document.getElementById("v-snr").textContent = fmt(r.snr, 1);
  const flag = (k) => (r[k] === "True" ? `<span class="flag-bad">${k.toUpperCase()}</span>` : "");
  document.getElementById("v-co2-flags").innerHTML = [flag("co2h_flag"), flag("co2l_flag")].filter(Boolean).join(" ");
  document.getElementById("v-temp-flags").innerHTML = [flag("temph_flag"), flag("templ_flag")].filter(Boolean).join(" ");
}

function rowHTML(r) {
  return `
    <td>${r.id ?? "—"}</td>
    <td>${fmtTimeShort(r.received_at)}</td>
    <td>${fmtInt(r.co2)}</td>
    <td>${fmt(r.temperature, 1)}</td>
    <td>${fmt(r.humidity, 1)}</td>
    <td>${fmt(r.air_pressure, 1)}</td>
    <td>${fmt(r.bat_v, 2)}</td>
    <td>${fmtInt(r.rssi)}</td>
    <td>${fmt(r.snr, 1)}</td>
    <td>${r.f_cnt ?? "—"}</td>`;
}

function prependRow(r, flash = false) {
  const tbody = document.getElementById("tbody");
  const tr = document.createElement("tr");
  if (flash) tr.classList.add("flash");
  tr.innerHTML = rowHTML(r);
  tbody.prepend(tr);
  while (tbody.children.length > 200) tbody.removeChild(tbody.lastChild);
}

function setRows(messages) {
  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  for (const m of messages.slice().reverse()) prependRow(m);
}

// --- anomaly panel ---

const LEVEL_FR = { normal: "Normal", moderate: "Anomalie modérée", strong: "Anomalie forte" };
const DIR_FR = { warmer: "plus chaud", cooler: "plus froid", neutral: "conforme" };

function renderAnomaly(a) {
  const panel = document.getElementById("anomaly-panel");
  const badge = document.getElementById("anomaly-badge");
  const delta = document.getElementById("anomaly-delta");
  const text = document.getElementById("anomaly-text");
  const stats = document.getElementById("anomaly-stats");
  const caveat = document.getElementById("anomaly-caveat");
  panel.classList.remove("lvl-normal", "lvl-moderate", "lvl-strong");

  const s = a.summary || {};
  if (!s.available) {
    badge.textContent = "Pas de référentiel";
    delta.textContent = "—";
    text.textContent = a.country_code
      ? "Pas assez de données sur la période pour comparer aux normales."
      : "Aucune normale climatique pour ce pays.";
    stats.innerHTML = "";
    return;
  }
  panel.classList.add("lvl-" + s.level);
  badge.textContent = LEVEL_FR[s.level] || s.level;
  const sign = s.anomaly_mean > 0 ? "+" : "";
  delta.textContent = `${sign}${s.anomaly_mean.toFixed(1)} °C`;

  const dir = DIR_FR[s.direction] || s.direction;
  const warm = s.warming
    ? " Le signal va dans le sens d'un <strong>climat plus chaud que la normale</strong>."
    : "";
  text.innerHTML =
    `Sur la période, ${a.university || "ce capteur"} mesure en moyenne ` +
    `<strong>${s.observed_mean} °C</strong>, contre une normale nationale de ` +
    `<strong>${s.normal_mean} °C</strong> (${a.country_name || a.country_code}). ` +
    `L'écart est <strong>${dir}</strong> de ${Math.abs(s.anomaly_mean).toFixed(1)} °C ` +
    `(z = ${s.z_mean}).${warm}`;

  stats.innerHTML = [
    ["z-score moyen", s.z_mean],
    ["Jours analysés", s.days_total],
    ["Jours en anomalie forte", s.days_strong],
    ["Normale", `${s.normal_mean} °C`],
  ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("");

  caveat.innerHTML = a.country_code === "FR"
    ? "Note : le capteur AQS01-L mesure l'air <em>intérieur</em> ; on l'utilise ici comme proxy de l'ambiance locale. Un biais positif systématique vis-à-vis des normales extérieures est donc attendu — l'intérêt de l'algorithme est la <em>variation</em> relative dans le temps."
    : "";
}

function renderCompare(a) {
  const daily = (a.daily || []).filter((d) => d.observed !== null);
  _strongFlags = daily.map((d) => d.z !== null && Math.abs(d.z) >= 2);
  compareChart.data.labels = daily.map((d) => d.date.slice(5));
  compareChart.data.datasets[0].data = daily.map((d) => d.observed);
  compareChart.data.datasets[1].data = daily.map((d) => d.normal);
  compareChart.update("none");
  document.getElementById("compare-sub").textContent = a.country_name
    ? `normale : ${a.country_name} · ${a.source || ""}` : "";
}

// --- heatmap ---

function valueToColor(v, min, max) {
  if (v === null || v === undefined || max === min) return cssVar("--panel-2") || "#1d2129";
  const t = Math.max(0, Math.min(1, (v - min) / (max - min)));
  const hue = 220 - 220 * t; // 220 (blue) -> 0 (red)
  return `hsl(${hue}, 70%, ${30 + 20 * t}%)`;
}

function renderHeatmap(h) {
  const el = document.getElementById("heatmap");
  const legend = document.getElementById("heatmap-legend");
  el.innerHTML = "";
  if (!h.cells || !h.cells.length) {
    el.innerHTML = '<div class="hm-empty">Aucune donnée</div>';
    legend.innerHTML = "";
    return;
  }
  const days = [...new Set(h.cells.map((c) => c.day))].sort();
  const grid = {};
  for (const c of h.cells) grid[c.day + "|" + c.hour] = c.value;

  const frag = document.createDocumentFragment();
  frag.appendChild(hmCell("", "hm-corner"));
  for (let hr = 0; hr < 24; hr++) frag.appendChild(hmCell(hr % 3 === 0 ? hr : "", "hm-hhead"));
  for (const day of days) {
    frag.appendChild(hmCell(day.slice(5), "hm-dhead"));
    for (let hr = 0; hr < 24; hr++) {
      const v = grid[day + "|" + hr];
      const cell = hmCell("", "hm-cell");
      cell.style.background = valueToColor(v, h.min, h.max);
      if (v !== undefined) cell.title = `${day} ${String(hr).padStart(2, "0")}h · ${fmt(v, 1)}`;
      frag.appendChild(cell);
    }
  }
  el.style.gridTemplateColumns = `48px repeat(24, 1fr)`;
  el.appendChild(frag);

  const unit = { temperature: "°C", co2: "ppm", humidity: "%", air_pressure: "hPa" }[h.metric] || "";
  legend.innerHTML =
    `<span>min ${fmt(h.min, 1)} ${unit}</span>` +
    `<span class="hm-gradient"></span>` +
    `<span>max ${fmt(h.max, 1)} ${unit}</span>` +
    `<span class="muted">(heures UTC)</span>`;
}

function hmCell(txt, cls) {
  const d = document.createElement("div");
  d.className = cls;
  if (txt !== "") d.textContent = txt;
  return d;
}

// --- stats line ---

function renderStats(s) {
  const net = document.getElementById("net-summary");
  net.textContent = `${(s.messages || 0).toLocaleString()} mesures · dernière ${fmtTimeShort(s.last)}`;
  const ps = document.getElementById("period-stats");
  if (s.messages) {
    ps.textContent =
      `CO₂ ${fmtInt(s.avg_co2)} ppm (${fmtInt(s.min_co2)}–${fmtInt(s.max_co2)}) · ` +
      `T° ${fmt(s.avg_temp, 1)} °C (${fmt(s.min_temp, 1)}–${fmt(s.max_temp, 1)}) · ` +
      `H ${fmt(s.avg_hum, 1)} % (${fmt(s.min_hum, 1)}–${fmt(s.max_hum, 1)})`;
  } else {
    ps.textContent = "Aucune donnée sur la période";
  }
}

// --- CSV export ---

function csvCell(v) {
  if (v === null || v === undefined) return "";
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

async function exportCSV() {
  const msgs = await DataSource.messages(scopeParams({ limit: 5000 }));
  const cols = [
    ["id", "id"], ["received_at", "recu_le"], ["co2", "co2_ppm"], ["temperature", "temperature_C"],
    ["humidity", "humidite_pct"], ["air_pressure", "pression_hPa"], ["bat_v", "batterie_V"],
    ["rssi", "rssi_dBm"], ["snr", "snr_dB"], ["f_cnt", "f_cnt"],
  ];
  const head = cols.map((c) => c[1]).join(",");
  const lines = msgs.map((m) => cols.map((c) => csvCell(m[c[0]])).join(","));
  const csv = [head, ...lines].join("\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const u = state.meta.universities.find((x) => x.slug === state.scope.university);
  const dev = u && (u.devices || []).find((d) => d.device_id === state.scope.device);
  const label = (dev && dev.label) || state.scope.device || state.scope.university || "donnees";
  const per = state.nav.period || "plage";
  a.href = url;
  a.download = `climacampus_${label}_${per}.csv`.replace(/\s+/g, "-");
  a.click();
  URL.revokeObjectURL(url);
}

// --- orchestration ---

function scopeParams(extra) {
  return Object.assign({}, state.scope, state.nav, extra || {});
}

async function reloadData() {
  const navLabel = state.nav.period ? PERIOD_LABEL[state.nav.period] : "plage";
  document.getElementById("tbl-period").textContent = "— " + navLabel;

  const [series, messages, latest, stats, anomaly, heatmap] = await Promise.all([
    DataSource.series(scopeParams()),
    DataSource.messages(scopeParams({ limit: 200 })),
    DataSource.latest(state.scope),
    DataSource.stats(scopeParams()),
    DataSource.anomaly(scopeParams()),
    DataSource.heatmap(scopeParams({ metric: state.metric })),
  ]);

  state.bucket = series.bucket;
  setSeries(series.points, series.bucket);
  setRows(messages);
  if (latest && (latest.id || latest.received_at)) setCards(latest);
  renderStats(stats);
  renderAnomaly(anomaly);
  renderCompare(anomaly);
  renderHeatmap(heatmap);
}

async function reloadHeatmapOnly() {
  const heatmap = await DataSource.heatmap(scopeParams({ metric: state.metric }));
  renderHeatmap(heatmap);
}

// --- selectors ---

function fillSelectors() {
  const unis = state.meta.universities;
  const uSel = document.getElementById("sel-uni");
  uSel.innerHTML = unis.map((u) => {
    const demo = u.is_demo ? " · démo" : "";
    return `<option value="${u.slug}">${ccFlag(u.country_code)} ${u.name} — ${u.city || u.country}${demo}</option>`;
  }).join("");
  uSel.value = state.scope.university;
  fillDeviceSelector();
  renderSiteMeta();
}

function fillDeviceSelector() {
  const u = state.meta.universities.find((x) => x.slug === state.scope.university);
  const dSel = document.getElementById("sel-device");
  const devices = (u && u.devices) || [];
  dSel.innerHTML = devices.map((d) => `<option value="${d.device_id}">${d.label || d.device_id}</option>`).join("");
  if (devices.length) {
    if (!devices.find((d) => d.device_id === state.scope.device)) state.scope.device = devices[0].device_id;
    dSel.value = state.scope.device;
  } else {
    state.scope.device = null;
  }
  dSel.disabled = devices.length <= 1;
}

function renderSiteMeta() {
  const u = state.meta.universities.find((x) => x.slug === state.scope.university);
  const el = document.getElementById("site-meta");
  if (!u) { el.textContent = ""; return; }
  const parts = [];
  parts.push(`${u.device_count || (u.devices || []).length} capteur(s)`);
  if (u.message_count != null) parts.push(`${Number(u.message_count).toLocaleString()} mesures`);
  if (u.last_seen) parts.push(`actif ${new Date(u.last_seen).toLocaleDateString()}`);
  if (u.is_demo) parts.push("données de démonstration");
  el.innerHTML = `<span class="site-name">${ccFlag(u.country_code)} ${u.city || ""}, ${u.country || ""}</span>` +
    `<span class="site-sub">${parts.join(" · ")}</span>`;
}

function applyUniversitySelection(slug) {
  state.scope.university = slug;
  document.getElementById("sel-uni").value = slug;
  fillDeviceSelector();
  renderSiteMeta();
  highlightMarker(slug);
  if (_map && _markers[slug]) {
    const ll = _markers[slug].getLatLng();
    _map.setView(ll, Math.max(_map.getZoom(), 5), { animate: true });
    _markers[slug].openPopup();
  }
  reloadData();
}

// --- live updates ---

function matchesScope(r) {
  if (state.scope.device && r.device_id && r.device_id !== state.scope.device) return false;
  if (state.scope.university && r.university_slug && r.university_slug !== state.scope.university) return false;
  return true;
}

let _statsTimer = null;
function onLiveReading(r) {
  if (!matchesScope(r)) return;
  setCards(r);
  if (state.nav.period === "day" && (state.bucket === "raw" || state.bucket === "hour")) {
    const t = fmtTimeShort(r.received_at);
    for (const [k, key] of [["co2", "co2"], ["temp", "temperature"], ["hum", "humidity"], ["pres", "air_pressure"]]) {
      const c = charts[k];
      c.data.labels.push(t);
      c.data.datasets[0].data.push(r[key]);
      while (c.data.labels.length > 1500) { c.data.labels.shift(); c.data.datasets[0].data.shift(); }
      c.update("none");
    }
    prependRow(r, true);
  }
  clearTimeout(_statsTimer);
  _statsTimer = setTimeout(() => {
    DataSource.stats(scopeParams()).then(renderStats);
    DataSource.anomaly(scopeParams()).then((a) => { renderAnomaly(a); renderCompare(a); });
  }, 1500);
}

function setConn(ok) {
  const dot = document.getElementById("conn-dot");
  const txt = document.getElementById("conn-text");
  if (DataSource.live) {
    dot.className = "dot " + (ok ? "on" : "off");
    txt.textContent = ok ? "En direct" : "Reconnexion…";
  } else {
    dot.className = "dot demo";
    txt.textContent = "Démonstration";
  }
}

// --- wiring ---

document.getElementById("theme-toggle").addEventListener("click", () => {
  setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
});

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
    state.nav = { period: btn.dataset.period };
    document.getElementById("range-from").value = "";
    document.getElementById("range-to").value = "";
    reloadData();
  });
});

document.querySelectorAll(".mtab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".mtab").forEach((b) => b.classList.toggle("active", b === btn));
    state.metric = btn.dataset.metric;
    reloadHeatmapOnly();
  });
});

document.getElementById("range-apply").addEventListener("click", () => {
  const from = document.getElementById("range-from").value;
  const to = document.getElementById("range-to").value;
  if (!from && !to) return;
  state.nav = {};
  if (from) state.nav.from = from + "T00:00:00Z";
  if (to) state.nav.to = to + "T23:59:59Z";
  document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
  reloadData();
});

document.getElementById("range-clear").addEventListener("click", () => {
  document.getElementById("range-from").value = "";
  document.getElementById("range-to").value = "";
  state.nav = { period: "week" };
  document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.period === "week"));
  reloadData();
});

document.getElementById("csv-export").addEventListener("click", exportCSV);

document.getElementById("sel-uni").addEventListener("change", (e) => applyUniversitySelection(e.target.value));

document.getElementById("sel-device").addEventListener("change", (e) => {
  state.scope.device = e.target.value;
  reloadData();
});

function renderAbout() {
  const gen = document.getElementById("about-generated");
  if (gen) gen.textContent = state.meta.generated_at ? new Date(state.meta.generated_at).toLocaleString() : "—";
  const cov = document.getElementById("about-coverage");
  const real = (state.meta.universities || []).find((u) => !u.is_demo);
  if (cov && real && real.message_count) {
    cov.textContent = ` Historique : ${Number(real.message_count).toLocaleString()} mesures.`;
  }
}

async function init() {
  state.meta = await DataSource.meta();
  const unis = state.meta.universities || [];
  if (!unis.length) {
    document.getElementById("net-summary").textContent = "Aucune université configurée";
    return;
  }
  const real = unis.find((u) => !u.is_demo) || unis[0];
  state.scope.university = real.slug;
  fillSelectors();
  renderAbout();
  setConn(false);
  initMap();
  highlightMarker(state.scope.university);
  applyChartTheme();
  await reloadData();
  if (DataSource.live) DataSource.subscribe(onLiveReading, setConn);
}

init();
