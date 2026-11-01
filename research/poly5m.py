"""Polymarket 5-minute Up or Down markets: history, trends and strategy tests.

    python research/poly5m.py                  # everything (runs in GitHub Actions: research.yml)
    python research/poly5m.py --recent-days 2  # how many recent days to pull from Polymarket itself

Two data sets:
  1. HISTORY: a public (CC0) recording of every 5-minute window for BTC, ETH, SOL,
     XRP, DOGE, HYPE and BNB, Mar 24 - May 18 2026, with the order book every second
     (huggingface.co/datasets/kachoio/polymarket-5-minute-crypto-up-down-markets).
  2. RECENT: the last few days straight from Polymarket (outcomes + minute prices),
     to check whether what held in the spring still holds now.
Coinbase 1-minute candles supply the coin's own price for both.

Every strategy decides only with information it would have had at that moment,
buys at the ask, and pays Polymarket's crypto taker fee (7% x p x (1-p) per share).

Writes results/poly5m/: summary.md (read this), windows.csv, strategies.csv, calibration.csv.
"""

import argparse
import io
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd
import requests

OUT = Path("results/poly5m")
HF = "kachoio/polymarket-5-minute-crypto-up-down-markets"
COINS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
COINBASE = {"btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD", "xrp": "XRP-USD", "doge": "DOGE-USD"}
OFFSETS = [5, 60, 120, 180, 240, 270]  # seconds into the window when a bot may decide
FEE_RATE = 0.07
S = requests.Session()
S.headers["User-Agent"] = "bot-lab-research/1.0"


def get(url, params=None, tries=5, raw=False):
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(2 + 2 * i)
                continue
            r.raise_for_status()
            return r.content if raw else r.json()
        except Exception as e:
            if i == tries - 1:
                raise
            print(f"  retry {url[:80]}: {e}")
            time.sleep(2 + 2 * i)


def fee(p):
    """Taker fee per share, p on a 0-1 scale."""
    return FEE_RATE * p * (1 - p)


# ---------------- data ----------------

def hf_files():
    tree = get(f"https://huggingface.co/api/datasets/{HF}/tree/main", {"recursive": "true"})
    return [x["path"] for x in tree if x.get("type") == "file" and x["path"].endswith(".parquet")]


def hf_read(path, columns=None):
    blob = get(f"https://huggingface.co/datasets/{HF}/resolve/main/{path}", raw=True)
    return pd.read_parquet(io.BytesIO(blob), columns=columns)


def history_windows(files):
    """One row per window: coin, start, outcome, and the Up/Down book at each decision offset."""
    rows = []
    for coin in COINS:
        mf = [f for f in files if f"{coin}_markets" in f.split("/")[-1]]
        tf = [f for f in files if f"{coin}_ticks" in f.split("/")[-1]]
        if not mf or not tf:
            print(f"  {coin}: no files found")
            continue
        m = pd.concat(hf_read(f) for f in mf)
        t = pd.concat(hf_read(f, ["condition_id", "t", "bu", "au", "bd", "ad"]) for f in tf)
        # the slug ends in the window's start time (unix seconds): the most reliable clock
        m["start"] = pd.to_numeric(m["slug"].astype(str).str.extract(r"(\d{9,11})$")[0], errors="coerce")
        m = m[m["outcome"].notna() & m["start"].notna()][["condition_id", "slug", "start", "outcome", "volume"]]
        m["start"] = m["start"].astype("int64")
        t = t.merge(m[["condition_id", "start"]], on="condition_id")
        t["off"] = t["t"] - t["start"]
        t = t.sort_values(["condition_id", "off"])
        base = m.set_index("condition_id")
        for off in OFFSETS:
            # the last book seen at or before `off` seconds (what a bot would see then)
            snap = t[t["off"] <= off].groupby("condition_id").tail(1).set_index("condition_id")
            fresh = (off - snap["off"]) <= 15
            for c in ["bu", "au", "bd", "ad"]:
                base[f"{c}_{off}"] = snap[c].where(fresh)
        base = base.reset_index()
        base["coin"] = coin
        base["up"] = base["outcome"].astype(str).str.lower().str.startswith("up").astype(int)
        rows.append(base)
        print(f"  {coin}: {len(base)} windows, {base['start'].min()}..{base['start'].max()}")
    return pd.concat(rows, ignore_index=True)


_probed = []


def gamma_market(slug):
    """A market by slug, closed or not (Gamma hides closed markets unless asked)."""
    for extra in ({"closed": "true"}, {}):
        try:
            ms = get("https://gamma-api.polymarket.com/markets", {"slug": slug, **extra})
        except Exception as e:
            ms, err = None, e
        if not _probed:
            _probed.append(1)
            print(f"  probe {slug} {extra}: {json.dumps(ms)[:600] if ms is not None else err}")
        if ms:
            return ms
    return None


def recent_windows(days):
    """Recent closed windows straight from Polymarket, with minute prices of the Up token."""
    end = int(time.time()) // 300 * 300 - 600
    starts = list(range(end - int(days * 86400), end, 300))
    jobs = [(coin, s) for coin in COINS for s in starts]

    def one(job):
        coin, s = job
        ms = gamma_market(f"{coin}-updown-5m-{s}")
        if not ms:
            return None
        mk = ms[0]
        try:
            outs = json.loads(mk.get("outcomes") or "[]")
            px = [float(x) for x in json.loads(mk.get("outcomePrices") or "[]")]
            toks = json.loads(mk.get("clobTokenIds") or "[]")
        except Exception:
            return None
        if len(px) < 2 or max(px) < 0.99 or not toks:
            return None  # not resolved yet
        up_idx = [o.lower() for o in outs].index("up") if "up" in [o.lower() for o in outs] else 0
        row = {"coin": coin, "start": s, "slug": mk.get("slug"), "up": int(px[up_idx] > 0.5), "volume": float(mk.get("volume") or 0)}
        try:
            h = get("https://clob.polymarket.com/prices-history", {"market": toks[up_idx], "startTs": s - 60, "endTs": s + 300, "fidelity": 1})
            hist = sorted((int(x["t"]), float(x["p"])) for x in h.get("history", []))
        except Exception:
            hist = []
        for off in OFFSETS:
            seen = [p for (tt, p) in hist if tt <= s + off]
            mid = seen[-1] if seen else np.nan
            # minute prices are trades/mids, not quotes: assume a 1-cent spread either side
            row[f"bu_{off}"], row[f"au_{off}"] = mid - 0.01, mid + 0.01
            row[f"bd_{off}"], row[f"ad_{off}"] = 1 - mid - 0.01, 1 - mid + 0.01
        return row

    out = []
    with ThreadPoolExecutor(8) as ex:
        for i, r in enumerate(ex.map(one, jobs)):
            if r:
                out.append(r)
            if i % 500 == 0:
                print(f"  recent: {i}/{len(jobs)} checked, {len(out)} resolved")
    return pd.DataFrame(out)


def coinbase_minutes(product, t0, t1):
    """1-minute candles from Coinbase, [t0, t1) unix seconds -> DataFrame indexed by minute start."""
    rows, s = [], t0
    while s < t1:
        e = min(s + 300 * 60, t1)
        page = get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                   {"granularity": 60, "start": pd.Timestamp(s, unit="s", tz="UTC").isoformat(),
                    "end": pd.Timestamp(e, unit="s", tz="UTC").isoformat()})
        rows.extend(page or [])
        s = e
        time.sleep(0.2)
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"]).drop_duplicates("t")
    return df.set_index("t").sort_index()[["open", "close"]]


def add_spot(w):
    """Coinbase features per window, all known by the decision time they're used at."""
    parts = []
    for coin, g in w.groupby("coin"):
        if coin not in COINBASE:
            parts.append(g)
            continue
        cb = coinbase_minutes(COINBASE[coin], int(g["start"].min()) - 3 * 3600, int(g["start"].max()) + 600)
        o, c = cb["open"], cb["close"]
        lr = np.log(c).diff()
        vol2h = np.sqrt((lr ** 2).rolling(120, min_periods=60).mean())  # per-minute, zero-mean
        g = g.copy()
        st = g["start"].values
        g["s0"] = o.reindex(st).values                         # price when the window opened
        g["prev_ret"] = np.log(g["s0"].values / o.reindex(st - 300).values)  # last window's move
        g["ret_1h"] = np.log(g["s0"].values / o.reindex(st - 3600).values)
        g["vol_min"] = vol2h.reindex(st - 60).values           # through the minute before the open
        for off in [60, 120, 180, 240]:
            g[f"s_{off}"] = c.reindex(st + off - 60).values    # close of the minute ending at `off`
        parts.append(g)
        print(f"  {coin}: Coinbase prices matched for {g['s0'].notna().mean():.0%} of windows")
    return pd.concat(parts, ignore_index=True)


# ---------------- analysis ----------------

def ncdf(x):
    return 0.5 * (1 + np.vectorize(math.erf)(x / math.sqrt(2)))


def model_up(w, off):
    """Chance the window closes at or above its open, given the move so far (random walk, 2h vol)."""
    left = (300 - off) / 60
    sd = w["vol_min"] * np.sqrt(left)
    z = np.log(w[f"s_{off}"] / w["s0"]) / sd
    return pd.Series(ncdf(z.values), index=w.index)


def bet(w, side_up, off, stake=1.0):
    """P&L per $1 staked buying `side_up` (bool Series, NaN = no bet) at the ask `off` seconds in."""
    ask = np.where(side_up, w[f"au_{off}"], w[f"ad_{off}"])
    ok = side_up.notna() & np.isfinite(ask) & (ask > 0.01) & (ask < 0.99)
    ask = pd.Series(ask, index=w.index)
    shares = stake / (ask + fee(ask))
    won = np.where(side_up == 1, w["up"] == 1, w["up"] == 0)
    pnl = shares * won - stake
    return pnl[ok], ask[ok]


def strategies(w):
    """name -> (side_up Series with NaN for no bet, offset)"""
    out = {}
    nan = pd.Series(np.nan, index=w.index)
    out["Always Up at the open"] = (nan.fillna(1.0), 5)
    out["Copy last window (momentum)"] = (w["prev_up"].astype(float), 5)
    out["Fade last window"] = (1 - w["prev_up"].astype(float), 5)
    for off in [60, 120, 180, 240]:
        if f"s_{off}" not in w:
            continue
        mp = model_up(w, off)
        ua, da = w[f"au_{off}"], w[f"ad_{off}"]
        ue, de = mp - ua - fee(ua), (1 - mp) - da - fee(da)
        side = pd.Series(np.where(ue >= 0.05, 1.0, np.where(de >= 0.05, 0.0, np.nan)), index=w.index)
        side[mp.isna()] = np.nan
        out[f"Model edge >=5c at {off}s"] = (side, off)
        lead = np.sign(np.log(w[f"s_{off}"] / w["s0"]))
        out[f"Follow the move so far at {off}s"] = (pd.Series(np.where(lead > 0, 1.0, np.where(lead < 0, 0.0, np.nan)), index=w.index), off)
    for off in [180, 240, 270]:
        bu, au = w[f"bu_{off}"], w[f"au_{off}"]
        mid = (bu + au) / 2
        fav = pd.Series(np.where((mid >= 0.70) & (mid <= 0.92), 1.0, np.where((mid <= 0.30) & (mid >= 0.08), 0.0, np.nan)), index=w.index)
        out[f"Buy the 70-92c favorite at {off}s"] = (fav, off)
        dog = pd.Series(np.where((mid >= 0.70) & (mid <= 0.92), 0.0, np.where((mid <= 0.30) & (mid >= 0.08), 1.0, np.nan)), index=w.index)
        out[f"Buy the 8-30c longshot at {off}s"] = (dog, off)
    # lead-lag: alt coins follow BTC's move so far when their own price hasn't caught up
    if "btc_move_120" in w:
        for off in [120, 180]:
            m = w[f"btc_move_{off}"]
            mid = (w[f"bu_{off}"] + w[f"au_{off}"]) / 2
            side = pd.Series(np.where((m > 0.0004) & (mid < 0.60), 1.0, np.where((m < -0.0004) & (mid > 0.40), 0.0, np.nan)), index=w.index)
            side[w["coin"] == "btc"] = np.nan
            out[f"Alts follow BTC's move at {off}s"] = (side, off)
    return out


def evaluate(w, label):
    rows = []
    for name, (side, off) in strategies(w).items():
        pnl, ask = bet(w, side, off)
        if len(pnl) < 30:
            continue
        r = pnl.loc[w.loc[pnl.index, "start"].sort_values(kind="stable").index].values  # in time order
        se = r.std(ddof=1) / math.sqrt(len(r))
        half = len(r) // 2
        rows.append({"data": label, "strategy": name, "bets": len(r), "win_rate": (r > 0).mean(),
                     "avg_price": ask.mean(), "roi_per_bet": r.mean(), "t_stat": r.mean() / se if se > 0 else 0,
                     "roi_first_half": r[:half].mean(), "roi_second_half": r[half:].mean()})
    return pd.DataFrame(rows).sort_values("roi_per_bet", ascending=False)


def calibration(w, label):
    rows = []
    for off in OFFSETS:
        mid = (w[f"bu_{off}"] + w[f"au_{off}"]) / 2
        b = pd.cut(mid, [0, .1, .2, .3, .4, .45, .55, .6, .7, .8, .9, 1.0])
        g = w.assign(mid=mid, b=b).dropna(subset=["mid"]).groupby("b", observed=True)
        for k, x in g:
            if len(x) >= 30:
                rows.append({"data": label, "offset_s": off, "price_bucket": str(k), "n": len(x),
                             "avg_mid": x["mid"].mean(), "up_rate": x["up"].mean(), "gap": x["up"].mean() - x["mid"].mean(),
                             "avg_spread": (x[f"au_{off}"] - x[f"bu_{off}"]).mean()})
    return pd.DataFrame(rows)


def trends(w, label):
    lines = [f"### {label}: {len(w):,} windows, {pd.Timestamp(w['start'].min(), unit='s'):%b %d} to {pd.Timestamp(w['start'].max(), unit='s'):%b %d %Y}", ""]
    lines.append("| coin | windows | Up rate | P(Up after Up) | P(Up after Down) | longest streak |")
    lines.append("|---|---|---|---|---|---|")
    for coin, g in w.sort_values("start").groupby("coin"):
        u = g["up"].values
        pu = g["prev_up"]
        runs = np.diff(np.flatnonzero(np.diff(np.r_[-1, u, -1]) != 0)).max() if len(u) else 0
        lines.append(f"| {coin.upper()} | {len(g):,} | {u.mean():.1%} | {g.loc[pu == 1, 'up'].mean():.1%} | {g.loc[pu == 0, 'up'].mean():.1%} | {runs} |")
    hr = w.assign(h=pd.to_datetime(w["start"], unit="s").dt.hour).groupby("h")["up"].mean()
    lines += ["", f"Up rate by UTC hour (all coins) ranges {hr.min():.1%} (hour {hr.idxmin()}) to {hr.max():.1%} (hour {hr.idxmax()}); "
              f"with ~{len(w) / 24:,.0f} windows per hour, anything within about ±{1.96 * 0.5 / math.sqrt(len(w) / 24):.1%} of 50% is noise.", ""]
    return lines


def prep(w):
    w = w.sort_values(["coin", "start"]).reset_index(drop=True)
    w["prev_up"] = w.groupby("coin")["up"].shift(1)
    w.loc[w.groupby("coin")["start"].diff() != 300, "prev_up"] = np.nan
    if "s_120" in w:
        btc = w[w["coin"] == "btc"].set_index("start")
        for off in [120, 180]:
            mv = np.log(btc[f"s_{off}"] / btc["s0"])
            w[f"btc_move_{off}"] = w["start"].map(mv)
    return w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent-days", type=float, default=2)
    ap.add_argument("--skip-history", action="store_true")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    sets = {}
    if not a.skip_history:
        print("History (Hugging Face)...")
        files = hf_files()
        print(f"  {len(files)} parquet files")
        sets["Spring 2026 history"] = prep(add_spot(history_windows(files)))
    if a.recent_days > 0:
        print("Recent (Polymarket)...")
        try:
            r = recent_windows(a.recent_days)
            if len(r):
                sets["Last days"] = prep(add_spot(r))
        except Exception as e:  # keep the history results even if the recent pull fails
            print(f"::warning::recent pull failed: {e}")

    head = ["# Polymarket 5-minute Up or Down: trends and strategy tests", "",
            f"_Generated by research/poly5m.py._ Every bet buys at the ask and pays the 7% x p x (1-p) taker fee. "
            "ROI is per $1 staked. |t| below about 2 means the result could easily be luck.", ""]
    for label, w in sets.items():
        key = "history" if "history" in label else "recent"
        w.to_csv(OUT / f"windows_{key}.csv.gz", index=False)
        md = [f"_Updated {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC._", ""] + trends(w, label)
        if key == "recent":
            # Polymarket's minute price history lags the real book, so a replay "buys" stale prices
            # after the move already happened. Outcomes and streaks are fine; strategy P&L is not.
            md += ["Strategy results are left out for recent days: Polymarket's minute price history is often stale, "
                   "so replaying it shows fake profits (a bot 'buys' an old price after the move). The live 5-minute "
                   "league tests strategies on real quotes instead.", ""]
            (OUT / f"summary_{key}.md").write_text("\n".join(md))
            continue
        s = evaluate(w, label)
        s.to_csv(OUT / "strategies.csv", index=False)
        md += [f"**Strategies on {label}**", "", "| strategy | bets | win rate | avg price | ROI/bet | t | 1st half | 2nd half |", "|---|---|---|---|---|---|---|---|"]
        for _, x in s.iterrows():
            md.append(f"| {x.strategy} | {x.bets:,} | {x.win_rate:.1%} | {x.avg_price * 100:.0f}c | {x.roi_per_bet:+.1%} | {x.t_stat:+.1f} | {x.roi_first_half:+.1%} | {x.roi_second_half:+.1%} |")
        c = calibration(w, label)
        c.to_csv(OUT / "calibration.csv", index=False)
        md += ["", f"**Is the price right? ({label})** Gap = how often Up actually won minus the price. A steady positive gap above 70c means favorites are underpriced.", "",
               "| seconds in | price | n | price avg | Up won | gap | spread |", "|---|---|---|---|---|---|---|"]
        for _, x in c[c["offset_s"].isin([60, 180, 240, 270])].iterrows():
            md.append(f"| {x.offset_s} | {x.price_bucket} | {x.n:,} | {x.avg_mid:.2f} | {x.up_rate:.2f} | {x.gap:+.3f} | {x.avg_spread:.3f} |")
        md.append("")
        (OUT / f"summary_{key}.md").write_text("\n".join(md))
    md = head + [(OUT / f"summary_{k}.md").read_text() for k in ("history", "recent") if (OUT / f"summary_{k}.md").exists()]
    (OUT / "summary.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
