"""The tournament's bots.

Every bot works the same way. `prepare(df)` adds its indicator columns to a
DataFrame of 15-minute candles (columns: open, high, low, close, volume).
`decide(row, meta)` looks at one finished candle and returns the share of
its money it wants in bitcoin, from 0.0 (all cash) to 1.0 (all in), or None
to keep things as they are. `meta` is a dict the bot can use to remember
things between candles (an entry price, how long it has held). It is saved
with the bot, so a restart picks up where it left off.

The same code runs in the backtest and in live paper mode.
"""

import numpy as np
import pandas as pd


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


class Strategy:
    name = "base"
    blurb = ""
    warmup = 0  # candles needed before the bot may trade

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def decide(self, row, meta: dict):
        raise NotImplementedError


class Benchmark(Strategy):
    name = "Benchmark"
    blurb = "Buys bitcoin once and holds it. The bot to beat."
    warmup = 1
    exempt_from_limits = True  # pure buy-and-hold: no loss limits, no retirement

    def decide(self, row, meta):
        return 1.0


class TrendRider(Strategy):
    name = "Trend Rider"
    blurb = "All in while the fast trend line (20 candles, ~5h) is above the slow one (80, ~20h) and price is above the slow line; out otherwise."
    warmup = 160

    def prepare(self, df):
        df["tr_fast"] = ema(df["close"], 20)
        df["tr_slow"] = ema(df["close"], 80)
        return df

    def decide(self, row, meta):
        up = row.tr_fast > row.tr_slow and row.close > row.tr_slow
        return 1.0 if up else 0.0


class Breakout(Strategy):
    name = "Breakout"
    blurb = "Buys when price closes above its 24-hour high; sells when it closes below its 12-hour low."
    warmup = 97

    def prepare(self, df):
        df["bo_high"] = df["high"].rolling(96).max().shift(1)
        df["bo_low"] = df["low"].rolling(48).min().shift(1)
        return df

    def decide(self, row, meta):
        if np.isnan(row.bo_high) or np.isnan(row.bo_low):
            return None
        if row.close > row.bo_high:
            return 1.0
        if row.close < row.bo_low:
            return 0.0
        return None


class DipHunter(Strategy):
    name = "Dip Hunter"
    blurb = "Buys after a 3%+ drop in 4 hours with RSI under 30; sells at +2.5%, a -4% stop, or after 24 hours."
    warmup = 30

    def __init__(self, drop=-0.03, rsi_max=30, take=0.025, stop=-0.04, max_bars=96):
        self.drop, self.rsi_max, self.take, self.stop, self.max_bars = drop, rsi_max, take, stop, max_bars

    def prepare(self, df):
        df["dh_ret"] = df["close"] / df["close"].shift(16) - 1
        df["dh_rsi"] = rsi(df["close"], 14)
        return df

    def decide(self, row, meta):
        entry = meta.get("entry")
        if entry:
            meta["held"] = meta.get("held", 0) + 1
            change = row.close / entry - 1
            if change >= self.take or change <= self.stop or meta["held"] >= self.max_bars:
                meta.pop("entry", None)
                meta.pop("held", None)
                return 0.0
            return None
        if row.dh_ret <= self.drop and row.dh_rsi < self.rsi_max:
            meta["entry"] = float(row.close)
            meta["held"] = 0
            return 1.0
        return None


class Grid(Strategy):
    name = "Grid"
    blurb = "Splits the last 7 days' price range into steps: holds more bitcoin near the bottom, less near the top. Goes to cash if price breaks 3% below the range."
    warmup = 673

    def __init__(self, lookback=672, steps=4, breakdown=0.03):
        self.lookback, self.steps, self.breakdown = lookback, steps, breakdown

    def prepare(self, df):
        df["gr_lo"] = df["low"].rolling(self.lookback).min().shift(1)
        df["gr_hi"] = df["high"].rolling(self.lookback).max().shift(1)
        return df

    def decide(self, row, meta):
        lo, hi = row.gr_lo, row.gr_hi
        if np.isnan(lo) or np.isnan(hi) or hi <= lo:
            return None
        if row.close < lo * (1 - self.breakdown):
            return 0.0
        frac = (hi - row.close) / (hi - lo)
        frac = min(1.0, max(0.0, frac))
        return round(frac * self.steps) / self.steps


def roster():
    """The bots entered in the tournament, in leaderboard order."""
    return [Benchmark(), TrendRider(), Breakout(), DipHunter(), Grid()]
