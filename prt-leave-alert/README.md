# PRT Leave Alert (CMU 15-113 HW3)

This project calls a public transit API (Pittsburgh Regional Transit / Port Authority “TrueTime” BusTime API) to fetch **real-time arrival predictions** for route **61** at stop **7117** (Forbes Ave + Morewood, Carnegie Mellon). It then computes **when you should leave** Tepper based on a configurable buffer (default 6 minutes), and sends reminders to your phone (Telegram).

## What API is being called?

- Endpoint style: HTTP GET (REST-like)
- API: BusTime API v3 (`bustime/api/v3/getpredictions`)
- Key parameters:
  - `key` (API key via environment variable, **never committed**)
  - `stpid` (stop id, e.g. `7117`)
  - `rtpidatafeed` (PRT is multi-feed; use `Port Authority Bus`)
  - `rt` (route; for 61 we fetch stop predictions and filter by prefix 61A/61B/61C/61D)
  - `format=json`
- Response format: JSON containing a list of predictions (each includes route, stop id, and predicted arrival time / countdown).

## Setup

> Important: do **not** paste API keys into your README, prompt log, screenshots, or commit history. Use environment variables or a local `.env` file (ignored by git).

1. Create a virtualenv and install deps:

```bash
cd prt-leave-alert
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create your config:

```bash
cp config.example.json config.json
```

3. Set secrets as environment variables (do not commit):

```bash
export PRT_BUSTIME_API_KEY="..."
export TELEGRAM_BOT_TOKEN="..."
```

4. Run:

```bash
python app.py
```

Open http://127.0.0.1:5000 to edit the leave buffer and see the next buses.

## Files

- `app.py` – Flask UI + scheduler loop
- `prt_bustime.py` – API client + prediction parsing
- `notifier.py` – Telegram notification sender
- `config.json` – local settings (ignored)
- `PROMPT_HISTORY.txt` – key AI prompts used

## Privacy / security

- No API keys are committed. Keys are read from environment variables.
- `config.json` is ignored; commit `config.example.json` instead.
