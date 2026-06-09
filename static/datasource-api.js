// Live data source: talks to the Flask backend and streams readings over SSE.

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== "") p.set(k, v);
  }
  const s = p.toString();
  return s ? "?" + s : "";
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> ${r.status}`);
  return r.json();
}

window.DataSource = {
  live: true,

  async meta() {
    const universities = await getJSON("/api/universities");
    return { live: true, generated_at: null, universities };
  },

  latest(scope) {
    return getJSON("/api/latest" + qs(scope));
  },

  series(scope) {
    return getJSON("/api/series" + qs(scope));
  },

  stats(scope) {
    return getJSON("/api/stats" + qs(scope));
  },

  messages(scope) {
    return getJSON("/api/messages" + qs(scope));
  },

  heatmap(scope) {
    return getJSON("/api/heatmap" + qs(scope));
  },

  anomaly(scope) {
    return getJSON("/api/anomaly" + qs(scope));
  },

  subscribe(onReading, onStatus) {
    const es = new EventSource("/api/stream");
    es.onopen = () => onStatus && onStatus(true);
    es.onerror = () => onStatus && onStatus(false);
    es.onmessage = (ev) => {
      let payload;
      try { payload = JSON.parse(ev.data); } catch { return; }
      if (payload.type === "reading") onReading(payload.data);
    };
    return es;
  },
};
