# AirGradient Outdoor Air Quality Dashboard

A Dash website that shows live and historical readings from several
AirGradient outdoor monitors, and compares them side by side.

## Features

- Pick which monitors to show, and any date range that has data
  (quick ranges: 24 h, 7 days, 30 days, 90 days, 1 year, all).
- Latest reading per monitor, with its US EPA PM2.5 category and an
  online/offline badge.
- Charts for PM2.5, PM10, CO₂, temperature, humidity, TVOC and NOx:
  - line chart over time, with a zoom slider
  - average per monitor (bar chart)
  - hourly / daily / monthly averages, monitors side by side (bar chart,
    with the WHO 24-hour guideline for PM2.5 and PM10)
  - average by hour of day
  - how closely the monitors agree (correlation heatmap)
- Summary statistics table, readings table, and CSV / Excel downloads.

## How the data flows

```
monitors ──► AirGradient cloud ──► collector.py ──► SQLite file ──► app.py (website)
```

The project runs as **two separate processes**:

- **`collector.py`** is the only part that talks to AirGradient. Every
  `REFRESH_SECONDS` it fetches the latest reading of every monitor. On
  start-up it downloads each monitor's history (10 days per request, newest
  first) and fills in anything missed while it was off. Progress is saved,
  so restarts continue where they stopped. Run exactly one collector per
  database.
- **`app.py`** is the website. It only reads the database, so it can be
  restarted or redeployed without interrupting data collection. The header
  shows "collector not running" if the collector has stopped.
- **`core.py`** holds what both share: settings, the parameter list
  (`METRICS`) and the database code.

Readings are stored in a local SQLite database (`data/airgradient.sqlite`,
table `measurements`, one row per monitor per reading).

| File | What to edit there |
|---|---|
| `app.py` | page layout, cards, charts, callbacks |
| `assets/style.css` | colours, spacing, card styling |
| `core.py` | parameters shown (`METRICS`), database schema |
| `collector.py` | how data is fetched from AirGradient |

## Setup

Requires Python 3.10+.

```bash
pip install -r requirements.txt
cp .env.example .env        # Windows PowerShell: Copy-Item .env.example .env
```

Edit `.env` and set your own `AIRGRADIENT_API_TOKEN` and `MONITOR_IDS`
(the location IDs of your monitors). Then start both processes, each in
its own terminal:

```bash
python collector.py     # terminal 1: collects data, keep it running
python app.py           # terminal 2: the website
```

and open http://localhost:8050.

## How the API key is protected

- **Server-side only.** The AirGradient token is read from the server's
  environment (`AIRGRADIENT_API_TOKEN`) by `collector.py` only; the website
  process does not need it at all. Only Python code on the server calls the
  AirGradient API. The browser never talks to
  AirGradient: it calls this app's own Dash callback endpoint
  (`/_dash-update-component`), which reads from the local database.
- **Nothing secret is sent to the browser.** The page, its layout and all
  callback responses contain neither the token nor the monitors' location
  IDs. The browser refers to monitors by neutral keys (`m1`, `m2`, …) that
  the server maps back to IDs.
- **Not in logs.** AirGradient only accepts the token as a `?token=` URL
  parameter, so error messages are reduced to the error type / HTTP status
  and the HTTP library's URL logging is switched off.
- **Not in git.** `.env` and `data/` are in `.gitignore`. Only
  `.env.example`, with placeholder values, is committed. Never put the real
  token in code, the README or any `NEXT_PUBLIC_*` / `VITE_*`-style public
  variable.
- **If the token leaks** (screenshot, chat, commit): create a new one in the
  AirGradient dashboard and update `.env` / your host's settings. Deleting a
  committed file does not remove it from git history.

## Deploying to a host

Do not upload `.env`. Set the same variables in the host's
environment-variable settings instead (e.g. Render → *Environment*,
Railway → *Variables*, Azure App Service → *Configuration*):

| Variable | Needed by | Secret? |
|---|---|---|
| `AIRGRADIENT_API_TOKEN` | collector | **yes** |
| `MONITOR_IDS` | both | yes (keep private) |
| `DATABASE_PATH`, `REFRESH_SECONDS`, `HISTORY_START` | both | no |
| `DISPLAY_UTC_OFFSET_HOURS`, `DISPLAY_TZ_LABEL` | website | no |

Run two services (e.g. two systemd units, or a web service plus a
background worker on Render / Railway), both pointing at the same
`DATABASE_PATH`:

```bash
python collector.py                                              # exactly one
gunicorn app:server --workers 2 --threads 4 --bind 0.0.0.0:$PORT   # the website
```

- Run **exactly one collector**. The website no longer collects anything,
  so it can use several gunicorn workers.
- Both services must see the same database file, so on a managed host they
  need a shared disk. Simplest: run both on the same server.
- Point `DATABASE_PATH` at a **persistent local disk** (for example
  `/var/data/airgradient.sqlite`), never a network share. Many hosts wipe
  the normal file system on every deploy, which would make the collector
  re-download all history.
