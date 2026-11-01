"""Open-Meteo ensemble forecasts at a station (free, worldwide, no key).

Three ensembles, one request: ECMWF (51 members), GFS (31) and ICON (40). Every hourly
series that starts with "temperature_2m" is one member, whatever the model suffix.
Times come back in the station's local time (timezone=auto), which is how the markets
count the day.
"""

from datetime import datetime, timezone

ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
MODELS = "ecmwf_ifs025,gfs025,icon_seamless"


def fetch(get, lat, lon, days=4):
    d = get(ENSEMBLE, {
        "latitude": round(lat, 4), "longitude": round(lon, 4), "hourly": "temperature_2m",
        "models": MODELS, "timezone": "auto", "forecast_days": days,
    })
    return parse(d)


def parse(d):
    hourly = d.get("hourly") or {}
    times = hourly.get("time") or []
    members = []
    for k, series in hourly.items():
        if k.startswith("temperature_2m") and isinstance(series, list) and series:
            if sum(v is not None for v in series) >= len(series) * 0.5:
                members.append(series)
    return {
        "timezone": d.get("timezone") or "UTC",
        "times": times,               # local, e.g. "2026-11-02T14:00"
        "members": members,           # list of hourly °C lists
        "fetched": datetime.now(timezone.utc).isoformat(timespec="minutes"),
    }


def member_daily_max(fc, local_date, from_hour=0):
    """Each member's max °C over the local date, counting hours >= from_hour only."""
    idx = [i for i, t in enumerate(fc["times"])
           if t.startswith(local_date) and int(t[11:13]) >= from_hour]
    out = []
    for s in fc["members"]:
        vals = [s[i] for i in idx if i < len(s) and s[i] is not None]
        out.append(max(vals) if vals else None)
    return out


def member_at(fc, local_date, hour):
    key = f"{local_date}T{hour:02d}:00"
    if key not in fc["times"]:
        return []
    i = fc["times"].index(key)
    return [s[i] for s in fc["members"] if i < len(s) and s[i] is not None]
