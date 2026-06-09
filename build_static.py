from __future__ import annotations

import json
import math
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = Path(__import__("os").environ.get("DB_PATH", str(BASE_DIR / "ttn_data.db")))
NORMALS_PATH = BASE_DIR / "climate_normals.json"
STATIC_DIR = BASE_DIR / "static"
TEMPLATE = BASE_DIR / "templates" / "index.html"
DIST = BASE_DIR / "dist"
DATA_DIR = DIST / "data"

NORMALS = json.loads(NORMALS_PATH.read_text(encoding="utf-8"))

DEMO_BANNER = (
    '<section class="demo-banner">Version de démonstration statique — '
    'instantané de données. Les capteurs « démo » européens sont simulés pour '
    'illustrer le réseau participatif ; les données de Villetaneuse sont réelles '
    '(1 mois collecté en LoRaWAN).</section>'
)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def iso_hour(s: str) -> str:
    """Normalise a strftime hour-bucket 'YYYY-MM-DD HH:00:00' to ISO with Z."""
    return s.replace(" ", "T") + "Z"


# ---------------------------------------------------------------------------
# Real data export
# ---------------------------------------------------------------------------

def export_real_device(conn: sqlite3.Connection, device_id: str) -> dict:
    hourly_rows = conn.execute(
        """
        SELECT strftime('%Y-%m-%d %H:00:00', m.received_at) AS h,
               AVG(d.co2) AS co2, AVG(d.temperature) AS temperature,
               AVG(d.humidity) AS humidity, AVG(d.air_pressure) AS air_pressure,
               AVG(d.bat_v) AS bat_v, AVG(bp.value) AS battery_percentage,
               COUNT(*) AS samples
        FROM messages m
        LEFT JOIN decoded_payloads d ON d.message_id = m.id
        LEFT JOIN battery_percentage bp ON bp.message_id = m.id
        WHERE m.device_id = ?
        GROUP BY h ORDER BY h ASC
        """,
        (device_id,),
    ).fetchall()
    hourly = [{
        "t": iso_hour(r["h"]),
        "co2": rnd(r["co2"], 0), "temperature": rnd(r["temperature"], 2),
        "humidity": rnd(r["humidity"], 1), "air_pressure": rnd(r["air_pressure"], 1),
        "bat_v": rnd(r["bat_v"], 3), "battery_percentage": rnd(r["battery_percentage"], 1),
        "samples": r["samples"],
    } for r in hourly_rows]

    recent_rows = conn.execute(
        """
        SELECT m.id, m.received_at, m.f_cnt, d.co2, d.temperature, d.humidity,
               d.air_pressure, d.bat_v,
               (SELECT rssi FROM rx_metadata r WHERE r.message_id = m.id ORDER BY rssi DESC LIMIT 1) AS rssi,
               (SELECT snr  FROM rx_metadata r WHERE r.message_id = m.id ORDER BY snr  DESC LIMIT 1) AS snr
        FROM messages m
        LEFT JOIN decoded_payloads d ON d.message_id = m.id
        WHERE m.device_id = ?
        ORDER BY m.id DESC LIMIT 300
        """,
        (device_id,),
    ).fetchall()
    recent = [dict(r) for r in recent_rows]

    latest_row = conn.execute(
        """
        SELECT m.id, m.device_id, m.received_at, m.f_cnt,
               d.bat_v, d.co2, d.temperature, d.humidity, d.air_pressure,
               d.co2h_flag, d.co2l_flag, d.temph_flag, d.templ_flag, d.node_type,
               bp.value AS battery_percentage,
               (SELECT rssi FROM rx_metadata r WHERE r.message_id = m.id ORDER BY rssi DESC LIMIT 1) AS rssi,
               (SELECT snr  FROM rx_metadata r WHERE r.message_id = m.id ORDER BY snr  DESC LIMIT 1) AS snr
        FROM messages m
        LEFT JOIN decoded_payloads d ON d.message_id = m.id
        LEFT JOIN battery_percentage bp ON bp.message_id = m.id
        WHERE m.device_id = ?
        ORDER BY m.id DESC LIMIT 1
        """,
        (device_id,),
    ).fetchone()

    return {"hourly": hourly, "recent": recent, "latest": dict(latest_row) if latest_row else {}}


def rnd(v, d):
    if v is None:
        return None
    return round(float(v), d)


# ---------------------------------------------------------------------------
# Synthetic demo data
# ---------------------------------------------------------------------------

def synth_device(slug: str, device_id: str, cc: str, start: datetime, end: datetime,
                 warming: float, seed: int) -> dict:
    rng = random.Random(seed)
    normals = NORMALS["countries"].get(cc, {})
    temps = normals.get("temp", [15] * 12)

    hourly = []
    walk = 0.0
    t = start.replace(minute=0, second=0, microsecond=0)
    while t <= end:
        base = temps[t.month - 1] + warming
        diurnal = 5.5 * math.sin(2 * math.pi * (t.hour - 15) / 24.0)
        walk = max(-3, min(3, walk + rng.uniform(-0.4, 0.4)))
        temperature = base + diurnal + walk + rng.uniform(-0.8, 0.8)
        humidity = max(20, min(95, 70 - 0.9 * (temperature - base) + rng.uniform(-5, 5)))
        occ = 1.0 if 8 <= t.hour <= 18 and t.weekday() < 5 else 0.2
        co2 = 430 + occ * rng.uniform(150, 420) + rng.uniform(-15, 15)
        pressure = 1013 + 6 * math.sin(2 * math.pi * (t.timetuple().tm_yday) / 30.0) + rng.uniform(-2, 2)
        hours_elapsed = (t - start).total_seconds() / 3600.0
        bat_v = 3.62 - 0.00018 * hours_elapsed
        battery = max(5, min(100, (bat_v - 3.0) / (3.65 - 3.0) * 100))
        hourly.append({
            "t": t.strftime("%Y-%m-%dT%H:00:00Z"),
            "co2": round(co2), "temperature": round(temperature, 2),
            "humidity": round(humidity, 1), "air_pressure": round(pressure, 1),
            "bat_v": round(bat_v, 3), "battery_percentage": round(battery, 1),
            "samples": rng.randint(8, 13),
        })
        t += timedelta(hours=1)

    # Recent raw-ish readings (~7 min cadence near the end).
    recent = []
    rt = end
    fcnt = 50000
    for i in range(300):
        h = next((x for x in reversed(hourly) if x["t"][:13] == rt.strftime("%Y-%m-%dT%H")), hourly[-1])
        recent.append({
            "id": 1000000 + (300 - i),
            "received_at": rt.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
            "f_cnt": fcnt - i,
            "co2": h["co2"] + rng.randint(-20, 20),
            "temperature": round(h["temperature"] + rng.uniform(-0.5, 0.5), 1),
            "humidity": round(h["humidity"] + rng.uniform(-2, 2), 1),
            "air_pressure": round(h["air_pressure"] + rng.uniform(-1, 1), 1),
            "bat_v": h["bat_v"],
            "rssi": rng.randint(-112, -40),
            "snr": round(rng.uniform(-6, 11), 1),
        })
        rt -= timedelta(minutes=7)

    last = hourly[-1]
    latest = {
        "id": recent[0]["id"], "device_id": device_id,
        "received_at": recent[0]["received_at"], "f_cnt": recent[0]["f_cnt"],
        "bat_v": last["bat_v"], "co2": last["co2"], "temperature": last["temperature"],
        "humidity": last["humidity"], "air_pressure": last["air_pressure"],
        "co2h_flag": "False", "co2l_flag": "False", "temph_flag": "False", "templ_flag": "False",
        "node_type": "AQS01-L", "battery_percentage": last["battery_percentage"],
        "rssi": recent[0]["rssi"], "snr": recent[0]["snr"],
    }
    return {"hourly": hourly, "recent": recent, "latest": latest}


DEMO_SITES = [
    {"slug": "univ-madrid", "name": "Universidad Politécnica de Madrid", "city": "Madrid",
     "country": "Espagne", "country_code": "ES", "latitude": 40.4406, "longitude": -3.7264,
     "device_id": "upm-aqs-01", "label": "Campus Sud", "warming": 1.8, "seed": 11},
    {"slug": "tu-berlin", "name": "Technische Universität Berlin", "city": "Berlin",
     "country": "Allemagne", "country_code": "DE", "latitude": 52.5125, "longitude": 13.3269,
     "device_id": "tub-aqs-01", "label": "Hauptgebäude", "warming": 0.4, "seed": 22},
    {"slug": "polimi-milano", "name": "Politecnico di Milano", "city": "Milan",
     "country": "Italie", "country_code": "IT", "latitude": 45.4781, "longitude": 9.2275,
     "device_id": "polimi-aqs-01", "label": "Bovisa", "warming": 1.1, "seed": 33},
    {"slug": "ulisboa", "name": "Universidade de Lisboa", "city": "Lisbonne",
     "country": "Portugal", "country_code": "PT", "latitude": 38.7528, "longitude": -9.1607,
     "device_id": "ul-aqs-01", "label": "Cidade Universitária", "warming": 0.6, "seed": 44},
]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def main():
    if DIST.exists():
        shutil.rmtree(DIST)
    DATA_DIR.mkdir(parents=True)

    conn = connect()
    universities = []

    # Real universities + devices.
    real_unis = conn.execute(
        "SELECT * FROM universities WHERE is_demo = 0 ORDER BY id"
    ).fetchall()
    span = conn.execute("SELECT MIN(received_at) a, MAX(received_at) b FROM messages").fetchone()
    start = datetime.fromisoformat(span["a"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(span["b"].replace("Z", "+00:00"))

    for u in real_unis:
        devices_meta = []
        devs = conn.execute(
            """SELECT dev.device_id, dev.label, dev.latitude, dev.longitude,
                      COUNT(m.id) AS message_count, MAX(m.received_at) AS last_seen
               FROM devices dev LEFT JOIN messages m ON m.device_id = dev.device_id
               WHERE dev.university_id = ? GROUP BY dev.device_id""",
            (u["id"],),
        ).fetchall()
        for d in devs:
            payload = export_real_device(conn, d["device_id"])
            fname = f"{u['slug']}__{d['device_id']}.json"
            (DATA_DIR / fname).write_text(json.dumps({"device": d["device_id"], **payload}), encoding="utf-8")
            devices_meta.append({
                "device_id": d["device_id"], "label": d["label"] or d["device_id"],
                "latitude": d["latitude"] or u["latitude"], "longitude": d["longitude"] or u["longitude"],
                "message_count": d["message_count"], "last_seen": d["last_seen"], "file": f"data/{fname}",
            })
        universities.append({
            "slug": u["slug"], "name": u["name"], "city": u["city"], "country": u["country"],
            "country_code": u["country_code"], "latitude": u["latitude"], "longitude": u["longitude"],
            "is_demo": 0, "device_count": len(devices_meta),
            "message_count": sum(d["message_count"] for d in devices_meta),
            "last_seen": max((d["last_seen"] for d in devices_meta), default=None),
            "devices": devices_meta,
        })

    # Synthetic demo universities.
    for site in DEMO_SITES:
        payload = synth_device(site["slug"], site["device_id"], site["country_code"],
                               start, end, site["warming"], site["seed"])
        fname = f"{site['slug']}__{site['device_id']}.json"
        (DATA_DIR / fname).write_text(json.dumps({"device": site["device_id"], **payload}), encoding="utf-8")
        msg_count = sum(h["samples"] for h in payload["hourly"])
        universities.append({
            "slug": site["slug"], "name": site["name"], "city": site["city"],
            "country": site["country"], "country_code": site["country_code"],
            "latitude": site["latitude"], "longitude": site["longitude"],
            "is_demo": 1, "device_count": 1, "message_count": msg_count,
            "last_seen": payload["latest"]["received_at"],
            "devices": [{
                "device_id": site["device_id"], "label": site["label"],
                "latitude": site["latitude"], "longitude": site["longitude"],
                "message_count": msg_count, "last_seen": payload["latest"]["received_at"],
                "file": f"data/{fname}",
            }],
        })

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "live": False,
        "normals": NORMALS,
        "universities": universities,
    }
    (DATA_DIR / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Static assets.
    for name in ("app.js", "style.css", "datasource-static.js"):
        shutil.copy(STATIC_DIR / name, DIST / name)

    html = TEMPLATE.read_text(encoding="utf-8")
    html = (html
            .replace("/static/style.css", "style.css")
            .replace("/static/datasource-api.js", "datasource-static.js")
            .replace("/static/app.js", "app.js")
            .replace("</header>", "</header>\n  " + DEMO_BANNER))
    (DIST / "index.html").write_text(html, encoding="utf-8")
    (DIST / ".nojekyll").write_text("", encoding="utf-8")

    total = sum(u["message_count"] for u in universities)
    print(f"built dist/ — {len(universities)} universities, "
          f"{sum(u['device_count'] for u in universities)} sensors, ~{total:,} measurements")


if __name__ == "__main__":
    main()
