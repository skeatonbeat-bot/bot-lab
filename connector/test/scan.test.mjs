// Offline tests for the wide scans. Run: node connector/test/scan.test.mjs
import assert from "node:assert/strict";
import { TOOLS } from "../src/index.js";
import { features } from "../src/scan.js";

// pin the clock 60 seconds into a 5-minute window so the Up/Down checks don't depend on when tests run
const realNow = Date.now;
const pinned = Math.floor(realNow() / 300000) * 300000 + 60000;
Date.now = () => pinned + (realNow() % 1000);
const now = Date.now();
const iso = (h) => new Date(now + h * 3.6e6).toISOString();

// ---- text features ----
let f = features("Will the price of Bitcoin be above $78,000 on November 1?");
assert.ok(f.tokens.has("bitcoin") && f.nums.has(78000) && f.dates.has("11-1") && f.dir === "up");
f = features("BTC price on Nov 1, 2026 at 5pm EST? $78,000 or above");
assert.ok(f.tokens.has("bitcoin") && f.nums.has(78000) && f.dates.has("11-1") && !f.nums.has(2026));
f = features("Will the highest temperature in New York City be between 60-61°F on November 2?");
assert.ok(f.nums.has(60) && f.nums.has(61) && f.tokens.has("newyork") && f.tokens.has("high"));
f = features("Jacksonville wins");
assert.ok(f.tokens.has("jaguar"));
f = features("TSA check-ins above 2.3M");
assert.ok(f.nums.has(2300000));

// ---- fake venues ----
const K = (o) => ({ yes_bid: null, yes_ask: null, no_bid: null, no_ask: null, volume_24h: 1000, open_interest: 500, ...o });
const kalshiMarkets = [
  // NYC bracket event: asks sum to 96 -> internal ARB; 60-61 also mispriced vs Polymarket
  K({ ticker: "KXHIGHNY-26NOV02-T60", event_ticker: "KXHIGHNY-26NOV02", title: "Highest temperature in NYC on Nov 2, 2026?", yes_sub_title: "59° or below", strike_type: "less", cap_strike: 60, yes_bid: 18, yes_ask: 20, no_ask: 82, close_time: iso(30) }),
  K({ ticker: "KXHIGHNY-26NOV02-B60.5", event_ticker: "KXHIGHNY-26NOV02", title: "Highest temperature in NYC on Nov 2, 2026?", yes_sub_title: "60° to 61°", strike_type: "between", floor_strike: 60, cap_strike: 61, yes_bid: 28, yes_ask: 30, no_ask: 72, close_time: iso(30) }),
  K({ ticker: "KXHIGHNY-26NOV02-B62.5", event_ticker: "KXHIGHNY-26NOV02", title: "Highest temperature in NYC on Nov 2, 2026?", yes_sub_title: "62° to 63°", strike_type: "between", floor_strike: 62, cap_strike: 63, yes_bid: 30, yes_ask: 32, no_ask: 70, close_time: iso(30) }),
  K({ ticker: "KXHIGHNY-26NOV02-T63", event_ticker: "KXHIGHNY-26NOV02", title: "Highest temperature in NYC on Nov 2, 2026?", yes_sub_title: "64° or above", strike_type: "greater", floor_strike: 63, yes_bid: 12, yes_ask: 14, no_ask: 88, close_time: iso(30) }),
  // gas ladder with an inversion
  K({ ticker: "KXAAAGASW-26NOV09-4.10", event_ticker: "KXAAAGASW-26NOV09", title: "Gas price this week?", yes_sub_title: "Above 4.10", strike_type: "greater", floor_strike: 4.1, yes_bid: 60, yes_ask: 62, no_ask: 40, close_time: iso(150) }),
  K({ ticker: "KXAAAGASW-26NOV09-4.15", event_ticker: "KXAAAGASW-26NOV09", title: "Gas price this week?", yes_sub_title: "Above 4.15", strike_type: "greater", floor_strike: 4.15, yes_bid: 66, yes_ask: 68, no_ask: 34, close_time: iso(150) }),
  // House control: Kalshi 88 vs Polymarket ~95
  K({ ticker: "CONTROLH-2026-D", event_ticker: "CONTROLH-2026", title: "Who will control the House?", yes_sub_title: "Democratic Party", yes_bid: 87, yes_ask: 88, no_ask: 13, close_time: iso(160) }),
  // NFL: Kalshi city name vs Polymarket nickname
  K({ ticker: "KXNFLGAME-26NOV05JACBAL-JAC", event_ticker: "KXNFLGAME-26NOV05JACBAL", title: "Jacksonville at Baltimore winner?", yes_sub_title: "Jacksonville", yes_bid: 41, yes_ask: 42, no_ask: 59, close_time: iso(100) }),
  // crypto: settles 5pm ET on Kalshi vs noon ET on Polymarket -> must NOT be paired
  K({ ticker: "KXBTCD-26NOV0117-T77999.99", event_ticker: "KXBTCD-26NOV0117", title: "BTC price on Nov 1, 2026 at 5pm EST?", yes_sub_title: "$78,000 or above", strike_type: "greater", floor_strike: 77999.99, yes_bid: 20, yes_ask: 21, no_ask: 80, close_time: iso(10) }),
  // trap: same number, different subject
  K({ ticker: "KXTGTFT-26NOV08-T85", event_ticker: "KXTGTFT-26NOV08", title: "Will Target Foot Traffic for October 2026 be above 85?", yes_sub_title: "Above 85", yes_bid: 10, yes_ask: 12, no_ask: 90, close_time: iso(150) }),
  // parlay: always skipped
  K({ ticker: "KXMVECROSS-1", event_ticker: "KXMVECROSS-1", title: "Parlay", yes_bid: 1, yes_ask: 2, close_time: iso(5) }),
];

const P = (o) => ({ outcomes: '["Yes","No"]', volume24hr: 5000, ...o });
const gammaMarkets = [
  P({ question: "Will the highest temperature in New York City be between 60-61°F on November 2?", outcomePrices: '["0.40","0.60"]', bestBid: 0.39, bestAsk: 0.41, endDate: iso(28), slug: "nyc-high-nov2-60-61" }),
  P({ question: "Will the Democratic Party control the House after the 2026 Midterm elections?", outcomePrices: '["0.95","0.05"]', bestBid: 0.94, bestAsk: 0.95, endDate: iso(70), slug: "dem-house" }),
  P({ question: "Jaguars vs. Ravens", outcomes: '["Jaguars","Ravens"]', outcomePrices: '["0.49","0.51"]', bestBid: 0.48, bestAsk: 0.50, endDate: iso(98), slug: "nfl-jax-bal" }),
  P({ question: "Will the price of Bitcoin be above $78,000 on November 1?", outcomePrices: '["0.35","0.65"]', bestBid: 0.34, bestAsk: 0.36, endDate: iso(5), slug: "btc-above-78k-nov1" }),
  P({ question: "Will the price of Bitcoin be above $85,000 on November 1?", outcomePrices: '["0.01","0.99"]', bestBid: 0.0, bestAsk: 0.01, endDate: iso(5), slug: "btc-above-85k-nov1" }),
  P({ question: "Will Bitcoin dip to $74,000 October 26-November 1?", outcomePrices: '["0.20","0.80"]', bestBid: 0.19, bestAsk: 0.21, endDate: iso(5), slug: "btc-dip-74k" }),
  // a multi-outcome event whose Yes asks sum to 95
  P({ question: "Will Alice win the Springfield mayoral race?", outcomePrices: '["0.50","0.50"]', bestBid: 0.49, bestAsk: 0.50, endDate: iso(60), slug: "alice", negRisk: true, negRiskMarketID: "0xabc", events: [{ id: "77", title: "Springfield mayor" }] }),
  P({ question: "Will Bob win the Springfield mayoral race?", outcomePrices: '["0.30","0.70"]', bestBid: 0.29, bestAsk: 0.30, endDate: iso(60), slug: "bob", negRisk: true, negRiskMarketID: "0xabc", events: [{ id: "77", title: "Springfield mayor" }] }),
  P({ question: "Will Carol win the Springfield mayoral race?", outcomePrices: '["0.15","0.85"]', bestBid: 0.14, bestAsk: 0.15, endDate: iso(60), slug: "carol", negRisk: true, negRiskMarketID: "0xabc", events: [{ id: "77", title: "Springfield mayor" }] }),
];

const btcEvents = { events: [{ title: "BTC price on Nov 1, 2026 at 5pm EST?", markets: [
  { ticker: "KXBTCD-26NOV0117-T77999.99", yes_sub_title: "$78,000 or above", strike_type: "greater", floor_strike: 77999.99, yes_bid: 20, yes_ask: 21, no_ask: 80, close_time: iso(10) },
  { ticker: "KXBTCD-26NOV0117-T75999.99", yes_sub_title: "$76,000 or above", strike_type: "greater", floor_strike: 75999.99, yes_bid: 80, yes_ask: 82, no_ask: 20, close_time: iso(10) },
] }] };
const btcRange = { events: [{ title: "BTC price range on Nov 1, 2026 at 5pm EST?", markets: [
  { ticker: "KXBTC-26NOV0117-B77750", yes_sub_title: "$77,500 to 77,999.99", strike_type: "between", floor_strike: 77500, cap_strike: 77999.99, yes_bid: 30, yes_ask: 32, no_ask: 70, close_time: iso(10) },
] }] };
// 7 days of hourly prices with ~0.4% hourly moves (about 37% a year)
const chart = { prices: Array.from({ length: 169 }, (_, i) => [now - (168 - i) * 3.6e6, 77800 * Math.exp(0.004 * Math.sin(i * 1.7) * (i % 2 ? 1 : -1))]) };

// 5 hours of 1-minute prices, quiet: ~0.02% a minute (about 15% a year), ending at 77,800
const curStart = Math.floor(now / 1000 / 300) * 300;
const minutes = Array.from({ length: 300 }, (_, i) => {
  const t = Math.floor(now / 60000) * 60 - (299 - i) * 60;
  const p = (t >= curStart ? 77700 : 77750) * Math.exp(0.00005 * (i % 2 ? 1 : -1));
  return [t, p, p, p, p, 1];
}).reverse();
const upDown = { question: "Bitcoin Up or Down - November 1, 8:00AM-8:05AM ET", outcomes: '["Up","Down"]', outcomePrices: '["0.5","0.5"]', bestBid: 0.49, bestAsk: 0.51, endDate: new Date((curStart + 300) * 1000).toISOString(), slug: `btc-updown-5m-${curStart}` };

let coingeckoDown = false;
const seen = [];
globalThis.fetch = async (url) => {
  url = String(url);
  seen.push(url);
  const json = (o) => new Response(JSON.stringify(o), { status: 200, headers: { "Content-Type": "application/json" } });
  if (url.includes("api.coingecko.com")) {
    if (coingeckoDown) return new Response("rate limited", { status: 429 });
    if (url.includes("/simple/price")) return json({ bitcoin: { usd: 77800 } });
    if (url.includes("/market_chart")) return json(chart);
  }
  if (url.includes("api.exchange.coinbase.com")) {
    if (url.endsWith("/ticker")) return json({ price: "77790.00" });
    if (url.includes("/candles") && url.includes("granularity=60")) return json(minutes);
    if (url.includes("/candles")) return json(chart.prices.slice().reverse().map(([t, p]) => [t / 1000, p, p, p, p, 1]));
  }
  if (url.includes("kalshi.com") && url.includes("/events?") && url.includes("series_ticker=KXBTCD&")) return json(btcEvents);
  if (url.includes("kalshi.com") && url.includes("/events?") && url.includes("series_ticker=KXBTC&")) return json(btcRange);
  if (url.includes("kalshi.com") && url.includes("/events?")) return json({ events: [] });
  if (url.includes("kalshi.com") && url.includes("/markets?")) return json({ markets: kalshiMarkets, cursor: "" });
  if (url.includes("gamma-api.polymarket.com/markets?") && url.includes("updown")) return json(url.includes(`slug=${upDown.slug}`) ? [upDown] : []);
  if (url.includes("gamma-api.polymarket.com/markets?") && url.includes("slug=")) return json(gammaMarkets.filter((m) => url.includes(`slug=${m.slug}`)).map((m) => ({ ...m, description: "Resolves Yes if NOAA reports a high of 60-61°F in Central Park.", liquidityNum: 12000 })));
  if (url.includes("gamma-api.polymarket.com/markets?")) return json(url.includes("offset=0") ? gammaMarkets : []);
  throw new Error("unmocked " + url);
};

const run = async (name, args = {}) => {
  const text = await TOOLS[name].run(args, {});
  console.log(`\n=== ${name} ${JSON.stringify(args)}\n${text}`);
  return text;
};

// ---- mismatch_scan ----
let t = await run("mismatch_scan", {});
const cross = t.split("KALSHI-INTERNAL")[0];
assert.ok(cross.includes("KXHIGHNY-26NOV02-B60.5") && cross.includes("nyc-high-nov2-60-61"), "NYC bracket pairs with its Polymarket twin");
assert.ok(cross.includes("CONTROLH-2026-D") && cross.includes("dem-house"), "House control pairs");
assert.ok(cross.includes("KXNFLGAME-26NOV05JACBAL-JAC") && cross.includes("nfl-jax-bal"), "city name pairs with nickname");
assert.ok(!cross.includes("KXBTCD"), "crypto with different settle times is left to crypto_fair_value");
assert.ok(!cross.includes("KXTGTFT"), "same number, different subject: no pair");
assert.ok(!t.includes("KXMVE"), "parlays skipped");
assert.ok(/Kalshi YES 88¢ vs Poly 95¢[^\n]*CONTROLH-2026-D/.test(cross), "House: Kalshi YES is the cheap side");
assert.ok(/Kalshi YES 42¢ vs Poly 49¢[^\n]*KXNFLGAME-26NOV05JACBAL-JAC/.test(cross), "NFL: Jaguars side, not Ravens");
assert.ok(/Kalshi YES 30¢ vs Poly 40¢[^\n]*KXHIGHNY-26NOV02-B60\.5[^\n]*CROSS-ARB\*/.test(cross), "NYC 60-61: quotes cross outright");
const internal = t.split("KALSHI-INTERNAL")[1];
assert.ok(internal.includes("KXHIGHNY-26NOV02") && internal.includes("POSSIBLE ARB"), "bracket asks summing to 96 flagged");
assert.ok(internal.includes("KXAAAGASW-26NOV09") && internal.includes("INVERSION"), "ladder inversion flagged");
assert.ok(t.includes("Springfield mayor") && t.includes("Yes asks sum to 95¢"), "Polymarket multi-outcome sum flagged");

t = await run("mismatch_scan", { text: "house", sections: "cross" });
assert.ok(t.includes("CONTROLH") && !t.includes("KXHIGHNY") && !t.includes("KALSHI-INTERNAL"));
t = await run("mismatch_scan", { min_gap: 50 });
assert.ok(t.includes("CROSS-VENUE (0)"));

// ---- crypto_fair_value ----
t = await run("crypto_fair_value", { coin: "BTC", hours: 48, min_edge: 0 });
assert.ok(t.includes("spot $77,800 (CoinGecko)"));
assert.ok(t.includes("KXBTCD-26NOV0117-T77999.99") && t.includes("KXBTC-26NOV0117-B77750") && t.includes("btc-above-78k-nov1") && t.includes("btc-dip-74k"));
const vol = Number(/volatility (\d+)%/.exec(t)[1]);
assert.ok(vol > 10 && vol < 200, `sane volatility, got ${vol}%`);
const short = Number(/, (\d+)% from the last (\d+) minutes/.exec(t)[1]);
assert.ok(short > 5 && short < 30 && short < vol, `quiet last 2 hours gives a lower short-window volatility, got ${short}%`);
// 5-minute window started at 77,700 (Coinbase 1-min open), spot now 77,800, Up at 51 -> Up looks cheap
assert.ok(new RegExp(`YES at 51¢[^\\n]*Polymarket[^\\n]*Up from \\$77,(69|70)[^\\n]*btc-updown-5m-${curStart}`).test(t), "prices the live 5-minute Up or Down window off its start price");
coingeckoDown = true;
t = await run("crypto_fair_value", { coin: "bitcoin", min_edge: 0 });
assert.ok(t.includes("spot $77,790 (Coinbase"), "falls back to Coinbase when CoinGecko fails");
t = await run("crypto_fair_value", { coin: "bitcoin", vol: 0.5, min_edge: 0 });
assert.ok(t.includes("50% a year (your override)"));
await assert.rejects(() => TOOLS.crypto_fair_value.run({ coin: "pepe" }, {}), /Unknown coin/);

// ---- polymarket_market ----
t = await run("polymarket_market", { slug: "nyc-high-nov2-60-61" });
assert.ok(t.includes("RULES: Resolves Yes if NOAA") && t.includes("39/41¢"));
await assert.rejects(() => TOOLS.polymarket_market.run({ slug: "nope" }, {}), /No Polymarket market/);

console.log("\nALL SCAN TESTS PASSED");
