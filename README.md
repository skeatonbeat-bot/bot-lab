# bot-lab

A for-fun experiment: how well can current AI find an edge in crypto and
prediction markets? Two parts live here.

## 1. Bot tournament (paper money)

Five bots, $100 of pretend money each, four leagues that differ only in how
often the bots decide: every 15 minutes (Speed), 30 minutes (Half-Hour),
1 hour (Hourly) and 4 hours (Slow).
Each league runs separately on five coins: bitcoin plus the more volatile
ETH, SOL, DOGE and XRP (Coinbase prices). Bitcoin results keep their original
files (`live/equity_4h.csv`); other coins add the coin (`live/equity_4h_SOL.csv`).
Change the list with `COINS` in the workflow, or run one by hand with
`--product SOL-USD`.

| Bot | Rule |
|---|---|
| Benchmark | Buys once and holds. The one to beat. |
| Trend Rider | In while the fast trend line is above the slow one. |
| Breakout | Buys a close above the recent high, sells a close below the recent low. |
| Dip Hunter | Buys sharp oversold drops, sells at +2.5% / -4% / time limit. |
| Grid | Holds more near the bottom of last week's range, less near the top. |

Every bot except the Benchmark pays a 0.6% fee per buy or sell, stops for the
day after a 10% loss, and retires at $60.

**It runs itself.** `.github/workflows/tournament.yml` runs all leagues every
15 minutes on GitHub's servers and saves results to `live/`. Watch it under
the repo's **Actions** tab; run it by hand with **Run workflow**.

Run locally instead: `pip install -r requirements.txt`, then
`python paper_live.py --candle 4h` (Ctrl+C stops; rerun to resume).
Backtest: download 1-minute prices from
https://github.com/ff137/bitstamp-btcusd-minute-data/raw/main/data/updates/btcusd_bitstamp_1min_latest.csv
to `data/btcusd_1min.csv`, then `python backtest.py data/btcusd_1min.csv --candle 4h`.

First backtest (Jan 2025 to Oct 2026, bitcoin -25%): every Speed League
challenger retired; in the Slow League, Breakout finished at $101 against $75
for holding. Details in `results/`.

## 2. Edge Finder connector

`connector/` is a small read-only MCP server that gives Claude live data for
mismatch hunting: Kalshi markets, events (with automatic checks for brackets
that don't add up and threshold ladders priced out of order), order books and
settlement rules; Polymarket reference prices; National Weather Service
forecasts, observations and daily climate reports; AAA gas prices; an
after-fees edge calculator; and the tournament leaderboard. It never trades.

### Deploy (about 10 minutes, free)

1. Sign up at https://dash.cloudflare.com (free plan).
2. **Workers & Pages → Create → Import a repository →** pick `bot-lab`.
   - Project name: `edge-finder`
   - Root directory / path: `connector`
   - Deploy command: `npx wrangler deploy` (the default)
3. After it deploys: **edge-finder → Settings → Variables and Secrets → Add**
   - Type **Secret**, name `CONNECTOR_KEY`, value: a long random string (keep it private)
   - Save and redeploy if asked.
4. Open `https://edge-finder.<your-subdomain>.workers.dev/health`; it should say it's running.
5. In Claude: **Customize → Connectors → + Add → Add custom connector**
   - Name: `Edge Finder`
   - URL: `https://edge-finder.<your-subdomain>.workers.dev/mcp/<CONNECTOR_KEY>`
   - Authentication: **No sign in**
6. In a chat, **+ → Connectors →** turn on Edge Finder.

### Tournament timer (keeps the bots on time)

GitHub's own 15-minute schedule often skips runs for hours, so the connector
also starts the tournament workflow every 15 minutes from Cloudflare. To turn
it on:

1. GitHub → your avatar → **Settings → Developer settings → Personal access
   tokens → Fine-grained tokens → Generate new token**.
   - Repository access: **Only select repositories →** `bot-lab`
   - Permissions → Repository → **Actions: Read and write** (nothing else)
   - Pick an expiration and generate; copy the token.
2. Cloudflare → the connector Worker → **Settings → Variables and Secrets →
   Add**: type **Secret**, name `GITHUB_TOKEN`, paste the token. Save.
3. Open `/health`; it should say **Tournament timer: on**. Runs then show up
   under the repo's **Actions** tab as "workflow_dispatch" every 15 minutes.

Tests: `node connector/test/test.mjs` (offline, fakes the outside APIs).
