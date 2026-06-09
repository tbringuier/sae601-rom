// Static data source: reads pre-exported JSON (no backend) and computes
// series / stats / heatmap / anomaly client-side. Used by the GitHub Pages build.

(function () {
  const DAY = 86400000;
  let _manifest = null;
  const _deviceCache = {};

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url} -> ${r.status}`);
    return r.json();
  }

  async function manifest() {
    if (!_manifest) _manifest = await getJSON("data/manifest.json");
    return _manifest;
  }

  function findUni(m, slug) {
    return (slug && m.universities.find((u) => u.slug === slug)) || m.universities[0];
  }

  function findDeviceRef(m, scope) {
    const u = findUni(m, scope.university);
    const dev = (scope.device && u.devices.find((d) => d.device_id === scope.device)) || u.devices[0];
    return { uni: u, dev };
  }

  async function loadDevice(m, scope) {
    const { uni, dev } = findDeviceRef(m, scope);
    if (!dev) return { uni, dev: null, data: null };
    if (!_deviceCache[dev.file]) _deviceCache[dev.file] = await getJSON(dev.file);
    return { uni, dev, data: _deviceCache[dev.file] };
  }

  function dataNow(data) {
    const h = data.hourly;
    return h.length ? new Date(h[h.length - 1].t).getTime() : Date.now();
  }

  function windowBounds(data, scope) {
    if (scope.from || scope.to) {
      return {
        since: scope.from ? new Date(scope.from).getTime() : -Infinity,
        until: scope.to ? new Date(scope.to).getTime() : Infinity,
        span: "range",
      };
    }
    const now = dataNow(data);
    const map = { day: 1, week: 7, month: 30 };
    const days = map[scope.period];
    return {
      since: days ? now - days * DAY : -Infinity,
      until: Infinity,
      span: scope.period || "day",
    };
  }

  function inWindow(rows, w, key = "t") {
    return rows.filter((r) => {
      const t = new Date(r[key]).getTime();
      return t >= w.since && t <= w.until;
    });
  }

  function avg(arr) {
    const v = arr.filter((x) => x !== null && x !== undefined && !isNaN(x));
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  }

  function aggregateDaily(hourly) {
    const byDay = {};
    for (const h of hourly) {
      const day = h.t.slice(0, 10);
      (byDay[day] = byDay[day] || []).push(h);
    }
    return Object.keys(byDay).sort().map((day) => {
      const rows = byDay[day];
      return {
        bucket: day,
        co2: avg(rows.map((r) => r.co2)),
        temperature: avg(rows.map((r) => r.temperature)),
        humidity: avg(rows.map((r) => r.humidity)),
        air_pressure: avg(rows.map((r) => r.air_pressure)),
        bat_v: avg(rows.map((r) => r.bat_v)),
        battery_percentage: avg(rows.map((r) => r.battery_percentage)),
        samples: rows.reduce((a, r) => a + (r.samples || 1), 0),
      };
    });
  }

  // --- climate normals (mirror of the Python algorithm) ---

  function normalFor(normals, cc, monthIdx) {
    const ds = (normals && normals.default_sigma) || 3.0;
    const c = normals && normals.countries && normals.countries[cc];
    if (!c) return [null, ds];
    const t = c.temp && c.temp[monthIdx] !== undefined ? c.temp[monthIdx] : null;
    const s = c.sigma && c.sigma[monthIdx] !== undefined ? c.sigma[monthIdx] : ds;
    return [t, s];
  }

  function classifyZ(normals, z) {
    const th = (normals && normals.thresholds) || { z_moderate: 1, z_strong: 2 };
    const a = Math.abs(z);
    let level = a < th.z_moderate ? "normal" : a < th.z_strong ? "moderate" : "strong";
    const direction = z > 0 ? "warmer" : z < 0 ? "cooler" : "neutral";
    return { level, direction };
  }

  window.DataSource = {
    live: false,

    async meta() {
      const m = await manifest();
      return { live: false, generated_at: m.generated_at, universities: m.universities, normals: m.normals };
    },

    async latest(scope) {
      const { data } = await loadDevice(await manifest(), scope);
      return (data && data.latest) || {};
    },

    async series(scope) {
      const m = await manifest();
      const { data } = await loadDevice(m, scope);
      if (!data) return { bucket: "hour", points: [] };
      const w = windowBounds(data, scope);
      const hourly = inWindow(data.hourly, w);
      let bucket, points;
      const spanDays = (w.until === Infinity ? dataNow(data) : w.until) - (w.since === -Infinity ? new Date(data.hourly[0].t).getTime() : w.since);
      if (w.span === "month" || w.span === "all" || spanDays > 21 * DAY) {
        bucket = "day";
        points = aggregateDaily(hourly);
      } else {
        bucket = "hour";
        points = hourly.map((h) => ({
          bucket: h.t.replace("T", " ").slice(0, 16) + ":00",
          co2: h.co2, temperature: h.temperature, humidity: h.humidity,
          air_pressure: h.air_pressure, bat_v: h.bat_v, battery_percentage: h.battery_percentage,
          samples: h.samples,
        }));
      }
      return { period: w.span, bucket, points };
    },

    async stats(scope) {
      const m = await manifest();
      const { data } = await loadDevice(m, scope);
      if (!data) return { messages: 0 };
      const w = windowBounds(data, scope);
      const hourly = inWindow(data.hourly, w);
      const col = (k) => hourly.map((h) => h[k]).filter((x) => x !== null && x !== undefined && !isNaN(x));
      const co2 = col("co2"), temp = col("temperature"), hum = col("humidity");
      const min = (a) => (a.length ? Math.min(...a) : null);
      const max = (a) => (a.length ? Math.max(...a) : null);
      return {
        period: w.span,
        messages: hourly.reduce((a, h) => a + (h.samples || 1), 0),
        first: hourly.length ? hourly[0].t : null,
        last: hourly.length ? hourly[hourly.length - 1].t : null,
        avg_co2: avg(co2), min_co2: min(co2), max_co2: max(co2),
        avg_temp: avg(temp), min_temp: min(temp), max_temp: max(temp),
        avg_hum: avg(hum), min_hum: min(hum), max_hum: max(hum),
        avg_pres: avg(col("air_pressure")),
      };
    },

    async messages(scope) {
      const m = await manifest();
      const { data } = await loadDevice(m, scope);
      if (!data || !data.recent) return [];
      const w = windowBounds(data, scope);
      const limit = scope.limit || 200;
      return data.recent
        .filter((r) => {
          const t = new Date(r.received_at).getTime();
          return t >= w.since && t <= w.until;
        })
        .slice(0, limit);
    },

    async heatmap(scope) {
      const m = await manifest();
      const { data } = await loadDevice(m, scope);
      const metric = scope.metric || "temperature";
      if (!data) return { metric, cells: [], min: null, max: null };
      const w = windowBounds(data, scope);
      const hourly = inWindow(data.hourly, w);
      const cells = hourly
        .filter((h) => h[metric] !== null && h[metric] !== undefined)
        .map((h) => ({ day: h.t.slice(0, 10), hour: parseInt(h.t.slice(11, 13), 10), value: h[metric], samples: h.samples }));
      const vals = cells.map((c) => c.value);
      return { metric, cells, min: vals.length ? Math.min(...vals) : null, max: vals.length ? Math.max(...vals) : null };
    },

    async anomaly(scope) {
      const m = await manifest();
      const { uni, data } = await loadDevice(m, scope);
      const normals = m.normals;
      const cc = uni && uni.country_code;
      if (!data) return { country_code: cc, daily: [], summary: { available: false } };
      const w = windowBounds(data, scope);
      const hourly = inWindow(data.hourly, w);
      const daily = aggregateDaily(hourly).map((d) => {
        const monthIdx = parseInt(d.bucket.slice(5, 7), 10) - 1;
        const [normal, sigma] = normalFor(normals, cc, monthIdx);
        const observed = d.temperature;
        let anomaly = null, z = null;
        if (normal !== null && observed !== null) {
          anomaly = observed - normal;
          z = sigma ? anomaly / sigma : null;
        }
        return { date: d.bucket, observed, normal, anomaly, z, samples: d.samples };
      });

      const diffs = daily.filter((d) => d.z !== null);
      let summary = { available: false };
      if (diffs.length) {
        const anomaly_mean = avg(diffs.map((d) => d.anomaly));
        const z_mean = avg(diffs.map((d) => d.z));
        const { level, direction } = classifyZ(normals, z_mean);
        summary = {
          available: true,
          observed_mean: round(avg(daily.map((d) => d.observed)), 2),
          normal_mean: round(avg(daily.map((d) => d.normal)), 2),
          anomaly_mean: round(anomaly_mean, 2),
          z_mean: round(z_mean, 2),
          level, direction,
          warming: anomaly_mean > 0 && Math.abs(z_mean) >= 1.0,
          days_total: daily.length,
          days_strong: daily.filter((d) => d.z !== null && Math.abs(d.z) >= 2.0).length,
        };
      }
      const country = normals && normals.countries && normals.countries[cc];
      return {
        country_code: cc,
        country_name: country ? country.name : null,
        university: uni ? uni.name : null,
        source: normals ? normals.source : null,
        thresholds: normals ? normals.thresholds : null,
        daily, summary,
      };
    },

    subscribe() { return null; },
  };

  function round(v, d) {
    if (v === null || v === undefined) return v;
    const f = Math.pow(10, d);
    return Math.round(v * f) / f;
  }
})();
