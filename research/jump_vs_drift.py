"""Jump vs drift: is the post-runaway edge just the long-shot bias, or does a fast move add something?

    python research/jump_vs_drift.py     # GitHub Actions: research.yml, job=jumpdrift

At fixed checkpoints (1:00, 2:00, 3:00, 4:00, 4:30) in every 5-minute window, record the
leading side's price, whether it got there by a JUMP (moved 15c+ toward it in the last
20 seconds) or a DRIFT (less than 5c in the last 20 seconds), and whether it won.
Same time, same price, different path: if jumps win more often than drifts, the crowd is
slow to react to fast moves. If they win the same, the edge is just the favorite-longshot bias.

Writes results/jump_vs_drift/summary.md.
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from poly5m import COINS, fee, hf_files, hf_read  # noqa: E402

OUT = Path("results/jump_vs_drift")
CHECKS = [60, 120, 180, 240, 270]


def rows_for_coin(coin, files):
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
        idx = g["s"].values
        grid = {}
        for col in ["bu", "au", "bd", "ad"]:
            a = np.full(300, np.nan)
            a[idx] = g[col].values
            grid[col] = pd.Series(a).ffill().values
        mid = (grid["bu"] + grid["au"]) / 2
        for c in CHECKS:
            now, before = mid[c], mid[c - 20]
            if not (np.isfinite(now) and np.isfinite(before)) or now == 0.5:
                continue
            lead_up = now > 0.5
            p = now if lead_up else 1 - now
            toward = (now - before) if lead_up else (before - now)   # how far the leader moved in 20 s
            path = "jump" if toward >= 0.15 else ("drift" if abs(now - before) < 0.05 else None)
            if path is None:
                continue
            ask = grid["au"][c] if lead_up else grid["ad"][c]
            out.append({"coin": coin, "check": c, "price": p, "path": path, "ask": ask,
                        "won": up_won[cid] == lead_up})
    print(f"  {coin}: {len(out)} checkpoints")
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    files = hf_files()
    d = pd.DataFrame([r for c in COINS for r in rows_for_coin(c, files)])
    d["bucket"] = pd.cut(d["price"], [0.5, 0.6, 0.7, 0.8, 0.9, 0.97], labels=["50-60c", "60-70c", "70-80c", "80-90c", "90-97c"])
    d = d.dropna(subset=["bucket"])
    ok = (d["ask"] > 0.01) & (d["ask"] < 0.99)
    d["roi"] = np.where(ok, d["won"] / (d["ask"] + fee(d["ask"])) - 1, np.nan)
    rows = []
    for (c, b), g in d.groupby(["check", "bucket"], observed=True):
        j, r = g[g["path"] == "jump"], g[g["path"] == "drift"]
        if len(j) < 50 or len(r) < 50:
            continue
        pj, pr = j["won"].mean(), r["won"].mean()
        se = math.sqrt(pj * (1 - pj) / len(j) + pr * (1 - pr) / len(r))
        rows.append({"check": c, "bucket": str(b), "jumps": len(j), "drifts": len(r),
                     "price_jump": j["price"].mean(), "won_jump": pj, "price_drift": r["price"].mean(), "won_drift": pr,
                     "gap_jump": pj - j["price"].mean(), "gap_drift": pr - r["price"].mean(),
                     "diff": (pj - j["price"].mean()) - (pr - r["price"].mean()), "z": (pj - pr) / se if se else 0,
                     "roi_jump": j["roi"].mean(), "roi_drift": r["roi"].mean()})
    s = pd.DataFrame(rows)
    s.to_csv(OUT / "table.csv", index=False)
    # pooled, weighting each cell by its jump count
    pooled = (s["diff"] * s["jumps"]).sum() / s["jumps"].sum() if len(s) else float("nan")
    md = ["# Jump vs drift: does a fast move add anything beyond the long-shot bias?", "",
          f"_Generated {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC by research/jump_vs_drift.py on the spring 2026 "
          "Polymarket 5-minute order-book recording._ Leader = the side above 50c at the checkpoint. Gap = win rate minus price. "
          "Diff = jump gap minus drift gap (positive = jumps are extra underpriced). z compares the two win rates; "
          "the price mix inside a bucket differs slightly, so read Diff, not raw win rates.", "",
          f"**Pooled diff (jumps minus drifts, same time and price bucket): {pooled * 100:+.1f} points.**", "",
          "| time | leader price | jumps | drifts | gap after jump | gap after drift | diff | z | ROI jump | ROI drift |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for _, x in s.iterrows():
        md.append(f"| {x.check // 60}:{x.check % 60:02d} | {x.bucket} | {x.jumps:,} | {x.drifts:,} | {x.gap_jump * 100:+.1f} | "
                  f"{x.gap_drift * 100:+.1f} | {x["diff"] * 100:+.1f} | {x.z:+.1f} | {x.roi_jump:+.1%} | {x.roi_drift:+.1%} |")
    (OUT / "summary.md").write_text("\n".join(md))
    print("\n".join(md))


if __name__ == "__main__":
    main()
