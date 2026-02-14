import json
import os
import datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
from flask import Flask, render_template, request, redirect, url_for, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from prt_bustime import next_predictions, next_arrivals, PRTBusTimeError
from notifier import send_telegram

APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
CONFIG_EXAMPLE_PATH = APP_DIR / "config.example.json"

load_dotenv()  # loads .env if present (not committed)

app = Flask(__name__)

APP_TZ = ZoneInfo(os.getenv("PRT_TIMEZONE", "America/New_York"))

STATE_PATH = os.getenv("PRT_ALERT_STATE_PATH", "/tmp/prt_alert_state.json")

state = {
    "last_sent": {},  # key -> timestamp
    "telegram_update_offset": None,  # used by getUpdates polling
    "last_seen_arrival": None,  # datetime of last observed arrival (best-effort)
    "scheduler_started": False,
}


def _load_persisted_state() -> None:
    """Best-effort persisted state (prevents duplicate Telegram replies after restarts)."""
    try:
        p = Path(STATE_PATH)
        if not p.exists():
            return
        data = json.loads(p.read_text())
        if isinstance(data, dict) and "telegram_update_offset" in data:
            state["telegram_update_offset"] = data.get("telegram_update_offset")
    except Exception:
        return


def _save_persisted_state() -> None:
    try:
        p = Path(STATE_PATH)
        p.write_text(json.dumps({
            "telegram_update_offset": state.get("telegram_update_offset"),
        }))
    except Exception:
        return


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


def _fmt_miss_or_in(seconds: float) -> str:
    """Telegram-friendly countdown label."""
    if seconds >= 0:
        return fmt_delta(seconds)
    # Make it obvious we are late, not just "ago"
    return f"Missed by {fmt_delta(-seconds)}"


def build_next_bus_reply(cfg: dict, now: dt.datetime | None = None) -> str:
    """Build a 3-row reply based on 'leave_at' relative to `now`.

    - upcoming #1
    - upcoming #2
    - recently missed (<= 3 min) if available

    If missed is not within 3 minutes, we show only 2 rows.
    """
    now = now or dt.datetime.now(tz=APP_TZ)

    stop_id = cfg["from_stop_id"]
    route = cfg.get("route")
    rtpidatafeed = cfg.get("rtpidatafeed")
    leave_buffer = int(cfg.get("leave_buffer_minutes", 6))
    extra_safety = int(cfg.get("extra_safety_seconds", 30))

    # Simple estimate: time on bus from from_stop -> to_stop (minutes)
    ride_min = int(cfg.get("ride_minutes_estimate", 14))

    try:
        preds = next_predictions(stop_id=stop_id, route=route, now=now, rtpi_datafeed=rtpidatafeed)
    except PRTBusTimeError as e:
        return f"Error fetching arrivals: {e}"

    if not preds:
        last = state.get("last_seen_arrival")
        if last:
            return f"No upcoming buses found right now. Last seen bus arrival was {last.strftime('%H:%M')}."
        return "No upcoming buses found for this stop right now."

    # Convert to items with leave times
    items = []
    for p in preds:
        a = p["arrival"]
        leave_at = a - dt.timedelta(minutes=leave_buffer, seconds=extra_safety)
        items.append({
            "rt": p.get("rt") or (route or ""),
            "arrival": a,
            "leave_at": leave_at,
            "raw": p.get("raw") or {},
        })

    # Classify by leave_at (matches the web UI behavior)
    recent_missed_sec = 3 * 60
    upcoming = [x for x in items if (x["leave_at"] - now).total_seconds() > 0]
    missed_recent = [x for x in items if 0 >= (x["leave_at"] - now).total_seconds() >= -recent_missed_sec]

    upcoming = sorted(upcoming, key=lambda x: x["leave_at"])[:2]
    missed = sorted(missed_recent, key=lambda x: x["leave_at"], reverse=True)[:1]

    show = upcoming + missed

    lines = [
        f"Stop {stop_id} • routes {route}*",
        f"Buffer: {leave_buffer}m + {extra_safety}s  (ride est. {ride_min}m)",
        "",
    ]

    for i, x in enumerate(show, start=1):
        secs_to_leave = (x["leave_at"] - now).total_seconds()
        eta_dest = x["arrival"] + dt.timedelta(minutes=ride_min)
        cdn = (x.get("raw") or {}).get("prdctdn")
        cdn_txt = f" • cdn {cdn}m" if (cdn is not None and str(cdn).isdigit()) else ""

        lines += [
            f"{i}) {x['rt']}",
            f"   Bus ETA / 到站: {x['arrival'].strftime('%H:%M')}  (dest~ / 目的地~ {eta_dest.strftime('%H:%M')}){cdn_txt}",
            f"   Leave by / 出门: {x['leave_at'].strftime('%H:%M')}  ({_fmt_miss_or_in(secs_to_leave)})",
            "",
        ]

    # Trim trailing blank line
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def _h(s: str) -> str:
    """HTML-escape helper for Telegram parse_mode=HTML."""
    import html
    return html.escape(str(s), quote=False)


def _select_trips_for_display(items: list[dict], now: dt.datetime) -> list[dict]:
    """Match the web rule:

    - Prefer catchable trips (leave_at in the future)
    - Show up to 3 total
    - Optionally include at most 1 recently-missed trip (leave_at within last 3 minutes)
      as a single "Missed" item (the most recent one), then show upcoming after it.

    This avoids showing multiple missed trips.
    """
    recent_missed_sec = 3 * 60

    upcoming = [x for x in items if (x["leave_at"] - now).total_seconds() > 0]
    missed_recent = [x for x in items if 0 >= (x["leave_at"] - now).total_seconds() >= -recent_missed_sec]

    upcoming = sorted(upcoming, key=lambda x: x["leave_at"])  # soonest leave first
    missed_recent = sorted(missed_recent, key=lambda x: x["leave_at"], reverse=True)  # most recent missed first

    if len(upcoming) >= 3:
        return upcoming[:3]

    if len(upcoming) >= 2 and missed_recent:
        return upcoming[:2] + missed_recent[:1]

    # If only 0-1 upcoming, include at most one recently missed, then fill with upcoming
    out: list[dict] = []
    if missed_recent:
        out.append(missed_recent[0])
    out.extend(upcoming[: (3 - len(out))])
    return out


def build_digest_reply(cfg: dict, now: dt.datetime | None = None, title: str = "Digest / 车次汇总") -> str:
    """Build a digest message like the web UI.

    Shows up to 3 items total:
      - upcoming #1
      - upcoming #2
      - optional recently-missed (<=3m) if available

    (If there's no recently missed bus, we just show upcoming items.)
    """
    now = now or dt.datetime.now(tz=APP_TZ)

    stop_id = cfg["from_stop_id"]
    route = cfg.get("route")
    rtpidatafeed = cfg.get("rtpidatafeed")
    leave_buffer = int(cfg.get("leave_buffer_minutes", 6))
    extra_safety = int(cfg.get("extra_safety_seconds", 30))
    ride_min = int(cfg.get("ride_minutes_estimate", 14))

    try:
        preds = next_predictions(stop_id=stop_id, route=route, now=now, rtpi_datafeed=rtpidatafeed)
    except PRTBusTimeError as e:
        return f"<b>Error / 错误:</b> {_h(e)}"

    # Build a larger candidate set so we can skip older missed trips and still find upcoming ones.
    items = []
    for p in preds[:12]:
        a = p["arrival"]
        leave_at = a - dt.timedelta(minutes=leave_buffer, seconds=extra_safety)
        items.append({
            "rt": p.get("rt") or (route or ""),
            "arrival": a,
            "leave_at": leave_at,
            "raw": p.get("raw") or {},
        })

    items = _select_trips_for_display(items, now)

    lines = [
        f"<b>{_h(title)}</b>",
        f"<b>Now / 当前:</b> {_h(now.strftime('%H:%M'))}",
        f"<b>Stop / 站点:</b> {_h(stop_id)}",
        f"<b>Routes / 线路:</b> {_h(route)}*",
        f"<b>Buffer / 缓冲:</b> {_h(str(leave_buffer))}m + {_h(str(extra_safety))}s  <b>Ride est. / 车程估计:</b> {_h(str(ride_min))}m",
        "",
    ]

    if not items:
        lines.append("<b>Status / 状态:</b> No upcoming buses right now / 当前暂无班次")
        return "\n".join(lines)

    for i, x in enumerate(items, start=1):
        cdn = (x.get("raw") or {}).get("prdctdn")
        cdn_txt = f"  <b>CDN / 倒计时:</b> {_h(cdn)}m" if (cdn is not None and str(cdn).isdigit()) else ""

        secs_to_leave = (x["leave_at"] - now).total_seconds()
        if secs_to_leave >= 0:
            leave_in = fmt_delta(secs_to_leave)
            status_txt = ""
        else:
            leave_in = fmt_delta(-secs_to_leave)
            status_txt = "  <b>Status / 状态:</b> Missed"

        lines += [
            f"<b>{i}) Route / 线路:</b> {_h(x['rt'])}",
            f"<b>Bus ETA / 到站:</b> {_h(x['arrival'].strftime('%H:%M'))}{cdn_txt}",
            f"<b>Leave by / 出门:</b> {_h(x['leave_at'].strftime('%H:%M'))}  <b>Leave in / 还剩:</b> {_h(leave_in)}{status_txt}",
            "",
        ]

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def maybe_send_reminders():
    cfg = load_config()

    # Old threshold-style reminders are optional. For HW4 we mainly use scheduled digests.
    if not cfg.get("threshold_reminders_enabled", False):
        return

    if not in_monitor_window(dt.datetime.now(tz=APP_TZ), cfg):
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

    now = dt.datetime.now(tz=APP_TZ)

    try:
        preds = next_predictions(stop_id=stop_id, route=route, now=now, rtpi_datafeed=rtpidatafeed)
    except PRTBusTimeError:
        return
    if not preds:
        return

    arrivals = [p["arrival"] for p in preds]

    # remember last seen arrival time (best-effort)
    try:
        state["last_seen_arrival"] = arrivals[0]
    except Exception:
        pass

    preds = preds[:2]
    arrivals = arrivals[:2]
    leave_times = compute_leave_times(arrivals, leave_buffer, extra_safety)

    # Choose the soonest bus as primary
    primary_arrival = arrivals[0]
    primary_rt = preds[0].get("rt") or (route or "")
    primary_leave = leave_times[0]

    # Secondary bus (if miss primary)
    secondary_arrival = arrivals[1] if len(arrivals) > 1 else None
    secondary_rt = preds[1].get("rt") if len(preds) > 1 else None

    for t in thresholds:
        # Trigger when we are within ~poll interval of crossing the threshold
        secs_to_leave = (primary_leave - now).total_seconds()
        if secs_to_leave <= t and secs_to_leave > t - int(cfg.get("poll_seconds", 20)) - 1:
            key = f"leave@{primary_arrival.isoformat()}@{t}"
            last = state["last_sent"].get(key)
            if last and (now - last).total_seconds() < 300:
                continue

            if t > 0:
                headline = f"Reminder: leave in {fmt_delta(secs_to_leave)} to catch {primary_rt}"
            else:
                headline = f"Leave now to catch {primary_rt}"

            miss_wait = ""
            if secondary_arrival:
                wait = (secondary_arrival - primary_arrival).total_seconds()
                if wait > 0:
                    tail = f" ({secondary_rt})" if secondary_rt else ""
                    miss_wait = f"\nIf you miss this one, next is ~{fmt_delta(wait)} later{tail}."

            msg = (
                f"<b>{headline}</b>\n"
                f"<b>Stop / 站点:</b> {stop_id}\n"
                f"<b>Bus ETA / 到站:</b> {primary_arrival.strftime('%H:%M')}\n"
                f"<b>Buffer / 缓冲:</b> {leave_buffer}m (+{extra_safety}s)\n"
                f"<b>Leave by / 出门:</b> {primary_leave.strftime('%H:%M')}"
                f"{miss_wait}"
            )
            try:
                send_telegram(chat_id=chat_id, text=msg, parse_mode="HTML")
                state["last_sent"][key] = now
            except Exception:
                pass


def send_scheduled_digest(name: str) -> None:
    """Send a scheduled digest message (next 3 buses from now)."""
    cfg = load_config()

    mode = cfg.get("notification", {}).get("mode", "telegram")
    chat_id = cfg.get("notification", {}).get("telegram_chat_id")
    if mode != "telegram" or not chat_id:
        return

    now = dt.datetime.now(tz=APP_TZ)
    msg = build_digest_reply(cfg, now=now, title="Scheduled digest / 定时汇总")

    # De-dupe (avoid duplicate sends if scheduler restarts)
    key = f"digest@{name}@{now.strftime('%Y-%m-%d')}"
    last = state["last_sent"].get(key)
    if last and (now - last).total_seconds() < 6 * 3600:
        return

    try:
        send_telegram(chat_id=chat_id, text=msg, parse_mode="HTML")
        state["last_sent"][key] = now
    except Exception:
        pass


def _acquire_lock_fd(lock_path: str) -> int | None:
    """Acquire a non-blocking flock and return the fd (caller must close)."""
    try:
        import fcntl  # type: ignore

        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        return None


def poll_telegram_and_reply():
    """Poll Telegram getUpdates and reply to simple commands.

    This avoids needing a public webhook URL.

    Notes:
    - We persist the update offset to avoid duplicate replies after restarts.
    - We use a lock to ensure only one process polls/sends at a time.
    """

    lock_fd = _acquire_lock_fd("/tmp/prt_alert_telegram_poll.lock")
    if lock_fd is None:
        return

    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            return

        cfg = load_config()

        params: dict = {"timeout": 0}
        if state.get("telegram_update_offset") is not None:
            params["offset"] = state["telegram_update_offset"]

        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
        except Exception:
            return

        updates = data.get("result", []) if isinstance(data, dict) else []
        if not updates:
            return

        max_update_id: int | None = None

        for u in updates:
            try:
                update_id = u.get("update_id")
                if isinstance(update_id, int):
                    max_update_id = update_id if (max_update_id is None) else max(max_update_id, update_id)

                msg = u.get("message") or u.get("edited_message")
                if not msg:
                    continue

                text = (msg.get("text") or "").strip()
                if not text:
                    continue

                chat_id = (msg.get("chat") or {}).get("id")
                if not chat_id:
                    continue

                # Only reply to the configured chat (if set to a real value).
                # In Render, config.json may be absent and config.example.json contains a placeholder.
                cfg_chat = (cfg.get("notification", {}) or {}).get("telegram_chat_id")
                if cfg_chat and str(cfg_chat).upper() not in {"YOUR_CHAT_ID_HERE", ""} and str(cfg_chat) != str(chat_id):
                    continue

                norm = text.lower().strip()

                if norm in {"next bus", "nextbus", "/nextbus", "/next", "next"}:
                    reply = build_next_bus_reply(cfg)
                    try:
                        send_telegram(chat_id=str(chat_id), text=reply, parse_mode=None)
                    except Exception:
                        pass

                if norm in {"/digest", "digest"}:
                    now_local = dt.datetime.now(tz=APP_TZ)
                    reply = build_digest_reply(cfg, now=now_local, title="On-demand digest / 即时查询")
                    try:
                        send_telegram(chat_id=str(chat_id), text=reply, parse_mode="HTML")
                    except Exception:
                        pass
            except Exception:
                continue

        # Advance offset once, after processing the batch
        if isinstance(max_update_id, int):
            state["telegram_update_offset"] = max_update_id + 1
            _save_persisted_state()

    finally:
        try:
            os.close(lock_fd)
        except Exception:
            pass


@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    now = dt.datetime.now(tz=APP_TZ)

    info = {"error": None, "arrivals": [], "leave_times": [], "preds": []}
    try:
        # Fetch more than 3 so the UI can skip missed buses and still show upcoming ones.
        preds = next_predictions(cfg["from_stop_id"], cfg.get("route"), now=now, rtpi_datafeed=cfg.get("rtpidatafeed"))[:12]
        arrivals = [p["arrival"] for p in preds]
        info["preds"] = preds
        info["arrivals"] = arrivals
        info["leave_times"] = compute_leave_times(
            arrivals,
            int(cfg.get("leave_buffer_minutes", 6)),
            int(cfg.get("extra_safety_seconds", 30)),
        )

        if arrivals:
            state["last_seen_arrival"] = arrivals[0]

    except PRTBusTimeError as e:
        # If there are no predictions right now, show the "No service" state (not an error).
        msg = str(e).lower()
        if ("no arrival times" in msg) or ("no service scheduled" in msg):
            info["arrivals"] = []
            info["leave_times"] = []
        else:
            info["error"] = str(e)
    except Exception as e:
        info["error"] = str(e)

    return render_template("index.html", cfg=cfg, info=info, now=now, state=state)


@app.route("/api/next", methods=["GET"])
def api_next():
    """CORS-friendly JSON endpoint for clients (optional)."""
    cfg = load_config()
    now = dt.datetime.now(tz=APP_TZ)

    try:
        # Fetch more than 3 so clients can skip missed buses and still show upcoming ones.
        preds = next_predictions(cfg["from_stop_id"], cfg.get("route"), now=now, rtpi_datafeed=cfg.get("rtpidatafeed"))[:12]
        arrivals = [p["arrival"] for p in preds]

        # remember last seen arrival time (best-effort)
        if arrivals:
            state["last_seen_arrival"] = arrivals[0]

        leave_times = compute_leave_times(
            arrivals,
            int(cfg.get("leave_buffer_minutes", 6)),
            int(cfg.get("extra_safety_seconds", 30)),
        )

        buf_min = float(cfg.get("leave_buffer_minutes", 6)) + float(cfg.get("extra_safety_seconds", 30)) / 60.0

        out = {
            "now": now.isoformat(),
            "stop_id": cfg.get("from_stop_id"),
            "route_prefix": cfg.get("route"),
            "buffer_minutes": buf_min,
            "last_seen_bus_arrival_hhmm": state.get("last_seen_arrival").strftime("%H:%M") if state.get("last_seen_arrival") else None,
            "arrivals": [
                {
                    "index": i + 1,
                    "rt": (preds[i].get("rt") if i < len(preds) else None),
                    "bus_arrival_iso": a.isoformat(),
                    "leave_iso": lt.isoformat(),
                    "bus_arrival_hhmm": a.strftime("%H:%M"),
                    "leave_hhmm": lt.strftime("%H:%M"),
                }
                for i, (a, lt) in enumerate(zip(arrivals, leave_times))
            ],
        }

        resp = jsonify(out)
    except PRTBusTimeError as e:
        # Normalize "no arrivals" states into a valid empty response for clients.
        msg = str(e).lower()
        if ("no arrival times" in msg) or ("no service scheduled" in msg):
            buf_min = float(cfg.get("leave_buffer_minutes", 6)) + float(cfg.get("extra_safety_seconds", 30)) / 60.0
            resp = jsonify({
                "now": now.isoformat(),
                "stop_id": cfg.get("from_stop_id"),
                "route_prefix": cfg.get("route"),
                "buffer_minutes": buf_min,
                "last_seen_bus_arrival_hhmm": state.get("last_seen_arrival").strftime("%H:%M") if state.get("last_seen_arrival") else None,
                "arrivals": [],
            })
        else:
            resp = jsonify({"error": str(e)})
    except Exception as e:
        resp = jsonify({"error": str(e)})

    # Allow static frontends to call this endpoint.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/health", methods=["GET"])
def health():
    """Simple health check endpoint for hosting providers (e.g., Render)."""
    resp = jsonify({"ok": True})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/version", methods=["GET"])
def version():
    """Build/version info to verify the deployed code on Render."""
    resp = jsonify({
        "render_git_commit": os.getenv("RENDER_GIT_COMMIT"),
        "render_service_name": os.getenv("RENDER_SERVICE_NAME"),
        "scheduler_started": bool(state.get("scheduler_started")),
    })
    resp.headers["Cache-Control"] = "no-store"
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


def _try_acquire_lock(lock_path: str = "/tmp/prt_leave_alert_scheduler.lock") -> bool:
    """Best-effort inter-process lock to avoid duplicate schedulers under gunicorn."""
    try:
        import fcntl  # type: ignore

        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # keep fd open for lifetime of process
        state["_lock_fd"] = fd  # type: ignore
        return True
    except Exception:
        return False


def start_scheduler() -> None:
    """Start APScheduler jobs (safe to call multiple times)."""
    if state.get("scheduler_started"):
        return

    # Allow disabling via env (useful for testing)
    if os.getenv("RUN_SCHEDULER", "1") not in {"1", "true", "True", "yes", "YES"}:
        return

    # Avoid duplicates when multiple workers/threads exist
    if not _try_acquire_lock():
        return

    cfg = load_config()

    sched = BackgroundScheduler(daemon=True, timezone=APP_TZ)

    # Existing polling-based reminders
    sched.add_job(maybe_send_reminders, "interval", seconds=int(cfg.get("poll_seconds", 20)))

    # Telegram command polling
    sched.add_job(poll_telegram_and_reply, "interval", seconds=3)

    # New: scheduled digest reminders (HW4-style feature)
    digest = cfg.get("telegram_digest_schedule")
    if isinstance(digest, list) and digest:
        for item in digest:
            try:
                name = str(item.get("name") or "digest")
                days = item.get("days") or []
                hhmm = str(item.get("time") or "")
                # anchor_time kept for backward-compatibility; digest is always based on 'now'
                anchor_hhmm = str(item.get("anchor_time") or "")

                # days like ["Mon","Wed"]
                day_map = {"Mon": "mon", "Tue": "tue", "Wed": "wed", "Thu": "thu", "Fri": "fri", "Sat": "sat", "Sun": "sun"}
                dows = [day_map[d] for d in days if d in day_map]
                if not dows:
                    continue

                t = _parse_hhmm(hhmm)
                trigger = CronTrigger(day_of_week=",".join(dows), hour=t.hour, minute=t.minute, timezone=APP_TZ)
                sched.add_job(send_scheduled_digest, trigger=trigger, args=[name])
            except Exception:
                continue

    sched.start()
    state["scheduler_started"] = True


# Load persisted state before starting any polling
_load_persisted_state()

# Start scheduler when imported (gunicorn / Render)
start_scheduler()


if __name__ == "__main__":
    # Local dev
    app.run(debug=True, port=5001)
