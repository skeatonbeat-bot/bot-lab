"""Replay past bitcoin prices through every bot and print a leaderboard.

    python backtest.py data/btcusd_1min.csv
    python backtest.py data/btcusd_1min.csv --fee 0.004 --start 2026-01-01

Each bot starts with $100 of paper money. Signals come from a finished
15-minute candle and fill at the next candle's open, so no bot sees the
future. Every buy and sell pays the fee plus slippage.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from lab.data import load_minutes, to_candles
from lab.engine import Bot, Costs
from lab.strategies import roster


def run(candles, start=None, end=None, costs=None, cash=100.0, retire_at=0.60, daily_loss=0.10):
    df = candles.copy()
    bots = [Bot(s, cash=cash, costs=costs, retire_at=retire_at, daily_loss=daily_loss) for s in roster()]
    for b in bots:
        df = b.strategy.prepare(df)
    # indicators are computed on all the data, so a late start is already warmed up
    if start or end:
        df = df.loc[start:end]
        for b in bots:
            b.candles_seen = 10_000
    idx = df.index
    opens = df["open"].to_numpy()
    curves = {b.name: np.empty(len(df)) for b in bots}
    for i, row in enumerate(df.itertuples()):
        last = i == len(df) - 1
        fill = row.close if last else opens[i + 1]
        for b in bots:
            b.on_candle(row, fill, idx[i])
            curves[b.name][i] = b.equity(row.close)
    return bots, pd.DataFrame(curves, index=idx)


def max_drawdown(curve):
    peak = curve.cummax()
    return float((curve / peak - 1).min())


def leaderboard(bots, curves):
    rows = []
    for b in bots:
        c = curves[b.name]
        trips = b.round_trips
        wins = sum(1 for t in trips if t["pnl"] > 0)
        rows.append({
            "bot": b.name,
            "final $": round(float(c.iloc[-1]), 2),
            "return %": round((float(c.iloc[-1]) / b.start_cash - 1) * 100, 1),
            "worst drop %": round(max_drawdown(c) * 100, 1),
            "trades": len(trips),
            "win rate %": round(wins / len(trips) * 100, 0) if trips else None,
            "fees $": round(b.fees_paid, 2),
            "retired": b.retired,
        })
    out = pd.DataFrame(rows).sort_values("return %", ascending=False).reset_index(drop=True)
    out.index = out.index + 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("minutes_csv")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--fee", type=float, default=0.006)
    ap.add_argument("--candle", default="15min", help="candle size: 15min, 1h, 4h ...")
    ap.add_argument("--retire-at", type=float, default=0.60, help="retire a bot at this share of its starting money; 0 turns it off")
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    candles = to_candles(load_minutes(args.minutes_csv), args.candle)
    bots, curves = run(candles, args.start, args.end, Costs(fee=args.fee), retire_at=args.retire_at)
    board = leaderboard(bots, curves)
    print(f"\n{curves.index[0]:%Y-%m-%d} to {curves.index[-1]:%Y-%m-%d}, fee {args.fee:.2%} per buy or sell\n")
    print(board.to_string())

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    board.to_csv(out / "leaderboard.csv")
    curves.resample("1h").last().to_csv(out / "equity_hourly.csv")
    fills = [f for b in bots for f in b.fills]
    pd.DataFrame(fills).to_csv(out / "fills.csv", index=False)
    print(f"\nSaved leaderboard, hourly equity and every fill to {out}/")


if __name__ == "__main__":
    main()
