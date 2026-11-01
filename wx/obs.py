"""The settlement value so far, read the way the markets' rules read it.

Rules (Polymarket, Nov 2026): only routine METARs count (SPECI reports don't); use the
remarks T-group (tenths of °C) when present, otherwise the whole-°C body temperature; take
the highest over every routine report whose LOCAL time falls on the market's date; convert
to °F if the market is in °F; round to the nearest whole degree with halves rounded up.
"""

import math
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

AWC = "https://aviationweather.gov/api/data/metar"

_TGROUP = re.compile(r"\bT([01])(\d{3})[01]\d{3}\b")
_BODY = re.compile(r"\s(M?\d{2})/(M?\d{2})?\s")


def temp_c(rec):
    """Best temperature in °C from one METAR record: T-group tenths, else body whole degrees."""
    raw = rec.get("rawOb") or ""
    m = _TGROUP.search(raw)
    if m:
        v = int(m.group(2)) / 10.0
        return -v if m.group(1) == "1" else v
    m = _BODY.search(" " + raw.split(" RMK")[0] + " ")
    if m:
        s = m.group(1)
        return -float(s[1:]) if s.startswith("M") else float(s)
    t = rec.get("temp")
    return float(t) if isinstance(t, (int, float)) else None


def is_routine(rec):
    t = (rec.get("metarType") or "").upper()
    raw = (rec.get("rawOb") or "").upper()
    return t != "SPECI" and not raw.startswith("SPECI")


def round_half_up(x):
    return int(math.floor(x + 0.5))


def to_unit(c, unit):
    return c * 9 / 5 + 32 if unit == "F" else c


def settle_value(c, unit):
    return round_half_up(to_unit(c, unit))


def obs_time(rec):
    t = rec.get("obsTime")
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t, tz=timezone.utc)
    s = rec.get("reportTime")
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def fetch(get, stations, hours=40):
    """Recent METARs for many stations in one call: {ICAO: [records, oldest first]}."""
    out = {s: [] for s in stations}
    if not stations:
        return out
    data = get(AWC, {"ids": ",".join(sorted(stations)), "format": "json", "hours": hours}) or []
    for r in data:
        if r.get("icaoId") in out:
            out[r["icaoId"]].append(r)
    for s in out:
        out[s].sort(key=lambda r: obs_time(r) or datetime.min.replace(tzinfo=timezone.utc))
    return out


def day_summary(records, tz_name, local_date):
    """Routine reports on the local date: max °C so far, the latest reading, report count.
    `local_date` is an ISO date string."""
    tz = ZoneInfo(tz_name)
    best, latest, n = None, None, 0
    for r in records:
        t = obs_time(r)
        if t is None or not is_routine(r):
            continue
        if t.astimezone(tz).date().isoformat() != local_date:
            continue
        c = temp_c(r)
        if c is None:
            continue
        n += 1
        best = c if best is None else max(best, c)
        latest = (t, c)
    return {"max_c": best, "latest": latest, "count": n}


def station_coords(records):
    for r in reversed(records):
        if r.get("lat") is not None and r.get("lon") is not None:
            return float(r["lat"]), float(r["lon"])
    return None
