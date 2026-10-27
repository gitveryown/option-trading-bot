"""Broker paper execution with durable intents and confirmed cumulative fills."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from .alpaca_data import DataError
from .alpaca_paper import OrderNotFound, OrderRejected
from .engine import Engine
from .models import money, stamp, validate_frame
from .strategy import momentum

D = Decimal
TERMINAL = {"filled", "canceled", "expired", "rejected", "replaced"}
ACTIVE = {"new", "accepted", "pending_new", "partially_filled", "pending_cancel", "pending_replace",
          "accepted_for_bidding", "stopped", "suspended", "calculated", "held", "done_for_day"}


class PaperEngine(Engine):
    backend_name = "alpaca_paper"

    def __init__(self, path, cfg=None, broker=None):
        existed = Path(path).exists()
        super().__init__(path, cfg)
        self.broker = broker
        self._lease = None
        try:
            with self.store.transaction():
                state = self.store.state()
                if existed and state.get("execution_backend") != "alpaca_paper":
                    raise ValueError("Use a separate database for Alpaca paper execution")
                state.setdefault("execution_backend", "alpaca_paper")
                state.setdefault("client_namespace", uuid.uuid4().hex[:12])
                state.setdefault("broker_account_id", None)
                state.setdefault("last_reconciled", None)
                self.store.save(state)
            self.store.db.execute("""CREATE TABLE IF NOT EXISTS broker_orders (
                client_id TEXT PRIMARY KEY, body TEXT NOT NULL)""")
        except BaseException:
            self.store.close()
            raise

    def close(self):
        super().close()
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def orders(self, pending=False):
        rows = [json.loads(r[0]) for r in self.store.db.execute("SELECT body FROM broker_orders ORDER BY rowid")]
        return [r for r in rows if r["status"] not in TERMINAL] if pending else rows

    def _save_order(self, order):
        self.store.db.execute("INSERT OR REPLACE INTO broker_orders VALUES(?,?)",
                              (order["client_id"], json.dumps(order, sort_keys=True)))

    def summary(self):
        result = super().summary()
        state = self.store.state()
        pending = self.orders(pending=True)
        reserve = sum(((D(o["limit_price"])*100+2*self.cfg.fee_per_contract)
                       * (o["qty"]-o["filled_qty"]) for o in pending if o["side"] == "buy"), D("0"))
        result.update(mode="ALPACA PAPER EXECUTION", execution_backend="alpaca_paper",
                      pending_orders=pending, broker_orders=self.orders()[-30:], reserved=str(money(reserve)),
                      broker_account=state["broker_account_id"], last_reconciled=state["last_reconciled"],
                      valuation="Broker confirmed fills; strategy budget equity uses modeled fees and last observed bids")
        return result

    def connect(self):
        if self.broker is None:
            raise DataError("Paper account transport is not connected")
        account = self.broker.account()
        if self._lease is None:
            try:
                import fcntl
            except ImportError:
                raise DataError("Broker paper execution currently requires macOS or Linux") from None
            name = "options-paper-"+hashlib.sha256((str(os.getuid())+account["id"]).encode()).hexdigest()+".lock"
            fd = os.open(str(Path(tempfile.gettempdir())/name), os.O_CREAT | os.O_RDWR | getattr(os,"O_NOFOLLOW",0), 0o600)
            self._lease = os.fdopen(fd, "a")
            try:
                fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self._lease.close()
                self._lease = None
                raise DataError("Another bot on this computer already controls this paper account") from None
        with self.store.transaction():
            s = self.store.state()
            if s["broker_account_id"] and s["broker_account_id"] != account["id"]:
                raise DataError("This database is bound to a different paper account")
            s["broker_account_id"] = account["id"]
            self.store.save(s)
        self.reconcile()
        return {"execution": "Alpaca paper", "account": account["id"], "budget": str(self.cfg.capital)}

    def _prepare(self, s, q, qty, side, now, key, reason):
        client_id = "op-"+s["client_namespace"]+"-"+hashlib.sha256(key.encode()).hexdigest()[:24]
        if self.store.db.execute("SELECT 1 FROM broker_orders WHERE client_id=?", (client_id,)).fetchone():
            return False
        limit = q.ask if side == "buy" else q.bid
        if limit <= 0:
            self.store.event(now.isoformat(), "EXIT_PENDING", symbol=q.symbol, reason="No positive executable bid")
            return False
        order = dict(client_id=client_id, symbol=q.symbol, qty=qty, side=side, limit_price=str(limit),
                     status="prepared", attempted=False, order_id=None, filled_qty=0, filled_notional="0",
                     created_at=now.isoformat(), quote_at=q.at.isoformat(), quote_bid=str(q.bid), reason=reason, underlying=q.underlying,
                     right=q.right, expiration=str(q.expiration))
        self._save_order(order)
        if side == "buy":
            s["entries"] += 1  # Count attempts conservatively, including rejected or canceled orders.
        self.store.event(now.isoformat(), "ORDER_PREPARED", symbol=q.symbol, side=side,
                         quantity=qty, client_order_id=client_id, reason=reason)
        return True

    def _receipt(self, order, receipt):
        if not isinstance(receipt, dict):
            raise DataError("Invalid broker order receipt")
        if (receipt.get("client_order_id") != order["client_id"] or receipt.get("symbol") != order["symbol"]
                or receipt.get("side") != order["side"] or D(str(receipt.get("qty", "0"))) != order["qty"]
                or receipt.get("type") != "limit" or receipt.get("time_in_force") != "day"
                or D(str(receipt.get("limit_price", "0"))) != D(order["limit_price"])):
            raise DataError("Broker order differs from its durable intent")
        count = D(str(receipt.get("filled_qty") or "0"))
        if not count.is_finite() or count != count.to_integral_value():
            raise DataError("Invalid broker option fill quantity")
        total = int(count)
        status = receipt.get("status")
        if status not in TERMINAL | ACTIVE or not order["filled_qty"] <= total <= order["qty"]:
            raise DataError("Unknown order state or inconsistent cumulative fills")
        if status == "filled" and total != order["qty"]:
            raise DataError("Broker marked an incomplete order filled")
        avg = D(str(receipt.get("filled_avg_price") or "0"))
        if not avg.is_finite() or (total and avg <= 0):
            raise DataError("Invalid broker fill price")
        if total and ((order["side"] == "buy" and avg > D(order["limit_price"]))
                      or (order["side"] == "sell" and avg < D(order["limit_price"]))):
            raise DataError("Broker fill violates the submitted limit")
        notional = avg*100*total
        delta_qty = total-order["filled_qty"]
        delta_value = notional-D(order["filled_notional"])
        if not delta_qty and delta_value:
            raise DataError("Fill price revision requires manual reconciliation")
        at = receipt.get("filled_at") or receipt.get("updated_at") or datetime.now(timezone.utc).isoformat()
        with self.store.transaction():
            s = self.store.state()
            if delta_qty:
                if order["side"] == "buy":
                    cost = money(delta_value+self.cfg.fee_per_contract*delta_qty)
                    p = s["positions"].get(order["symbol"])
                    if p is None:
                        p = dict(underlying=order["underlying"], right=order["right"], expiration=order["expiration"],
                                 qty=0, cost="0", mark=order["quote_bid"], peak=order["quote_bid"], entry=str(avg),
                                 opened_at=at, exit_reason=s["halt"] or s["day_halt"], realized_accum="0")
                        s["positions"][order["symbol"]] = p
                    p["qty"] += delta_qty
                    p["cost"] = str(money(D(p["cost"])+cost))
                    p["entry"] = str((D(p["cost"])-self.cfg.fee_per_contract*p["qty"])/(100*p["qty"]))
                    s["cash"] = str(money(D(s["cash"])-cost))
                    self.store.event(at, "BUY_TO_OPEN", symbol=order["symbol"], quantity=delta_qty,
                        price=str(delta_value/(100*delta_qty)), cost=cost,
                        client_order_id=order["client_id"], source="ALPACA_PAPER_FILL", reason=order["reason"])
                else:
                    p = s["positions"].get(order["symbol"])
                    if not p or delta_qty > p["qty"]:
                        raise DataError("Closing fill does not match a tracked long position")
                    allocated = money(D(p["cost"])*delta_qty/p["qty"])
                    proceeds = money(delta_value-self.cfg.fee_per_contract*delta_qty)
                    pnl = money(proceeds-allocated)
                    p["realized_accum"] = str(money(D(p.get("realized_accum", "0"))+pnl))
                    p["qty"] -= delta_qty
                    p["cost"] = str(money(D(p["cost"])-allocated))
                    s["unsettled"] = str(money(D(s["unsettled"])+proceeds))
                    if not p["qty"]:
                        s["loss_streak"] = s["loss_streak"]+1 if D(p["realized_accum"]) < 0 else 0
                        del s["positions"][order["symbol"]]
                    self.store.event(at, "SELL_TO_CLOSE", symbol=order["symbol"], quantity=delta_qty,
                        price=str(delta_value/(100*delta_qty)), pnl=pnl,
                        client_order_id=order["client_id"], source="ALPACA_PAPER_FILL", reason=order["reason"])
            changed = order["status"] != status
            order.update(status=status, order_id=receipt["id"], filled_qty=total, filled_notional=str(notional))
            self._save_order(order)
            self.store.save(s)
            if changed:
                self.store.event(at, "ORDER_STATUS", symbol=order["symbol"], status=status,
                                 client_order_id=order["client_id"], reason="Alpaca paper confirmation")

    def _refresh(self):
        for order in self.orders(pending=True):
            if not order["attempted"]:
                continue
            try:
                self._receipt(order, self.broker.order(order["client_id"]))
            except OrderNotFound:
                self.halt("order_status_unknown")
                raise DataError("Order outcome unknown. No resubmission; inspect the paper account and reconnect later") from None

    def reconcile(self):
        if self.broker is None or self._lease is None:
            raise DataError("Connect the paper transport and acquire account ownership first")
        account = self.broker.account()
        if self.store.state()["broker_account_id"] != account["id"]:
            raise DataError("Connect and bind this database to its paper account first")
        # Broker position snapshots can race a new fill. Requery before declaring a mismatch.
        for _ in range(3):
            self._refresh()
            expected = {k: D(p["qty"]) for k, p in self.store.state()["positions"].items()}
            actual = {p["symbol"]: D(str(p["qty"])) for p in self.broker.positions() if D(str(p["qty"]))}
            if expected == actual:
                break
        else:
            self.halt("broker_position_mismatch")
            raise DataError("Unmanaged or mismatched positions detected. Use a dedicated flat paper account")
        known = {o["client_id"] for o in self.orders()}
        if any(o.get("client_order_id") not in known for o in self.broker.open_orders()):
            self.halt("unmanaged_broker_order")
            raise DataError("Unmanaged open orders detected. The bot will not cancel manual orders")
        now = datetime.now(timezone.utc)
        s = self.store.state()
        for order in self.orders(pending=True):
            if not order["attempted"]:
                continue
            age = (now-stamp(order["created_at"])).total_seconds()
            exit_needed = any(p["exit_reason"] for p in s["positions"].values())
            cancel = ((s["halt"] or s["day_halt"] or exit_needed) and order["side"] == "buy") or age >= (60 if order["side"] == "buy" else 30)
            if cancel and order["order_id"] and order["status"] != "pending_cancel":
                self.broker.cancel(order["order_id"])
                self.store.event(now.isoformat(), "CANCEL_REQUESTED", symbol=order["symbol"],
                                 client_order_id=order["client_id"], reason="Halt or order timeout; awaiting confirmation")
        with self.store.transaction():
            s = self.store.state()
            s["last_reconciled"] = now.isoformat()
            self.store.save(s)
        return account

    def _submit_ready(self, now):
        for order in self.orders(pending=True):
            if order["attempted"]:
                continue
            clock = self.broker.clock()
            now = datetime.now(timezone.utc)
            s = self.store.state()
            age = (now-stamp(order["created_at"])).total_seconds()
            quote_age = (now-stamp(order["quote_at"])).total_seconds()
            if (clock.get("is_open") is not True or abs((now-stamp(clock["timestamp"])).total_seconds()) > 5
                    or age < 0 or age > 10 or not 0 <= quote_age <= self.cfg.quote_age_seconds
                    or (order["side"] == "buy" and (s["halt"] or s["day_halt"]))):
                with self.store.transaction():
                    order["status"] = "canceled"
                    self._save_order(order)
                continue
            # Commit attempt BEFORE touching the network. A timeout must never cause another POST.
            with self.store.transaction():
                order["attempted"] = True
                self._save_order(order)
            payload = dict(symbol=order["symbol"], qty=str(order["qty"]), side=order["side"],
                type="limit", time_in_force="day", limit_price=order["limit_price"],
                client_order_id=order["client_id"], position_intent="buy_to_open" if order["side"] == "buy" else "sell_to_close")
            try:
                self._receipt(order, self.broker.submit(payload))
            except OrderRejected:
                # An HTTP failure is not final proof there is no order with this id.
                try:
                    self._receipt(order, self.broker.order(order["client_id"]))
                except OrderNotFound:
                    with self.store.transaction():
                        order["status"] = "rejected"
                        self._save_order(order)
                    self.store.event(now.isoformat(), "ORDER_REJECTED", symbol=order["symbol"],
                                     reason="Broker rejected the order; no matching client id exists")
            except DataError:
                self.halt("order_submission_unknown")
                raise

    def step(self, frame, kill=False):
        now, opening, closing, day, quotes = validate_frame(frame)
        if any(q.feed != "opra" for q in quotes):
            raise ValueError("Broker paper execution requires actual OPRA quotes")
        # Historical input cannot be used to place brokerage orders.
        if abs((datetime.now(timezone.utc)-now).total_seconds()) > 10:
            raise ValueError("Broker paper execution requires a current frame")
        if kill:
            self.halt("kill_switch", now.isoformat())
        account = self.reconcile()
        digest = hashlib.sha256(json.dumps(frame, sort_keys=True).encode()).hexdigest()
        with self.store.transaction():
            existing = self.store.db.execute("SELECT digest FROM frames WHERE id=?", (frame["id"],)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise ValueError("Frame id reused with changed contents")
                return "duplicate"
            s = self.store.state()
            if s["last_at"] and now <= stamp(s["last_at"]):
                raise ValueError("Frames must advance time")
            if kill:
                s["halt"] = "kill_switch"
            if s["day"] != str(day):
                if s["positions"] or self.orders(pending=True):
                    s["halt"] = s["halt"] or "exposure_carried_between_sessions"
                s["cash"] = str(money(D(s["cash"])+D(s["unsettled"])))
                s.update(unsettled="0.00", day=str(day), day_equity=str(self.equity(s)),
                         entries=0, loss_streak=0, day_halt=None)
            by_symbol = {q.symbol: q for q in quotes}
            pending = self.orders(pending=True)
            for symbol, p in s["positions"].items():
                q = by_symbol.get(symbol)
                if (not q or not q.usable(now, self.cfg) or q.underlying != p["underlying"]
                        or q.right != p["right"] or str(q.expiration) != p["expiration"]):
                    s["halt"] = s["halt"] or "unpriced_position"
                    p["exit_reason"] = s["halt"]
                    continue
                p["mark"], p["peak"] = str(q.bid), str(max(q.bid, D(p["peak"])))
                entry, peak = D(p["entry"]), D(p["peak"])
                reason = None
                if now >= closing-timedelta(minutes=15): reason = "session_flatten"
                elif now-stamp(p["opened_at"]) >= timedelta(minutes=self.cfg.max_hold_minutes): reason = "maximum_holding_time"
                elif q.bid <= entry*(1-self.cfg.stop): reason = "premium_stop"
                elif q.bid >= entry*(1+self.cfg.target): reason = "profit_target"
                elif peak >= entry*(1+self.cfg.trail_activation) and q.bid <= peak*(1-self.cfg.trail_distance): reason = "trailing_exit"
                p["exit_reason"] = s["halt"] or p["exit_reason"] or reason
            self._risk_check(s, now)
            for symbol, p in s["positions"].items():
                q = by_symbol.get(symbol)
                if (p["exit_reason"] and q and q.usable(now, self.cfg) and not pending
                        and q.underlying == p["underlying"] and q.right == p["right"]
                        and str(q.expiration) == p["expiration"]):
                    self._prepare(s, q, p["qty"], "sell", now, frame["id"]+":exit:"+symbol, p["exit_reason"])
                    pending = self.orders(pending=True)
            can_enter = (not s["halt"] and not s["day_halt"] and not s["positions"] and not pending
                and s["entries"] < self.cfg.max_trades
                and opening+timedelta(minutes=30) <= now < closing-timedelta(minutes=30))
            if can_enter:
                for symbol in self.cfg.symbols:
                    signal = momentum(symbol, frame.get("bars", {}).get(symbol, []), now, opening, self.cfg)
                    if signal is None: continue
                    choice = self._choose(s, signal, quotes, now, day)
                    if choice is None:
                        self.store.event(now.isoformat(), "SKIP", underlying=symbol, reason="No contract fits liquidity and full premium budget")
                        continue
                    q, qty = choice
                    power = min(D(str(account.get("options_buying_power") or "0")),
                                max(D("0"), D(str(account.get("cash") or "0"))))
                    qty = min(qty, int(power//(q.ask*100+2*self.cfg.fee_per_contract)))
                    if qty < 1: continue
                    signal_bar = frame["bars"][symbol][-1]["end"]
                    self._prepare(s, q, qty, "buy", now, str(day)+":"+symbol+":"+signal.right+":"+signal_bar, signal.reason)
                    break
            s["last_at"] = now.isoformat()
            self.store.save(s)
            self.store.event(now.isoformat(), "SNAPSHOT", equity=self.equity(s), halt=s["halt"], day_halt=s["day_halt"])
            self.store.db.execute("INSERT INTO frames VALUES(?,?)", (frame["id"], digest))
        self._submit_ready(datetime.now(timezone.utc))
        return "processed"
