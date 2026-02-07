import os
import datetime as dt
from zoneinfo import ZoneInfo
import requests

BUSTIME_BASE = "https://truetime.portauthority.org/bustime/api/v3"

# Render/Linux hosts often default to UTC. PRT times are local Pittsburgh time.
PRT_TZ = ZoneInfo(os.getenv("PRT_TIMEZONE", "America/New_York"))

class PRTBusTimeError(RuntimeError):
    pass

def _get_key() -> str:
    key = os.getenv("PRT_BUSTIME_API_KEY")
    if not key:
        raise PRTBusTimeError("Missing env var PRT_BUSTIME_API_KEY")
    return key


def get_predictions(
    stop_id: str,
    route: str | None = None,
    rtpi_datafeed: str | None = None,
    timeout: int = 10,
) -> list[dict]:
    """Return raw prediction dicts from BusTime getpredictions.

    Note: PRT TrueTime is a multi-feed site, so `rtpidatafeed` must be provided.
    Use `getrtpidatafeeds` to discover available feeds.
    """

    if rtpi_datafeed is None:
        # PRT is multi-feed. Default feed name discovered via getrtpidatafeeds.
        rtpi_datafeed = os.getenv("PRT_RTPIDATAFEED", "Port Authority Bus")

    params = {
        "key": _get_key(),
        "stpid": stop_id,
        "format": "json",
        "rtpidatafeed": rtpi_datafeed,
    }

    # Note: On PRT, individual routes are often coded as 61A/61B/61C/61D.
    # If the user provides "61" we *do not* pass rt=61 to the API (it returns no data).
    # We instead fetch all predictions for the stop and filter client-side by prefix.
    if route and route.upper() != "61":
        params["rt"] = route

    url = f"{BUSTIME_BASE}/getpredictions"
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    resp = data.get("bustime-response", {})

    if "error" in resp:
        # BusTime sometimes returns an "error" field even for non-fatal states,
        # e.g. when there are simply no arrival predictions at the moment.
        err = resp.get("error")

        # Normalize to a list of error dicts if possible
        items = []
        if isinstance(err, list):
            items = err
        elif isinstance(err, dict):
            items = [err]
        else:
            items = []

        msgs = " ".join(str(i.get("msg", "")) for i in items if isinstance(i, dict)).lower()
        # Treat "no arrivals" states as empty (not fatal).
        if ("no arrival times" in msgs) or ("no service scheduled" in msgs):
            return []

        # Otherwise treat as fatal (invalid key, invalid stop, etc.)
        raise PRTBusTimeError(str(err))

    preds = resp.get("prd", [])
    if isinstance(preds, dict):
        preds = [preds]
    return preds


def _parse_prdtm(prdtm: str) -> dt.datetime:
    # Format is typically: "YYYYMMDD HH:MM" in PRT local time.
    naive = dt.datetime.strptime(prdtm, "%Y%m%d %H:%M")
    return naive.replace(tzinfo=PRT_TZ)


def _prediction_arrival_dt(p: dict, now: dt.datetime) -> dt.datetime | None:
    """Best-effort predicted arrival time as a datetime.

    BusTime sometimes provides both:
      - prdtm: an absolute timestamp (minute precision)
      - prdctdn: a countdown in minutes (relative)

    To better match rider-facing apps (which often emphasize the countdown), we prefer
    a numeric countdown when available; otherwise we fall back to prdtm.
    """

    cdn = p.get("prdctdn")
    if isinstance(cdn, str) and cdn.lower() in {"due", "dly"}:
        # "due" = arriving now; "dly" = delayed (no numeric)
        if cdn.lower() == "due":
            return now
        # "dly" has no numeric time
        cdn = None

    # If countdown is numeric, prefer it (seconds preserved from `now`)
    try:
        if cdn is not None:
            minutes = int(cdn)
            return now + dt.timedelta(minutes=minutes)
    except Exception:
        pass

    # Fallback: absolute predicted time if provided
    prdtm = p.get("prdtm")
    if prdtm:
        try:
            return _parse_prdtm(prdtm)
        except Exception:
            pass

    return None


def next_predictions(
    stop_id: str,
    route: str | None = None,
    now: dt.datetime | None = None,
    rtpi_datafeed: str | None = None,
) -> list[dict]:
    """Return upcoming predictions with route codes preserved.

    Each item looks like:
      {"rt": "61A", "arrival": datetime, "raw": <original prediction dict>}

    If `route` is provided, we treat it as a prefix (e.g. "61" matches 61A/61B/61C/61D).
    """
    now = now or dt.datetime.now(tz=PRT_TZ)
    preds = get_predictions(stop_id=stop_id, route=route, rtpi_datafeed=rtpi_datafeed)

    if route:
        rp = str(route).upper()
        preds = [p for p in preds if str(p.get("rt", "")).upper().startswith(rp)]

    out: list[dict] = []
    for p in preds:
        arrival = _prediction_arrival_dt(p, now)
        if not arrival:
            continue
        out.append({
            "rt": str(p.get("rt") or "").strip(),
            "arrival": arrival,
            "raw": p,
        })

    # De-dupe by (rt, arrival)
    seen: set[tuple[str, str]] = set()
    uniq: list[dict] = []
    for item in sorted(out, key=lambda x: (x["arrival"], x.get("rt", ""))):
        key = (item.get("rt", ""), item["arrival"].isoformat())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)

    return uniq


def next_arrivals(
    stop_id: str,
    route: str | None = None,
    now: dt.datetime | None = None,
    rtpi_datafeed: str | None = None,
) -> list[dt.datetime]:
    """Backward-compatible: just datetimes, sorted."""
    items = next_predictions(stop_id=stop_id, route=route, now=now, rtpi_datafeed=rtpi_datafeed)
    return [i["arrival"] for i in items]
