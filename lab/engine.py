"""Paper money for each bot: cash, bitcoin, fees, and the safety limits.

A bot's strategy says how much of its money it wants in bitcoin. The Bot
turns that into a buy or a sell, charges a fee and slippage on every fill,
and enforces two limits the strategy can't override:

* daily loss limit: if a bot loses `daily_loss` of its equity within one
  day (UTC), it sells everything and sits out until the next day;
* retirement: if a bot falls to `retire_at` of its starting money, it
  sells everything and stops trading for good.
"""

from dataclasses import dataclass, field


@dataclass
class Costs:
    fee: float = 0.006   # charged on every buy and sell (0.6%)
    slippage: float = 0.0005  # fill a little worse than the quoted price (0.05%)
    min_trade: float = 1.0    # dollars; smaller changes are skipped
    min_shift: float = 0.05   # skip changes under 5% of the bot's equity


class Bot:
    def __init__(self, strategy, cash=100.0, costs=None, daily_loss=0.10, retire_at=0.60):
        self.strategy = strategy
        self.name = strategy.name
        self.costs = costs or Costs()
        self.daily_loss = daily_loss
        self.retire_at = retire_at
        self.start_cash = float(cash)
        self.cash = float(cash)
        self.units = 0.0
        self.meta = {}
        self.fees_paid = 0.0
        self.day = None
        self.day_start_equity = float(cash)
        self.locked_day = None
        self.retired = False
        self.fills = []        # every buy and sell
        self.round_trips = []  # from first buy back to all cash
        self._open_trip = None
        self.candles_seen = 0

    # ---- numbers ----
    def equity(self, price):
        return self.cash + self.units * price

    def frac_in(self, price):
        eq = self.equity(price)
        return 0.0 if eq <= 0 else self.units * price / eq

    # ---- one finished candle ----
    def on_candle(self, row, fill_price, ts):
        """Decide on a finished candle, then fill at `fill_price`
        (the next candle's open in a backtest, the latest price live)."""
        self.candles_seen += 1
        price = float(row.close)
        day = ts.date() if hasattr(ts, "date") else None
        if day != self.day:
            self.day = day
            self.day_start_equity = self.equity(price)

        eq = self.equity(price)
        target = None
        reason = None
        if self.retired:
            return
        if getattr(self.strategy, "exempt_from_limits", False):
            if self.candles_seen > self.strategy.warmup:
                target = self.strategy.decide(row, self.meta)
                if target is not None:
                    self._rebalance(float(target), float(fill_price), ts, "signal")
            return
        if eq <= self.start_cash * self.retire_at:
            self.retired = True
            target, reason = 0.0, "retired: hit the retirement floor"
        elif self.locked_day == day:
            return
        elif eq <= self.day_start_equity * (1 - self.daily_loss):
            self.locked_day = day
            target, reason = 0.0, "daily loss limit"
        elif self.candles_seen > self.strategy.warmup:
            target = self.strategy.decide(row, self.meta)
            reason = "signal"

        if reason != "signal":
            self.meta.clear()
        if target is not None:
            self._rebalance(float(target), float(fill_price), ts, reason)

    def _rebalance(self, target, price, ts, reason):
        c = self.costs
        eq = self.equity(price)
        now = self.frac_in(price)
        shift = target - now
        dollars = abs(shift) * eq
        if target > 0 and (abs(shift) < c.min_shift or dollars < c.min_trade):
            return
        if target == 0 and self.units <= 0:
            return
        if shift > 0:  # buy
            spend = min(dollars, self.cash)
            fee = spend * c.fee
            got = (spend - fee) / (price * (1 + c.slippage))
            self.cash -= spend
            self.units += got
            side, amount = "buy", spend
            if self._open_trip is None:
                self._open_trip = {"opened": ts, "equity": eq}
        else:  # sell
            sell_units = self.units if target == 0 else min(self.units, dollars / price)
            gross = sell_units * price * (1 - c.slippage)
            fee = gross * c.fee
            self.units -= sell_units
            if self.units < 1e-12:
                self.units = 0.0
            self.cash += gross - fee
            side, amount = "sell", gross
        self.fees_paid += fee
        self.fills.append({
            "time": ts, "bot": self.name, "side": side, "price": round(price, 2),
            "dollars": round(amount, 2), "fee": round(fee, 4), "reason": reason,
            "equity_after": round(self.equity(price), 2),
        })
        if self.units == 0 and self._open_trip is not None:
            pnl = self.equity(price) - self._open_trip["equity"]
            self.round_trips.append({"opened": self._open_trip["opened"], "closed": ts, "pnl": pnl})
            self._open_trip = None

    # ---- saving and loading (live paper mode) ----
    def to_state(self):
        return {
            "name": self.name, "start_cash": self.start_cash, "cash": self.cash, "units": self.units,
            "meta": self.meta, "fees_paid": self.fees_paid,
            "day": str(self.day) if self.day else None, "day_start_equity": self.day_start_equity,
            "locked_day": str(self.locked_day) if self.locked_day else None,
            "retired": self.retired, "candles_seen": self.candles_seen,
            "open_trip_equity": self._open_trip["equity"] if self._open_trip else None,
            "open_trip_opened": str(self._open_trip["opened"]) if self._open_trip else None,
        }

    def load_state(self, s):
        import datetime as dt
        self.start_cash = s["start_cash"]
        self.cash = s["cash"]
        self.units = s["units"]
        self.meta = s.get("meta", {})
        self.fees_paid = s.get("fees_paid", 0.0)
        self.day = dt.date.fromisoformat(s["day"]) if s.get("day") else None
        self.day_start_equity = s.get("day_start_equity", self.cash)
        self.locked_day = dt.date.fromisoformat(s["locked_day"]) if s.get("locked_day") else None
        self.retired = s.get("retired", False)
        self.candles_seen = s.get("candles_seen", 0)
        if s.get("open_trip_equity") is not None:
            self._open_trip = {"opened": s.get("open_trip_opened"), "equity": s["open_trip_equity"]}
