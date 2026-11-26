"""
Shared by the website (app.py) and the collector (collector.py):
settings both need, the list of parameters, and the SQLite database.
"""

import os
import sqlite3
import logging
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

# The monitors, in display order. Order fixes each monitor's colour, so a
# monitor keeps its colour whatever is selected.
# Set in .env (never in code), e.g. MONITOR_IDS=111111,222222
MONITOR_IDS = [
    int(x) for x in os.getenv("MONITOR_IDS", "").split(",") if x.strip()
]
if not MONITOR_IDS:
    raise SystemExit(
        "MONITOR_IDS is not set. Copy .env.example to .env and fill in "
        "your AirGradient location IDs."
    )

REFRESH_SECONDS = int(os.getenv("REFRESH_SECONDS", "60"))

# How far back the history backfill looks.
HISTORY_START = datetime.fromisoformat(
    os.getenv("HISTORY_START", "2024-01-01")
).replace(tzinfo=timezone.utc)

DB_PATH = Path(os.getenv("DATABASE_PATH", "data/airgradient.sqlite"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ============================================================
# PARAMETERS
# ============================================================

# Column name -> how it is shown and which API fields feed it. The first
# field that has a value wins, so AirGradient's corrected values are used
# when present.
METRICS = {
    "pm25": dict(label="PM2.5", unit="µg/m³", dp=1, who=15,
                 keys=["pm02_corrected", "pm02"]),
    "pm10": dict(label="PM10", unit="µg/m³", dp=1, who=45,
                 keys=["pm10_corrected", "pm10"]),
    "co2": dict(label="CO₂", unit="ppm", dp=0,
                keys=["rco2_corrected", "rco2"]),
    "temp": dict(label="Temperature", unit="°C", dp=1,
                 keys=["atmp_corrected", "atmp"]),
    "rh": dict(label="Humidity", unit="%", dp=0,
               keys=["rhum_corrected", "rhum"]),
    "tvoc": dict(label="TVOC index", unit="index", dp=0,
                 keys=["tvocIndex"]),
    "nox": dict(label="NOx index", unit="index", dp=0,
                keys=["noxIndex"]),
}
COLS = list(METRICS)


# ============================================================
# DATABASE
# ============================================================

def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_database():
    metric_cols = ", ".join(f"{c} REAL" for c in COLS)
    with closing(connect()) as con, con:
        con.executescript(f"""
            CREATE TABLE IF NOT EXISTS measurements (
                location_id INTEGER NOT NULL,
                ts TEXT NOT NULL,            -- UTC, 'YYYY-MM-DD HH:MM:SS'
                {metric_cols},
                PRIMARY KEY (location_id, ts)
            ) WITHOUT ROWID;

            CREATE TABLE IF NOT EXISTS monitors (
                location_id INTEGER PRIMARY KEY,
                name TEXT,
                model TEXT,
                last_seen TEXT
            );

            -- Progress of the history backfill for each monitor.
            CREATE TABLE IF NOT EXISTS backfill (
                location_id INTEGER PRIMARY KEY,
                oldest_done TEXT NOT NULL,
                complete INTEGER NOT NULL DEFAULT 0
            );

            -- One row, written by the collector after every live poll,
            -- so the website can show whether collection is working.
            CREATE TABLE IF NOT EXISTS collector_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_poll TEXT,              -- UTC, last successful poll
                last_error TEXT,             -- NULL when the last poll worked
                heartbeat TEXT NOT NULL      -- UTC, last poll attempt
            );
        """)


def query(sql, params=()):
    with closing(connect()) as con:
        return pd.read_sql_query(sql, con, params=list(params))


def utc_str(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_utc(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )


def save_rows(rows):
    if not rows:
        return
    placeholders = ",".join("?" * (2 + len(COLS)))
    with closing(connect()) as con, con:
        con.executemany(
            f"INSERT OR REPLACE INTO measurements "
            f"(location_id, ts, {', '.join(COLS)}) "
            f"VALUES ({placeholders})",
            rows,
        )


def write_status(ok, error=None):
    """Record the result of a live poll (called by the collector)."""
    now = utc_str(datetime.now(timezone.utc))
    with closing(connect()) as con, con:
        con.execute("""
            INSERT INTO collector_status (id, last_poll, last_error, heartbeat)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_poll = COALESCE(excluded.last_poll,
                                     collector_status.last_poll),
                last_error = excluded.last_error,
                heartbeat = excluded.heartbeat
        """, (now if ok else None, error, now))


def read_status():
    """{'last_poll', 'last_error', 'heartbeat'} as UTC datetimes / text,
    or None if the collector has never run."""
    df = query("SELECT last_poll, last_error, heartbeat "
               "FROM collector_status WHERE id = 1")
    if df.empty:
        return None
    row = df.iloc[0]
    return {
        "last_poll": parse_utc(row["last_poll"]) if row["last_poll"] else None,
        "last_error": row["last_error"],
        "heartbeat": parse_utc(row["heartbeat"]),
    }
