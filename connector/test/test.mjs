// Offline test: fake the outside APIs, then talk to the worker the way Claude does.
// Run: node connector/test/test.mjs
import assert from "node:assert/strict";
import worker from "../src/index.js";

const now = Date.now();
const iso = (h) => new Date(now + h * 3.6e6).toISOString();

const kalshiMarkets = {
  markets: [
    { ticker: "KXAAAGASD-26NOV03-4.20", event_ticker: "KXAAAGASD-26NOV03", title: "Gas price on Nov 3?", yes_sub_title: "Above $4.20", yes_bid_dollars: "0.20", yes_ask_dollars: "0.21", no_bid_dollars: "0.79", no_ask_dollars: "0.82", last_price_dollars: "0.21", volume_24h: 51000, open_interest: 9000, close_time: iso(50), floor_strike: 4.2, strike_type: "greater" },
    { ticker: "KXMVECROSSCATEGORY-X", event_ticker: "KXMVECROSSCATEGORY-X", title: "Parlay", yes_bid: 3, yes_ask: 5, volume_24h: 99999, close_time: iso(5) },
    { ticker: "KXNHLGAME-VGKDET", event_ticker: "KXNHLGAME-VGKDET", title: "Vegas at Detroit", yes_bid: 50, yes_ask: 52, volume_24h: 80000, close_time: iso(5) },
    { ticker: "KXTSAW-26NOV01-2.3", event_ticker: "KXTSAW-26NOV01", title: "TSA weekly average", yes_sub_title: "Above 2.3M", yes_bid: 74, yes_ask: 77, no_bid: 23, no_ask: 40, last_price: 76, volume_24h: 1000, open_interest: 500, close_time: iso(30) },
  ],
  cursor: "",
};

const ladderEvent = {
  event: {
    title: "Gas price on Nov 3", series_ticker: "KXAAAGASD", mutually_exclusive: false,
    markets: [
      { ticker: "G-4.05", yes_sub_title: "Above $4.05", floor_strike: 4.05, strike_type: "greater", yes_bid: 95, yes_ask: 96, close_time: iso(50) },
      { ticker: "G-4.20", yes_sub_title: "Above $4.20", floor_strike: 4.20, strike_type: "greater", yes_bid: 20, yes_ask: 21, close_time: iso(50) },
      { ticker: "G-4.25", yes_sub_title: "Above $4.25", floor_strike: 4.25, strike_type: "greater", yes_bid: 25, yes_ask: 27, close_time: iso(50) }, // inverted on purpose
    ],
  },
};

const bracketEvent = {
  event: {
    title: "Highest temperature in NYC on Nov 1", series_ticker: "KXHIGHNY", mutually_exclusive: true,
    markets: [
      { ticker: "H-A", yes_sub_title: "57° or below", yes_bid: 20, yes_ask: 22, strike_type: "less", cap_strike: 57, close_time: iso(30) },
      { ticker: "H-B", yes_sub_title: "58° to 59°", yes_bid: 30, yes_ask: 32, strike_type: "between", floor_strike: 58, close_time: iso(30) },
      { ticker: "H-C", yes_sub_title: "60° to 61°", yes_bid: 28, yes_ask: 30, strike_type: "between", floor_strike: 60, close_time: iso(30) },
      { ticker: "H-D", yes_sub_title: "62° or above", yes_bid: 10, yes_ask: 12, strike_type: "greater", floor_strike: 62, close_time: iso(30) },
    ],
  },
};

const gamma = [
  { question: "Bitcoin above $78,000 on November 6?", outcomes: '["Yes","No"]', outcomePrices: '["0.48","0.52"]', bestBid: 0.47, bestAsk: 0.49, volume24hr: 12000.5, endDate: iso(120), slug: "btc-78k" },
  { question: "Highest temperature in NYC on November 1?", outcomes: '["Yes","No"]', outcomePrices: '["0.5","0.5"]', bestBid: 0.49, bestAsk: 0.51, volume24hr: 800, endDate: iso(30), slug: "nyc" },
];

const nwsPoints = { properties: { timeZone: "America/New_York", forecast: "https://api.weather.gov/gridpoints/OKX/33,37/forecast", forecastHourly: "https://api.weather.gov/gridpoints/OKX/33,37/forecast/hourly" } };
const nwsForecast = { properties: { periods: [
  { name: "Tonight", temperature: 48, temperatureUnit: "F", probabilityOfPrecipitation: { value: 10 }, shortForecast: "Clear" },
  { name: "Sunday", temperature: 61, temperatureUnit: "F", probabilityOfPrecipitation: { value: 20 }, shortForecast: "Sunny" },
] } };
const nwsHourly = { properties: { periods: Array.from({ length: 30 }, (_, i) => ({ startTime: iso(i), temperature: 50 + (i % 12), probabilityOfPrecipitation: { value: i % 5 } })) } };
const nwsObs = { features: [
  { properties: { timestamp: iso(-1), temperature: { value: 15.6 } } },
  { properties: { timestamp: iso(-3), temperature: { value: 16.7 } } },
  { properties: { timestamp: iso(-6), temperature: { value: 10.0 } } },
] };
const cliList = { "@graph": [{ id: "abc-123" }] };
const cliProd = { productText: "CLIMATE REPORT\nNATIONAL WEATHER SERVICE NEW YORK NY\n...MAXIMUM 62 ...MINIMUM 48" };
const aaaHtml = `<p>Price as of 10/31/26</p><h3>National average gas prices</h3><table>
<tr><td>Current Avg.</td><td>$4.184</td><td>$4.6</td><td>$5.0</td><td>$6.310</td><td>$3.5</td></tr>
<tr><td>Yesterday Avg.</td><td>$4.205</td><td>$4.6</td><td>$5.0</td><td>$6.320</td><td>$3.5</td></tr></table>`;
const equityCsv = "time,btc_price,Benchmark,Trend Rider\n2026-10-31 16:00:00+00:00,77300,100.1,99.5\n2026-10-31 20:00:00+00:00,77498,100.4,101.2\n";

const seen = [];
globalThis.fetch = async (url) => {
  url = String(url);
  seen.push(url);
  const json = (o) => new Response(JSON.stringify(o), { status: 200, headers: { "Content-Type": "application/json" } });
  const text = (t) => new Response(t, { status: 200 });
  if (url.includes("/markets?") && url.includes("mve_filter")) return new Response("bad param", { status: 400 }); // exercise the fallback
  if (url.includes("kalshi.com") && url.includes("/markets?")) return json(kalshiMarkets);
  if (url.includes("/events/GASLADDER")) return json(ladderEvent);
  if (url.includes("/events/NYCHIGH")) return json(bracketEvent);
  if (url.includes("/events?")) return json({ events: [{ event_ticker: "KXHIGHNY-26NOV01", title: "Highest temperature in NYC", sub_title: "On Nov 1" }] });
  if (url.includes("/orderbook")) return json({ orderbook: { yes: [[20, 100], [19, 50]], no_dollars: [["0.79", 40], ["0.78", 10]] } });
  if (url.includes("kalshi.com") && url.includes("/markets/")) return json({ market: { ...kalshiMarkets.markets[0], rules_primary: "Resolves Yes if the AAA national average on Nov 3 is above $4.20." } });
  if (url.includes("gamma-api")) return json(gamma);
  if (url.includes("/points/")) return json(nwsPoints);
  if (url.endsWith("/forecast/hourly")) return json(nwsHourly);
  if (url.endsWith("/forecast")) return json(nwsForecast);
  if (url.includes("/observations")) return json(nwsObs);
  if (url.includes("/products/types/CLI")) return json(cliList);
  if (url.includes("/products/abc-123")) return json(cliProd);
  if (url.includes("gasprices.aaa.com")) return text(aaaHtml);
  if (url.includes("raw.githubusercontent.com") && url.endsWith("equity_4h.csv")) return text(equityCsv);
  if (url.includes("raw.githubusercontent.com") && url.endsWith("equity_4h_SOL.csv"))
    return text(equityCsv.replace("btc_price", "sol_price"));
  if (url.includes("raw.githubusercontent.com")) return new Response("404", { status: 404 });
  throw new Error("unmocked " + url);
};

const env = { CONNECTOR_KEY: "secret123", GITHUB_REPO: "skeatonbeat-bot/bot-lab" };
const call = async (path, body, headers = {}) => {
  const r = await worker.fetch(new Request(`https://x.workers.dev${path}`, { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json", ...headers } }), env);
  return { status: r.status, body: r.status === 202 ? null : await r.json().catch(() => null) };
};

// auth
assert.equal((await call("/mcp", { jsonrpc: "2.0", id: 1, method: "ping" })).status, 404);
assert.equal((await call("/mcp/wrong", { jsonrpc: "2.0", id: 1, method: "ping" })).status, 404);
assert.equal((await call("/mcp", { jsonrpc: "2.0", id: 1, method: "ping" }, { Authorization: "Bearer secret123" })).status, 200);
const openEnv = await worker.fetch(new Request("https://x/mcp/x", { method: "POST", body: "{}" }), {});
assert.equal(openEnv.status, 404, "no key configured must refuse");

// handshake
const init = await call("/mcp/secret123", { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "1" } } });
assert.equal(init.body.result.serverInfo.name, "edge-finder");
assert.equal((await call("/mcp/secret123", { jsonrpc: "2.0", method: "notifications/initialized" })).status, 202);
const list = await call("/mcp/secret123", { jsonrpc: "2.0", id: 2, method: "tools/list" });
const names = list.body.result.tools.map((t) => t.name);
console.log("tools:", names.join(", "));

const run = async (name, args) => {
  const r = await call("/mcp/secret123", { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name, arguments: args } });
  const res = r.body.result;
  console.log(`\n=== ${name} ${JSON.stringify(args)} ${res.isError ? "(ERROR)" : ""}\n${res.content[0].text}`);
  return res;
};

let r = await run("kalshi_markets_closing_soon", { hours: 72 });
assert.ok(!r.isError && r.content[0].text.includes("KXAAAGASD") && !r.content[0].text.includes("KXMVE") && !r.content[0].text.includes("NHL"));
r = await run("kalshi_markets_closing_soon", { text: "tsa" });
assert.ok(r.content[0].text.includes("KXTSAW") && !r.content[0].text.includes("KXAAAGASD"));
r = await run("kalshi_event", { event_ticker: "GASLADDER" });
assert.ok(r.content[0].text.includes("INVERSION"));
r = await run("kalshi_event", { event_ticker: "NYCHIGH" });
assert.ok(r.content[0].text.includes("sum to 96") && r.content[0].text.includes("POSSIBLE ARB"));
r = await run("kalshi_market", { ticker: "KXAAAGASD-26NOV03-4.20" });
assert.ok(r.content[0].text.includes("20¢ x 100") && r.content[0].text.includes("79¢ x 40") && r.content[0].text.includes("RULES"));
r = await run("kalshi_series_events", { series_ticker: "KXHIGHNY" });
assert.ok(r.content[0].text.includes("KXHIGHNY-26NOV01"));
r = await run("polymarket_find", { text: "bitcoin" });
assert.ok(r.content[0].text.includes("Yes 48¢") && !r.content[0].text.includes("NYC"));
r = await run("weather_forecast", { station: "NYC" });
assert.ok(r.content[0].text.includes("Sunday: 61°F"));
r = await run("weather_observed", { station: "nyc" });
assert.ok(r.content[0].text.includes("observed high 62°F") && r.content[0].text.includes("MAXIMUM 62"));
r = await run("gas_prices", {});
assert.ok(r.content[0].text.includes("regular $4.184") && r.content[0].text.includes("diesel $6.310"));
r = await run("edge_calculator", { price_cents: 82, my_probability: 0.88, dollars: 25 });
assert.ok(r.content[0].text.includes("30 contracts") && r.content[0].text.includes("positive edge"));
r = await run("edge_calculator", { price_cents: 82, my_probability: 0.8 });
assert.ok(r.content[0].text.includes("no edge"));
r = await run("bot_leaderboard", {});
assert.ok(r.content[0].text.includes("Trend Rider: $101.20") && r.content[0].text.includes("15min league, BTC: no data yet"));
assert.ok(r.content[0].text.includes("4h league, SOL, as of") && r.content[0].text.includes("4h league, ETH: no data yet"));
r = await run("bot_leaderboard", { league: "4h", coin: "sol" });
assert.ok(r.content[0].text.includes("4h league, SOL") && !r.content[0].text.includes("BTC"));
r = await run("weather_forecast", {});
assert.ok(r.isError);
const bad = await call("/mcp/secret123", { jsonrpc: "2.0", id: 9, method: "tools/call", params: { name: "nope" } });
assert.ok(bad.body.error);
console.log("\nALL TESTS PASSED");
