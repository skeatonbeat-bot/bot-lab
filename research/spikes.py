"""Runaways and collapses in Polymarket's 5-minute Up or Down markets.

    python research/spikes.py        # runs in GitHub Actions (research.yml, job=spikes)

Question: when the crowd moves a price fast, does it go too far (fade it) or not far
enough (follow it)? Uses the spring 2026 order-book recording (every second, 78k windows).

Events, at most one of each kind per window (the first one):
  RUNAWAY   the Up mid moves 15c or more within 20 seconds, between 0:30 and 4:00
  COLLAPSE  a side that was 80c+ within the last 30 seconds drops to 50c or below
For each: did the side the crowd just rushed into win more or less often than its new
price said? Buying at the ask with the 7% x p x (1-p) taker fee, which side paid?

Writes results/spikes/summary.md and events.csv.
"""

import io
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from poly5m import COINS, fee, hf_files, hf_read  # noqa: E402

OUT = Path("results/spikes")


def events_for_coin(coin, files):
    mf = [f for f in files if f"{coin}_markets" in f.split("/")[-1]]
    tf = [f for f in files if f"{coin}_ticks" in f.split("/")[-1]]
    m = pd.concat(hf_read(f) for f in mf)
    m["start"] = pd.to_numeric(m["slug"].astype(str).str.extract(r"(\d{9,11})$")[0], errors="coerce")
    m = m[m["outcome"].notna() & m["start"].notna()]
    up_won = dict(zip(m["condition_id"], m["outcome"].astype(str).str.lower().str.startswith("up")))
    start = dict(zip(m["condition_id"], m["start"].astype("int64")))
    t = pd.concat(hf_read(f, ["condition_id", "t", "bu", "au", "bd", "ad"]) for f in tf)
    t = t[t["condition_id"].isin(start)]
    t["s"] = (t["t"] - t["condition_id"].map(start)).astype("int64")
    t = t[(t["s"] >= 0) & (t["s"] < 300)].sort_values(["condition_id", "s"])
    out = []
    for cid, g in t.groupby("condition_id", sort=False):
        # one value per second, carried forward
        idx = g["s"].values
        grid = {}
        for col in ["bu", "au", "bd", "ad"]:
            a = np.full(300, np.nan)
            a[idx] = g[col].values
            grid[col] = pd.Series(a).ffill().values
        mid = (grid["bu"] + grid["au"]) / 2
        if np.isnan(mid[30:240]).all():
            continue
        won = up_won[cid]
        base = {"coin": coin, "condition_id": cid, "start": start[cid], "up_won": won}

        def rec(kind, s, up_side):
            """up_side: the side the crowd rushed into (True = Up)."""
            ask_in = grid["au"][s] if up_side else grid["ad"][s]
            ask_out = grid["ad"][s] if up_side else grid["au"][s]
            m_now = mid[s] if up_side else 1 - mid[s]
            later = lambda k: (mid[min(s + k, 299)] if up_side else 1 - mid[min(s + k, 299)])
            out.append({**base, "kind": kind, "sec": s, "into_up": up_side,
                        "move": abs(mid[s] - mid[max(s - 20, 0)]) if kind == "runaway" else np.nan,
                        "price_now": m_now, "price_30s": later(30), "price_60s": later(60),
                        "ask_in": ask_in, "ask_out": ask_out, "rushed_side_won": won == up_side})

        d20 = mid[20:] - mid[:-20]                    # change over the last 20 s, at second s = i + 20
        for i in np.flatnonzero(np.abs(np.nan_to_num(d20)) >= 0.15):
            s = i + 20
            if 30 <= s <= 240:
                rec("runaway", s, bool(d20[i] > 0))
                break
        roll_max = pd.Series(mid).rolling(30, min_periods=1).max().values
        roll_min = pd.Series(mid).rolling(30, min_periods=1).min().values
        for s in range(30, 270):
            if roll_max[s - 1] >= 0.80 and mid[s] <= 0.50:      # Up favorite collapsed -> crowd rushed Down
                rec("collapse", s, False); break
            if roll_min[s - 1] <= 0.20 and mid[s] >= 0.50:      # Down favorite collapsed -> crowd rushed Up
                rec("collapse", s, True); break
    print(f"  {coin}: {len(out)} events")
    return out


def roi(price, won):
    """Per $1 staked buying at `price` with the taker fee."""
    p = np.asarray(price, float)
    ok = np.isfinite(p) & (p > 0.01) & (p < 0.99)
    shares = 1 / (p[ok] + fee(p[ok]))
    return (shares * np.asarray(won)[ok] - 1), ok.sum()


def table(ev, by, label):
    rows = []
    for k, g in ev.groupby(by, observed=True):
        if len(g) < 40:
            continue
        r_in, n_in = roi(g["ask_in"], g["rushed_side_won"])
        r_out, n_out = roi(g["ask_out"], ~g["rushed_side_won"])
        se_in = r_in.std(ddof=1) / math.sqrt(max(n_in, 2))
        se_out = r_out.std(ddof=1) / math.sqrt(max(n_out, 2))
        rows.append({label: str(k), "events": len(g), "price_now": g["price_now"].mean(),
                     "won": g["rushed_side_won"].mean(), "gap": g["rushed_side_won"].mean() - g["price_now"].mean(),
                     "drift_30s": (g["price_30s"] - g["price_now"]).mean(),
                     "follow_roi": r_in.mean(), "follow_t": r_in.mean() / se_in if se_in else 0,
                     "fade_roi": r_out.mean(), "fade_t": r_out.mean() / se_out if se_out else 0})
    return pd.DataFrame(rows)


def md_table(df, first):
    lines = [f"| {first} | events | price after | rushed side won | gap | next 30s drift | follow ROI (t) | fade ROI (t) |",
             "|---|---|---|---|---|---|---|---|"]
    for _, x in df.iterrows():
        lines.append(f"| {x.iloc[0]} | {x.events:,} | {x.price_now:.2f} | {x.won:.1%} | {x.gap:+.3f} | {x.drift_30s:+.3f} | "
                     f"{x.follow_roi:+.1%} ({x.follow_t:+.1f}) | {x.fade_roi:+.1%} ({x.fade_t:+.1f}) |")
    return lines


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    files = hf_files()
    ev = pd.DataFrame([e for c in COINS for e in events_for_coin(c, files)])
    ev.to_csv(OUT / "events.csv.gz", index=False)
    md = ["# Runaways and collapses: does the crowd overshoot?", "",
          f"_Generated {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC by research/spikes.py from the spring 2026 "
          "Polymarket 5-minute order-book recording._ 'Rushed side' = the side the price just ran toward. "
          "Gap = how often it won minus its price right after the move (negative = the crowd overshot). "
          "ROI per $1 buying at the ask with the taker fee; |t| under ~2 is noise.", ""]

    run = ev[ev["kind"] == "runaway"].copy()
    run["size"] = pd.cut(run["move"], [0.15, 0.25, 0.40, 1.0], labels=["15-25c", "25-40c", "40c+"])
    run["when"] = pd.cut(run["sec"], [29, 90, 180, 241], labels=["0:30-1:30", "1:30-3:00", "3:00-4:00"])
    run["landed"] = pd.cut(run["price_now"], [0, 0.5, 0.7, 0.85, 1.0], labels=["under 50c", "50-70c", "70-85c", "85c+"])
    md += [f"## Runaways (15c+ in 20 seconds): {len(run):,} windows", ""]
    for col, name in [("size", "move size"), ("when", "time in window"), ("landed", "rushed side's new price")]:
        md += [f"**By {name}**", ""] + md_table(table(run, col, name), name) + [""]

    col = ev[ev["kind"] == "collapse"].copy()
    col["when"] = pd.cut(col["sec"], [29, 120, 210, 270], labels=["0:30-2:00", "2:00-3:30", "3:30-4:30"])
    md += [f"## Collapses (an 80c+ favorite falls to 50c or below within 30 seconds): {len(col):,} windows", "",
           "Here the rushed side is the new leader; fading it = backing the fallen favorite.", ""]
    md += md_table(table(col, "when", "time in window"), "time in window") + [""]
    (OUT / "summary.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
