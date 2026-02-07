# PRT Leave Alert / PRT Alert (CMU 15-113 HW3)

This project calls a public transit API (Pittsburgh Regional Transit / Port Authority **TrueTime BusTime API**) to fetch **real-time arrival predictions** for route **61** at stop **7117** (Forbes Ave + Morewood, Carnegie Mellon). It computes a recommended **Leave by** time (bus ETA minus a configurable safety buffer), shows a mobile-friendly live UI with countdowns, and can send Telegram reminders / answer simple Telegram queries.

## What API is being called? (3–5 sentences)

This app makes HTTP **GET** requests to the PRT TrueTime **BusTime API v3** endpoint `bustime/api/v3/getpredictions`. The request includes an API key (`key`) plus parameters like the stop id (`stpid=7117`), a required feed name for PRT’s multi-feed system (`rtpidatafeed=Port Authority Bus`), and `format=json`. The API returns JSON containing a list of prediction objects (route, stop, and predicted arrival time / countdown). For route **61**, the app fetches all predictions for the stop and filters client-side by the route prefix **61** to match 61A/61B/61C/61D.

## Features / Interactivity

- **Responsive web UI** (mobile-first) with live countdowns (tabular numbers to avoid jitter).
- Shows **Bus ETA** (when the bus arrives at the stop) and **Leave by** (ETA minus safety buffer).
- **Best pick** highlighting + “Missed” handling:
  - hides buses missed “too long ago”
  - can show a recently missed bus as a lower-priority item
- **Telegram integration**:
  - scheduled reminders (when enabled)
  - optional command: send the bot “next bus” and it replies with ETA + when to leave

## Running locally

> Important: **do not commit API keys or tokens**. Use environment variables or a local `.env` file (ignored by git).

```bash
cd prt-leave-alert
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json

# put secrets in prt-leave-alert/.env OR export them in your shell
export PRT_BUSTIME_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."

python app.py
```

Open:
- **http://127.0.0.1:5001**

## Deployment (Render)

**Live URL:** https://catchthebus.onrender.com

This project is designed to be deployed as a Python web service.

- Start command (production):
  - `gunicorn app:app --bind 0.0.0.0:$PORT`
- Secrets to set as environment variables on the host:
  - `PRT_BUSTIME_API_KEY`
  - `TELEGRAM_BOT_TOKEN`

Notes:
- `config.json` is ignored by git. In cloud deploys (like Render), the app falls back to `config.example.json` if `config.json` is not present.

## Files

- `app.py` – Flask UI + scheduler loop + Telegram polling + `/api/next`
- `prt_bustime.py` – API client + prediction parsing
- `notifier.py` – Telegram notification sender
- `config.example.json` – example config (committed)
- `config.json` – local settings (ignored)
- `.env` – local secrets (ignored)
- `PROMPT_HISTORY.txt` – key AI prompts used

## Privacy / Security

- No API keys are committed. Keys are read from environment variables / `.env`.
- `config.json` and `.env` are ignored via `.gitignore`.
