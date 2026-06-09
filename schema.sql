PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS universities (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  slug         TEXT UNIQUE NOT NULL,
  name         TEXT NOT NULL,
  city         TEXT,
  country      TEXT,
  country_code TEXT,
  latitude     REAL,
  longitude    REAL,
  timezone     TEXT,
  is_demo      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT
);

CREATE TABLE IF NOT EXISTS devices (
  device_id      TEXT PRIMARY KEY,
  university_id  INTEGER,
  label          TEXT,
  latitude       REAL,
  longitude      REAL,
  application_id TEXT,
  dev_eui        TEXT,
  join_eui       TEXT,
  dev_addr       TEXT,
  first_seen     TEXT,
  last_seen      TEXT,
  FOREIGN KEY (university_id) REFERENCES universities(id)
);

CREATE TABLE IF NOT EXISTS messages (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id          TEXT NOT NULL,
  received_at        TEXT NOT NULL,
  correlation_ids    TEXT,
  session_key_id     TEXT,
  f_port             INTEGER,
  f_cnt              INTEGER,
  frm_payload        TEXT,
  consumed_airtime   TEXT,
  uplink_received_at TEXT,
  raw_json           TEXT NOT NULL,
  FOREIGN KEY (device_id) REFERENCES devices(device_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_device_received
  ON messages(device_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_received
  ON messages(received_at);

CREATE TABLE IF NOT EXISTS decoded_payloads (
  message_id    INTEGER PRIMARY KEY,
  bat_v         REAL,
  co2h_flag     TEXT,
  co2l_flag     TEXT,
  node_type     TEXT,
  temph_flag    TEXT,
  templ_flag    TEXT,
  air_pressure  REAL,
  co2           INTEGER,
  humidity      REAL,
  temperature   REAL,
  FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rx_metadata (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id        INTEGER NOT NULL,
  gateway_id        TEXT,
  gateway_eui       TEXT,
  time              TEXT,
  timestamp         INTEGER,
  rssi              INTEGER,
  channel_rssi      INTEGER,
  snr               REAL,
  frequency_offset  TEXT,
  uplink_token      TEXT,
  channel_index     INTEGER,
  received_at       TEXT,
  FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_rx_message ON rx_metadata(message_id);

CREATE TABLE IF NOT EXISTS settings (
  message_id        INTEGER PRIMARY KEY,
  bandwidth         INTEGER,
  spreading_factor  INTEGER,
  coding_rate       TEXT,
  frequency         TEXT,
  timestamp         INTEGER,
  time              TEXT,
  FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS network_ids (
  message_id      INTEGER PRIMARY KEY,
  net_id          TEXT,
  ns_id           TEXT,
  tenant_id       TEXT,
  cluster_id      TEXT,
  cluster_address TEXT,
  FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS battery_percentage (
  message_id   INTEGER PRIMARY KEY,
  value        REAL,
  received_at  TEXT,
  FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
);
