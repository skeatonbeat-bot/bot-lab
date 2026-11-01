"""The Weather League: paper bets on Polymarket's worldwide daily-high temperature markets.

    python weather_live.py            # one cycle (what the workflow does every 15 minutes)
    python weather_live.py --force    # run even if the last cycle was under 25 minutes ago

Every cycle:
  1. finds the open "Highest temperature in <city> on <date>?" events (hourly)
  2. reads every station's routine METARs, the same feed the markets settle on
  3. pulls Open-Meteo ensembles (ECMWF + GFS + ICON) at each station
  4. turns forecast + live readings into a probability for every bracket (wx/model.py)
  5. lets five bots bet pretend money where the model and the live order book disagree
  6. settles finished bets from Polymarket's own result, and checks our METAR reading
     against it so we learn each station's quirks

The bots ($100 each, 2% of their money per bet, at least $1, retire at $60). Every bet
buys at the real best ask from Polymarket's order book and pays the weather taker fee
(5% x price x (1 - price) per share). One bet per bot per event.

  Favorite        same day from 9am local, buys the market's own favorite (the one to beat)
  Forecaster      the day before, buys the bracket (Yes or No) with the biggest edge >= 7c
  Nowcaster       same day 8am-5pm local, biggest edge >= 7c using live readings
  Strict Nowcast  same, but only edges >= 15c
  Late Closer     same day after 3pm local, edge >= 5c on a side the model gives >= 80%

Files in live/weather/:
  state.json            bankrolls, open bets, tracked events, learned station bias
  forecasts_YYYY-MM.csv every event's model vs market, each cycle
  bets.csv              every pretend bet and how it settled
  equity.csv            each bot's money after every settled bet
  outcomes.csv          per event: our METAR reading vs Polymarket's result, forecast error
Nothing real is bought or sold.
"""

import argparse
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from wx import forecast as F
from wx import markets as M
from wx import model as MD
from wx import obs as O

OUT = Path("live/weather")
CLOB = "https://clob.polymarket.com"
FEE_RATE = 0.05
START, STAKE_FRAC, MIN_STAKE, RETIRE_AT = 100.0, 0.02, 1.0, 60.0
MIN_GAP_MIN = 25          # minutes between cycles
DISCOVER_EVERY_MIN = 55

S = requests.Session()
S.headers["User-Agent"] = "bot-lab-weather/1.0"


def http_get(url, params=None, tries=3, timeout=15):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            if r.status_code == 204:
                return []
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def fee(p):
    return FEE_RATE * p * (1 - p)


# ---------- the bots ----------
# Each rule sees the event, the model output and the local hour, and returns a candidate
# list of (bracket index, side "YES"/"NO") to price, best first; the engine checks the
# live book and the bot's edge threshold.

def _edges(ev, pred):
    rows = []
    for i, (b, p) in enumerate(zip(ev["brackets"], pred["probs"])):
        if b["closed"]:
            continue
        if b["ask"] is not None and 0 < b["ask"] < 1:
            rows.append((p - b["ask"] - fee(b["ask"]), i, "YES", p))
        if b["bid"] is not None and 0 < b["bid"] < 1:
            na = 1 - b["bid"]
            rows.append(((1 - p) - na - fee(na), i, "NO", 1 - p))
    return sorted(rows, reverse=True)


def bot_favorite(ev, pred, hour):
    if pred["mode"] != "today" or hour < 9:
        return []
    live = [(b["ask"], i) for i, b in enumerate(ev["brackets"])
            if not b["closed"] and b["ask"] is not None and 0.30 <= b["ask"] <= 0.90]
    if not live:
        return []
    ask, i = max(live)
    return [(i, "YES", 0.0, ask)]   # it trusts the market, so its "model" is the price


def _edge_bot(min_edge, modes, hours=(0, 24), min_p=0.0, lead1_only=False):
    def rule(ev, pred, hour):
        if pred["mode"] not in modes or not (hours[0] <= hour < hours[1]):
            return []
        if lead1_only and pred.get("lead") != 1:
            return []
        return [(i, side, e, p) for e, i, side, p in _edges(ev, pred) if e >= min_edge and p >= min_p][:3]
    return rule


BOTS = {
    "Favorite": bot_favorite,
    "Forecaster": _edge_bot(0.07, {"future"}, lead1_only=True),
    "Nowcaster": _edge_bot(0.07, {"today"}, hours=(8, 17)),
    "Strict Nowcast": _edge_bot(0.15, {"today"}, hours=(8, 17)),
    "Late Closer": _edge_bot(0.05, {"today"}, hours=(15, 24), min_p=0.80),
}


def best_ask(get, token):
    """Live best ask and the dollars available there, from the CLOB order book."""
    if not token:
        return None, 0.0
    try:
        book = get(f"{CLOB}/book", {"token_id": token})
    except Exception:
        return None, 0.0
    asks = [(float(a["price"]), float(a["size"])) for a in (book or {}).get("asks") or []]
    if not asks:
        return None, 0.0
    p = min(a[0] for a in asks)
    return p, sum(s for q, s in asks if q == p) * p


# ---------- files ----------

def load_state():
    f = OUT / "state.json"
    if f.exists():
        return json.loads(f.read_text())
    return {"bots": {n: {"cash": START, "retired": False} for n in BOTS}, "open_bets": [],
            "events": {}, "stations": {}, "bias": {}, "last_cycle": None,
            "last_discover": None, "fc_fetched": {}}


def save_state(st):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "state.json").write_text(json.dumps(st, indent=1, sort_keys=True, default=str))


def append(name, row, fields):
    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / name
    new = not f.exists()
    with f.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


BET_FIELDS = ["time", "bot", "slug", "station", "date", "bracket", "side", "price", "fee", "shares",
              "stake", "p_model", "edge", "mode", "local_hour", "result", "payout", "settled"]
FC_FIELDS = ["time", "slug", "station", "date", "unit", "mode", "local_hour", "obs_max", "gap_c",
             "median", "p10", "p90", "top_bracket", "top_p", "top_ask", "best_edge", "brackets"]
OUT_FIELDS = ["date", "slug", "station", "unit", "our_metar_value", "our_bracket",
              "official_bracket", "match", "d1_median_c", "actual_c", "d1_error_c"]
EQ_FIELDS = ["time", "bot", "cash"]


def station_bias(st, icao):
    errs = st["bias"].get(icao, [])[-20:]
    if not errs:
        return 0.0
    return sum(errs) / len(errs) * len(errs) / (len(errs) + 5)   # shrink toward 0 while n is small


# ---------- one cycle ----------

def minutes_since(iso, now):
    if not iso:
        return 1e9
    return (now - datetime.fromisoformat(iso)).total_seconds() / 60


def run_cycle(get, now=None, force=False, log=print):
    now = now or datetime.now(timezone.utc)
    st = load_state()
    if not force and minutes_since(st.get("last_cycle"), now) < MIN_GAP_MIN:
        log("last cycle too recent; skipping")
        return st
    stamp = now.isoformat(timespec="minutes")

    # 1. events
    if minutes_since(st.get("last_discover"), now) >= DISCOVER_EVERY_MIN or not st["events"]:
        found = M.discover(get, now.date())
        for slug, ev in found.items():
            keep = st["events"].get(slug, {})
            ev.update({k: keep[k] for k in ("d1_median_c", "our_value_c", "outcome_logged") if k in keep})
            st["events"][slug] = ev
        st["last_discover"] = now.isoformat()
        log(f"discovered {len(found)} events")
    else:
        for slug in list(st["events"]):
            try:
                ev = M.refresh(get, slug)
            except Exception:
                continue
            if ev:
                keep = st["events"][slug]
                ev.update({k: keep[k] for k in ("d1_median_c", "our_value_c", "outcome_logged") if k in keep})
                st["events"][slug] = ev

    stations = sorted({ev["station"] for ev in st["events"].values()})

    # 2. observations (one call for every station)
    try:
        metars = O.fetch(get, stations)
    except Exception as e:
        log(f"METAR fetch failed: {e}")
        metars = {s: [] for s in stations}
    for icao, recs in metars.items():
        c = O.station_coords(recs)
        if c:
            st["stations"].setdefault(icao, {}).update({"lat": c[0], "lon": c[1]})

    # 3. forecasts (hourly per station; ensembles only update every 6h anyway)
    fcs = {}
    for icao in stations:
        info = st["stations"].get(icao) or {}
        if "lat" not in info:
            continue
        try:
            fcs[icao] = F.fetch(get, info["lat"], info["lon"])
            info["tz"] = fcs[icao]["timezone"]
        except Exception as e:
            log(f"forecast {icao} failed: {e}")

    # 4-5. model, log, bet
    taken = {(b["bot"], b["slug"]) for b in st["open_bets"]}
    for slug, ev in sorted(st["events"].items()):
        icao = ev["station"]
        tz = (st["stations"].get(icao) or {}).get("tz")
        fc = fcs.get(icao)
        if not tz or not fc:
            continue
        now_local = now.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
        day = O.day_summary(metars.get(icao, []), tz, ev["date"])
        pred = MD.predict(ev, fc, day, now_local, bias_c=station_bias(st, icao))
        if not pred:
            continue
        pred["lead"] = (datetime.fromisoformat(ev["date"]).date() - now_local.date()).days
        if pred["mode"] == "future" and pred["lead"] == 1:
            ev["d1_median_c"] = round(pred["median_c"], 2)
        if pred["mode"] == "past" and day["max_c"] is not None:
            ev["our_value_c"] = day["max_c"]

        if pred["mode"] == "past":
            continue   # day is over: just waiting on Polymarket's result

        edges = _edges(ev, pred)
        top =max(range(len(pred["probs"])), key=lambda i: pred["probs"][i])
        append(f"forecasts_{now:%Y-%m}.csv", {
            "time": stamp, "slug": slug, "station": icao, "date": ev["date"], "unit": ev["unit"],
            "mode": pred["mode"], "local_hour": now_local.hour, "obs_max": pred["obs_max"],
            "gap_c": pred["gap_c"], "median": round(pred["median"], 1), "p10": round(pred["p10"], 1),
            "p90": round(pred["p90"], 1), "top_bracket": ev["brackets"][top]["label"],
            "top_p": round(pred["probs"][top], 3), "top_ask": ev["brackets"][top]["ask"],
            "best_edge": round(edges[0][0], 3) if edges else "",
            "brackets": ";".join(f"{b['label']}={p:.3f}/{b['ask']}" for b, p in zip(ev["brackets"], pred["probs"])),
        }, FC_FIELDS)

        for name, rule in BOTS.items():
            bot = st["bots"].setdefault(name, {"cash": START, "retired": False})
            if bot["retired"] or (name, slug) in taken:
                continue
            for i, side, _, p in rule(ev, pred, now_local.hour):
                b = ev["brackets"][i]
                price, depth = best_ask(get, b["yes_token"] if side == "YES" else b["no_token"])
                if price is None or not 0 < price < 1:
                    continue
                edge = p - price - fee(price)
                if name != "Favorite" and edge < (0.15 if name == "Strict Nowcast" else 0.05 if name == "Late Closer" else 0.07):
                    continue   # the live book moved since the listing price
                stake = max(MIN_STAKE, bot["cash"] * STAKE_FRAC)
                if stake > bot["cash"] or depth < stake:
                    continue
                shares = stake / (price + fee(price))
                bot["cash"] -= stake
                bet = {"time": stamp, "bot": name, "slug": slug, "station": icao, "date": ev["date"],
                       "bracket": b["label"], "market_id": b["market_id"], "side": side,
                       "price": round(price, 4), "fee": round(fee(price) * shares, 4),
                       "shares": round(shares, 4), "stake": round(stake, 2), "p_model": round(p, 3),
                       "edge": round(edge, 3), "mode": pred["mode"], "local_hour": now_local.hour}
                st["open_bets"].append(bet)
                taken.add((name, slug))
                log(f"{name}: {side} {b['label']} @ {price:.2f} on {slug} (model {p:.2f}, edge {edge:+.2f})")
                break

    # 6. settle bets and log outcomes
    still = []
    for bet in st["open_bets"]:
        ev = st["events"].get(bet["slug"])
        b = next((x for x in (ev or {}).get("brackets", []) if x["market_id"] == bet["market_id"]), None)
        if not b or b["won"] is None:
            still.append(bet)
            continue
        win = b["won"] if bet["side"] == "YES" else not b["won"]
        payout = round(bet["shares"] if win else 0.0, 4)
        bot = st["bots"][bet["bot"]]
        bot["cash"] = round(bot["cash"] + payout, 4)
        if bot["cash"] < RETIRE_AT:
            bot["retired"] = True
        append("bets.csv", {**bet, "result": "win" if win else "loss", "payout": payout, "settled": stamp}, BET_FIELDS)
        append("equity.csv", {"time": stamp, "bot": bet["bot"], "cash": bot["cash"]}, EQ_FIELDS)
    st["open_bets"] = still

    for slug, ev in list(st["events"].items()):
        winners = [b["label"] for b in ev["brackets"] if b["won"] is True]
        if winners and not ev.get("outcome_logged"):
            ours_c = ev.get("our_value_c")
            ours = None if ours_c is None else O.settle_value(ours_c, ev["unit"])
            our_b = MD.settled_bracket(ev, ours_c)
            d1 = ev.get("d1_median_c")
            err = None if (d1 is None or ours_c is None) else round(ours_c - d1, 2)
            append("outcomes.csv", {
                "date": ev["date"], "slug": slug, "station": ev["station"], "unit": ev["unit"],
                "our_metar_value": ours, "our_bracket": our_b, "official_bracket": winners[0],
                "match": "" if our_b is None else our_b == winners[0], "d1_median_c": d1,
                "actual_c": ours_c, "d1_error_c": err}, OUT_FIELDS)
            if err is not None:
                st["bias"].setdefault(ev["station"], []).append(err)
                st["bias"][ev["station"]] = st["bias"][ev["station"]][-30:]
            ev["outcome_logged"] = True
        too_old = ev["date"] < (now.date() - timedelta(days=6)).isoformat()
        open_here = any(b["slug"] == slug for b in st["open_bets"])
        if (ev.get("outcome_logged") or too_old) and not open_here:
            del st["events"][slug]

    st["last_cycle"] = now.isoformat()
    save_state(st)
    log("bots: " + ", ".join(f"{n} ${b['cash']:.2f}{' (retired)' if b['retired'] else ''}" for n, b in st["bots"].items()))
    log(f"open bets: {len(st['open_bets'])}, tracked events: {len(st['events'])}")
    return st


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    run_cycle(http_get, force=ap.parse_args().force)
