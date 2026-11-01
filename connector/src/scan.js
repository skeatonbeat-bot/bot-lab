/**
 * Wide mismatch hunting for Edge Finder.
 *
 *  mismatch_scan      Kalshi vs Polymarket on the same question, plus each
 *                     venue's own brackets / ladders / multi-outcome sums.
 *  crypto_fair_value  Every Kalshi and Polymarket crypto price market priced
 *                     off CoinGecko spot and recent volatility.
 *  polymarket_market  One Polymarket market in full, with its rules.
 *
 * Matching across venues is text-based and imperfect: every pair it returns is
 * a CANDIDATE to check against both venues' rules, never a sure thing.
 */

const COINGECKO = "https://api.coingecko.com/api/v3";
const COINBASE = "https://api.exchange.coinbase.com";

// ---------- text features ----------

const MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
const MONTH_RE = "(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)";
const DATE_RE = new RegExp(`\\b${MONTH_RE}\\.?\\s+(\\d{1,2})(?:st|nd|rd|th)?\\b(?:,?\\s*(20\\d\\d))?`, "g");
const MONTH_ONLY_RE = new RegExp(`\\b${MONTH_RE}\\b`, "g");
const ISO_RE = /\b(20\d\d)-(\d\d)-(\d\d)\b/g;
const NUM_RE = /(\$?)(\d[\d,]*(?:\.\d+)?)\s*(k\b|m\b|mm\b|b\b|bn\b|million|billion|thousand|%|°)?/g;
const UP_RE = /\b(above|over|greater|more than|higher|at least|or more|or above|exceed\w*)\b|>/;
const DOWN_RE = /\b(below|under|less than|lower|at most|or less|or below)\b|</;

const STOP = new Set(("will the be on in of a an at by to for than and or is are was what who which how many much this that " +
  "with from yes no market price prices end ending before after during next any its it as get gets do does did " +
  "above over greater more higher least exceed exceeds below under less lower most between and/or est edt et pm am " +
  "there their win wins winner vs").split(/\s+/));

const SYN = {
  btc: "bitcoin", eth: "ethereum", ether: "ethereum", sol: "solana", doge: "dogecoin", ripple: "xrp",
  gop: "republican", republicans: "republican", rep: "republican", democrats: "democrat", democratic: "democrat", dem: "democrat", dems: "democrat",
  federal: "fed", fomc: "fed", cpi: "inflation", nyc: "newyork", "new york": "newyork", la: "losangeles",
  temp: "temperature", temps: "temperature", highest: "high", maximum: "high", max: "high",
  usa: "us", "united states": "us",
};

// City -> team nickname for unambiguous NFL/NBA/NHL/MLB cities (Kalshi uses cities, Polymarket often nicknames).
const TEAMS = {
  arizona: "cardinals", atlanta: "falcons", baltimore: "ravens", buffalo: "bills", carolina: "panthers", chicago: "bears",
  cincinnati: "bengals", cleveland: "browns", dallas: "cowboys", denver: "broncos", detroit: "lions", "green bay": "packers",
  houston: "texans", indianapolis: "colts", jacksonville: "jaguars", "kansas city": "chiefs", "las vegas": "raiders",
  miami: "dolphins", minnesota: "vikings", "new england": "patriots", "new orleans": "saints", philadelphia: "eagles",
  pittsburgh: "steelers", "san francisco": "49ers", seattle: "seahawks", "tampa bay": "buccaneers", tennessee: "titans",
  washington: "commanders", boston: "celtics", brooklyn: "nets", charlotte: "hornets", milwaukee: "bucks", memphis: "grizzlies",
  "oklahoma city": "thunder", orlando: "magic", phoenix: "suns", portland: "blazers", sacramento: "kings", "san antonio": "spurs",
  toronto: "raptors", utah: "jazz", "golden state": "warriors",
};

const roundNum = (v) => Number(v.toPrecision(5));

// lookups built once: single words, and two-word names checked as pairs
const SYN1 = new Map(), SYN2 = new Map(), TEAM1 = new Map(), TEAM2 = new Map();
for (const [k, v] of Object.entries(SYN)) (k.includes(" ") ? SYN2 : SYN1).set(k, v);
for (const [k, v] of Object.entries(TEAMS)) (k.includes(" ") ? TEAM2 : TEAM1).set(k, v);

function addToken(tokens, w) {
  if (SYN1.has(w)) w = SYN1.get(w);
  if (!w || w.length < 2 || STOP.has(w) || (w.charCodeAt(0) <= 57 && /^\d+$/.test(w))) return;
  if (w.length > 4 && w.endsWith("s") && !w.endsWith("ss")) w = w.slice(0, -1);
  tokens.add(w);
}

export function features(text) {
  let s = ` ${(text || "").toLowerCase().replace(/[’']/g, "")} `;
  const dates = new Set();
  if (s.includes("-")) s = s.replace(ISO_RE, (_, y, m, d) => { dates.add(`${+m}-${+d}`); return " "; });
  s = s.replace(DATE_RE, (_, mon, d) => { dates.add(`${MONTHS[mon.slice(0, 3)]}-${+d}`); return " "; });
  const tokens = new Set();
  s = s.replace(MONTH_ONLY_RE, (mon) => { tokens.add(`m${MONTHS[mon.slice(0, 3)]}`); return " "; });
  const dir = UP_RE.test(s) ? "up" : DOWN_RE.test(s) ? "down" : null;
  const nums = new Set();
  s = s.replace(NUM_RE, (all, dollar, raw, unit) => {
    let v = parseFloat(raw.replace(/,/g, ""));
    if (!Number.isFinite(v)) return " ";
    if (!dollar && !unit && /^(19|20)\d\d$/.test(raw)) return " "; // a year
    const u = (unit || "").toLowerCase();
    if (u === "k" || u === "thousand") v *= 1e3;
    else if (u === "m" || u === "mm" || u === "million") v *= 1e6;
    else if (u === "b" || u === "bn" || u === "billion") v *= 1e9;
    nums.add(roundNum(v));
    return " ";
  });
  const words = s.split(/[^a-z0-9]+/);
  for (let i = 0; i < words.length; i++) {
    const w = words[i];
    if (!w) continue;
    const pair = i + 1 < words.length ? `${w} ${words[i + 1]}` : "";
    if (pair && SYN2.has(pair)) { addToken(tokens, SYN2.get(pair)); i++; continue; }
    if (pair && TEAM2.has(pair)) addToken(tokens, TEAM2.get(pair));
    if (TEAM1.has(w)) addToken(tokens, TEAM1.get(w));
    addToken(tokens, w);
  }
  return { tokens, nums, dates, dir };
}

// Kalshi tickers carry the event date: KXHIGHNY-26NOV01, KXNFLGAME-26NOV05JACBAL, KXBTCD-26NOV0117.
function kalshiTickerDate(ticker) {
  const m = /-(\d\d)(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d\d)/.exec(ticker || "");
  return m ? `${MONTHS[m[2].toLowerCase()]}-${+m[3]}` : null;
}

const CRYPTO_WORDS = /\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin|doge)\b/i;
const PRICE_WORDS = /\b(price|above|below|between|range|reach|dip)\b/i;

// ---------- helpers shared by the tools ----------

export function makeScanTools(h) {
  const { getJSON, qs, cents, num, compactKalshi, kalshiFee, ladderCheck, table, hoursUntil, KALSHI, GAMMA } = h;

  const feeC = (priceCents) => Math.round(kalshiFee(priceCents, 100)); // Kalshi taker fee in cents per contract

  async function kalshiOpen(hours, pages) {
    const maxTs = Math.floor(Date.now() / 1000 + hours * 3600);
    const out = [];
    let cursor;
    for (let i = 0; i < pages; i++) {
      const base = { status: "open", max_close_ts: maxTs, limit: 1000, cursor };
      const d = await getJSON(`${KALSHI}/markets?${qs({ ...base, mve_filter: "exclude" })}`).catch(() => getJSON(`${KALSHI}/markets?${qs(base)}`));
      out.push(...(d.markets || []).filter((m) => !/^KXMVE/i.test(m.ticker || "")));
      cursor = d.cursor;
      if (!cursor) break;
    }
    return { markets: out, more: !!cursor };
  }

  async function polyOpen(hours, pages) {
    const end = new Date(Date.now() + hours * 3.6e6).toISOString();
    const out = [];
    for (let i = 0; i < pages; i++) {
      const d = await getJSON(`${GAMMA}/markets?${qs({ active: true, closed: false, end_date_min: new Date().toISOString(), end_date_max: end, order: "volume24hr", ascending: false, limit: 500, offset: i * 500 })}`);
      const arr = Array.isArray(d) ? d : d.markets || [];
      out.push(...arr);
      if (arr.length < 500) break;
    }
    return out;
  }

  function polyRow(m) {
    let outs = [], px = [];
    try { outs = JSON.parse(m.outcomes || "[]"); px = JSON.parse(m.outcomePrices || "[]"); } catch {}
    const c = (x) => (x === undefined || x === null || x === "" ? null : Math.round(num(x) * 100));
    return {
      question: m.question || "",
      item: m.groupItemTitle || "",
      outcomes: outs,
      mids: px.map((x) => c(x)),
      bid0: c(m.bestBid),
      ask0: c(m.bestAsk),
      vol: num(m.volume24hr) || 0,
      ends: m.endDate,
      slug: m.slug,
      negRisk: !!m.negRisk,
      group: m.negRiskMarketID || m.events?.[0]?.id || null,
      groupTitle: m.events?.[0]?.title || "",
    };
  }

  // Yes-side quotes for outcome `idx` of a Polymarket market, in cents.
  function polyQuote(p, idx) {
    if (p.bid0 === null || p.ask0 === null) return null;
    const mid = p.mids[idx] ?? (idx === 0 ? (p.bid0 + p.ask0) / 2 : 100 - (p.bid0 + p.ask0) / 2);
    return idx === 0 ? { bid: p.bid0, ask: p.ask0, mid } : { bid: 100 - p.ask0, ask: 100 - p.bid0, mid };
  }

  // ---------- cross-venue matching ----------

  function crossVenue(kalshi, poly, opt) {
    const K = [];
    for (const m of kalshi) {
      const c = compactKalshi(m);
      if (c.yes_ask === null || c.yes_bid === null) continue;
      if (opt.text && !opt.text.every((w) => `${c.title} ${c.option} ${c.ticker}`.toLowerCase().includes(w))) continue;
      const f = features(`${c.title} ${c.option}`);
      const td = kalshiTickerDate(c.event || c.ticker);
      if (td) f.dates.add(td);
      K.push({ c, f, opt: null });
    }
    const P = poly.map((m) => {
      const p = polyRow(m);
      return { p, f: features(`${p.question} ${p.item}`) };
    });
    // document frequency over both venues -> rarer shared words count more
    const df = new Map();
    for (const x of [...K, ...P]) for (const t of x.f.tokens) df.set(t, (df.get(t) || 0) + 1);
    const N = K.length + P.length;
    const idf = (t) => Math.log(1 + N / (df.get(t) || 1));
    const index = new Map();
    P.forEach((x, i) => { for (const t of x.f.tokens) if ((df.get(t) || 0) <= 400) (index.get(t) || index.set(t, []).get(t)).push(i); });
    const wsum = (set) => { let s = 0; for (const t of set) s += idf(t); return s; };

    const found = [];
    for (const k of K) {
      const cand = new Map();
      for (const t of k.f.tokens) for (const i of index.get(t) || []) cand.set(i, true);
      const kw = wsum(k.f.tokens);
      let best = null;
      for (const i of cand.keys()) {
        const q = P[i];
        let shared = 0;
        for (const t of k.f.tokens) if (q.f.tokens.has(t)) shared += idf(t);
        let sim = (2 * shared) / (kw + wsum(q.f.tokens) || 1);
        if (sim < opt.minSim * 0.7) continue;
        // numbers (strikes, thresholds) must agree when both sides state them
        if (k.f.nums.size && q.f.nums.size) {
          let hit = false;
          for (const n of k.f.nums) if (q.f.nums.has(n)) hit = true;
          if (!hit) continue;
        } else if (k.f.nums.size || q.f.nums.size) sim *= 0.75;
        // dates must agree when both sides state them
        if (k.f.dates.size && q.f.dates.size) {
          let hit = false;
          for (const d of k.f.dates) if (q.f.dates.has(d)) hit = true;
          if (!hit) continue;
        }
        if (sim < opt.minSim) continue;
        if (!best || sim > best.sim) best = { i, sim };
      }
      if (!best) continue;
      const q = P[best.i];
      // crypto price markets settle at different times of day on each venue -> crypto_fair_value instead
      const cryptoPrice = CRYPTO_WORDS.test(k.c.title) && PRICE_WORDS.test(`${k.c.title} ${k.c.option}`);
      const hoursApart = Math.abs(Date.parse(k.c.closes) - Date.parse(q.p.ends)) / 3.6e6;
      if (cryptoPrice && hoursApart > 1) continue;
      // which Polymarket outcome is Kalshi's YES?
      let idx = 0;
      const o0 = (q.p.outcomes[0] || "").toLowerCase();
      if (o0 !== "yes" && q.p.outcomes.length === 2) {
        // Kalshi's option ("Jacksonville") names its YES side; the title often names both sides
        const score = (set) => q.p.outcomes.map((o) => { const f = features(o); let s = 0; for (const t of f.tokens) if (set.has(t)) s++; return s / (f.tokens.size || 1); });
        k.opt ||= features(k.c.option);
        let sc = score(k.opt.tokens);
        if (sc[0] === sc[1]) sc = score(k.f.tokens);
        if (sc[0] === sc[1]) continue; // can't tell the sides apart
        idx = sc[0] > sc[1] ? 0 : 1;
      }
      let pq = polyQuote(q.p, idx);
      if (!pq) continue;
      const flip = k.f.dir && q.f.dir && k.f.dir !== q.f.dir;
      if (flip) pq = { bid: 100 - pq.ask, ask: 100 - pq.bid, mid: 100 - pq.mid };
      const ka = k.c.yes_ask, kb = k.c.yes_bid, kna = k.c.no_ask ?? 100 - kb;
      if (ka <= 0 || ka >= 100 || kna <= 0 || kna >= 100) continue;
      const yesEdge = pq.mid - ka - feeC(ka);
      const noEdge = 100 - pq.mid - kna - feeC(kna);
      const arbYes = pq.bid - ka - feeC(ka); // buy Kalshi YES, buy Polymarket NO
      const arbNo = 100 - pq.ask - kna - feeC(kna); // buy Kalshi NO, buy Polymarket YES
      const yes = yesEdge >= noEdge;
      const edge = Math.round(Math.max(yesEdge, noEdge));
      if (edge < opt.minGap) continue;
      const flags = [];
      if (Math.max(arbYes, arbNo) > 0) flags.push("CROSS-ARB*");
      if (flip) flags.push("opposite wording");
      if (hoursApart > 24) flags.push(`closes ${Math.round(hoursApart / 24)}d apart`);
      if (idx === 1) flags.push(`Poly side "${q.p.outcomes[1]}"`);
      found.push({
        edge,
        side: yes ? `Kalshi YES ${ka}¢ vs Poly ${Math.round(pq.mid)}¢` : `Kalshi NO ${kna}¢ vs Poly ${Math.round(100 - pq.mid)}¢`,
        match: `${Math.round(best.sim * 100)}%`,
        kalshi: k.c.ticker,
        kalshi_market: `${k.c.title} · ${k.c.option}`.slice(0, 90),
        poly_market: (q.p.item ? `${q.p.question} · ${q.p.item}` : q.p.question).slice(0, 90),
        poly_slug: q.p.slug,
        k_bid_ask: `${kb}/${ka}`,
        p_bid_ask: `${Math.round(pq.bid)}/${Math.round(pq.ask)}`,
        k_vol: k.c.vol_24h,
        p_vol: Math.round(q.p.vol),
        closes: k.c.closes,
        flags: flags.join("; "),
      });
    }
    found.sort((a, b) => b.edge - a.edge);
    return found;
  }

  // ---------- each venue checked against itself ----------

  function kalshiInternal(kalshi) {
    const byEvent = new Map();
    for (const m of kalshi) (byEvent.get(m.event_ticker) || byEvent.set(m.event_ticker, []).get(m.event_ticker)).push(m);
    const out = [];
    for (const [ev, ms] of byEvent) {
      if (ms.length < 2) continue;
      const types = new Set(ms.map((m) => m.strike_type));
      if (types.has("between")) {
        const { rows, notes } = ladderCheck(ms, true);
        const asks = rows.map((r) => r.yes_ask), bids = rows.map((r) => r.yes_bid);
        const sumAsk = asks.reduce((a, b) => a + (b ?? 0), 0), sumBid = bids.reduce((a, b) => a + (b ?? 0), 0);
        if ((asks.every((x) => x !== null && x > 0) && sumAsk < 100) || sumBid >= 103)
          out.push({ event: ev, title: (ms[0].title || "").slice(0, 70), issue: notes.filter((n) => /ARB|above 100/.test(n)).join(" ") || notes[0], gap: Math.max(100 - sumAsk, sumBid - 100) });
      } else if ([...types].every((t) => /^greater/.test(t || ""))) {
        const { notes } = ladderCheck(ms, false);
        const inv = notes.filter((n) => n.startsWith("INVERSION"));
        if (inv.length) out.push({ event: ev, title: (ms[0].title || "").slice(0, 70), issue: inv.slice(0, 2).join(" "), gap: inv.length });
      }
    }
    return out.sort((a, b) => b.gap - a.gap);
  }

  function polyInternal(poly) {
    const groups = new Map();
    for (const m of poly) {
      const p = polyRow(m);
      if (!p.negRisk || !p.group) continue;
      (groups.get(p.group) || groups.set(p.group, []).get(p.group)).push(p);
    }
    const out = [];
    for (const ps of groups.values()) {
      if (ps.length < 3) continue;
      const asks = ps.map((p) => p.ask0), bids = ps.map((p) => p.bid0);
      if (asks.some((x) => x === null) || bids.some((x) => x === null)) continue;
      const sa = asks.reduce((a, b) => a + b, 0), sb = bids.reduce((a, b) => a + b, 0);
      const title = (ps[0].groupTitle || ps[0].question).slice(0, 80);
      if (sa < 100) out.push({ event: title, outcomes: ps.length, issue: `Yes asks sum to ${sa}¢: buying Yes on all ${ps.length} outcomes costs ${sa}¢ for a sure 100¢`, gap: 100 - sa });
      else if (sb > 100) out.push({ event: title, outcomes: ps.length, issue: `Yes bids sum to ${sb}¢: No on every outcome is collectively cheap by ${sb - 100}¢`, gap: sb - 100 });
    }
    return out.sort((a, b) => b.gap - a.gap);
  }

  // ---------- crypto pricing ----------

  const COINS = {
    bitcoin: { cg: "bitcoin", cb: "BTC-USD", series: ["KXBTCD", "KXBTC"], words: /\b(bitcoin|btc)\b/i },
    ethereum: { cg: "ethereum", cb: "ETH-USD", series: ["KXETHD", "KXETH"], words: /\b(ethereum|eth)\b/i },
    solana: { cg: "solana", cb: "SOL-USD", series: ["KXSOLD", "KXSOL"], words: /\b(solana|sol)\b/i },
    xrp: { cg: "ripple", cb: "XRP-USD", series: ["KXXRPD", "KXXRP"], words: /\bxrp\b/i },
    dogecoin: { cg: "dogecoin", cb: "DOGE-USD", series: ["KXDOGED", "KXDOGE"], words: /\b(dogecoin|doge)\b/i },
  };
  const COIN_ALIAS = { btc: "bitcoin", eth: "ethereum", sol: "solana", doge: "dogecoin", ripple: "xrp" };

  function normCdf(x) {
    // Abramowitz-Stegun 7.1.26
    const t = 1 / (1 + 0.3275911 * Math.abs(x) / Math.SQRT2);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-(x * x) / 2);
    return x >= 0 ? 0.5 * (1 + y) : 0.5 * (1 - y);
  }
  // chance the price finishes above K after `hours`, lognormal, no drift
  const pAbove = (S, K, sigma, hours) => {
    const s = sigma * Math.sqrt(Math.max(hours, 1 / 60) / 8760);
    return normCdf((Math.log(S / K) - (s * s) / 2) / s);
  };
  // chance the price touches K at any point before `hours` (reflection principle)
  const pTouch = (S, K, sigma, hours) => {
    const s = sigma * Math.sqrt(Math.max(hours, 1 / 60) / 8760);
    return Math.min(1, 2 * normCdf(-Math.abs(Math.log(K / S)) / s));
  };

  async function spotAndVol(coin, env) {
    const c = COINS[coin];
    const headers = env?.COINGECKO_API_KEY ? { "x-cg-demo-api-key": env.COINGECKO_API_KEY } : {};
    let spot, prices, source = "CoinGecko";
    try {
      const [p, chart] = await Promise.all([
        getJSON(`${COINGECKO}/simple/price?${qs({ ids: c.cg, vs_currencies: "usd" })}`, headers),
        getJSON(`${COINGECKO}/coins/${c.cg}/market_chart?${qs({ vs_currency: "usd", days: 7 })}`, headers),
      ]);
      spot = num(p[c.cg]?.usd);
      prices = (chart.prices || []).map((x) => num(x[1])).filter((x) => x > 0);
    } catch (e) {
      source = `Coinbase (CoinGecko failed: ${e.message.slice(0, 60)})`;
    }
    if (!spot || !prices || prices.length < 48) {
      const [t, candles] = await Promise.all([
        getJSON(`${COINBASE}/products/${c.cb}/ticker`),
        getJSON(`${COINBASE}/products/${c.cb}/candles?${qs({ granularity: 3600 })}`),
      ]);
      spot = num(t.price);
      prices = candles.map((x) => num(x[4])).reverse();
      if (!source.startsWith("Coinbase")) source = "Coinbase";
    }
    // annualised volatility from hourly log returns
    const rets = [];
    for (let i = 1; i < prices.length; i++) rets.push(Math.log(prices[i] / prices[i - 1]));
    const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
    const sd = Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1));
    const sigma = sd * Math.sqrt(24 * 365);
    return { spot, sigma, source, n: rets.length };
  }

  function parsePolyCrypto(q) {
    const nums = [...q.matchAll(/\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k)?/gi)].map((m) => parseFloat(m[1].replace(/,/g, "")) * (m[2] ? 1e3 : 1));
    if (/\bbetween\b/i.test(q) && nums.length >= 2) return { kind: "between", lo: Math.min(nums[0], nums[1]), hi: Math.max(nums[0], nums[1]) };
    if (!nums.length) return null;
    if (/\b(reach|hit)\b/i.test(q)) return { kind: "touch", k: nums[0] };
    if (/\bdip\b/i.test(q)) return { kind: "touch", k: nums[0] };
    if (/\babove\b/i.test(q)) return { kind: "above", k: nums[0] };
    if (/\bbelow\b/i.test(q)) return { kind: "below", k: nums[0] };
    return null;
  }

  function modelProb(spec, S, sigma, hours) {
    if (spec.kind === "above") return pAbove(S, spec.k, sigma, hours);
    if (spec.kind === "below") return 1 - pAbove(S, spec.k, sigma, hours);
    if (spec.kind === "between") return Math.max(0, pAbove(S, spec.lo, sigma, hours) - pAbove(S, spec.hi, sigma, hours));
    if (spec.kind === "touch") return pTouch(S, spec.k, sigma, hours);
    return null;
  }

  const fmtUsd = (x) => (x >= 100 ? `$${Math.round(x).toLocaleString("en-US")}` : `$${x.toPrecision(4)}`);

  // ---------- the tools ----------

  return {
    mismatch_scan: {
      description:
        "Wide mismatch hunt across Kalshi and Polymarket in one call. (1) CROSS-VENUE: Kalshi markets matched to the Polymarket market asking the same question (by wording, numbers and dates), with the price gap after Kalshi fees; CROSS-ARB* = the two quotes cross outright, though US users can't trade Polymarket's main exchange. (2) KALSHI-INTERNAL: bracket events whose prices don't add to 100, threshold ladders priced out of order. (3) POLYMARKET-INTERNAL: multi-outcome events whose Yes prices don't add to 100. Matches are candidates only: confirm both rules with kalshi_market and polymarket_market before trusting a gap. Crypto price markets are left to crypto_fair_value (the venues settle at different times of day).",
      inputSchema: {
        type: "object",
        properties: {
          hours: { type: "number", description: "Markets closing within this many hours (default 168). Use more (e.g. 2400) with `text` for elections or long-dated questions." },
          text: { type: "string", description: "Optional words a Kalshi market must contain, e.g. 'senate', 'fed', 'temperature'" },
          min_gap: { type: "number", description: "Smallest cross-venue gap to show, cents after fees (default 4)" },
          min_similarity: { type: "number", description: "Matching strictness 0-1 (default 0.5); raise it to cut false matches" },
          sections: { type: "string", description: "Comma list of cross,kalshi,poly (default all)" },
          limit: { type: "number", description: "Rows per section (default 25)" },
          pages: { type: "number", description: "Pages of 1000 Kalshi / 500 Polymarket markets to read (default 3)" },
        },
      },
      async run(a) {
        const hours = a.hours ?? 168, pages = Math.min(a.pages ?? 3, 6), limit = a.limit ?? 25;
        const want = new Set((a.sections || "cross,kalshi,poly").split(",").map((s) => s.trim()));
        const [k, poly] = await Promise.all([
          want.has("cross") || want.has("kalshi") ? kalshiOpen(hours, pages) : { markets: [], more: false },
          want.has("cross") || want.has("poly") ? polyOpen(hours, pages) : [],
        ]);
        const out = [`Scanned ${k.markets.length} Kalshi${k.more ? " (more exist: raise pages or narrow hours)" : ""} and ${poly.length} Polymarket markets closing within ${hours}h.`];
        if (want.has("cross")) {
          const rows = crossVenue(k.markets, poly, {
            minGap: a.min_gap ?? 4,
            minSim: a.min_similarity ?? 0.5,
            text: a.text ? a.text.toLowerCase().split(/\s+/) : null,
          }).slice(0, limit);
          out.push("", `CROSS-VENUE (${rows.length}): gap = Polymarket mid minus Kalshi ask, after Kalshi fees. Polymarket = main exchange, reference only.`,
            table(rows, ["edge", "side", "match", "kalshi", "kalshi_market", "poly_market", "poly_slug", "k_bid_ask", "p_bid_ask", "k_vol", "p_vol", "closes", "flags"]));
        }
        if (want.has("kalshi")) {
          const rows = kalshiInternal(k.markets).slice(0, limit);
          out.push("", `KALSHI-INTERNAL (${rows.length}): open the event with kalshi_event to see every bracket.`, table(rows, ["event", "title", "issue"]));
        }
        if (want.has("poly")) {
          const rows = polyInternal(poly).slice(0, limit);
          out.push("", `POLYMARKET-INTERNAL (${rows.length}):`, table(rows, ["event", "outcomes", "issue"]));
        }
        out.push("", "Every row is a lead, not a sure thing: check both venues' settlement rules, times and liquidity before acting.");
        return out.join("\n");
      },
    },

    crypto_fair_value: {
      description:
        "Prices every open Kalshi and Polymarket crypto price market (above / below / between / reach / dip) off the live spot price and the last 7 days' volatility from CoinGecko (Coinbase as backup), using a simple lognormal model. Shows the model's chance, each market's quotes, and the edge after Kalshi fees. The model ignores fat tails and the exact settlement index (Kalshi: CF Benchmarks 60-second average; Polymarket: usually a Binance 1-minute close), so treat small edges as noise.",
      inputSchema: {
        type: "object",
        properties: {
          coin: { type: "string", description: "bitcoin, ethereum, solana, xrp or dogecoin (default bitcoin)" },
          hours: { type: "number", description: "Markets settling within this many hours (default 48)" },
          min_edge: { type: "number", description: "Smallest edge to show, cents (default 4)" },
          vol: { type: "number", description: "Override annualised volatility, e.g. 0.45 for 45%" },
          limit: { type: "number", description: "Max rows (default 30)" },
        },
      },
      async run(a, env) {
        const coin = COIN_ALIAS[(a.coin || "bitcoin").toLowerCase()] || (a.coin || "bitcoin").toLowerCase();
        const c = COINS[coin];
        if (!c) throw new Error(`Unknown coin. Use one of: ${Object.keys(COINS).join(", ")}`);
        const hours = a.hours ?? 48;
        const [sv, kalshiEvents, poly] = await Promise.all([
          spotAndVol(coin, env),
          Promise.all(c.series.map((s) => getJSON(`${KALSHI}/events?${qs({ series_ticker: s, status: "open", with_nested_markets: true, limit: 50 })}`).catch(() => ({ events: [] })))),
          polyOpen(hours, 2).catch(() => []),
        ]);
        const sigma = a.vol ?? sv.sigma;
        const rows = [];
        const push = (venue, ticker, label, hrs, spec, bid, ask, noAsk, fee) => {
          if (hrs <= 0 || hrs > hours || bid === null || ask === null) return;
          const p = modelProb(spec, sv.spot, sigma, hrs);
          if (p === null) return;
          const pc = p * 100;
          const nA = noAsk ?? 100 - bid;
          const yesEdge = ask > 0 && ask < 100 ? pc - ask - (fee ? feeC(ask) : 0) : -99;
          const noEdge = nA > 0 && nA < 100 ? 100 - pc - nA - (fee ? feeC(nA) : 0) : -99;
          const edge = Math.round(Math.max(yesEdge, noEdge));
          rows.push({ edge, side: yesEdge >= noEdge ? `YES at ${ask}¢` : `NO at ${nA}¢`, model: `${pc.toFixed(1)}%`, venue, market: label, ticker, bid_ask: `${bid}/${ask}`, settles_in: `${hrs.toFixed(1)}h` });
        };
        for (const ev of kalshiEvents.flatMap((d) => d.events || [])) {
          for (const m of ev.markets || []) {
            const hrs = hoursUntil(m.close_time);
            const lo = num(m.floor_strike), hi = num(m.cap_strike), t = m.strike_type || "";
            let spec = null;
            if (/^greater/.test(t) && lo !== null) spec = { kind: "above", k: lo };
            else if (/^less/.test(t) && hi !== null) spec = { kind: "below", k: hi };
            else if (t === "between" && lo !== null && hi !== null) spec = { kind: "between", lo, hi };
            if (!spec) continue;
            push("Kalshi", m.ticker, `${ev.title || ""} · ${m.yes_sub_title || ""}`.slice(0, 70), hrs, spec, cents(m, "yes_bid"), cents(m, "yes_ask"), cents(m, "no_ask"), true);
          }
        }
        for (const m of poly) {
          const p = polyRow(m);
          if (!c.words.test(p.question) || (p.outcomes[0] || "").toLowerCase() !== "yes") continue;
          const spec = parsePolyCrypto(p.question);
          if (!spec) continue;
          push("Polymarket", p.slug, p.question.slice(0, 70), hoursUntil(p.ends), spec, p.bid0, p.ask0, p.bid0 === null ? null : 100 - p.bid0, false);
        }
        const shown = rows.filter((r) => r.edge >= (a.min_edge ?? 4)).sort((x, y) => y.edge - x.edge).slice(0, a.limit ?? 30);
        return [
          `${coin}: spot ${fmtUsd(sv.spot)} (${sv.source}); volatility ${(sigma * 100).toFixed(0)}% a year${a.vol ? " (your override)" : ` from ${sv.n} hourly moves over 7 days`}.`,
          `Priced ${rows.length} markets settling within ${hours}h; ${shown.length} show an edge of ${a.min_edge ?? 4}¢ or more (Kalshi edges are after fees).`,
          table(shown, ["edge", "side", "model", "venue", "market", "ticker", "bid_ask", "settles_in"]),
          "Model = lognormal, no drift, recent volatility. Crypto has fat tails, so the model tends to underprice far-out strikes and overprice near ones; prefer edges that survive a ±25% change in vol (pass `vol`).",
          "Reach/dip markets are priced from now on: if the level was already hit earlier in their window, they're effectively settled, so check before trusting those rows.",
        ].join("\n");
      },
    },

    polymarket_market: {
      description: "One Polymarket market in full: question, outcomes and quotes, volume, liquidity, end date and the full resolution rules. Pass the slug shown by polymarket_find or mismatch_scan.",
      inputSchema: { type: "object", properties: { slug: { type: "string" } }, required: ["slug"] },
      async run(a) {
        const d = await getJSON(`${GAMMA}/markets?${qs({ slug: a.slug })}`);
        const m = (Array.isArray(d) ? d : d.markets || [])[0];
        if (!m) throw new Error(`No Polymarket market with slug ${a.slug}`);
        const p = polyRow(m);
        return [
          p.question + (p.item ? ` | ${p.item}` : ""),
          `slug ${p.slug} | ends ${p.ends} (${hoursUntil(p.ends).toFixed(1)}h)`,
          `Outcomes: ${p.outcomes.map((o, i) => `${o} ${p.mids[i]}¢`).join(" / ")}`,
          `Best bid/ask (${p.outcomes[0] || "first outcome"}): ${p.bid0}/${p.ask0}¢ | 24h vol $${Math.round(p.vol)} | liquidity $${Math.round(num(m.liquidityNum ?? m.liquidity) || 0)}`,
          p.groupTitle ? `Event: ${p.groupTitle}${p.negRisk ? " (multi-outcome)" : ""}` : "",
          `RULES: ${(m.description || "").slice(0, 2500)}`,
          m.resolutionSource ? `Resolution source: ${m.resolutionSource}` : "",
          "Main (international) exchange; US users trade the separate polymarket.us app, whose prices can differ.",
        ].filter(Boolean).join("\n");
      },
    },
  };
}
