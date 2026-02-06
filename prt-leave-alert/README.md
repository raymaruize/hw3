# PRT Leave Alert (CMU 15-113 HW3)

This project calls a public transit API (Pittsburgh Regional Transit / Port Authority **TrueTime BusTime API**) to fetch **real-time arrival predictions** for route **61** at stop **7117** (Forbes Ave + Morewood, Carnegie Mellon). It computes **when you should leave** based on a configurable buffer/safety margin, shows a mobile-friendly live UI (with countdowns), and can send Telegram reminders / answer simple Telegram queries.

## What API is being called? (3–5 sentences)

This app makes HTTP **GET** requests to the PRT TrueTime **BusTime API v3** endpoint `bustime/api/v3/getpredictions`. The request includes an API key (`key`) plus parameters like the stop id (`stpid=7117`), a required feed name for PRT’s multi-feed system (`rtpidatafeed=Port Authority Bus`), and `format=json`. The API returns JSON containing a list of prediction objects (route, stop, and predicted arrival time / countdown). For route **61**, the app fetches all predictions for the stop and filters client-side by the route prefix **61** to match 61A/61B/61C/61D.

## Features / Interactivity

- **Responsive web UI** (mobile-first) showing the next arrivals, computed “leave at” times, and a **live countdown** (tabular numbers to avoid jitter).
- **Best pick** highlighting + “Missed” handling:
  - hides buses missed “too long ago”
  - can show a recently missed bus as a lower-priority item
- **Telegram integration**:
  - scheduled reminders (when enabled)
  - optional command: send the bot “next bus” and it replies with ETA + when to leave

## Setup

> Important: **do not commit API keys or tokens**. Use environment variables or a local `.env` file (ignored by git).

1) Create a virtualenv and install deps:

```bash
cd prt-leave-alert
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2) Create your local config:

```bash
cp config.example.json config.json
```

3) Create a local `.env` (recommended) or export env vars:

```bash
# Option A: put these in prt-leave-alert/.env
PRT_BUSTIME_API_KEY="..."
TELEGRAM_BOT_TOKEN="..."

# Option B: export in your shell
export PRT_BUSTIME_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
```

4) Run locally:

```bash
python app.py
```

Open:
- **http://127.0.0.1:5001**

## How to view on your phone (same Wi‑Fi)

By default, `127.0.0.1` only works on the same computer. To view on your phone, run the server so it listens on your LAN address.

**Option 1 (quickest):** temporarily change the Flask run command to bind `0.0.0.0` (all interfaces), then restart. Once it’s running, find your laptop’s LAN IP (e.g. `192.168.1.23`) and open on your phone:

- `http://192.168.1.23:5001`

**Important:** Only do this on a trusted network (your home Wi‑Fi). Don’t expose this directly to the public internet.

## Deployment / Hosting status

Currently this project is running as a **local dev server** on the developer machine (Flask debug server). It is **not deployed publicly** by default.

If you want others (not on your Wi‑Fi) to access it, you need to deploy it (for example: Render/Fly.io/Railway) or use a secure tunnel (ngrok/Cloudflare Tunnel). If you tell me which you prefer, I can help you set it up safely.

## Files

- `app.py` – Flask UI + scheduler loop + Telegram polling
- `prt_bustime.py` – API client + prediction parsing
- `notifier.py` – Telegram notification sender
- `config.json` – local settings (ignored)
- `.env` – local secrets (ignored)
- `config.example.json` – example config (committed)
- `PROMPT_HISTORY.txt` – key AI prompts used

## Privacy / Security

- No API keys are committed. Keys are read from environment variables / `.env`.
- `config.json` and `.env` are ignored via `.gitignore`.
