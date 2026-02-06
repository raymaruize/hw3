import os
import datetime as dt
import requests

BUSTIME_BASE = "https://truetime.portauthority.org/bustime/api/v3"

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
        # Usually: invalid key, invalid stop, etc.
        raise PRTBusTimeError(str(resp["error"]))

    preds = resp.get("prd", [])
    if isinstance(preds, dict):
        preds = [preds]
    return preds


def _parse_prdtm(prdtm: str) -> dt.datetime:
    # Format is typically: "YYYYMMDD HH:MM"
    return dt.datetime.strptime(prdtm, "%Y%m%d %H:%M")


def next_arrivals(
    stop_id: str,
    route: str | None = None,
    now: dt.datetime | None = None,
    rtpi_datafeed: str | None = None,
) -> list[dt.datetime]:
    """Return the next arrival datetimes (local time) sorted ascending.

    If `route` is provided, we treat it as a prefix (e.g. "61" matches 61A/61B/61C/61D).
    """
    now = now or dt.datetime.now()
    preds = get_predictions(stop_id=stop_id, route=route, rtpi_datafeed=rtpi_datafeed)

    if route:
        rp = route.upper()
        preds = [p for p in preds if str(p.get("rt", "")).upper().startswith(rp)]

    arrivals: list[dt.datetime] = []
    for p in preds:
        # Prefer absolute predicted time if provided
        prdtm = p.get("prdtm")
        if prdtm:
            try:
                arrivals.append(_parse_prdtm(prdtm))
                continue
            except Exception:
                pass

        # Fallback: countdown minutes
        cdn = p.get("prdctdn")
        if cdn is None:
            continue
        if isinstance(cdn, str) and cdn.lower() in {"due", "dly"}:
            # "due" = arriving now; "dly" = delayed (no numeric)
            if cdn.lower() == "due":
                arrivals.append(now)
            continue
        try:
            minutes = int(cdn)
        except Exception:
            continue
        arrivals.append(now + dt.timedelta(minutes=minutes))

    arrivals = sorted(set(arrivals))
    return arrivals
