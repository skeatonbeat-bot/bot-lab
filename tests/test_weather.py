"""Offline tests for the Weather League (fakes every outside API).

    python -m unittest tests/test_weather.py
"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import weather_live as W
from wx import markets as M
from wx import model as MD
from wx import obs as O


class Parsing(unittest.TestCase):
    def test_brackets(self):
        self.assertEqual(M.parse_bracket("7°C"), ("C", 7, 7))
        self.assertEqual(M.parse_bracket("-3°C"), ("C", -3, -3))
        self.assertEqual(M.parse_bracket("56-57°F"), ("F", 56, 57))
        self.assertEqual(M.parse_bracket("6°C or below"), ("C", None, 6))
        self.assertEqual(M.parse_bracket("74°F or higher"), ("F", 74, None))
        self.assertEqual(M.parse_bracket("between 76-77°F"), ("F", 76, 77))
        self.assertIsNone(M.parse_bracket("Other"))

    def test_slug_and_station(self):
        self.assertEqual(M.slug_date("highest-temperature-in-nyc-on-november-2-2026").isoformat(), "2026-11-02")
        ev = {"resolutionSource": "https://aviationweather.gov/api/data/metar?ids=KLGA&format=raw"}
        self.assertEqual(M.station_from_event(ev), "KLGA")

    def test_temperature_reading(self):
        # T-group wins over the body; 1 = negative
        self.assertEqual(O.temp_c({"rawOb": "METAR KLGA 011851Z 18010KT 10SM FEW250 19/08 A3012 RMK AO2 T01890083"}), 18.9)
        self.assertEqual(O.temp_c({"rawOb": "METAR KLGA 011851Z 18010KT 10SM M02/M08 A3012 RMK AO2 T10221083"}), -2.2)
        self.assertEqual(O.temp_c({"rawOb": "METAR EGLC 012020Z AUTO 35006KT 9999 OVC021 10/07 Q1034"}), 10.0)
        self.assertEqual(O.temp_c({"rawOb": "METAR EGLC 012020Z AUTO 35006KT 9999 OVC021 M01/M03 Q1034"}), -1.0)

    def test_rounding_like_the_rules(self):
        self.assertEqual(O.settle_value(18.9, "F"), 66)   # 66.02
        self.assertEqual(O.settle_value(19.17, "F"), 67)  # 66.506 -> half up
        self.assertEqual(O.settle_value(-0.5, "C"), 0)    # halves go up, even below zero
        self.assertEqual(O.settle_value(7.5, "C"), 8)

    def test_speci_and_local_day(self):
        recs = [
            {"rawOb": "METAR KLGA 020351Z 19/10 RMK T02000100", "obsTime": int(datetime(2026, 11, 2, 3, 51, tzinfo=timezone.utc).timestamp())},  # 10:51pm Nov 1 local
            {"rawOb": "SPECI KLGA 021815Z 25/10 RMK T02500100", "metarType": "SPECI", "obsTime": int(datetime(2026, 11, 2, 18, 15, tzinfo=timezone.utc).timestamp())},
            {"rawOb": "METAR KLGA 021851Z 18/10 RMK T01830100", "obsTime": int(datetime(2026, 11, 2, 18, 51, tzinfo=timezone.utc).timestamp())},
        ]
        s = O.day_summary(recs, "America/New_York", "2026-11-02")
        self.assertEqual(s["count"], 1)
        self.assertEqual(s["max_c"], 18.3)


# ---------- a fake world ----------

def fake_event(city, d, icao, unit, labels, asks, closed_winner=None):
    mk = []
    for i, (lab, ask) in enumerate(zip(labels, asks)):
        closed = closed_winner is not None
        won = closed and lab == closed_winner
        mk.append({
            "id": f"{city}{i}", "groupItemTitle": lab, "question": f"Will it be {lab}?",
            "clobTokenIds": json.dumps([f"{city}{i}Y", f"{city}{i}N"]),
            "outcomePrices": json.dumps(["1", "0"] if won else ["0", "1"] if closed else [str(ask), str(1 - ask)]),
            "bestAsk": ask, "bestBid": max(0.001, ask - 0.02), "closed": closed,
        })
    return {"slug": M.event_slug(city, d), "title": "t", "markets": mk,
            "resolutionSource": f"https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"}


class FakeWorld:
    def __init__(self, day):
        self.day = day
        self.winner = None
        self.calls = 0

    def get(self, url, params=None):
        self.calls += 1
        p = params or {}
        if url.endswith("/events"):
            if p["slug"] == M.event_slug("london", self.day):
                # the market thinks 12°C; our fake ensemble says ~15°C
                return [fake_event("london", self.day, "EGLC", "C",
                                   ["10°C or below", "11°C", "12°C", "13°C", "14°C", "15°C", "16°C or higher"],
                                   [0.05, 0.15, 0.45, 0.20, 0.08, 0.05, 0.03], self.winner)]
            return []
        if url == O.AWC:
            t = datetime(self.day.year, self.day.month, self.day.day, 13, 20, tzinfo=timezone.utc)
            return [{"icaoId": "EGLC", "lat": 51.505, "lon": 0.055, "obsTime": int(t.timestamp()),
                     "metarType": "METAR", "rawOb": "METAR EGLC 021320Z AUTO 20008KT 9999 FEW030 14/07 Q1020"}]
        if url == W.F.ENSEMBLE:
            times, members = [], {}
            for k in range(4 * 24):
                times.append((datetime(self.day.year, self.day.month, self.day.day) + timedelta(hours=k - 24)).strftime("%Y-%m-%dT%H:00"))
            for m in range(20):
                members[f"temperature_2m_member{m:02d}_ecmwf_ifs025"] = [
                    8 + 7 * max(0, 1 - abs((int(t[11:13]) - 15) / 7)) + (m % 5) * 0.2 for t in times]
            return {"timezone": "Europe/London", "hourly": {"time": times, **members}}
        if url.endswith("/book"):
            tok = p["token_id"]
            i = int(tok[len("london"):-1])
            ask = [0.05, 0.15, 0.45, 0.20, 0.08, 0.05, 0.03][i]
            price = ask if tok.endswith("Y") else round(1 - (ask - 0.02), 3)
            return {"asks": [{"price": str(price), "size": "500"}], "bids": []}
        raise AssertionError(f"unexpected url {url}")


class League(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp)

    def test_cycle_bets_and_settles(self):
        day = datetime(2026, 11, 2).date()
        world = FakeWorld(day)
        logs = []
        # 1:30pm in London on the day: 14°C observed, members peak ~15°C at 3pm
        now = datetime(2026, 11, 2, 13, 30, tzinfo=timezone.utc)
        st = W.run_cycle(world.get, now=now, log=logs.append)
        self.assertIn(M.event_slug("london", day), st["events"])
        bots = {b["bot"] for b in st["open_bets"]}
        self.assertIn("Favorite", bots)          # buys 12°C, the market's pick
        self.assertIn("Nowcaster", bots)         # sees the warm day the market missed
        fav = next(b for b in st["open_bets"] if b["bot"] == "Favorite")
        self.assertEqual(fav["bracket"], "12°C")

        # a second call 5 minutes later is throttled
        W.run_cycle(world.get, now=now + timedelta(minutes=5), log=logs.append)
        self.assertIn("last cycle too recent; skipping", logs)

        # next day: the market settles on 15°C
        world.winner = "15°C"
        st = W.run_cycle(world.get, now=now + timedelta(hours=20), log=logs.append)
        self.assertEqual(st["open_bets"], [])
        self.assertLess(st["bots"]["Favorite"]["cash"], 100)
        rows = open("live/weather/bets.csv").read()
        self.assertIn("loss", rows)
        self.assertTrue(os.path.exists("live/weather/outcomes.csv"))
        self.assertTrue(any(f.startswith("forecasts_") for f in os.listdir("live/weather")))

    def test_model_floor_from_observations(self):
        day = datetime(2026, 11, 2).date()
        world = FakeWorld(day)
        ev = M.parse_event(world.get(M.GAMMA + "/events", {"slug": M.event_slug("london", day)})[0])
        fc = W.F.parse(world.get(W.F.ENSEMBLE))
        obs = {"max_c": 17.0, "latest": (datetime(2026, 11, 2, 16, 0), 16.0), "count": 10}
        pred = MD.predict(ev, fc, obs, datetime(2026, 11, 2, 16, 30))
        # already 17°C observed, so nothing below "16°C or higher" can win
        self.assertGreater(pred["probs"][-1], 0.99)
        self.assertAlmostEqual(sum(pred["probs"][:-1]), 0, delta=0.03)


if __name__ == "__main__":
    unittest.main()
