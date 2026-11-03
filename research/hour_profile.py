"""Hour-of-day volatility profile for crypto_fair_value (connector/src/scan.js HOUR_PROFILE_ET).

    python research/hour_profile.py [days]     # default 60

Crypto swings far more around the US open (8-11am New York) than overnight. This measures it:
for BTC and ETH hourly Coinbase candles over the last `days` days, the root of each New York
hour's mean squared log return over the all-hours mean (each hour winsorised at its 97.5th
percentile so one crash can't own an hour), averaged across the two coins, smoothed with
0.25/0.5/0.25 neighbour weights and normalised so the 24 hours average to 1. Paste the printed
array into HOUR_PROFILE_ET. Hours are New York local time, so the profile follows daylight saving.
"""

import json
import math
import sys
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
COINS = ["BTC-USD", "ETH-USD"]


def candles(product, days):
    """Hourly [time, low, high, open, close, volume] rows, oldest first (Coinbase pages 300 at a time)."""
    out, end = {}, int(time.time()) // 3600 * 3600
    start = end - days * 86400
    while end > start:
        lo = max(start, end - 300 * 3600)
        url = (f"https://api.exchange.coinbase.com/products/{product}/candles?granularity=3600"
               f"&start={datetime.utcfromtimestamp(lo).isoformat()}Z&end={datetime.utcfromtimestamp(end).isoformat()}Z")
        req = urllib.request.Request(url, headers={"User-Agent": "bot-lab-research"})
        for row in json.load(urllib.request.urlopen(req, timeout=30)):
            out[int(row[0])] = row
        end = lo
        time.sleep(0.4)
    return [out[t] for t in sorted(out)]


def profile(rows):
    sq = {h: [] for h in range(24)}
    for a, b in zip(rows, rows[1:]):
        if b[0] - a[0] != 3600:
            continue
        r = math.log(b[4] / a[4])
        sq[datetime.fromtimestamp(b[0], ET).hour].append(r * r)
    var = {}
    for h, v in sq.items():
        v = sorted(v)
        cap = v[int(len(v) * 0.975)]
        var[h] = sum(min(x, cap) for x in v) / len(v)
    avg = sum(var.values()) / 24
    return [math.sqrt(var[h] / avg) for h in range(24)]


def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    per = {p: profile(candles(p, days)) for p in COINS}
    m = [sum(per[p][h] for p in COINS) / len(COINS) for h in range(24)]
    s = [0.25 * m[h - 1] + 0.5 * m[h] + 0.25 * m[(h + 1) % 24] for h in range(24)]
    k = math.sqrt(sum(x * x for x in s) / 24)
    s = [round(x / k, 2) for x in s]
    print("ET hour  " + "  ".join(f"{p.split('-')[0]}" for p in COINS) + "  -> profile")
    for h in range(24):
        print(f"  {h:>2}    " + "  ".join(f"{per[p][h]:.2f}" for p in COINS) + f"  -> {s[h]:.2f}")
    print(f"\nHOUR_PROFILE_ET ({days} days to {datetime.now(ET):%Y-%m-%d}):")
    print(json.dumps(s))


if __name__ == "__main__":
    main()
