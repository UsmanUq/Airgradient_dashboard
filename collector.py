"""
AirGradient collector: the only part of the project that talks to the
AirGradient API and writes to the database.

Run it as its own long-running process, next to the website:

    python collector.py

It stores the latest reading of every monitor every REFRESH_SECONDS, and
in the background downloads each monitor's history (10 days per request,
newest first), filling in anything missed while it was not running.
Run exactly one collector per database.
"""

import os
import time
import logging
import threading
from contextlib import closing
from datetime import datetime, timezone, timedelta

import requests

from core import (
    MONITOR_IDS, REFRESH_SECONDS, HISTORY_START, METRICS, COLS,
    connect, init_database, query, utc_str, parse_utc, save_rows,
    write_status,
)


log = logging.getLogger("collector")
# AirGradient only accepts the token as a ?token= URL parameter. Requests
# go server-to-server over HTTPS; keep the HTTP library from ever logging
# those URLs, even if someone turns on debug logging.
logging.getLogger("urllib3").setLevel(logging.WARNING)

API_BASE = os.getenv(
    "AIRGRADIENT_BASE_URL",
    "https://api.airgradient.com/public/api/v1",
).rstrip("/")
API_TOKEN = os.getenv("AIRGRADIENT_API_TOKEN", "").strip()
AUTH_MODE = os.getenv("AIRGRADIENT_AUTH_MODE", "query").lower()
AUTH_HEADER = os.getenv("AIRGRADIENT_AUTH_HEADER", "X-API-Key")

BACKFILL_WINDOW_DAYS = 10   # the API's maximum range per history request


# ============================================================
# AIRGRADIENT API
# ============================================================

def api_get(path, **params):
    """GET an API path, retrying on errors. Never puts the token in logs."""
    headers = {"Accept": "application/json"}
    if AUTH_MODE == "header":
        headers[AUTH_HEADER] = API_TOKEN
    else:
        params["token"] = API_TOKEN

    problem = "unknown error"
    for attempt in range(4):
        try:
            response = requests.get(
                f"{API_BASE}{path}", params=params,
                headers=headers, timeout=60,
            )
            if response.status_code == 429:
                problem = "rate limited (429)"
                time.sleep(30 * (attempt + 1))
                continue
            if response.status_code >= 400:
                problem = f"HTTP {response.status_code}"
                if response.status_code < 500:
                    break           # a client error will not fix itself
                time.sleep(5 * (attempt + 1))
                continue
            return response.json()
        except requests.RequestException as error:
            problem = type(error).__name__
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"AirGradient API request failed: {problem}")


def api_time(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def first_number(measure, keys):
    for key in keys:
        value = measure.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def to_row(location_id, measure):
    stamp = measure.get("timestamp")
    if not stamp:
        return None
    try:
        ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (
        int(location_id),
        utc_str(ts),
        *[first_number(measure, METRICS[c]["keys"]) for c in COLS],
    )


def fetch_past(location_id, start, end):
    data = api_get(
        f"/locations/{location_id}/measures/past",
        **{"from": api_time(start), "to": api_time(end)},
    )
    if not isinstance(data, list):
        return []
    return [r for r in (to_row(location_id, m) for m in data) if r]


# ============================================================
# LIVE READINGS
# ============================================================

def poll_current():
    """Store the latest reading of every monitor (one API request)."""
    data = api_get("/locations/measures/current")
    rows, monitors = [], []
    for measure in data if isinstance(data, list) else []:
        location_id = measure.get("locationId")
        if location_id not in MONITOR_IDS:
            continue
        row = to_row(location_id, measure)
        if row:
            rows.append(row)
        monitors.append((
            int(location_id),
            measure.get("locationName") or f"Monitor {location_id}",
            measure.get("model"),
            row[1] if row else None,
        ))
    save_rows(rows)
    with closing(connect()) as con, con:
        con.executemany("""
            INSERT INTO monitors (location_id, name, model, last_seen)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(location_id) DO UPDATE SET
                name = excluded.name,
                model = excluded.model,
                last_seen = COALESCE(excluded.last_seen, monitors.last_seen)
        """, monitors)


def live_loop():
    while True:
        try:
            poll_current()
            write_status(ok=True)
        except Exception as error:     # keep collecting whatever happens
            log.warning("Live poll failed: %s", error)
            try:
                write_status(ok=False, error=str(error))
            except Exception as db_error:
                log.warning("Could not record status: %s", db_error)
        time.sleep(REFRESH_SECONDS)


# ============================================================
# HISTORY BACKFILL
# ============================================================

def fill_gap_since_last_run():
    """Fetch readings missed while the collector was not running."""
    now = datetime.now(timezone.utc)
    started = query("SELECT location_id FROM backfill")
    for location_id in started["location_id"]:
        if int(location_id) not in MONITOR_IDS:
            continue
        latest = query(
            "SELECT MAX(ts) AS ts FROM measurements WHERE location_id = ?",
            [int(location_id)],
        )["ts"].iloc[0]
        if not latest:
            continue
        cursor = parse_utc(latest)
        while cursor < now - timedelta(minutes=5):
            window_end = min(cursor + timedelta(days=BACKFILL_WINDOW_DAYS), now)
            save_rows(fetch_past(int(location_id), cursor, window_end))
            cursor = window_end
            time.sleep(0.3)


def backfill_worker():
    """Walk back through each monitor's history, 10 days per request.

    Monitors take turns, so all of them get their recent history first.
    Progress is saved, so a restart continues where it stopped.
    """
    try:
        fill_gap_since_last_run()
    except Exception as error:
        log.warning("Gap fill failed: %s", error)

    now_text = utc_str(datetime.now(timezone.utc))
    with closing(connect()) as con, con:
        con.executemany(
            "INSERT OR IGNORE INTO backfill (location_id, oldest_done) "
            "VALUES (?, ?)",
            [(m, now_text) for m in MONITOR_IDS],
        )

    while True:
        pending = query(
            f"SELECT location_id, oldest_done FROM backfill "
            f"WHERE complete = 0 AND location_id IN "
            f"({','.join('?' * len(MONITOR_IDS))})",
            MONITOR_IDS,
        )
        if pending.empty:
            log.info("History backfill complete.")
            return

        for location_id, oldest in pending.itertuples(index=False):
            end = parse_utc(oldest)
            start = max(end - timedelta(days=BACKFILL_WINDOW_DAYS),
                        HISTORY_START)
            try:
                save_rows(fetch_past(int(location_id), start, end))
            except Exception as error:
                log.warning("Backfill %s failed: %s", location_id, error)
                time.sleep(60)
                continue
            with closing(connect()) as con, con:
                con.execute(
                    "UPDATE backfill SET oldest_done = ?, complete = ? "
                    "WHERE location_id = ?",
                    (utc_str(start), int(start <= HISTORY_START),
                     int(location_id)),
                )
            time.sleep(0.3)


# ============================================================
# START
# ============================================================

def main():
    if not API_TOKEN:
        raise SystemExit("AIRGRADIENT_API_TOKEN is missing from .env")
    init_database()
    log.info("Collector started: %d monitors, polling every %ds.",
             len(MONITOR_IDS), REFRESH_SECONDS)
    threading.Thread(target=backfill_worker, daemon=True,
                     name="backfill").start()
    try:
        live_loop()
    except KeyboardInterrupt:
        log.info("Collector stopped.")


if __name__ == "__main__":
    main()
