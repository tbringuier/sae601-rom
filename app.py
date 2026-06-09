from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import queue
import shutil
import signal
import sqlite3
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

load_dotenv()


def _clean(value: str | None) -> str:
    if value is None:
        return ""
    s = value
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1]
    else:
        idx = -1
        for i, ch in enumerate(s):
            if ch == "#" and i > 0 and s[i - 1] in " \t":
                idx = i
                break
        if idx >= 0:
            s = s[:idx]
    return s.strip()


def env_str(name: str, default: str) -> str:
    v = _clean(os.getenv(name))
    return v if v else default


def env_int(name: str, default: int) -> int:
    v = _clean(os.getenv(name))
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        raise SystemExit(f"invalid integer for {name!r}: {v!r}")


def env_bool(name: str, default: bool) -> bool:
    v = _clean(os.getenv(name)).lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


BASE_DIR = Path(__file__).parent
DB_PATH = Path(env_str("DB_PATH", str(BASE_DIR / "ttn_data.db")))
SCHEMA_PATH = BASE_DIR / "schema.sql"
FEEDS_PATH = Path(env_str("FEEDS_PATH", str(BASE_DIR / "feeds.json")))
NORMALS_PATH = Path(env_str("NORMALS_PATH", str(BASE_DIR / "climate_normals.json")))

WEB_HOST = env_str("WEB_HOST", "0.0.0.0")
WEB_PORT = env_int("WEB_PORT", 5000)
WEB_THREADS = env_int("WEB_THREADS", 8)

BACKUP_DIR = Path(env_str("BACKUP_DIR", str(BASE_DIR / "backups")))
BACKUP_INTERVAL_SECONDS = env_int("BACKUP_INTERVAL_SECONDS", 21600)
BACKUP_KEEP = env_int("BACKUP_KEEP", 30)

LOG_FILE = Path(env_str("LOG_FILE", str(BASE_DIR / "app.log")))
LOG_LEVEL = env_str("LOG_LEVEL", "INFO").upper()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("sae")
    logger.setLevel(LOG_LEVEL)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_h = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_h.setFormatter(fmt)
    logger.addHandler(file_h)

    stream_h = logging.StreamHandler(sys.stdout)
    stream_h.setFormatter(fmt)
    logger.addHandler(stream_h)

    logger.propagate = False
    return logger


log = setup_logging()


# ---------------------------------------------------------------------------
# Config (feeds + climate normals)
# ---------------------------------------------------------------------------

def load_feeds() -> dict:
    if not FEEDS_PATH.exists():
        log.warning("feeds file %s not found; running without MQTT feeds", FEEDS_PATH)
        return {"universities": []}
    try:
        return json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to read feeds file %s", FEEDS_PATH)
        return {"universities": []}


def load_normals() -> dict:
    try:
        return json.loads(NORMALS_PATH.read_text(encoding="utf-8"))
    except Exception:
        log.exception("failed to read climate normals %s", NORMALS_PATH)
        return {"countries": {}, "default_sigma": 3.0,
                "thresholds": {"z_moderate": 1.0, "z_strong": 2.0}}


FEEDS = load_feeds()
NORMALS = load_normals()


# ---------------------------------------------------------------------------
# Shutdown coordination
# ---------------------------------------------------------------------------

_stop_event = threading.Event()
_signal_count = 0


def _handle_signal(signum, _frame):
    global _signal_count
    _signal_count += 1
    if _signal_count == 1:
        log.info("signal %s received, shutting down (Ctrl+C again to force)", signum)
        _stop_event.set()
    else:
        log.warning("forced exit")
        os._exit(1)


for _sig in (signal.SIGTERM, signal.SIGINT):
    signal.signal(_sig, _handle_signal)


# ---------------------------------------------------------------------------
# Flask app + state
# ---------------------------------------------------------------------------

app = Flask(__name__)

_subscribers: list[queue.Queue] = []
_subscribers_lock = threading.Lock()
_db_lock = threading.Lock()

_mqtt_state = {
    "feeds": {},          # slug -> {connected, last_connect_at, last_message_at, messages_received}
}
_mqtt_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an older single-sensor database up to the multi-university schema."""
    cols = _columns(conn, "devices")
    for col, ddl in [
        ("university_id", "ALTER TABLE devices ADD COLUMN university_id INTEGER"),
        ("label", "ALTER TABLE devices ADD COLUMN label TEXT"),
        ("latitude", "ALTER TABLE devices ADD COLUMN latitude REAL"),
        ("longitude", "ALTER TABLE devices ADD COLUMN longitude REAL"),
    ]:
        if col not in cols:
            conn.execute(ddl)
            log.info("migration: added devices.%s", col)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_devices_university ON devices(university_id)")


def seed_universities(conn: sqlite3.Connection) -> dict[str, int]:
    """Upsert universities from feeds.json and attribute devices by application.

    Returns a mapping feed-slug -> university_id for the MQTT threads.
    """
    now = datetime.now(timezone.utc).isoformat()
    slug_to_id: dict[str, int] = {}
    for u in FEEDS.get("universities", []):
        conn.execute(
            """
            INSERT INTO universities
              (slug, name, city, country, country_code, latitude, longitude, timezone, is_demo, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(slug) DO UPDATE SET
              name = excluded.name, city = excluded.city, country = excluded.country,
              country_code = excluded.country_code, latitude = excluded.latitude,
              longitude = excluded.longitude, timezone = excluded.timezone
            """,
            (u["slug"], u["name"], u.get("city"), u.get("country"), u.get("country_code"),
             u.get("latitude"), u.get("longitude"), u.get("timezone"), now),
        )
        uid = conn.execute("SELECT id FROM universities WHERE slug = ?", (u["slug"],)).fetchone()["id"]
        slug_to_id[u["slug"]] = uid
        # Attribute historical + future devices of this university's TTN applications.
        for app_id in u.get("applications", []):
            conn.execute(
                "UPDATE devices SET university_id = ? WHERE application_id = ?",
                (uid, app_id),
            )
        # Friendly display names (e.g. room numbers) for known devices.
        for dev_id, label in (u.get("device_labels") or {}).items():
            conn.execute("UPDATE devices SET label = ? WHERE device_id = ?", (label, dev_id))
    conn.commit()
    return slug_to_id


def init_db() -> dict[str, int]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -8000")
        migrate(conn)
        slug_to_id = seed_universities(conn)
    log.info("database ready at %s (%d universities configured)", DB_PATH, len(slug_to_id))
    return slug_to_id


def upsert_device(conn: sqlite3.Connection, ids: dict, received_at: str, university_id: int | None) -> str:
    device_id = ids.get("device_id")
    conn.execute(
        """
        INSERT INTO devices (device_id, university_id, application_id, dev_eui, join_eui, dev_addr, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
          university_id  = COALESCE(excluded.university_id, devices.university_id),
          application_id = excluded.application_id,
          dev_eui        = excluded.dev_eui,
          join_eui       = excluded.join_eui,
          dev_addr       = excluded.dev_addr,
          last_seen      = excluded.last_seen
        """,
        (
            device_id,
            university_id,
            (ids.get("application_ids") or {}).get("application_id"),
            ids.get("dev_eui"),
            ids.get("join_eui"),
            ids.get("dev_addr"),
            received_at,
            received_at,
        ),
    )
    return device_id


def store_message(payload: dict, university_id: int | None) -> int | None:
    ids = payload.get("end_device_ids") or {}
    uplink = payload.get("uplink_message") or {}
    received_at = payload.get("received_at") or uplink.get("received_at")

    with _db_lock, get_db() as conn:
        device_id = upsert_device(conn, ids, received_at, university_id)

        cur = conn.execute(
            """
            INSERT INTO messages
              (device_id, received_at, correlation_ids, session_key_id,
               f_port, f_cnt, frm_payload, consumed_airtime, uplink_received_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                device_id,
                received_at,
                json.dumps(payload.get("correlation_ids") or []),
                uplink.get("session_key_id"),
                uplink.get("f_port"),
                uplink.get("f_cnt"),
                uplink.get("frm_payload"),
                uplink.get("consumed_airtime"),
                uplink.get("received_at"),
                json.dumps(payload),
            ),
        )
        message_id = cur.lastrowid

        decoded = uplink.get("decoded_payload") or {}
        if decoded:
            conn.execute(
                """
                INSERT INTO decoded_payloads
                  (message_id, bat_v, co2h_flag, co2l_flag, node_type,
                   temph_flag, templ_flag, air_pressure, co2, humidity, temperature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    decoded.get("BatV"),
                    decoded.get("CO2H_flag"),
                    decoded.get("CO2L_flag"),
                    decoded.get("Node_type"),
                    decoded.get("TEMPH_flag"),
                    decoded.get("TEMPL_flag"),
                    decoded.get("air_pressure"),
                    decoded.get("co2"),
                    decoded.get("humidity"),
                    decoded.get("temperature"),
                ),
            )

        for rx in uplink.get("rx_metadata") or []:
            gw = rx.get("gateway_ids") or {}
            conn.execute(
                """
                INSERT INTO rx_metadata
                  (message_id, gateway_id, gateway_eui, time, timestamp,
                   rssi, channel_rssi, snr, frequency_offset, uplink_token,
                   channel_index, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    gw.get("gateway_id"),
                    gw.get("eui"),
                    rx.get("time"),
                    rx.get("timestamp"),
                    rx.get("rssi"),
                    rx.get("channel_rssi"),
                    rx.get("snr"),
                    rx.get("frequency_offset"),
                    rx.get("uplink_token"),
                    rx.get("channel_index"),
                    rx.get("received_at"),
                ),
            )

        s = uplink.get("settings") or {}
        lora = ((s.get("data_rate") or {}).get("lora")) or {}
        if s or lora:
            conn.execute(
                """
                INSERT INTO settings
                  (message_id, bandwidth, spreading_factor, coding_rate,
                   frequency, timestamp, time)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    lora.get("bandwidth"),
                    lora.get("spreading_factor"),
                    lora.get("coding_rate"),
                    s.get("frequency"),
                    s.get("timestamp"),
                    s.get("time"),
                ),
            )

        net = uplink.get("network_ids") or {}
        if net:
            conn.execute(
                """
                INSERT INTO network_ids
                  (message_id, net_id, ns_id, tenant_id, cluster_id, cluster_address)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    net.get("net_id"),
                    net.get("ns_id"),
                    net.get("tenant_id"),
                    net.get("cluster_id"),
                    net.get("cluster_address"),
                ),
            )

        bat = uplink.get("last_battery_percentage") or {}
        if bat:
            conn.execute(
                "INSERT INTO battery_percentage (message_id, value, received_at) VALUES (?, ?, ?)",
                (message_id, bat.get("value"), bat.get("received_at")),
            )

        conn.commit()
        return message_id


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------

def broadcast(event: dict) -> None:
    data = json.dumps(event)
    with _subscribers_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _subscribers.remove(q)


def latest_reading_dict(message_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT m.id, m.device_id, m.received_at, m.f_cnt,
                   dev.university_id, dev.label AS device_label,
                   u.slug AS university_slug, u.name AS university_name,
                   d.bat_v, d.co2, d.temperature, d.humidity, d.air_pressure,
                   d.co2h_flag, d.co2l_flag, d.temph_flag, d.templ_flag, d.node_type,
                   bp.value AS battery_percentage,
                   (SELECT rssi FROM rx_metadata r WHERE r.message_id = m.id ORDER BY rssi DESC LIMIT 1) AS rssi,
                   (SELECT snr  FROM rx_metadata r WHERE r.message_id = m.id ORDER BY snr  DESC LIMIT 1) AS snr
            FROM messages m
            LEFT JOIN decoded_payloads d ON d.message_id = m.id
            LEFT JOIN battery_percentage bp ON bp.message_id = m.id
            LEFT JOIN devices dev ON dev.device_id = m.device_id
            LEFT JOIN universities u ON u.id = dev.university_id
            WHERE m.id = ?
            """,
            (message_id,),
        ).fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# MQTT (one client thread per configured feed)
# ---------------------------------------------------------------------------

def _make_callbacks(slug: str, university_id: int | None, topic: str):
    def on_connect(client, userdata, flags, reason_code, properties=None):
        log.info("[%s] MQTT connected (rc=%s), subscribing to %s", slug, reason_code, topic)
        client.subscribe(topic, qos=1)
        with _mqtt_state_lock:
            st = _mqtt_state["feeds"].setdefault(slug, {})
            st["connected"] = True
            st["last_connect_at"] = datetime.now(timezone.utc).isoformat()

    def on_disconnect(client, userdata, *args):
        log.warning("[%s] MQTT disconnected", slug)
        with _mqtt_state_lock:
            st = _mqtt_state["feeds"].setdefault(slug, {})
            st["connected"] = False
            st["last_disconnect_at"] = datetime.now(timezone.utc).isoformat()

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            log.error("[%s] bad MQTT payload: %s", slug, e)
            return
        try:
            message_id = store_message(payload, university_id)
            with _mqtt_state_lock:
                st = _mqtt_state["feeds"].setdefault(slug, {})
                st["last_message_at"] = datetime.now(timezone.utc).isoformat()
                st["messages_received"] = st.get("messages_received", 0) + 1
            reading = latest_reading_dict(message_id) if message_id else None
            if reading:
                broadcast({"type": "reading", "data": reading})
                log.info("[%s] stored msg id=%s co2=%s t=%s h=%s", slug, message_id,
                         reading.get("co2"), reading.get("temperature"), reading.get("humidity"))
        except Exception:
            log.exception("[%s] failed to store MQTT message", slug)

    return on_connect, on_disconnect, on_message


def mqtt_loop(slug: str, feed: dict, university_id: int | None):
    host = feed["host"]
    port = int(feed.get("port", 8883))
    tls = bool(feed.get("tls", True))
    user = feed.get("user", "")
    password = feed.get("password", "")
    topic = feed.get("topic", "v3/+/devices/+/up")

    backoff = 5
    while not _stop_event.is_set():
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"sae-{slug}-{int(time.time())}",
            clean_session=True,
        )
        if user:
            client.username_pw_set(user, password)
        if tls:
            client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
        on_c, on_d, on_m = _make_callbacks(slug, university_id, topic)
        client.on_connect = on_c
        client.on_disconnect = on_d
        client.on_message = on_m
        client.reconnect_delay_set(min_delay=1, max_delay=120)

        try:
            client.connect(host, port, keepalive=60)
            client.loop_start()
            backoff = 5
            while not _stop_event.is_set():
                time.sleep(2 if client.is_connected() else 1)
            client.loop_stop()
            client.disconnect()
            return
        except Exception as e:
            log.error("[%s] MQTT loop error: %s; reconnecting in %ss", slug, e, backoff)
            try:
                client.loop_stop()
            except Exception:
                pass
            if _stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, 120)


def start_feeds(slug_to_id: dict[str, int]) -> list[threading.Thread]:
    threads = []
    for u in FEEDS.get("universities", []):
        feed = u.get("feed") or {}
        if not feed.get("enabled", False):
            continue
        slug = u["slug"]
        t = threading.Thread(
            target=mqtt_loop, args=(slug, feed, slug_to_id.get(slug)),
            name=f"mqtt-{slug}", daemon=True,
        )
        t.start()
        threads.append(t)
        log.info("started MQTT feed for %s", slug)
    if not threads:
        log.warning("no enabled MQTT feeds; running in read-only / replay mode")
    return threads


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def backup_db_once() -> Path | None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_path = BACKUP_DIR / f"ttn_data_{ts}.db"
    final_path = BACKUP_DIR / f"ttn_data_{ts}.db.gz"

    try:
        src = sqlite3.connect(str(DB_PATH), timeout=30)
        try:
            dst = sqlite3.connect(str(tmp_path))
            try:
                src.backup(dst, pages=50, progress=None, sleep=0.05)
            finally:
                dst.close()
        finally:
            src.close()

        with open(tmp_path, "rb") as f_in, gzip.open(final_path, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        tmp_path.unlink(missing_ok=True)

        rotate_backups()
        size = final_path.stat().st_size
        log.info("backup written: %s (%.1f KB)", final_path.name, size / 1024)
        return final_path
    except Exception:
        log.exception("backup failed")
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        return None


def rotate_backups() -> None:
    files = sorted(BACKUP_DIR.glob("ttn_data_*.db.gz"), key=lambda p: p.stat().st_mtime)
    excess = len(files) - BACKUP_KEEP
    for old in files[: max(0, excess)]:
        try:
            old.unlink()
            log.info("rotated old backup: %s", old.name)
        except Exception:
            log.exception("failed to delete old backup %s", old)


def backup_loop():
    if _stop_event.wait(60):
        return
    backup_db_once()
    while not _stop_event.is_set():
        if _stop_event.wait(BACKUP_INTERVAL_SECONDS):
            return
        backup_db_once()


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

VALID_PERIODS = {"day", "week", "month", "all"}
METRICS = {"temperature", "humidity", "co2", "air_pressure", "bat_v"}


def period_since(period: str) -> str | None:
    now = datetime.now(timezone.utc)
    if period == "day":
        since = now - timedelta(days=1)
    elif period == "week":
        since = now - timedelta(days=7)
    elif period == "month":
        since = now - timedelta(days=30)
    else:
        return None
    return since.isoformat().replace("+00:00", "Z")


def auto_bucket(period: str) -> str:
    if period == "day":
        return "raw"
    if period == "week":
        return "hour"
    return "day"


def time_window(args) -> tuple[str | None, str | None, str]:
    """Resolve a query window from either a rolling period or an explicit range.

    Returns (since_iso, until_iso, span_label) where span_label drives bucketing.
    """
    frm = args.get("from")
    to = args.get("to")
    if frm or to:
        return frm, to, "range"
    period = args.get("period", "day")
    if period not in VALID_PERIODS:
        period = "day"
    return period_since(period), None, period


def range_bucket(since: str | None, until: str | None) -> str:
    try:
        a = datetime.fromisoformat((since or "").replace("Z", "+00:00"))
        b = datetime.fromisoformat((until or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
        days = (b - a).total_seconds() / 86400
    except Exception:
        return "hour"
    if days <= 2:
        return "raw"
    if days <= 21:
        return "hour"
    return "day"


def scope_clauses(args) -> tuple[list[str], list]:
    """WHERE fragments (on alias m, joined to devices dev) for university/device scoping."""
    clauses, params = [], []
    dev = args.get("device")
    uni = args.get("university")
    if dev:
        clauses.append("m.device_id = ?")
        params.append(dev)
    if uni:
        clauses.append("dev.university_id = (SELECT id FROM universities WHERE slug = ?)")
        params.append(uni)
    return clauses, params


def build_where(args, since: str | None = None, until: str | None = None) -> tuple[str, list]:
    clauses, params = scope_clauses(args)
    if since:
        clauses.append("m.received_at >= ?")
        params.append(since)
    if until:
        clauses.append("m.received_at <= ?")
        params.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


# ---------------------------------------------------------------------------
# Climate normals / anomaly algorithm
# ---------------------------------------------------------------------------

def country_for_scope(conn: sqlite3.Connection, args) -> tuple[str | None, dict | None]:
    uni = args.get("university")
    dev = args.get("device")
    row = None
    if uni:
        row = conn.execute("SELECT * FROM universities WHERE slug = ?", (uni,)).fetchone()
    elif dev:
        row = conn.execute(
            "SELECT u.* FROM devices d JOIN universities u ON u.id = d.university_id WHERE d.device_id = ?",
            (dev,),
        ).fetchone()
    if row is None:
        return None, None
    return row["country_code"], dict(row)


def normal_for(country_code: str | None, month_index: int) -> tuple[float | None, float]:
    """Return (monthly mean temperature, sigma) for a country/month (0=Jan)."""
    default_sigma = float(NORMALS.get("default_sigma", 3.0))
    c = (NORMALS.get("countries") or {}).get(country_code or "")
    if not c:
        return None, default_sigma
    temp = c.get("temp") or []
    sig = c.get("sigma") or []
    t = temp[month_index] if 0 <= month_index < len(temp) else None
    s = sig[month_index] if 0 <= month_index < len(sig) else default_sigma
    return t, float(s)


def classify_z(z: float) -> tuple[str, str]:
    th = NORMALS.get("thresholds", {})
    zm = float(th.get("z_moderate", 1.0))
    zs = float(th.get("z_strong", 2.0))
    a = abs(z)
    if a < zm:
        level = "normal"
    elif a < zs:
        level = "moderate"
    else:
        level = "strong"
    direction = "warmer" if z > 0 else ("cooler" if z < 0 else "neutral")
    return level, direction


def compute_anomaly(conn: sqlite3.Connection, args) -> dict:
    country_code, uni = country_for_scope(conn, args)
    since, until, _span = time_window(args)
    where, params = build_where(args, since, until)

    rows = conn.execute(
        f"""
        SELECT strftime('%Y-%m-%d', m.received_at) AS day,
               AVG(d.temperature) AS observed,
               COUNT(*)           AS samples
        FROM messages m
        LEFT JOIN decoded_payloads d ON d.message_id = m.id
        LEFT JOIN devices dev ON dev.device_id = m.device_id
        {where}
        GROUP BY day
        HAVING observed IS NOT NULL
        ORDER BY day ASC
        """,
        params,
    ).fetchall()

    daily = []
    diffs = []
    for r in rows:
        day = r["day"]
        try:
            month_idx = int(day[5:7]) - 1
        except Exception:
            continue
        normal, sigma = normal_for(country_code, month_idx)
        observed = r["observed"]
        if normal is None or observed is None:
            anomaly = z = None
        else:
            anomaly = observed - normal
            z = anomaly / sigma if sigma else None
            diffs.append((anomaly, z))
        daily.append({
            "date": day, "observed": observed, "normal": normal,
            "anomaly": anomaly, "z": z, "samples": r["samples"],
        })

    summary = {"available": False}
    if diffs:
        anomaly_mean = sum(a for a, _ in diffs) / len(diffs)
        z_mean = sum(z for _, z in diffs if z is not None) / len(diffs)
        level, direction = classify_z(z_mean)
        observed_mean = sum(d["observed"] for d in daily if d["observed"] is not None) / \
            max(1, len([d for d in daily if d["observed"] is not None]))
        normal_mean = sum(d["normal"] for d in daily if d["normal"] is not None) / \
            max(1, len([d for d in daily if d["normal"] is not None]))
        n_strong = sum(1 for d in daily if d["z"] is not None and abs(d["z"]) >= 2.0)
        summary = {
            "available": True,
            "observed_mean": round(observed_mean, 2),
            "normal_mean": round(normal_mean, 2),
            "anomaly_mean": round(anomaly_mean, 2),
            "z_mean": round(z_mean, 2),
            "level": level,
            "direction": direction,
            "warming": bool(anomaly_mean > 0 and abs(z_mean) >= 1.0),
            "days_total": len(daily),
            "days_strong": n_strong,
        }

    return {
        "country_code": country_code,
        "country_name": (NORMALS.get("countries", {}).get(country_code or "", {}) or {}).get("name"),
        "university": uni.get("name") if uni else None,
        "source": NORMALS.get("source"),
        "thresholds": NORMALS.get("thresholds"),
        "daily": daily,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/universities")
def api_universities():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.slug, u.name, u.city, u.country, u.country_code,
                   u.latitude, u.longitude, u.timezone, u.is_demo,
                   COUNT(DISTINCT dev.device_id) AS device_count,
                   COUNT(msg.id)                 AS message_count,
                   MAX(msg.received_at)          AS last_seen
            FROM universities u
            LEFT JOIN devices dev ON dev.university_id = u.id
            LEFT JOIN messages msg ON msg.device_id = dev.device_id
            GROUP BY u.id
            ORDER BY u.is_demo ASC, u.name ASC
            """
        ).fetchall()
        unis = []
        for u in rows:
            d = dict(u)
            devs = conn.execute(
                """
                SELECT dev.device_id, dev.label, dev.latitude, dev.longitude,
                       COUNT(m.id) AS message_count, MAX(m.received_at) AS last_seen
                FROM devices dev
                LEFT JOIN messages m ON m.device_id = dev.device_id
                WHERE dev.university_id = ?
                GROUP BY dev.device_id
                ORDER BY dev.device_id
                """,
                (u["id"],),
            ).fetchall()
            d["devices"] = [dict(x) for x in devs]
            unis.append(d)
        return jsonify(unis)


@app.route("/api/latest")
def api_latest():
    where, params = build_where(request.args)
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT m.id, m.device_id, m.received_at, m.f_cnt,
                   dev.university_id, u.slug AS university_slug, u.name AS university_name,
                   d.bat_v, d.co2, d.temperature, d.humidity, d.air_pressure,
                   d.co2h_flag, d.co2l_flag, d.temph_flag, d.templ_flag, d.node_type,
                   bp.value AS battery_percentage,
                   (SELECT rssi FROM rx_metadata r WHERE r.message_id = m.id ORDER BY rssi DESC LIMIT 1) AS rssi,
                   (SELECT snr  FROM rx_metadata r WHERE r.message_id = m.id ORDER BY snr  DESC LIMIT 1) AS snr
            FROM messages m
            LEFT JOIN decoded_payloads d ON d.message_id = m.id
            LEFT JOIN battery_percentage bp ON bp.message_id = m.id
            LEFT JOIN devices dev ON dev.device_id = m.device_id
            LEFT JOIN universities u ON u.id = dev.university_id
            {where}
            ORDER BY m.id DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return jsonify(dict(row) if row else {})


@app.route("/api/series")
def api_series():
    since, until, span = time_window(request.args)
    bucket = request.args.get("bucket", "auto")
    if bucket == "auto":
        bucket = range_bucket(since, until) if span == "range" else auto_bucket(span)
    if bucket not in {"raw", "hour", "day"}:
        return jsonify({"error": "invalid bucket"}), 400

    where, params = build_where(request.args, since, until)

    if bucket == "raw":
        sql = f"""
            SELECT m.received_at AS bucket,
                   d.co2, d.temperature, d.humidity, d.air_pressure, d.bat_v,
                   bp.value AS battery_percentage
            FROM messages m
            LEFT JOIN decoded_payloads d ON d.message_id = m.id
            LEFT JOIN battery_percentage bp ON bp.message_id = m.id
            LEFT JOIN devices dev ON dev.device_id = m.device_id
            {where}
            ORDER BY m.received_at ASC
            LIMIT 6000
        """
    else:
        fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d"
        sql = f"""
            SELECT strftime(?, m.received_at) AS bucket,
                   AVG(d.co2)           AS co2,
                   AVG(d.temperature)   AS temperature,
                   AVG(d.humidity)      AS humidity,
                   AVG(d.air_pressure)  AS air_pressure,
                   AVG(d.bat_v)         AS bat_v,
                   AVG(bp.value)        AS battery_percentage,
                   COUNT(*)             AS samples
            FROM messages m
            LEFT JOIN decoded_payloads d ON d.message_id = m.id
            LEFT JOIN battery_percentage bp ON bp.message_id = m.id
            LEFT JOIN devices dev ON dev.device_id = m.device_id
            {where}
            GROUP BY bucket
            ORDER BY bucket ASC
        """
        params = [fmt] + params

    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return jsonify({"period": span, "bucket": bucket, "points": [dict(r) for r in rows]})


@app.route("/api/heatmap")
def api_heatmap():
    metric = request.args.get("metric", "temperature")
    if metric not in METRICS:
        return jsonify({"error": "invalid metric"}), 400
    since, until, _span = time_window(request.args)
    where, params = build_where(request.args, since, until)
    sql = f"""
        SELECT strftime('%Y-%m-%d', m.received_at)        AS day,
               CAST(strftime('%H', m.received_at) AS INT)  AS hour,
               AVG(d.{metric})                             AS value,
               COUNT(*)                                    AS samples
        FROM messages m
        LEFT JOIN decoded_payloads d ON d.message_id = m.id
        LEFT JOIN devices dev ON dev.device_id = m.device_id
        {where}
        GROUP BY day, hour
        HAVING value IS NOT NULL
        ORDER BY day ASC, hour ASC
    """
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
        cells = [dict(r) for r in rows]
        vals = [c["value"] for c in cells if c["value"] is not None]
        return jsonify({
            "metric": metric,
            "cells": cells,
            "min": min(vals) if vals else None,
            "max": max(vals) if vals else None,
        })


@app.route("/api/anomaly")
def api_anomaly():
    with get_db() as conn:
        return jsonify(compute_anomaly(conn, request.args))


@app.route("/api/messages")
def api_messages():
    since, until, _span = time_window(request.args)
    limit = min(int(request.args.get("limit", 100)), 1000)
    where, params = build_where(request.args, since, until)
    params = params + [limit]
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT m.id, m.device_id, m.received_at, m.f_cnt, m.f_port,
                   d.co2, d.temperature, d.humidity, d.air_pressure, d.bat_v,
                   (SELECT rssi FROM rx_metadata r WHERE r.message_id = m.id ORDER BY rssi DESC LIMIT 1) AS rssi,
                   (SELECT snr  FROM rx_metadata r WHERE r.message_id = m.id ORDER BY snr  DESC LIMIT 1) AS snr
            FROM messages m
            LEFT JOIN decoded_payloads d ON d.message_id = m.id
            LEFT JOIN devices dev ON dev.device_id = m.device_id
            {where}
            ORDER BY m.id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def api_stats():
    since, until, span = time_window(request.args)
    where, params = build_where(request.args, since, until)
    with get_db() as conn:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS messages,
                   MIN(m.received_at) AS first,
                   MAX(m.received_at) AS last,
                   AVG(d.co2)         AS avg_co2,
                   MIN(d.co2)         AS min_co2,
                   MAX(d.co2)         AS max_co2,
                   AVG(d.temperature) AS avg_temp,
                   MIN(d.temperature) AS min_temp,
                   MAX(d.temperature) AS max_temp,
                   AVG(d.humidity)    AS avg_hum,
                   MIN(d.humidity)    AS min_hum,
                   MAX(d.humidity)    AS max_hum,
                   AVG(d.air_pressure) AS avg_pres
            FROM messages m
            LEFT JOIN decoded_payloads d ON d.message_id = m.id
            LEFT JOIN devices dev ON dev.device_id = m.device_id
            {where}
            """,
            params,
        ).fetchone()
        return jsonify({"period": span, **dict(row)})


@app.route("/api/health")
def api_health():
    with _mqtt_state_lock:
        feeds = json.loads(json.dumps(_mqtt_state["feeds"]))
    total_msgs = sum(f.get("messages_received", 0) for f in feeds.values())
    any_connected = any(f.get("connected") for f in feeds.values())

    backups = sorted(BACKUP_DIR.glob("ttn_data_*.db.gz"), key=lambda p: p.stat().st_mtime)
    latest_backup = backups[-1].name if backups else None
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    return jsonify({
        "ok": any_connected or total_msgs > 0,
        "feeds": feeds,
        "messages_received": total_msgs,
        "db_path": str(DB_PATH),
        "db_size_bytes": db_size,
        "backups_count": len(backups),
        "latest_backup": latest_backup,
        "backup_dir": str(BACKUP_DIR),
    })


@app.route("/api/backup", methods=["POST"])
def api_backup():
    path = backup_db_once()
    if not path:
        return jsonify({"ok": False}), 500
    return jsonify({"ok": True, "file": path.name, "size": path.stat().st_size})


@app.route("/api/stream")
def api_stream():
    q: queue.Queue = queue.Queue(maxsize=64)
    with _subscribers_lock:
        _subscribers.append(q)

    def gen():
        try:
            yield "retry: 3000\n\n"
            while not _stop_event.is_set():
                try:
                    data = q.get(timeout=15)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": keep-alive\n\n"
        finally:
            with _subscribers_lock:
                if q in _subscribers:
                    _subscribers.remove(q)

    return Response(gen(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    slug_to_id = init_db()

    feed_threads = start_feeds(slug_to_id)
    backup_thread = threading.Thread(target=backup_loop, name="backup", daemon=True)
    backup_thread.start()

    log.info("starting web server on %s:%s (threads=%d)", WEB_HOST, WEB_PORT, WEB_THREADS)
    from waitress import create_server
    server = create_server(
        app,
        host=WEB_HOST,
        port=WEB_PORT,
        threads=WEB_THREADS,
        ident="sae-veille",
        channel_timeout=120,
    )
    web_thread = threading.Thread(target=server.run, name="web", daemon=True)
    web_thread.start()

    _stop_event.wait()

    log.info("closing listener...")
    try:
        server.close()
    except Exception:
        log.exception("server.close() failed")
    log.info("shutdown complete")


if __name__ == "__main__":
    main()
