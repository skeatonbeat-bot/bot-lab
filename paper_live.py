"""Run the tournament live with paper money, on real Coinbase prices.

    python paper_live.py --candle 4h          # the Slow League
    python paper_live.py --candle 15min       # the Speed League (run in a second window)

It reads public Coinbase prices (no account or API key needed), lets every
bot decide when a candle finishes, and pretends to fill at the latest price
with fees and slippage. Nothing real is bought or sold.

Everything is saved in the `live/` folder, one set of files per league:
  state_<league>.json   each bot's cash, bitcoin and memory (so you can stop and restart)
  fills_<league>.csv    every pretend buy and sell
  equity_<league>.csv   each bot's money after every candle
Stop it any time with Ctrl+C; start it again and it carries on.

Test it without the internet by replaying saved prices:
    python paper_live.py --candle 4h --replay data/btcusd_1min.csv --replay-from 2026-09-01
"""

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from lab.engine import Bot, Costs
from lab.strategies import roster

API = "https://api.exchange.coinbase.com"
PRODUCT = "BTC-USD"
OUT = Path("live")


# ---------- prices ----------

def _get(url, params=None, tries=4):
    import requests
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=20, headers={"User-Agent": "bot-lab/1.0"})
            if r.status_code == 429:
                time.sleep(2 + i * 2)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # network blips: wait and retry
            if i == tries - 1:
                raise
            print(f"  price fetch failed ({e}); retrying...")
            time.sleep(3 + i * 3)


def fetch_candles(granularity, count):
    """The last `count` candles of `granularity` seconds from Coinbase, oldest first."""
    rows = []
    end = datetime.now(timezone.utc)
    while len(rows) < count:
        start = end - timedelta(seconds=granularity * 300)
        page = _get(f"{API}/products/{PRODUCT}/candles",
                    {"granularity": granularity, "start": start.isoformat(), "end": end.isoformat()})
        if not page:
            break
        rows.extend(page)
        end = start
        time.sleep(0.35)  # stay well under Coinbase's rate limit
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"]).drop_duplicates("t")
    df["time"] = pd.to_datetime(df["t"], unit="s", utc=True)
    return df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()


def fetch_price():
    return float(_get(f"{API}/products/{PRODUCT}/ticker")["price"])


SIZES = {"15min": (900, "15min", pd.Timedelta("15min")),
         "1h": (3600, "1h", pd.Timedelta("1h")),
         "4h": (3600, "4h", pd.Timedelta("4h"))}  # Coinbase has no 4h candles, so 1h ones are rolled up


def closed_candles(candle, need, now=None):
    gran, rule, length = SIZES[candle]
    per = int(length / pd.Timedelta(seconds=gran))
    raw = fetch_candles(gran, need * per + per)
    if per > 1:
        raw = raw.resample(rule, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    now = now or pd.Timestamp.now(tz="UTC")
    return raw[raw.index + length <= now]  # drop the candle that's still forming


def fill_at(candles, price):
    """Backlog candles fill at the next candle's open; the newest one at the live price."""
    opens = candles["open"]
    def f(ts):
        later = opens[opens.index > ts]
        return float(later.iloc[0]) if len(later) else price
    return f


# ---------- the tournament ----------

class League:
    def __init__(self, name, fee, cash):
        self.name = name
        self.state_path = OUT / f"state_{name}.json"
        self.fills_path = OUT / f"fills_{name}.csv"
        self.equity_path = OUT / f"equity_{name}.csv"
        self.bots = [Bot(s, cash=cash, costs=Costs(fee=fee)) for s in roster()]
        self.last = None  # time of the last candle handled
        self.warm = max(s.warmup for s in roster()) + 5

    def load(self):
        if not self.state_path.exists():
            return False
        s = json.loads(self.state_path.read_text())
        self.last = pd.Timestamp(s["last"])
        by_name = {b["name"]: b for b in s["bots"]}
        for b in self.bots:
            if b.name in by_name:
                b.load_state(by_name[b.name])
        return True

    def save(self):
        OUT.mkdir(exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last": str(self.last), "bots": [b.to_state() for b in self.bots]}, indent=1))
        tmp.replace(self.state_path)

    def start_fresh(self, candles):
        """New league: bots see the history but trade only from the next candle on."""
        self.last = candles.index[-1]
        for b in self.bots:
            b.candles_seen = 10_000
        self.save()

    def handle(self, candles, fill_price_for):
        """Feed every finished candle after `self.last` to every bot."""
        new = candles[candles.index > self.last]
        for ts in new.index:
            df = candles.loc[:ts].tail(self.warm + 50).copy()
            for b in self.bots:
                df = b.strategy.prepare(df)
            row = next(df.tail(1).itertuples())
            fill = fill_price_for(ts)
            before = {b.name: len(b.fills) for b in self.bots}
            for b in self.bots:
                b.on_candle(row, fill, ts)
            self.last = ts
            self._log(ts, float(row.close), before)
            self.save()
        return len(new)

    def _log(self, ts, price, before):
        OUT.mkdir(exist_ok=True)
        new_fills = [f for b in self.bots for f in b.fills[before[b.name]:]]
        if new_fills:
            write_head = not self.fills_path.exists()
            with self.fills_path.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(new_fills[0].keys()))
                if write_head:
                    w.writeheader()
                for f in new_fills:
                    w.writerow({**f, "time": str(f["time"])})
        write_head = not self.equity_path.exists()
        with self.equity_path.open("a", newline="") as fh:
            w = csv.writer(fh)
            if write_head:
                w.writerow(["time", "btc_price"] + [b.name for b in self.bots])
            w.writerow([str(ts), round(price, 2)] + [round(b.equity(price), 2) for b in self.bots])
        for f in new_fills:
            print(f"  {f['bot']}: {f['side']} ${f['dollars']:.2f} at ${f['price']:,.2f} ({f['reason']})")

    def board(self, price):
        rows = sorted(((b.equity(price), b) for b in self.bots), key=lambda x: -x[0])
        lines = []
        for i, (eq, b) in enumerate(rows, 1):
            pos = "in BTC" if b.units > 0 else "cash"
            flag = " (retired)" if b.retired else ""
            lines.append(f"  {i}. {b.name:<12} ${eq:8.2f}  {eq / b.start_cash * 100 - 100:+6.1f}%  {pos}{flag}")
        return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candle", choices=list(SIZES), default="4h")
    ap.add_argument("--fee", type=float, default=0.006)
    ap.add_argument("--cash", type=float, default=100.0)
    ap.add_argument("--poll", type=int, default=60, help="seconds between price checks")
    ap.add_argument("--replay", help="a 1-minute CSV to replay instead of live prices (for testing)")
    ap.add_argument("--replay-from", help="first date to replay, e.g. 2026-09-01")
    ap.add_argument("--once", action="store_true", help="check once and exit (for scheduled cloud runs)")
    args = ap.parse_args()

    name = args.candle + ("_replay" if args.replay else "")
    league = League(name, args.fee, args.cash)

    if args.replay:
        from lab.data import load_minutes, to_candles
        candles = to_candles(load_minutes(args.replay), SIZES[args.candle][1])
        start = pd.Timestamp(args.replay_from, tz="UTC") if args.replay_from else candles.index[len(candles) // 2]
        if not league.load():
            league.start_fresh(candles[candles.index < start])
        opens = candles["open"]
        nxt = lambda ts: float(opens[opens.index > ts].iloc[0]) if (opens.index > ts).any() else float(candles.loc[ts, "close"])
        n = league.handle(candles, nxt)
        price = float(candles["close"].iloc[-1])
        print(f"Replayed {n} candles to {candles.index[-1]:%Y-%m-%d %H:%M} UTC. BTC ${price:,.2f}\n{league.board(price)}")
        return

    if args.once:
        resumed = league.load()
        candles = closed_candles(args.candle, league.warm)
        if not resumed:
            league.start_fresh(candles)
            print(f"New {args.candle} league started at {league.last:%Y-%m-%d %H:%M} UTC. Bots trade from the next candle.")
            return
        price = fetch_price()
        n = league.handle(candles, fill_at(candles, price))
        print(f"{args.candle} league | BTC ${price:,.2f} | {n} new candle(s)\n{league.board(price)}")
        return

    print(f"Bot tournament, {args.candle} league, paper money only. Ctrl+C to stop.")
    resumed = league.load()
    length = SIZES[args.candle][2]
    while True:
        try:
            # only download history once a new candle has had time to finish
            due = league.last + 2 * length + pd.Timedelta(seconds=20) if resumed else None
            if due is not None and pd.Timestamp.now(tz="UTC") < due:
                time.sleep(args.poll)
                continue
            candles = closed_candles(args.candle, league.warm)
            if not resumed:
                league.start_fresh(candles)
                resumed = True
                print(f"New league started at {league.last:%Y-%m-%d %H:%M} UTC. Bots trade from the next candle.")
            price = fetch_price()
            n = league.handle(candles, fill_at(candles, price))
            if n:
                print(f"\n{datetime.now():%b %d %H:%M} | BTC ${price:,.2f} | {n} new candle(s)\n{league.board(price)}")
        except KeyboardInterrupt:
            print("\nStopped. Everything is saved; run the same command to carry on.")
            sys.exit(0)
        except Exception as e:
            print(f"{datetime.now():%H:%M} problem: {e}. Trying again shortly.")
        try:
            time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\nStopped. Everything is saved; run the same command to carry on.")
            sys.exit(0)


if __name__ == "__main__":
    main()
