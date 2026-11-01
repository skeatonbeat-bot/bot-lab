"""The 5-minute league: paper bets on Polymarket's 5-minute "Up or Down" markets,
decided on LIVE order-book quotes.

    python poly5m_live.py --minutes 12      # watch the markets for 12 minutes (what the workflow does)
    python poly5m_live.py --settle-only     # just settle finished bets

Every 5 minutes Polymarket opens a market per coin: Up wins if the price at the end
of the window is at or above the price at the start. Five bots get $100 of pretend
money each. While this runs it reads the real best bid/ask for Up and Down every few
seconds; when a bot's decision moment arrives it "buys" at the real ask, pays the
crypto taker fee (7% x p x (1-p) per share), and wins $1 a share if its side is right.
Bets settle from Polymarket's own result once the window closes.

Why live quotes: Polymarket's minute price history is often minutes old, so replaying
it after the fact lets a bot "buy" at a stale price after the move already happened
and shows fake profits. Only quotes seen at the moment count here.

The roster comes from the spring 2026 history (results/poly5m/summary.md), where the
only thing that beat the fee was buying the late favorite:

  Coin Flip        always Up at the open (the one to beat)
  Favorite 3m      at 3:00 buys whichever side costs 70-92c
  Favorite 4m      same at 4:00
  Favorite 4.5m    same at 4:30
  Follow Move 4m   at 4:00 buys the side the coin has moved toward since the open

Files in live/ (BTC keeps the plain name, other coins add theirs):
  state_5min[_COIN].json   bankrolls and bets waiting to settle
  bets_5min[_COIN].csv     every pretend bet and how it settled
  equity_5min[_COIN].csv   each bot's money after every settled window
Nothing real is bought or sold.
"""

import argparse
import csv
import json
import time
from pathlib import Path

import requests

OUT = Path("live")
COINS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "DOGE": "DOGE-USD"}
FEE_RATE = 0.07
STAKE_FRAC = 0.02    # each bet risks 2% of the bot's money...
MIN_STAKE = 1.0      # ...but at least $1
RETIRE_AT = 60.0
POLL = 4             # seconds between quote checks
STALE = 12           # a quote older than this many seconds doesn't count
GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB = "https://clob.polymarket.com"
S = requests.Session()
S.headers["User-Agent"] = "bot-lab/1.0"


def get(url, params=None, tries=3, timeout=8):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(1 + i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def fee(p):
    return FEE_RATE * p * (1 - p)


# ---------- the bots: (decision second, rule) ----------
# A rule sees q = {"up_mid","up_bid","up_ask","down_bid","down_ask"} (0-1, None if that
# side of the book is empty), the coin's open price s0
# and its live price now; it returns True (Up), False (Down) or None (no bet).

def favorite(q, s0, now):
    mid = q["up_mid"]
    if 0.70 <= mid <= 0.92:
        return True
    if 0.08 <= mid <= 0.30:
        return False
    return None


def follow_move(q, s0, now):
    if not s0 or not now or now == s0:
        return None
    return now > s0


BOTS = {
    "Coin Flip": (5, lambda q, s0, now: True),
    "Favorite 3m": (180, favorite),
    "Favorite 4m": (240, favorite),
    "Favorite 4.5m": (270, favorite),
    "Follow Move 4m": (240, follow_move),
}


# ---------- market data ----------

def market(coin, start, closed=False):
    slug = f"{coin.lower()}-updown-5m-{start}"
    params = {"slug": slug, **({"closed": "true"} if closed else {})}
    ms = get(GAMMA, params)
    if not ms and closed:
        ms = get(GAMMA, {"slug": slug})
    return ms[0] if ms else None


def tokens(m):
    outs = [o.lower() for o in json.loads(m.get("outcomes") or "[]")]
    toks = json.loads(m.get("clobTokenIds") or "[]")
    if "up" not in outs or "down" not in outs or len(toks) < 2:
        return None
    return toks[outs.index("up")], toks[outs.index("down")]


def best(token):
    b = get(f"{CLOB}/book", {"token_id": token}, tries=2)
    bids = [float(x["price"]) for x in b.get("bids", [])]
    asks = [float(x["price"]) for x in b.get("asks", [])]
    return (max(bids) if bids else None), (min(asks) if asks else None)


def spot(product):
    return float(get(f"https://api.exchange.coinbase.com/products/{product}/ticker")["price"])


def result(coin, start):
    """True if Up won, False if Down, None if not settled yet."""
    m = market(coin, start, closed=True)
    if not m:
        return None
    outs = [o.lower() for o in json.loads(m.get("outcomes") or "[]")]
    px = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
    if "up" not in outs or len(px) < 2 or max(px) < 0.99:
        return None
    return px[outs.index("up")] > 0.5


# ---------- state ----------

def paths(coin):
    tag = "5min" if coin == "BTC" else f"5min_{coin}"
    return OUT / f"state_{tag}.json", OUT / f"bets_{tag}.csv", OUT / f"equity_{tag}.csv"


def load(coin):
    p = paths(coin)[0]
    st = json.loads(p.read_text()) if p.exists() else {}
    if "pending" not in st:  # new league, or one from the old replay version: start clean
        st = {"pending": [], "bots": {}}
    for b in BOTS:
        st["bots"].setdefault(b, {"cash": 100.0, "retired": False})
    st["bots"] = {k: v for k, v in st["bots"].items() if k in BOTS}
    return st


def save(coin, st):
    OUT.mkdir(exist_ok=True)
    p = paths(coin)[0]
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(p)


def append(path, rows, header):
    if not rows:
        return
    new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(header)
        w.writerows(rows)


def settle(coin, st, now):
    """Settle every pending bet whose window has closed and resolved."""
    _, bets_p, eq_p = paths(coin)
    by_window = {}
    for bet in st["pending"]:
        by_window.setdefault(bet["start"], []).append(bet)
    keep, bet_rows, eq_rows = [], [], []
    for start in sorted(by_window):
        bets = by_window[start]
        if now < start + 330:
            keep += bets
            continue
        try:
            up = result(coin, start)
        except Exception:
            up = None
        if up is None:
            if now > start + 6 * 3600:
                continue  # never resolved: drop (stake was never taken)
            keep += bets
            continue
        for bet in bets:
            b = st["bots"][bet["bot"]]
            won = bet["up"] == up
            pnl = (bet["shares"] if won else 0.0) - bet["stake"]
            b["cash"] += pnl
            if b["cash"] <= RETIRE_AT:
                b["retired"] = True
            bet_rows.append([time.strftime("%Y-%m-%d %H:%M", time.gmtime(start)), bet["slug"], bet["bot"],
                             "Up" if bet["up"] else "Down", bet["sec"], bet["ask"], round(bet["stake"], 2),
                             "won" if won else "lost", round(pnl, 2), round(b["cash"], 2)])
        eq_rows.append([time.strftime("%Y-%m-%d %H:%M:%S+00:00", time.gmtime(start)), bets[0].get("s0") or ""] +
                       [round(st["bots"][n]["cash"], 2) for n in BOTS])
    st["pending"] = keep
    append(bets_p, bet_rows, ["window", "slug", "bot", "side", "seconds_in", "ask", "stake", "result", "pnl", "cash_after"])
    append(eq_p, eq_rows, ["time", f"{coin.lower()}_price"] + list(BOTS))
    return len(bet_rows)


# ---------- watching live ----------

def watch(coins, minutes):
    states = {c: load(c) for c in coins}
    stop = time.time() + minutes * 60
    seen = {}     # (coin, start) -> {"toks":..., "s0":..., "done": set(bot)}
    while time.time() < stop:
        t = time.time()
        start = int(t) // 300 * 300
        sec = t - start
        for coin in coins:
            key = (coin, start)
            w = seen.get(key)
            if w is None:
                try:
                    m = market(coin, start)
                    tk = tokens(m) if m else None
                except Exception:
                    tk = None
                w = seen[key] = {"toks": tk, "slug": f"{coin.lower()}-updown-5m-{start}",
                                 # the open price only counts if we saw it in the first 20 seconds
                                 "s0": None, "done": set(), "late": sec > 20}
            if not w["toks"]:
                continue
            due = [n for n, (at, _) in BOTS.items() if n not in w["done"] and at <= sec < at + 25]
            need_s0 = w["s0"] is None and not w["late"] and sec <= 20
            if not due and not need_s0:
                continue
            try:
                px = spot(COINS[coin])
            except Exception as e:
                if due:
                    print(f"{coin} {int(sec)}s: price check failed ({str(e)[:80]}), retrying")
                continue
            if need_s0:
                w["s0"] = px
            if not due:
                continue
            try:
                qt = time.time()
                ub, ua = best(w["toks"][0])
                db, da = best(w["toks"][1])
            except Exception as e:
                print(f"{coin} {int(sec)}s: order book failed ({str(e)[:80]}), retrying")
                continue
            if time.time() - qt > STALE:
                continue
            # Late in a window the book is often one-sided (the winner at 99c, nobody selling).
            # Read the Up price from whichever quotes exist; a bot can only buy a side that has an ask.
            ups = [x for x in (ub, ua, None if da is None else 1 - da, None if db is None else 1 - db) if x is not None]
            if not ups:
                continue
            up_mid = (ub + ua) / 2 if ub is not None and ua is not None else sum(ups) / len(ups)
            q = {"up_bid": ub, "up_ask": ua, "down_bid": db, "down_ask": da, "up_mid": up_mid}
            st = states[coin]
            for n in due:
                w["done"].add(n)
                b = st["bots"][n]
                if b["retired"]:
                    continue
                side = BOTS[n][1](q, w["s0"], px)
                if side is None:
                    print(f"{coin} {int(sec)}s {n}: no bet (Up ~{up_mid:.2f}{'' if w['s0'] else ', joined mid-window'})")
                    continue
                ask = ua if side else da
                if ask is None or not 0.01 < ask < 0.99:
                    print(f"{coin} {int(sec)}s {n}: wanted {'Up' if side else 'Down'} but no seller below 99c")
                    continue
                stake = max(MIN_STAKE, STAKE_FRAC * b["cash"])
                committed = sum(x["stake"] for x in st["pending"] if x["bot"] == n)
                if stake > b["cash"] - committed:
                    continue
                st["pending"].append({"start": start, "slug": w["slug"], "bot": n, "up": side, "sec": int(sec),
                                      "ask": ask, "stake": stake, "shares": stake / (ask + fee(ask)),
                                      "s0": w["s0"]})
                print(f"{coin} {time.strftime('%H:%M:%S', time.gmtime())} {n}: {'Up' if side else 'Down'} at {ask:.2f}")
            save(coin, st)
        # settle once a minute, and forget windows we're done with
        if int(t) % 60 < POLL:
            for coin in coins:
                if settle(coin, states[coin], int(t)):
                    save(coin, states[coin])
            for k in [k for k in seen if k[1] < start - 300]:
                del seen[k]
        time.sleep(max(0.5, POLL - (time.time() - t)))
    for coin in coins:
        settle(coin, states[coin], int(time.time()))
        save(coin, states[coin])
        board = sorted(((v["cash"], k) for k, v in states[coin]["bots"].items()), reverse=True)
        print(f"{coin}: {len(states[coin]['pending'])} bet(s) waiting | " + " | ".join(f"{k} ${c:.2f}" for c, k in board))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coin", help="BTC, ETH, SOL, XRP or DOGE (default all)")
    ap.add_argument("--minutes", type=float, default=12, help="how long to watch live quotes")
    ap.add_argument("--settle-only", action="store_true")
    a = ap.parse_args()
    coins = [a.coin.upper()] if a.coin else list(COINS)
    if a.settle_only:
        for c in coins:
            st = load(c)
            n = settle(c, st, int(time.time()))
            save(c, st)
            print(f"{c}: settled {n} bet(s)")
        return
    watch(coins, a.minutes)


if __name__ == "__main__":
    main()
