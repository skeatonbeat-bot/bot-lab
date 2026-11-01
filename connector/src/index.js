/**
 * Edge Finder: a read-only MCP connector for the prediction-market experiment.
 *
 * It runs as a Cloudflare Worker and speaks MCP over Streamable HTTP
 * (stateless JSON responses). It never trades and holds no account keys;
 * every source it reads is public.
 *
 * URL to give Claude:  https://<your-worker>.workers.dev/mcp/<CONNECTOR_KEY>
 * CONNECTOR_KEY is a secret you set in Cloudflare, so nobody else can use it.
 */

import { makeScanTools } from "./scan.js";

const KALSHI = "https://api.elections.kalshi.com/trade-api/v2";
const GAMMA = "https://gamma-api.polymarket.com";
const NWS = "https://api.weather.gov";
const UA = "edge-finder-connector (personal research)";
const PROTOCOL = "2025-06-18";

// ---------- small helpers ----------

async function getJSON(url, headers = {}) {
  const r = await fetch(url, { headers: { "User-Agent": UA, Accept: "application/json", ...headers } });
  if (!r.ok) throw new Error(`${r.status} from ${new URL(url).host}: ${(await r.text()).slice(0, 200)}`);
  return r.json();
}

async function getText(url) {
  const r = await fetch(url, { headers: { "User-Agent": UA } });
  if (!r.ok) throw new Error(`${r.status} from ${new URL(url).host}`);
  return r.text();
}

const qs = (o) =>
  Object.entries(o)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

// Kalshi has sent prices as integer cents (yes_bid) and as dollar strings
// (yes_bid_dollars). Return cents either way.
function cents(m, field) {
  const d = m[`${field}_dollars`];
  if (d !== undefined && d !== null && d !== "") return Math.round(parseFloat(d) * 100);
  const c = m[field];
  return c === undefined || c === null ? null : Number(c);
}

function num(x) {
  if (x === undefined || x === null || x === "") return null;
  const n = typeof x === "string" ? parseFloat(x) : x;
  return Number.isFinite(n) ? n : null;
}

function compactKalshi(m) {
  return {
    ticker: m.ticker,
    event: m.event_ticker,
    title: m.title,
    option: m.yes_sub_title || m.subtitle || "",
    yes_bid: cents(m, "yes_bid"),
    yes_ask: cents(m, "yes_ask"),
    no_bid: cents(m, "no_bid"),
    no_ask: cents(m, "no_ask"),
    last: cents(m, "last_price"),
    vol_24h: num(m.volume_24h_fp ?? m.volume_24h),
    open_interest: num(m.open_interest_fp ?? m.open_interest),
    closes: m.close_time,
    strike: m.floor_strike ?? m.cap_strike ?? null,
    strike_type: m.strike_type ?? null,
  };
}

function hoursUntil(iso) {
  return (Date.parse(iso) - Date.now()) / 3.6e6;
}

function table(rows, cols) {
  if (!rows.length) return "(none)";
  const head = cols.join(" | ");
  const body = rows.map((r) => cols.map((c) => (r[c] === null || r[c] === undefined ? "" : String(r[c]))).join(" | "));
  return [head, ...body].join("\n");
}

// Kalshi taker fee: ceil(0.07 * contracts * P * (1 - P)) to the cent.
function kalshiFee(priceCents, contracts, rate = 0.07) {
  const p = priceCents / 100;
  return Math.ceil(rate * contracts * p * (1 - p) * 100) / 100;
}

// ---------- ladder / bracket logic ----------

function ladderCheck(markets, mutuallyExclusive) {
  const notes = [];
  const rows = markets.map(compactKalshi);
  if (mutuallyExclusive) {
    const asks = rows.map((r) => r.yes_ask).filter((x) => x !== null);
    const bids = rows.map((r) => r.yes_bid).filter((x) => x !== null);
    const sumAsk = asks.reduce((a, b) => a + b, 0);
    const sumBid = bids.reduce((a, b) => a + b, 0);
    notes.push(`Brackets are mutually exclusive: Yes asks sum to ${sumAsk}¢, Yes bids sum to ${sumBid}¢ (fair is 100).`);
    if (asks.length === rows.length && sumAsk < 100)
      notes.push(`POSSIBLE ARB: buying Yes on every bracket costs ${sumAsk}¢ for a sure 100¢ (before fees).`);
    if (bids.length === rows.length && sumBid > 100)
      notes.push(`Bids sum above 100: the field is collectively overpriced by ${sumBid - 100}¢; No on the richest brackets is worth a look.`);
  } else {
    // threshold ladder: higher "above X" strikes must be priced lower
    const above = rows
      .filter((r) => r.strike !== null && /^greater/i.test(r.strike_type || "greater"))
      .sort((a, b) => a.strike - b.strike);
    for (let i = 1; i < above.length; i++) {
      const lo = above[i - 1], hi = above[i];
      if (hi.yes_bid !== null && lo.yes_ask !== null && hi.yes_bid > lo.yes_ask)
        notes.push(`INVERSION: "${hi.option}" bid ${hi.yes_bid}¢ > "${lo.option}" ask ${lo.yes_ask}¢. A higher bar can't be more likely: buy Yes ${lo.ticker}, buy No ${hi.ticker}.`);
    }
    if (!notes.length) notes.push(`Threshold ladder of ${above.length} strikes is in order (no inversions).`);
  }
  return { rows, notes };
}

// ---------- tools ----------

const TOOLS = {
  kalshi_markets_closing_soon: {
    description:
      "Kalshi markets that close within the next N hours, newest prices, sorted by 24h volume. Skips multi-game parlays. Use `cursor` from a previous call to page. Text filter matches title, option and ticker.",
    inputSchema: {
      type: "object",
      properties: {
        hours: { type: "number", description: "Close within this many hours (default 72)" },
        text: { type: "string", description: "Optional words to match, e.g. 'temperature', 'gas', 'TSA', 'CPI'" },
        exclude_sports: { type: "boolean", description: "Drop sports/parlay markets (default true)" },
        min_volume_24h: { type: "number", description: "Hide markets with less 24h volume (default 0)" },
        limit: { type: "number", description: "Max rows returned (default 60)" },
        cursor: { type: "string" },
      },
    },
    async run(a) {
      const hours = a.hours ?? 72;
      const maxTs = Math.floor(Date.now() / 1000 + hours * 3600);
      // Kalshi returns 1000 markets a page in no useful order, so read up to 3 pages before sorting
      const all = [];
      let cursor = a.cursor;
      for (let i = 0; i < (a.cursor ? 1 : 3); i++) {
        const base = { status: "open", max_close_ts: maxTs, limit: 1000, cursor };
        const page = await getJSON(`${KALSHI}/markets?${qs({ ...base, mve_filter: "exclude" })}`).catch(() =>
          getJSON(`${KALSHI}/markets?${qs(base)}`)
        );
        all.push(...(page.markets || []));
        cursor = page.cursor;
        if (!cursor) break;
      }
      const data = { cursor };
      const sportsy = /KXMVE|NFL|NBA|NHL|MLB|NCAA|SOCCER|EPL|UFC|GAME|MATCH|PGA|ATP|WTA|MLS|LALIGA|SERIEA|BUNDES|F1/i;
      let rows = all.map(compactKalshi);
      if (a.exclude_sports !== false) rows = rows.filter((r) => !sportsy.test(r.ticker) && !sportsy.test(r.event || ""));
      if (a.text) {
        const t = a.text.toLowerCase().split(/\s+/);
        rows = rows.filter((r) => t.every((w) => `${r.title} ${r.option} ${r.ticker}`.toLowerCase().includes(w)));
      }
      if (a.min_volume_24h) rows = rows.filter((r) => (r.vol_24h || 0) >= a.min_volume_24h);
      rows.sort((x, y) => (y.vol_24h || 0) - (x.vol_24h || 0));
      rows = rows.slice(0, a.limit ?? 60);
      const cols = ["ticker", "title", "option", "yes_bid", "yes_ask", "no_ask", "vol_24h", "open_interest", "closes"];
      return `${rows.length} markets closing within ${hours}h${data.cursor ? ` (more: cursor=${data.cursor})` : ""}\n${table(rows, cols)}`;
    },
  },

  kalshi_event: {
    description:
      "Every market in one Kalshi event (e.g. KXHIGHNY-26NOV01), with prices and an automatic consistency check: brackets that don't sum to 100, or threshold ladders priced out of order.",
    inputSchema: { type: "object", properties: { event_ticker: { type: "string" } }, required: ["event_ticker"] },
    async run(a) {
      const d = await getJSON(`${KALSHI}/events/${encodeURIComponent(a.event_ticker)}?with_nested_markets=true`);
      const ev = d.event || {};
      const markets = ev.markets || d.markets || [];
      const { rows, notes } = ladderCheck(markets, !!ev.mutually_exclusive);
      const cols = ["ticker", "option", "strike", "yes_bid", "yes_ask", "no_bid", "no_ask", "last", "vol_24h", "closes"];
      return `${ev.title || a.event_ticker} (${ev.series_ticker || ""})\n${notes.join("\n")}\n\n${table(rows, cols)}`;
    },
  },

  kalshi_market: {
    description: "One Kalshi market in full: prices, volume, close time, the exact settlement rules, and the order book depth.",
    inputSchema: { type: "object", properties: { ticker: { type: "string" }, depth: { type: "number" } }, required: ["ticker"] },
    async run(a) {
      const t = encodeURIComponent(a.ticker);
      const [m, ob] = await Promise.all([
        getJSON(`${KALSHI}/markets/${t}`),
        getJSON(`${KALSHI}/markets/${t}/orderbook?${qs({ depth: a.depth ?? 10 })}`).catch((e) => ({ error: e.message })),
      ]);
      const mk = m.market || m;
      const c = compactKalshi(mk);
      const book = ob.orderbook || ob.orderbook_fp || {};
      // levels arrive as [cents, qty] or, in the dollar format, ["0.45", qty]
      const side = (centsArr, dollarArr) => {
        const lv = dollarArr?.length
          ? dollarArr.map((x) => [Math.round(num(x[0]) * 100), num(x[1])])
          : (centsArr || []).map((x) => [num(x[0]), num(x[1])]);
        return lv.sort((a, b) => b[0] - a[0]).map(([p, q]) => `${p}¢ x ${q}`).join(", ") || "(empty)";
      };
      return [
        `${c.title} | ${c.option}`,
        `ticker ${c.ticker} | closes ${c.closes} (${hoursUntil(c.closes).toFixed(1)}h)`,
        `Yes bid/ask ${c.yes_bid}/${c.yes_ask}¢ | No bid/ask ${c.no_bid}/${c.no_ask}¢ | last ${c.last}¢ | 24h vol ${c.vol_24h} | OI ${c.open_interest}`,
        `Order book Yes bids: ${side(book.yes, book.yes_dollars)}`,
        `Order book No bids: ${side(book.no, book.no_dollars)}`,
        ob.error ? `(order book unavailable: ${ob.error})` : "",
        `RULES: ${mk.rules_primary || ""}`,
        mk.rules_secondary ? `MORE RULES: ${mk.rules_secondary}` : "",
      ].filter(Boolean).join("\n");
    },
  },

  kalshi_series_events: {
    description: "Open events in one Kalshi series, e.g. KXHIGHNY (NYC high temp), KXAAAGASD (gas), KXTSAW (TSA), KXU3 (unemployment). Lists event tickers to pass to kalshi_event.",
    inputSchema: { type: "object", properties: { series_ticker: { type: "string" } }, required: ["series_ticker"] },
    async run(a) {
      const d = await getJSON(`${KALSHI}/events?${qs({ series_ticker: a.series_ticker, status: "open", limit: 50 })}`);
      const rows = (d.events || []).map((e) => ({ event_ticker: e.event_ticker, title: e.title, sub: e.sub_title || "" }));
      return table(rows, ["event_ticker", "title", "sub"]);
    },
  },

  polymarket_find: {
    description:
      "Polymarket markets matching words, ending within N hours. NOTE: these are prices on Polymarket's main (international) exchange, which US users can't trade; the US app (polymarket.us) is a separate exchange whose prices usually track these but can differ. Use as a reference price.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Words to match in the question, e.g. 'Bitcoin', 'NYC high'" },
        hours: { type: "number", description: "Ending within this many hours (default 168)" },
        limit: { type: "number" },
      },
    },
    async run(a) {
      const end = new Date(Date.now() + (a.hours ?? 168) * 3.6e6).toISOString();
      const d = await getJSON(`${GAMMA}/markets?${qs({ active: true, closed: false, end_date_max: end, end_date_min: new Date().toISOString(), order: "volume24hr", ascending: false, limit: 500 })}`);
      let rows = (Array.isArray(d) ? d : d.markets || []).map((m) => {
        let outs = [], px = [];
        try { outs = JSON.parse(m.outcomes || "[]"); px = JSON.parse(m.outcomePrices || "[]"); } catch {}
        return {
          question: m.question,
          prices: outs.map((o, i) => `${o} ${Math.round(num(px[i]) * 100)}¢`).join(" / "),
          bid: m.bestBid != null ? Math.round(num(m.bestBid) * 100) : null,
          ask: m.bestAsk != null ? Math.round(num(m.bestAsk) * 100) : null,
          vol_24h: m.volume24hr != null ? Math.round(num(m.volume24hr)) : null,
          ends: m.endDate,
          slug: m.slug,
        };
      });
      if (a.text) {
        const t = a.text.toLowerCase().split(/\s+/);
        rows = rows.filter((r) => t.every((w) => (r.question || "").toLowerCase().includes(w)));
      }
      rows = rows.slice(0, a.limit ?? 40);
      return table(rows, ["question", "prices", "bid", "ask", "vol_24h", "ends", "slug"]);
    },
  },

  weather_forecast: {
    description:
      "National Weather Service forecast for a market's weather station: daily highs/lows and rain chances for the next few days, plus the hourly-forecast max/min per day. Use a station preset (NYC, MDW, ORD, MIA, LAX, AUS, DEN, PHL, SFO, HOU, ATL, BOS, DCA, SEA, PHX, LAS, MSP, DFW, SAT, NOLA) or lat/lon. Check the market's rules for its exact station.",
    inputSchema: {
      type: "object",
      properties: { station: { type: "string" }, lat: { type: "number" }, lon: { type: "number" } },
    },
    async run(a) {
      const p = a.station ? STATIONS[a.station.toUpperCase()] : null;
      const lat = p ? p.lat : a.lat, lon = p ? p.lon : a.lon;
      if (lat == null || lon == null) throw new Error(`Give a station preset (${Object.keys(STATIONS).join(", ")}) or lat/lon.`);
      const pt = await getJSON(`${NWS}/points/${lat.toFixed(4)},${lon.toFixed(4)}`);
      const tz = pt.properties.timeZone;
      const [fc, hr] = await Promise.all([getJSON(pt.properties.forecast), getJSON(pt.properties.forecastHourly)]);
      const daily = fc.properties.periods.slice(0, 8).map((x) => `${x.name}: ${x.temperature}°${x.temperatureUnit}, rain ${x.probabilityOfPrecipitation?.value ?? 0}% (${x.shortForecast})`);
      const byDay = {};
      for (const h of hr.properties.periods) {
        const day = new Date(h.startTime).toLocaleDateString("en-US", { timeZone: tz, weekday: "short", month: "short", day: "numeric" });
        const d = (byDay[day] ||= { max: -999, min: 999, rain: 0 });
        d.max = Math.max(d.max, h.temperature);
        d.min = Math.min(d.min, h.temperature);
        d.rain = Math.max(d.rain, h.probabilityOfPrecipitation?.value ?? 0);
      }
      const hourly = Object.entries(byDay).slice(0, 4).map(([d, v]) => `${d}: hourly max ${v.max}°, min ${v.min}°, peak rain chance ${v.rain}%`);
      return [`${p ? p.name : `${lat},${lon}`} (time zone ${tz})`, "Forecast periods:", ...daily, "Hourly forecast by local day:", ...hourly].join("\n");
    },
  },

  weather_observed: {
    description:
      "What has actually happened at a weather station: today's observed high/low so far from live readings, and the latest NWS Daily Climate Report (the official record many weather markets settle on). Station presets as in weather_forecast.",
    inputSchema: { type: "object", properties: { station: { type: "string" } }, required: ["station"] },
    async run(a) {
      const p = STATIONS[a.station.toUpperCase()];
      if (!p) throw new Error(`Unknown station. Presets: ${Object.keys(STATIONS).join(", ")}`);
      const since = new Date(Date.now() - 30 * 3.6e6).toISOString();
      const [obs, cli] = await Promise.all([
        getJSON(`${NWS}/stations/${p.obs}/observations?${qs({ start: since })}`).catch((e) => ({ error: e.message })),
        p.cli ? getJSON(`${NWS}/products/types/CLI/locations/${p.cli}`).catch((e) => ({ error: e.message })) : null,
      ]);
      const out = [`${p.name} (station ${p.obs})`];
      if (obs.features) {
        const pts = obs.features
          .map((f) => ({ t: f.properties.timestamp, c: f.properties.temperature?.value }))
          .filter((x) => x.c !== null && x.c !== undefined);
        const f = (c) => Math.round((c * 9) / 5 + 32);
        const byDay = {};
        for (const x of pts) {
          const d = new Date(x.t).toLocaleDateString("en-US", { timeZone: p.tz, month: "short", day: "numeric" });
          const v = (byDay[d] ||= { max: -999, min: 999, n: 0 });
          v.max = Math.max(v.max, f(x.c)); v.min = Math.min(v.min, f(x.c)); v.n++;
        }
        for (const [d, v] of Object.entries(byDay)) out.push(`${d}: observed high ${v.max}°F, low ${v.min}°F (${v.n} readings, local standard day may differ)`);
        if (pts[0]) out.push(`Latest reading ${f(pts[0].c)}°F at ${pts[0].t}`);
      } else out.push(`(observations unavailable: ${obs.error})`);
      if (cli && cli["@graph"]?.length) {
        const prod = await getJSON(`${NWS}/products/${cli["@graph"][0].id}`);
        out.push("", "Latest Daily Climate Report:", (prod.productText || "").slice(0, 2500));
      } else if (cli?.error) out.push(`(climate report unavailable: ${cli.error})`);
      return out.join("\n");
    },
  },

  gas_prices: {
    description: "AAA US national average gas and diesel prices: today, yesterday, a week, a month and a year ago (what Kalshi's gas markets settle on).",
    inputSchema: { type: "object", properties: {} },
    async run() {
      const html = await getText("https://gasprices.aaa.com/");
      const date = (html.match(/Price as of\s*([0-9/]+)/i) || [])[1] || "?";
      const block = html.split(/National average gas prices/i)[1] || html;
      const rows = [...block.matchAll(/<tr>\s*<td>\s*([^<]+?)\s*<\/td>((?:\s*<td>\s*\$?[0-9.]+\s*<\/td>)+)/gi)].slice(0, 5);
      if (!rows.length) return `Couldn't parse AAA's page (as of ${date}); its layout may have changed.`;
      const lines = rows.map((r) => {
        const vals = [...r[2].matchAll(/\$?([0-9.]+)/g)].map((x) => x[1]);
        return `${r[1].trim()}: regular $${vals[0]}, mid $${vals[1]}, premium $${vals[2]}, diesel $${vals[3]}${vals[4] ? `, E85 $${vals[4]}` : ""}`;
      });
      return [`AAA national averages, as of ${date}`, ...lines].join("\n");
    },
  },

  edge_calculator: {
    description:
      "Is a bet worth it after Kalshi fees? Give the price you'd pay for the side you'd buy and your probability that side wins. Returns fee, expected profit, and edge per dollar. Fee assumes taker (0.07 rate); pass fee_rate 0 for fee-free markets.",
    inputSchema: {
      type: "object",
      properties: {
        price_cents: { type: "number", description: "Price of the side you'd buy, 1-99" },
        my_probability: { type: "number", description: "Your chance that side wins, 0-1" },
        dollars: { type: "number", description: "Stake in dollars (default 10)" },
        fee_rate: { type: "number", description: "Default 0.07" },
      },
      required: ["price_cents", "my_probability"],
    },
    async run(a) {
      const price = a.price_cents, p = a.my_probability, stake = a.dollars ?? 10, rate = a.fee_rate ?? 0.07;
      const contracts = Math.floor((stake * 100) / price);
      const cost = (contracts * price) / 100;
      const fee = kalshiFee(price, contracts, rate);
      const ev = p * contracts - cost - fee;
      const breakeven = (price + (fee * 100) / contracts) / 100;
      return [
        `${contracts} contracts at ${price}¢ = $${cost.toFixed(2)} + fee $${fee.toFixed(2)}`,
        `Pays $${contracts.toFixed(2)} if it wins (profit $${(contracts - cost - fee).toFixed(2)}); loses $${(cost + fee).toFixed(2)} if not`,
        `Break-even probability ${(breakeven * 100).toFixed(1)}% vs yours ${(p * 100).toFixed(1)}%`,
        `Expected profit $${ev.toFixed(2)} (${((ev / (cost + fee)) * 100).toFixed(1)}% of money risked)`,
        ev > 0 ? "VERDICT: positive edge at your probability." : "VERDICT: no edge after fees.",
      ].join("\n");
    },
  },

  bot_leaderboard: {
    description: "Latest standings of the paper-trading bot tournament (BTC, ETH, SOL, DOGE, XRP), read from the GitHub repo's live/ folder.",
    inputSchema: {
      type: "object",
      properties: {
        league: { type: "string", description: "4h, 1h, 30min or 15min (default all)" },
        coin: { type: "string", description: "BTC, ETH, SOL, DOGE or XRP (default all)" },
      },
    },
    async run(a, env) {
      const repo = env.GITHUB_REPO || "skeatonbeat-bot/bot-lab";
      const leagues = a.league ? [a.league] : ["4h", "1h", "30min", "15min"];
      const coins = a.coin ? [a.coin.toUpperCase().replace(/-USD$/, "")] : ["BTC", "ETH", "SOL", "DOGE", "XRP"];
      const out = [];
      for (const lg of leagues) {
        for (const coin of coins) {
          const file = coin === "BTC" ? lg : `${lg}_${coin}`; // BTC keeps the original file names
          try {
            const csv = await getText(`https://raw.githubusercontent.com/${repo}/main/live/equity_${file}.csv`);
            const lines = csv.trim().split("\n");
            const head = lines[0].split(","), last = lines[lines.length - 1].split(",");
            const bots = head.slice(2).map((name, i) => ({ name, eq: num(last[i + 2]) })).sort((x, y) => y.eq - x.eq);
            out.push(`${lg} league, ${coin}, as of ${last[0]} (${coin} $${last[1]}), ${lines.length - 1} candles:`);
            bots.forEach((b, i) => out.push(`  ${i + 1}. ${b.name}: $${b.eq.toFixed(2)} (${(b.eq - 100).toFixed(1)}%)`));
          } catch (e) {
            out.push(`${lg} league, ${coin}: no data yet (${e.message})`);
          }
        }
      }
      return out.join("\n");
    },
  },
};

// Wide scans: cross-venue matching, each venue's own consistency, crypto fair value.
Object.assign(
  TOOLS,
  makeScanTools({ getJSON, qs, cents, num, compactKalshi, kalshiFee, ladderCheck, table, hoursUntil, KALSHI, GAMMA })
);

// Weather stations used by Kalshi/Polymarket city markets. `obs` = observation
// station, `cli` = NWS Daily Climate Report location code.
const STATIONS = {
  NYC: { name: "New York, Central Park", lat: 40.7789, lon: -73.9692, obs: "KNYC", cli: "NYC", tz: "America/New_York" },
  MDW: { name: "Chicago Midway", lat: 41.7861, lon: -87.7522, obs: "KMDW", cli: "MDW", tz: "America/Chicago" },
  ORD: { name: "Chicago O'Hare", lat: 41.9786, lon: -87.9048, obs: "KORD", cli: "ORD", tz: "America/Chicago" },
  MIA: { name: "Miami Intl", lat: 25.7881, lon: -80.3169, obs: "KMIA", cli: "MIA", tz: "America/New_York" },
  LAX: { name: "Los Angeles Intl", lat: 33.9382, lon: -118.3866, obs: "KLAX", cli: "LAX", tz: "America/Los_Angeles" },
  AUS: { name: "Austin Bergstrom", lat: 30.1831, lon: -97.6799, obs: "KAUS", cli: "AUS", tz: "America/Chicago" },
  DEN: { name: "Denver Intl", lat: 39.8466, lon: -104.6562, obs: "KDEN", cli: "DEN", tz: "America/Denver" },
  PHL: { name: "Philadelphia Intl", lat: 39.8733, lon: -75.2268, obs: "KPHL", cli: "PHL", tz: "America/New_York" },
  SFO: { name: "San Francisco Intl", lat: 37.6197, lon: -122.3647, obs: "KSFO", cli: "SFO", tz: "America/Los_Angeles" },
  HOU: { name: "Houston Hobby", lat: 29.6375, lon: -95.2825, obs: "KHOU", cli: "HOU", tz: "America/Chicago" },
  ATL: { name: "Atlanta Hartsfield", lat: 33.6301, lon: -84.4418, obs: "KATL", cli: "ATL", tz: "America/New_York" },
  BOS: { name: "Boston Logan", lat: 42.3606, lon: -71.0097, obs: "KBOS", cli: "BOS", tz: "America/New_York" },
  DCA: { name: "Washington National", lat: 38.8483, lon: -77.0342, obs: "KDCA", cli: "DCA", tz: "America/New_York" },
  SEA: { name: "Seattle-Tacoma", lat: 47.4447, lon: -122.3144, obs: "KSEA", cli: "SEA", tz: "America/Los_Angeles" },
  PHX: { name: "Phoenix Sky Harbor", lat: 33.4278, lon: -112.0036, obs: "KPHX", cli: "PHX", tz: "America/Phoenix" },
  LAS: { name: "Las Vegas Harry Reid", lat: 36.0719, lon: -115.1634, obs: "KLAS", cli: "LAS", tz: "America/Los_Angeles" },
  MSP: { name: "Minneapolis-St Paul", lat: 44.8831, lon: -93.2289, obs: "KMSP", cli: "MSP", tz: "America/Chicago" },
  DFW: { name: "Dallas-Fort Worth", lat: 32.8975, lon: -97.0219, obs: "KDFW", cli: "DFW", tz: "America/Chicago" },
  SAT: { name: "San Antonio Intl", lat: 29.5443, lon: -98.4839, obs: "KSAT", cli: "SAT", tz: "America/Chicago" },
  NOLA: { name: "New Orleans Armstrong", lat: 29.9934, lon: -90.2580, obs: "KMSY", cli: "MSY", tz: "America/Chicago" },
};

// ---------- MCP over HTTP ----------

function rpcResult(id, result) {
  return { jsonrpc: "2.0", id, result };
}
function rpcError(id, code, message) {
  return { jsonrpc: "2.0", id, error: { code, message } };
}

async function handleRpc(msg, env) {
  const { id, method, params } = msg;
  if (id === undefined || id === null) return null; // notification: no reply
  switch (method) {
    case "initialize":
      return rpcResult(id, {
        protocolVersion: params?.protocolVersion || PROTOCOL,
        capabilities: { tools: { listChanged: false } },
        serverInfo: { name: "edge-finder", version: "1.1.0" },
        instructions:
          "Read-only market research tools for Kalshi and Polymarket plus the public data they settle on (NWS weather, AAA gas, CoinGecko crypto prices). Prices are in cents. Start wide with mismatch_scan (cross-venue gaps and each venue's own inconsistencies) and crypto_fair_value, then confirm any lead with kalshi_market and polymarket_market (rules, times, depth). Matches are candidates, never sure things. Nothing here places trades.",
      });
    case "ping":
      return rpcResult(id, {});
    case "tools/list":
      return rpcResult(id, {
        tools: Object.entries(TOOLS).map(([name, t]) => ({
          name,
          description: t.description,
          inputSchema: t.inputSchema,
          annotations: { readOnlyHint: true, openWorldHint: true },
        })),
      });
    case "tools/call": {
      const tool = TOOLS[params?.name];
      if (!tool) return rpcError(id, -32602, `Unknown tool: ${params?.name}`);
      try {
        const text = await tool.run(params.arguments || {}, env);
        return rpcResult(id, { content: [{ type: "text", text }] });
      } catch (e) {
        return rpcResult(id, { content: [{ type: "text", text: `Error: ${e.message}` }], isError: true });
      }
    }
    default:
      return rpcError(id, -32601, `Method not found: ${method}`);
  }
}

function authorized(request, env, url) {
  const key = env.CONNECTOR_KEY;
  if (!key) return false; // refuse to run open to the world
  const fromPath = url.pathname.split("/")[2];
  const fromHeader = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  return fromPath === key || fromHeader === key;
}

// GitHub's own 15-minute schedule is best-effort and often skips hours, so
// Cloudflare's cron (see wrangler.toml) starts the tournament workflow instead.
// Needs a GITHUB_TOKEN secret: a fine-grained token for this repo only, with
// "Actions: Read and write" permission. Nothing else.
export async function startTournament(env) {
  if (!env.GITHUB_TOKEN) return "skipped: GITHUB_TOKEN not set";
  const repo = env.GITHUB_REPO || "skeatonbeat-bot/bot-lab";
  const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/tournament.yml/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": UA,
    },
    body: JSON.stringify({ ref: "main" }),
  });
  if (r.status !== 204) throw new Error(`GitHub said ${r.status}: ${(await r.text()).slice(0, 200)}`);
  return "started";
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(startTournament(env).then((m) => console.log("tournament:", m)));
  },

  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/health")
      return new Response(
        (env.CONNECTOR_KEY
          ? "edge-finder is running. Key is set. Connect Claude to /mcp/<your key>."
          : "edge-finder is running, but CONNECTOR_KEY is NOT set yet. Add it under Settings > Variables and Secrets.") +
          (env.GITHUB_TOKEN
            ? "\nTournament timer: on (starts the GitHub workflow every 15 minutes)."
            : "\nTournament timer: off. Add a GITHUB_TOKEN secret to turn it on."),
        { status: 200 }
      );
    if (!url.pathname.startsWith("/mcp")) return new Response("Not found", { status: 404 });
    // Wrong or missing key: answer 404, not 401, so clients don't mistake it for an OAuth sign-in.
    if (!authorized(request, env, url))
      return new Response(env.CONNECTOR_KEY ? "Not found (check the key in your URL)" : "Not found (CONNECTOR_KEY not set)", { status: 404 });
    if (request.method === "GET") return new Response("This server answers POST requests only.", { status: 405, headers: { Allow: "POST" } });
    if (request.method === "DELETE") return new Response(null, { status: 204 });
    if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });

    let body;
    try {
      body = await request.json();
    } catch {
      return Response.json(rpcError(null, -32700, "Parse error"), { status: 400 });
    }
    const msgs = Array.isArray(body) ? body : [body];
    const replies = (await Promise.all(msgs.map((m) => handleRpc(m, env)))).filter(Boolean);
    if (!replies.length) return new Response(null, { status: 202 });
    return Response.json(Array.isArray(body) ? replies : replies[0], {
      headers: { "Content-Type": "application/json" },
    });
  },
};

// exported for tests
export { TOOLS, ladderCheck, kalshiFee, compactKalshi, handleRpc };
