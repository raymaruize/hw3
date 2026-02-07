# HW3: Explore an API — PRT Leave Alert

This repo contains my CMU 15-113 HW3 project: **PRT Leave Alert / PRT Alert**.

The app calls a public transit API (Pittsburgh Regional Transit / Port Authority **TrueTime BusTime API**) to fetch **real-time arrival predictions** for route **61** at stop **7117** (Forbes Ave + Morewood, Carnegie Mellon). It computes a recommended **Leave by** time (bus ETA minus a configurable safety buffer), shows a mobile-friendly live UI with countdowns, and can send Telegram reminders / answer simple Telegram queries.

## What API is being called? (3–5 sentences)

This app makes HTTP **GET** requests to the PRT TrueTime **BusTime API v3** endpoint `bustime/api/v3/getpredictions`. The request includes an API key (`key`) plus parameters like the stop id (`stpid=7117`), a required feed name for PRT’s multi-feed system (`rtpidatafeed=Port Authority Bus`), and `format=json`. The API returns JSON containing a list of prediction objects (route, stop, and predicted arrival time / countdown). For route **61**, the app fetches all predictions for the stop and filters client-side by the route prefix **61** to match 61A/61B/61C/61D.

## Where is the code?

- Main app (Flask): `prt-leave-alert/`
  - `prt-leave-alert/app.py`
  - `prt-leave-alert/templates/index.html`
  - `prt-leave-alert/prt_bustime.py`
  - `prt-leave-alert/notifier.py`
- Prompt log: `prt-leave-alert/PROMPT_HISTORY.txt`

## Running locally

```bash
cd prt-leave-alert
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json

# Use a local .env or export env vars (do not commit secrets)
export PRT_BUSTIME_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."

python app.py
```

Open:
- http://127.0.0.1:5001

## Deployment

**Live (Render):** https://hw3-wccq.onrender.com

The project can be deployed as a Python web service (e.g., Render) using:

- Start command:
  - `gunicorn app:app --bind 0.0.0.0:$PORT`
- Environment variables:
  - `PRT_BUSTIME_API_KEY`
  - `TELEGRAM_BOT_TOKEN`

Notes:
- The Render *service name* can be changed (mine is currently **catchthebusorfreeze**), but the default `*.onrender.com` subdomain is not always editable after creation. If you want a prettier URL, use a **custom domain** or create a new Render service with the desired subdomain.

## Privacy / Security

- No API keys are committed. Secrets are read from environment variables / `.env`.
- Local config/secrets files are ignored via `.gitignore`.
