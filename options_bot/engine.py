import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from .models import Config, money, stamp, validate_frame
from .store import Store
from .strategy import momentum

D = Decimal


class Engine:
    """Consumes ordered snapshots and simulates fills. Never sends an order."""

    backend_name = "local"

    def __init__(self, db_path, cfg=None):
        self.cfg = cfg or Config()
        self.store = Store(db_path, self.cfg)
        if self.store.state().get("execution_backend", self.backend_name) != self.backend_name:
            self.store.close()
            raise ValueError("Database belongs to a different execution backend")

    def close(self):
        self.store.close()

    def equity(self, state):
        return money(D(state["cash"]) + D(state["unsettled"]) + sum(
            (D(p["mark"]) * 100 * p["qty"] - self.cfg.fee_per_contract * p["qty"]
             for p in state["positions"].values()), D("0")))

    def halt(self, reason, at="manual"):
        with self.store.transaction():
            s = self.store.state()
            s["halt"] = reason
            for p in s["positions"].values():
                p["exit_reason"] = reason
            self.store.event(at, "HALT", reason=reason)
            self.store.save(s)

    def summary(self):
        s = self.store.state()
        return dict(mode="LOCAL PAPER ONLY", cash=s["cash"], unsettled=s["unsettled"],
                    equity=str(self.equity(s)), day=s["day"], entries_today=s["entries"],
                    halt=s["halt"], day_halt=s["day_halt"], positions=s["positions"],
                    valuation="Last observed bid, less modeled exit fees; may be stale")

    def _close(self, s, p, q, now):
        qty = p["qty"]
        if q.bid_size < qty:
            self.store.event(now.isoformat(), "EXIT_PENDING", symbol=q.symbol,
                             reason="Insufficient displayed bid size")
            return
        proceeds = money(q.bid * 100 * qty - self.cfg.fee_per_contract * qty)
        pnl = money(proceeds - D(p["cost"]))
        s["unsettled"] = str(money(D(s["unsettled"]) + proceeds))
        s["loss_streak"] = s["loss_streak"] + 1 if pnl < 0 else 0
        self.store.event(now.isoformat(), "SELL_TO_CLOSE", symbol=q.symbol,
                         quantity=qty, price=q.bid, pnl=pnl, reason=p["exit_reason"])
        del s["positions"][q.symbol]

    def _risk_check(self, s, now):
        equity = self.equity(s)
        s["peak_equity"] = str(max(equity, D(s["peak_equity"])))
        if equity <= D(s["peak_equity"]) * (1 - self.cfg.drawdown_cap):
            s["halt"] = s["halt"] or "account_drawdown"
        if D(s["day_equity"]) - equity >= D(s["day_equity"]) * self.cfg.daily_loss_cap:
            s["day_halt"] = s["day_halt"] or "daily_loss"
        if s["loss_streak"] >= self.cfg.max_losses:
            s["day_halt"] = s["day_halt"] or "loss_streak"
        if s["halt"] or s["day_halt"]:
            for p in s["positions"].values():
                p["exit_reason"] = s["halt"] or s["day_halt"]
        return equity

    def _choose(self, s, signal, quotes, now, day):
        cfg = self.cfg
        equity = self.equity(s)
        basis = min(cfg.capital, equity, D(s["day_equity"]))
        exposure = sum((D(p["cost"]) for p in s["positions"].values()), D("0"))
        day_loss = max(D("0"), D(s["day_equity"]) - equity)
        # Reserve full premium and both fees against the remaining daily budget.
        daily_room = D(s["day_equity"]) * cfg.daily_loss_cap - day_loss - exposure
        budget = min(basis * cfg.premium_cap, basis * cfg.exposure_cap - exposure,
                     D(s["cash"]), daily_room)
        choices = []
        for q in quotes:
            dte = (q.expiration - day).days
            correct_delta = q.delta > 0 if q.right == "call" else q.delta < 0
            if (q.underlying != signal.underlying or q.right != signal.right
                    or not q.usable(now, cfg) or q.bid <= 0
                    or not cfg.min_dte <= dte <= cfg.max_dte
                    or not correct_delta or not cfg.min_delta <= abs(q.delta) <= cfg.max_delta
                    or q.open_interest < cfg.min_open_interest):
                continue
            spread = (q.ask - q.bid) / ((q.ask + q.bid) / 2)
            if spread > cfg.max_spread:
                continue
            # Conservative worst case: entire premium lost plus entry and exit fees.
            worst_case = q.ask * 100 + 2 * cfg.fee_per_contract
            qty = min(int(budget // worst_case), q.ask_size)
            if qty < 1:
                continue
            choices.append((abs(abs(q.delta) - 0.5), spread, abs(dte - 7), q.symbol, q, qty))
        if not choices:
            return None
        best = min(choices, key=lambda x: x[:4])
        return best[-2:]

    def step(self, frame, kill=False):
        now, opening, closing, day, quotes = validate_frame(frame)
        digest = hashlib.sha256(json.dumps(frame, sort_keys=True).encode()).hexdigest()
        cfg = self.cfg
        with self.store.transaction():
            existing = self.store.db.execute("SELECT digest FROM frames WHERE id=?",
                                             (frame["id"],)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError("Frame id reused with changed contents")
                if kill:
                    s = self.store.state()
                    s["halt"] = "kill_switch"
                    for p in s["positions"].values():
                        p["exit_reason"] = "kill_switch"
                    self.store.save(s)
                return "duplicate"
            s = self.store.state()
            if s["last_at"] and now <= stamp(s["last_at"]):
                raise ValueError("Replay frames must advance time")
            if kill:
                s["halt"] = "kill_switch"
            if s["day"] != str(day):
                if s["positions"]:
                    s["halt"] = s["halt"] or "position_carried_between_sessions"
                s["cash"] = str(money(D(s["cash"]) + D(s["unsettled"])))
                s["unsettled"] = "0.00"
                s["day"] = str(day)
                s["day_equity"] = str(self.equity(s))
                s["entries"], s["loss_streak"], s["day_halt"] = 0, 0, None
            by_symbol = {q.symbol: q for q in quotes}
            flatten = closing - timedelta(minutes=15)
            for symbol, p in list(s["positions"].items()):
                q = by_symbol.get(symbol)
                if (q is None or not q.usable(now, cfg) or q.underlying != p["underlying"]
                        or q.right != p["right"] or str(q.expiration) != p["expiration"]):
                    s["halt"] = s["halt"] or "unpriced_position"
                    p["exit_reason"] = s["halt"]
                    self.store.event(now.isoformat(), "EXIT_PENDING", symbol=symbol,
                                     reason="Missing, stale, or invalid quote")
                    continue
                p["mark"] = str(q.bid)
                p["peak"] = str(max(D(p["peak"]), q.bid))
                entry, peak = D(p["entry"]), D(p["peak"])
                reason = None
                if now >= flatten:
                    reason = "session_flatten"
                elif now - stamp(p["opened_at"]) >= timedelta(minutes=cfg.max_hold_minutes):
                    reason = "maximum_holding_time"
                elif q.bid <= entry * (1 - cfg.stop):
                    reason = "premium_stop"
                elif q.bid >= entry * (1 + cfg.target):
                    reason = "profit_target"
                elif peak >= entry * (1 + cfg.trail_activation) and q.bid <= peak * (1 - cfg.trail_distance):
                    reason = "trailing_exit"
                p["exit_reason"] = s["halt"] or p["exit_reason"] or reason
            self._risk_check(s, now)
            for symbol, p in list(s["positions"].items()):
                q = by_symbol.get(symbol)
                if (p["exit_reason"] and q and q.usable(now, cfg)
                        and q.underlying == p["underlying"] and q.right == p["right"]
                        and str(q.expiration) == p["expiration"]):
                    self._close(s, p, q, now)
            self._risk_check(s, now)
            # No new entry on a frame that closed a position, avoiding churn.
            just_closed = any(e[0] == "SELL_TO_CLOSE" for e in self.store.db.execute(
                "SELECT kind FROM events WHERE at=?", (now.isoformat(),)))
            can_enter = (not s["halt"] and not s["day_halt"] and not just_closed
                         and len(s["positions"]) < cfg.max_positions and s["entries"] < cfg.max_trades
                         and opening + timedelta(minutes=30) <= now < closing - timedelta(minutes=30))
            if can_enter:
                for underlying in cfg.symbols:
                    signal = momentum(underlying, frame.get("bars", {}).get(underlying, []), now, opening, cfg)
                    if signal is None:
                        continue
                    choice = self._choose(s, signal, quotes, now, day)
                    if choice is None:
                        self.store.event(now.isoformat(), "SKIP", underlying=underlying,
                                         reason="No liquid contract fits DTE, delta, freshness and premium budget")
                        continue
                    q, qty = choice
                    cost = money((q.ask * 100 + cfg.fee_per_contract) * qty)
                    s["cash"] = str(money(D(s["cash"]) - cost))
                    s["positions"][q.symbol] = dict(underlying=underlying, right=q.right,
                        expiration=str(q.expiration), qty=qty, cost=str(cost), entry=str(q.ask),
                        mark=str(q.bid), peak=str(q.bid), opened_at=now.isoformat(), exit_reason=None)
                    s["entries"] += 1
                    self.store.event(now.isoformat(), "BUY_TO_OPEN", symbol=q.symbol,
                                     quantity=qty, price=q.ask, cost=cost, reason=signal.reason)
                    break
            self._risk_check(s, now)
            s["last_at"] = now.isoformat()
            self.store.event(now.isoformat(), "SNAPSHOT", equity=self.equity(s),
                             halt=s["halt"], day_halt=s["day_halt"])
            self.store.save(s)
            self.store.db.execute("INSERT INTO frames VALUES(?,?)", (frame["id"], digest))
        return "processed"
