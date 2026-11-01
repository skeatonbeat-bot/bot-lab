"""Find Polymarket's "Highest temperature in <city> on <date>?" events and read their rules.

Each event is a set of bracket markets ("7°C", "56-57°F", "6°C or below", "74°F or higher").
The event's resolutionSource is an aviationweather.gov METAR link that names the station
(ids=EGLC), which is exactly the feed obs.py reads, so the station comes from the rules
themselves rather than from a hand-made table.
"""

import json
import re
from datetime import date, timedelta

GAMMA = "https://gamma-api.polymarket.com"

# Slugs Polymarket has used for its city temperature events. Unknown or retired ones just
# come back empty, so it is fine for this list to be generous. Add a city here to track it.
CITY_SLUGS = [
    # Americas
    "nyc", "chicago", "miami", "los-angeles", "seattle", "denver", "dallas", "houston",
    "austin", "atlanta", "boston", "philadelphia", "washington-dc", "san-francisco",
    "phoenix", "las-vegas", "minneapolis", "toronto", "mexico-city", "sao-paulo",
    "buenos-aires",
    # Europe / Middle East / Africa
    "london", "paris", "berlin", "madrid", "rome", "amsterdam", "helsinki", "moscow",
    "istanbul", "ankara", "warsaw", "dubai", "tel-aviv", "cairo", "lagos",
    # Asia / Pacific
    "seoul", "tokyo", "hong-kong", "shanghai", "beijing", "chongqing", "shenzhen",
    "taipei", "singapore", "kuala-lumpur", "jakarta", "manila", "bangkok", "mumbai",
    "delhi", "karachi", "lucknow", "sydney", "wellington",
]

MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
          "september", "october", "november", "december"]


def event_slug(city, d):
    return f"highest-temperature-in-{city}-on-{MONTHS[d.month - 1]}-{d.day}-{d.year}"


def slug_date(slug):
    m = re.search(r"-on-([a-z]+)-(\d{1,2})-(\d{4})$", slug or "")
    if not m or m.group(1) not in MONTHS:
        return None
    return date(int(m.group(3)), MONTHS.index(m.group(1)) + 1, int(m.group(2)))


def station_from_event(ev):
    """ICAO code from the rules: the METAR link (ids=KLGA) or, failing that, the text."""
    for field in ("resolutionSource", "description"):
        m = re.search(r"ids=([A-Z0-9]{4})\b", ev.get(field) or "")
        if m:
            return m.group(1)
    m = re.search(r"\(([A-Z]{4})\)", ev.get("description") or "")
    return m.group(1) if m else None


_NUM = r"(-?\d+)"


def parse_bracket(label):
    """'7°C' -> ('C', 7, 7); '56-57°F' -> ('F', 56, 57); '6°C or below' -> ('C', None, 6);
    '74°F or higher' -> ('F', 74, None). Bounds are whole degrees, inclusive.
    Returns None for anything it doesn't recognise (that market is then skipped)."""
    s = (label or "").replace("º", "°").replace(" ", "").lower()
    s = s.replace("−", "-").replace("–", "-")
    m = re.fullmatch(_NUM + r"°?([cf])(orbelow|orless|orlower)", s)
    if m:
        return (m.group(2).upper(), None, int(m.group(1)))
    m = re.fullmatch(_NUM + r"°?([cf])(orhigher|ormore|orabove)", s)
    if m:
        return (m.group(2).upper(), int(m.group(1)), None)
    m = re.fullmatch(_NUM + r"-" + _NUM + r"°?([cf])", s) or re.fullmatch(r"between" + _NUM + r"-" + _NUM + r"°?([cf])", s)
    if m:
        return (m.group(3).upper(), int(m.group(1)), int(m.group(2)))
    m = re.fullmatch(_NUM + r"°?([cf])", s)
    if m:
        v = int(m.group(1))
        return (m.group(2).upper(), v, v)
    return None


def in_bracket(value, lo, hi):
    return (lo is None or value >= lo) and (hi is None or value <= hi)


def _list(x):
    if isinstance(x, list):
        return x
    try:
        return json.loads(x or "[]")
    except Exception:
        return []


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_event(ev):
    """Gamma event JSON -> dict with station, date, unit and brackets, or None if unusable."""
    slug = ev.get("slug", "")
    d = slug_date(slug)
    icao = station_from_event(ev)
    if not d or not icao:
        return None
    brackets, unit = [], None
    for m in ev.get("markets") or []:
        b = parse_bracket(m.get("groupItemTitle") or "")
        if not b:
            q = re.search(r"be (.+?) on ", m.get("question") or "")
            b = parse_bracket(q.group(1)) if q else None
        if not b:
            continue
        unit = unit or b[0]
        toks = _list(m.get("clobTokenIds"))
        px = _list(m.get("outcomePrices"))
        brackets.append({
            "label": m.get("groupItemTitle") or m.get("question"),
            "lo": b[1], "hi": b[2],
            "market_id": str(m.get("id", "")),
            "yes_token": toks[0] if len(toks) > 0 else None,
            "no_token": toks[1] if len(toks) > 1 else None,
            "bid": _f(m.get("bestBid")), "ask": _f(m.get("bestAsk")),
            "last": _f(px[0]) if px else None,
            "closed": bool(m.get("closed")),
            # once settled, outcomePrices become ["1","0"] (Yes won) or ["0","1"]
            "won": (None if not m.get("closed") or len(px) < 2 else
                    (True if _f(px[0]) == 1.0 else False if _f(px[1]) == 1.0 else None)),
        })
    if not brackets:
        return None
    brackets.sort(key=lambda b: (b["lo"] if b["lo"] is not None else -999))
    return {
        "slug": slug, "title": ev.get("title", ""), "date": d.isoformat(), "station": icao,
        "unit": unit, "brackets": brackets, "closed": bool(ev.get("closed")),
        "city": re.sub(r"^highest-temperature-in-|-on-[a-z]+-\d+-\d{4}$", "", slug),
    }


def discover(get, today_utc, days_ahead=2, cities=CITY_SLUGS):
    """Every open event for yesterday..today+days_ahead (yesterday catches places still in it).
    `get(url, params)` returns parsed JSON. Returns {slug: parsed event}."""
    found = {}
    for city in cities:
        for k in range(-1, days_ahead + 1):
            slug = event_slug(city, today_utc + timedelta(days=k))
            try:
                data = get(f"{GAMMA}/events", {"slug": slug})
            except Exception:
                continue
            for ev in (data if isinstance(data, list) else [data] if data else []):
                p = parse_event(ev)
                if p:
                    found[p["slug"]] = p
    return found


def refresh(get, slug):
    data = get(f"{GAMMA}/events", {"slug": slug})
    evs = data if isinstance(data, list) else [data] if data else []
    return parse_event(evs[0]) if evs else None
