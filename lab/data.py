"""Loading candles."""

import pandas as pd


def load_minutes(path):
    """A CSV of 1-minute candles: timestamp (unix seconds), open, high, low, close, volume."""
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    return df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()


def to_candles(minutes, rule="15min"):
    """Roll 1-minute candles up into longer ones (15-minute by default)."""
    out = minutes.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["close"])
