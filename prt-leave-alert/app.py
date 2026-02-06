import json
import os
import datetime as dt
from pathlib import Path

import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from prt_bustime import next_arrivals, PRTBusTimeError
from notifier import send_telegram

APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
CONFIG_EXAMPLE_PATH = APP_DIR / "config.example.json"

load_dotenv()  # loads .env if present (not committed)

app = Flask(__name__)

state = {
    "last_sent": {},  # key -> timestamp
    "telegram_update_offset": None,  # used by getUpdates polling
}


def load_config() -> dict:
    """Load config.

    - Prefer local config.json (user-specific; ignored by git)
    - Fall back to config.example.json (safe defaults; committed)

    This allows cloud deploys (e.g., Render) to run without requiring a writable config.json.
    """
    path = CONFIG_PATH if CONFIG_PATH.exists() else CONFIG_EXAMPLE_PATH
    if not path.exists():
        raise RuntimeError("Missing config.json (copy from config.example.json)")
    return json.loads(path.read_text())


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def compute_leave_times(arrivals: list[dt.datetime], leave_buffer_minutes: int, extra_safety_seconds: int) -> list[dt.datetime]:
    buf = dt.timedelta(minutes=leave_buffer_minutes, seconds=extra_safety_seconds)
    return [a - buf for a in arrivals]


def fmt_delta(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 0:
        return f"{-seconds}s ago"
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m{s:02d}s"


def _parse_hhmm(s: str) -> dt.time:
    hh, mm = s.split(":")
    return dt.time(int(hh), int(mm))


def _day_ok(day: str, now: dt.datetime) -> bool:
    # Accept Mon/Tue/... in config
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return day in days and days[now.weekday()] == day


def in_monitor_window(now: dt.datetime, cfg: dict) -> bool:
    """Return True if reminders should run now.

    If `monitor.always_on` is true -> always.
    Else we check either:
      - `class_end_times` (MW / TTh)
      - or legacy `schedule` array
    and only run within `monitor.window_minutes` after the class end time.
    """
    monitor = cfg.get("monitor", {})
    if monitor.get("always_on", True):
        return True

    window_min = int(monitor.get("window_minutes", 10))

    # Preferred: compact MW/TTh config
    cet = cfg.get("class_end_times")
    if isinstance(cet, dict):
        weekday = now.weekday()  # Mon=0
        key = None
        if weekday in (0, 2):
            key = "MW"
        elif weekday in (1, 3):
            key = "TTh"
        else:
            key = None  # Fri/Sat/Sun: no class

        if key and key in cet:
            try:
                val = str(cet[key]).strip()
                if not val:
                    return False
                t = _parse_hhmm(val)
                start = dt.datetime.combine(now.date(), t)
                end = start + dt.timedelta(minutes=window_min)
                return start <= now <= end
            except Exception:
                pass

    # Fallback: schedule list
    schedule = cfg.get("schedule", [])
    for item in schedule:
        try:
            t = _parse_hhmm(item["time"])
        except Exception:
            continue
        days = item.get("days", [])
        if days and not any(_day_ok(d, now) for d in days):
            continue

        start = dt.datetime.combine(now.date(), t)
        end = start + dt.timedelta(minutes=window_min)
        if start <= now <= end:
            return True

    return False


def build_next_bus_reply(cfg: dict, now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now()

    stop_id = cfg["from_stop_id"]
    route = cfg.get("route")
    rtpidatafeed = cfg.get("rtpidatafeed")
    leave_buffer = int(cfg.get("leave_buffer_minutes", 6))
    extra_safety = int(cfg.get("extra_safety_seconds", 30))

    # Simple estimate: time on bus from from_stop -> to_stop (minutes)
    ride_min = int(cfg.get("ride_minutes_estimate", 14))

    try:
        arrivals = next_arrivals(stop_id=stop_id, route=route, now=now, rtpi_datafeed=rtpidatafeed)
    except PRTBusTimeError as e:
        return f"Error fetching arrivals: {e}"

    if not arrivals:
        return "No upcoming buses found for this stop right now."

    arrivals = arrivals[:2]
    leave_times = compute_leave_times(arrivals, leave_buffer, extra_safety)

    a = arrivals[0]
    leave_at = leave_times[0]
    secs_to_leave = (leave_at - now).total_seconds()

    eta_stop = a.strftime("%H:%M")
    leave_clock = leave_at.strftime("%H:%M:%S")
    leave_in = fmt_delta(secs_to_leave)

    eta_dest = (a + dt.timedelta(minutes=ride_min))
    total_to_dest = (eta_dest - now).total_seconds()

    lines = [
        f"Next bus (route {route})",
        f"Arrives at stop {stop_id}: {eta_stop}",
        f"Leave in: {leave_in} (leave at {leave_clock})",
        f"Est. ride time: {ride_min} min",
        f"Est. arrive destination: {eta_dest.strftime('%H:%M')} (in {fmt_delta(total_to_dest)})",
    ]

    if len(arrivals) > 1:
        wait = (arrivals[1] - a).total_seconds()
        if wait > 0:
            lines.append(f"If you miss it, next is ~{fmt_delta(wait)} later ({arrivals[1].strftime('%H:%M')}).")

    return "\n".join(lines)


def maybe_send_reminders():
    cfg = load_config()
    if not in_monitor_window(dt.datetime.now(), cfg):
        return

    stop_id = cfg["from_stop_id"]
    route = cfg.get("route")
    rtpidatafeed = cfg.get("rtpidatafeed")
    leave_buffer = int(cfg.get("leave_buffer_minutes", 6))
    extra_safety = int(cfg.get("extra_safety_seconds", 30))
    thresholds = [int(x) for x in cfg.get("remind_thresholds_seconds", [300, 60, 30, 0])]

    mode = cfg.get("notification", {}).get("mode", "telegram")
    chat_id = cfg.get("notification", {}).get("telegram_chat_id")
    if mode != "telegram" or not chat_id:
        return

    now = dt.datetime.now()

    try:
        arrivals = next_arrivals(stop_id=stop_id, route=route, now=now, rtpi_datafeed=rtpidatafeed)
    except PRTBusTimeError:
        return
    if not arrivals:
        return

    arrivals = arrivals[:2]
    leave_times = compute_leave_times(arrivals, leave_buffer, extra_safety)

    # Choose the soonest bus as primary
    primary_arrival = arrivals[0]
    primary_leave = leave_times[0]

    # Secondary bus (if miss primary)
    secondary_arrival = arrivals[1] if len(arrivals) > 1 else None

    for t in thresholds:
        # Trigger when we are within ~poll interval of crossing the threshold
        secs_to_leave = (primary_leave - now).total_seconds()
        if secs_to_leave <= t and secs_to_leave > t - int(cfg.get("poll_seconds", 20)) - 1:
            key = f"leave@{primary_arrival.isoformat()}@{t}"
            last = state["last_sent"].get(key)
            if last and (now - last).total_seconds() < 300:
                continue

            if t > 0:
                headline = f"Reminder: leave in {fmt_delta(secs_to_leave)} to catch 61"
            else:
                headline = "Leave now to catch 61"

            miss_wait = ""
            if secondary_arrival:
                wait = (secondary_arrival - primary_arrival).total_seconds()
                if wait > 0:
                    miss_wait = f"\nIf you miss this one, next is ~{fmt_delta(wait)} later."

            msg = (
                f"{headline}\n"
                f"Stop 7117 (Forbes + Morewood)\n"
                f"Bus ETA: {primary_arrival.strftime('%H:%M')}\n"
                f"Your leave buffer: {leave_buffer} min (+{extra_safety}s)\n"
                f"Target leave time: {primary_leave.strftime('%H:%M:%S')}"
                f"{miss_wait}"
            )
            try:
                send_telegram(chat_id=chat_id, text=msg)
                state["last_sent"][key] = now
            except Exception:
                pass


def poll_telegram_and_reply():
    """Poll Telegram getUpdates and reply to simple commands.

    This avoids needing a public webhook URL.
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return

    cfg = load_config()

    params: dict = {"timeout": 0}
    if state.get("telegram_update_offset") is not None:
        params["offset"] = state["telegram_update_offset"]

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return

    updates = data.get("result", []) if isinstance(data, dict) else []
    if not updates:
        return

    for u in updates:
        try:
            update_id = u.get("update_id")
            msg = u.get("message") or u.get("edited_message")
            if not msg:
                continue

            text = (msg.get("text") or "").strip()
            if not text:
                continue

            chat_id = (msg.get("chat") or {}).get("id")
            if not chat_id:
                continue

            norm = text.lower().strip()
            if norm in {"next bus", "nextbus", "/nextbus", "/next", "next"}:
                reply = build_next_bus_reply(cfg)
                try:
                    send_telegram(chat_id=str(chat_id), text=reply)
                except Exception:
                    pass

            # advance offset regardless so we don't re-handle the same message
            if isinstance(update_id, int):
                state["telegram_update_offset"] = update_id + 1
        except Exception:
            continue


@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    now = dt.datetime.now()

    info = {"error": None, "arrivals": [], "leave_times": []}
    try:
        arrivals = next_arrivals(cfg["from_stop_id"], cfg.get("route"), now=now, rtpi_datafeed=cfg.get("rtpidatafeed"))[:3]
        info["arrivals"] = arrivals
        info["leave_times"] = compute_leave_times(
            arrivals,
            int(cfg.get("leave_buffer_minutes", 6)),
            int(cfg.get("extra_safety_seconds", 30)),
        )
    except Exception as e:
        info["error"] = str(e)

    return render_template("index.html", cfg=cfg, info=info, now=now)


@app.route("/api/next", methods=["GET"])
def api_next():
    """CORS-friendly JSON endpoint for a static frontend (e.g., GitHub Pages)."""
    cfg = load_config()
    now = dt.datetime.now()

    try:
        arrivals = next_arrivals(cfg["from_stop_id"], cfg.get("route"), now=now, rtpi_datafeed=cfg.get("rtpidatafeed"))[:3]
        leave_times = compute_leave_times(
            arrivals,
            int(cfg.get("leave_buffer_minutes", 6)),
            int(cfg.get("extra_safety_seconds", 30)),
        )

        buf_min = float(cfg.get("leave_buffer_minutes", 6)) + float(cfg.get("extra_safety_seconds", 30)) / 60.0

        out = {
            "now": now.isoformat(),
            "stop_id": cfg.get("from_stop_id"),
            "route": cfg.get("route"),
            "buffer_minutes": buf_min,
            "arrivals": [
                {
                    "index": i + 1,
                    "bus_arrival_iso": a.isoformat(),
                    "leave_iso": lt.isoformat(),
                    "bus_arrival_hhmm": a.strftime("%H:%M"),
                    "leave_hhmm": lt.strftime("%H:%M"),
                }
                for i, (a, lt) in enumerate(zip(arrivals, leave_times))
            ],
        }

        resp = jsonify(out)
    except Exception as e:
        resp = jsonify({"error": str(e)})

    # Allow static frontends to call this endpoint.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/update", methods=["POST"])
def update():
    cfg = load_config()
    cfg["leave_buffer_minutes"] = int(request.form.get("leave_buffer_minutes", cfg.get("leave_buffer_minutes", 6)))
    cfg["extra_safety_seconds"] = int(request.form.get("extra_safety_seconds", cfg.get("extra_safety_seconds", 30)))
    cfg["poll_seconds"] = int(request.form.get("poll_seconds", cfg.get("poll_seconds", 20)))

    # class end times (MW / TTh)
    mw = request.form.get("mw_end_time")
    tth = request.form.get("tth_end_time")
    if mw or tth:
        cfg.setdefault("class_end_times", {})
        if mw:
            cfg["class_end_times"]["MW"] = mw
        if tth:
            cfg["class_end_times"]["TTh"] = tth

    # Optional: update telegram chat id from UI
    chat = request.form.get("telegram_chat_id")
    if chat:
        cfg.setdefault("notification", {})["telegram_chat_id"] = chat.strip()

    # Optional: monitor mode
    always_on = request.form.get("always_on") == "on"
    cfg.setdefault("monitor", {})["always_on"] = always_on
    cfg["monitor"]["window_minutes"] = int(request.form.get("window_minutes", cfg.get("monitor", {}).get("window_minutes", 10)))

    save_config(cfg)
    return redirect(url_for("index"))


if __name__ == "__main__":
    cfg = load_config()
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(maybe_send_reminders, "interval", seconds=int(cfg.get("poll_seconds", 20)))

    # Poll Telegram for simple queries ("next bus")
    sched.add_job(poll_telegram_and_reply, "interval", seconds=3)

    sched.start()
    app.run(debug=True, port=5001)
