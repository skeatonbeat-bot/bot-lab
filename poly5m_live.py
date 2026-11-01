"""The 5-minute league: paper bets on Polymarket's 5-minute "Up or Down" markets.

    python poly5m_live.py                 # all coins, settles every finished window since the last run
    python poly5m_live.py --coin ETH      # one coin

Every 5 minutes Polymarket opens a market per coin: Up wins if the price at the end
of the window is at or above the price at the start. Five bots get $100 of pretend
money each and decide every window. A bot may only use what it could have known at
its decision time: the coin's Coinbase 1-minute prices and Polymarket's price for Up
at that minute. It "buys" at that price plus 1 cent (Polymarket's minute history is
a mid price, not a quote), pays the crypto taker fee (7% x p x (1-p) per share), and
wins $1 a share if its side is right.

It runs after the windows close (the tournament workflow calls it every 15 minutes),
replaying each finished window with only the information available at the time.
Nothing real is bought or sold.

Files in live/ (BTC keeps the plain name, other coins add theirs):
  state_5min[_COIN].json   bankrolls and the last window handled
  bets_5min[_COIN].csv     every pretend bet and how it settled
  equity_5min[_COIN].csv   each bot's money after every window
"""

import argparse
import csv
import json
import math
import time
from pathlib import Path

import requests

OUT = Path("live")
COINS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
FEE_RATE = 0.07
SPREAD = 0.01        # assumed half-spread over the minute mid
STAKE_FRAC = 0.02    # each bet risks 2% of the bot's money...
MIN_STAKE = 1.0      # ...but at least $1
RETIRE_AT = 60.0
MAX_BACKLOG = 36     # windows (3 hours) to catch up after a gap; older ones are skipped
S = requests.Session()
S.headers["User-Agent"] = "bot-lab/1.0"


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 + 2 * i)


def fee(p):
    return FEE_RATE * p * (1 - p)


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---------- data for one window ----------

def coinbase_minutes(product, t0, t1):
    out = {}
    s = t0
    while s < t1:
        e = min(s + 300 * 60, t1)
        page = get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                   {"granularity": 60, "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s)),
                    "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e))})
        for t, lo, hi, op, cl, v in page or []:
            out[int(t)] = (float(op), float(cl))
        s = e
        time.sleep(0.2)
    return out


def window(coin, start):
    """Outcome and the Up price by minute for one finished window, or None if not settled."""
    ms = get("https://gamma-api.polymarket.com/markets", {"slug": f"{coin.lower()}-updown-5m-{start}"})
    if not ms:
        return None
    m = ms[0]
    outs = [o.lower() for o in json.loads(m.get("outcomes") or "[]")]
    px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    toks = json.loads(m.get("clobTokenIds") or "[]")
    if "up" not in outs or len(px) < 2 or max(px) < 0.99 or not toks:
        return None
    i = outs.index("up")
    try:
        h = get("https://clob.polymarket.com/prices-history",
                {"market": toks[i], "startTs": start - 60, "endTs": start + 300, "fidelity": 1})
        hist = sorted((int(x["t"]), float(x["p"])) for x in h.get("history", []))
    except Exception:
        hist = []
    return {"up": px[i] > 0.5, "hist": hist, "slug": m.get("slug")}


def up_price_at(hist, t):
    seen = [p for tt, p in hist if tt <= t]
    return seen[-1] if seen else None


# ---------- the bots ----------
# Each returns (side, seconds_in) or None. side True = Up.
# `x` holds what's known: open price s0, closes by minute, last window's result,
# 2-hour minute volatility, BTC's move so far (for the other coins), and the Up price by minute.

def coin_flip(x):
    return True, 5                                   # benchmark: always Up at the open


def momentum(x):
    return (x["prev_up"], 5) if x["prev_up"] is not None else None


def late_favorite(x):
    p = x["up_at"](240)
    if p is None:
        return None
    if 0.70 <= p <= 0.92:
        return True, 240
    if 0.08 <= p <= 0.30:
        return False, 240
    return None


def model_edge(x, off=180, need=0.05):
    s, s0, v = x["close_at"](off), x["s0"], x["vol"]
    p = x["up_at"](off)
    if None in (s, s0, v, p) or v <= 0:
        return None
    mp = ncdf(math.log(s / s0) / (v * math.sqrt((300 - off) / 60)))
    ua, da = p + SPREAD, 1 - p + SPREAD
    if mp - ua - fee(ua) >= need:
        return True, off
    if (1 - mp) - da - fee(da) >= need:
        return False, off
    return None


def btc_leader(x, off=120):
    m, p = x["btc_move"](off), x["up_at"](off)
    if m is None or p is None:
        return None
    if m > 0.0004 and p < 0.60:
        return True, off
    if m < -0.0004 and p > 0.40:
        return False, off
    return None


BOTS = {"Coin Flip": coin_flip, "Momentum": momentum, "Late Favorite": late_favorite,
        "Model Edge": model_edge, "BTC Leader": btc_leader}


# ---------- the league ----------

def names(coin):
    tag = "5min" if coin == "BTC" else f"5min_{coin}"
    return OUT / f"state_{tag}.json", OUT / f"bets_{tag}.csv", OUT / f"equity_{tag}.csv"


def run_coin(coin, now, btc_cache):
    state_p, bets_p, eq_p = names(coin)
    last_closed = (now - 30) // 300 * 300 - 300       # newest window that has finished (+30s to settle)
    if state_p.exists():
        st = json.loads(state_p.read_text())
    else:
        st = {"last": last_closed, "prev_up": None, "bots": {b: {"cash": 100.0, "retired": False} for b in BOTS}}
        OUT.mkdir(exist_ok=True)
        state_p.write_text(json.dumps(st, indent=1))
        print(f"{coin}: new 5-minute league, bets from the next window")
        return
    starts = [s for s in range(st["last"] + 300, last_closed + 1, 300)][-MAX_BACKLOG:]
    if not starts:
        print(f"{coin}: nothing new")
        return
    mins = coinbase_minutes(COINS[coin], starts[0] - 2 * 3600 - 60, starts[-1] + 300)
    if coin != "BTC" and not btc_cache:
        btc_cache.update(coinbase_minutes(COINS["BTC"], starts[0] - 60, starts[-1] + 300))

    bet_rows, eq_rows = [], []
    for start in starts:
        w = window(coin, start)
        if w is None:
            print(f"{coin} {start}: not settled yet, will retry")
            break
        s0 = mins.get(start, (None,))[0]
        lr = [math.log(mins[t][1] / mins[t - 60][1]) for t in range(start - 7200, start - 60 + 1, 60)
              if t in mins and t - 60 in mins]
        vol = math.sqrt(sum(r * r for r in lr) / len(lr)) if len(lr) >= 60 else None
        b0 = btc_cache.get(start, (None,))[0] if coin != "BTC" else None

        def close_at(off, m=mins):
            return m.get(start + off - 60, (None, None))[1]

        def btc_move(off):
            if coin == "BTC" or not b0:
                return None
            c = btc_cache.get(start + off - 60, (None, None))[1]
            return math.log(c / b0) if c else None

        x = {"s0": s0, "vol": vol, "prev_up": st["prev_up"], "close_at": close_at, "btc_move": btc_move,
             "up_at": lambda off: up_price_at(w["hist"], start + off)}
        for name, fn in BOTS.items():
            b = st["bots"].setdefault(name, {"cash": 100.0, "retired": False})
            if b["retired"]:
                continue
            pick = fn(x)
            if pick is None:
                continue
            side, off = pick
            p_up = x["up_at"](off)
            if p_up is None:
                p_up = 0.5 if off <= 5 else None   # no trade printed yet at the open: a fair coin
            if p_up is None:
                continue
            price = min(0.99, (p_up if side else 1 - p_up) + SPREAD)
            stake = max(MIN_STAKE, STAKE_FRAC * b["cash"])
            if stake > b["cash"]:
                continue
            shares = stake / (price + fee(price))
            won = side == w["up"]
            pnl = (shares if won else 0.0) - stake
            b["cash"] += pnl
            if b["cash"] <= RETIRE_AT:
                b["retired"] = True
            bet_rows.append({"window": time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)), "slug": w["slug"], "bot": name,
                             "side": "Up" if side else "Down", "seconds_in": off, "price": round(price, 3),
                             "stake": round(stake, 2), "result": "won" if won else "lost", "pnl": round(pnl, 2),
                             "cash_after": round(b["cash"], 2)})
        st["prev_up"] = w["up"]
        st["last"] = start
        eq_rows.append([time.strftime("%Y-%m-%d %H:%M:%S+00:00", time.gmtime(start)), round(s0, 6) if s0 else ""] +
                       [round(st["bots"][n]["cash"], 2) for n in BOTS])

    if bet_rows:
        head = not bets_p.exists()
        with bets_p.open("a", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=list(bet_rows[0]))
            if head:
                wr.writeheader()
            wr.writerows(bet_rows)
    if eq_rows:
        head = not eq_p.exists()
        with eq_p.open("a", newline="") as fh:
            wr = csv.writer(fh)
            if head:
                wr.writerow(["time", f"{coin.lower()}_price"] + list(BOTS))
            wr.writerows(eq_rows)
    state_p.write_text(json.dumps(st, indent=1))
    board = sorted(((v["cash"], k) for k, v in st["bots"].items()), reverse=True)
    print(f"{coin}: settled {len(eq_rows)} window(s), {len(bet_rows)} bet(s) | " +
          " | ".join(f"{k} ${c:.2f}" for c, k in board))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", help="BTC, ETH, SOL, XRP or DOGE (default all)")
    a = ap.parse_args()
    now = int(time.time())
    btc_cache = {}
    for coin in ([a.coin.upper()] if a.coin else list(COINS)):
        try:
            run_coin(coin, now, btc_cache)
        except Exception as e:
            print(f"::warning::5min {coin} failed: {e}")


if __name__ == "__main__":
    main()
