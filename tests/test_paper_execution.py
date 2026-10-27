import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import uuid

from options_bot.alpaca_data import DataError
from options_bot.alpaca_paper import AlpacaPaper, OrderNotFound, OrderRejected
from options_bot.demo import frame
from options_bot.engine import Engine
from options_bot.models import Config, stamp
from options_bot.paper_engine import PaperEngine


class Clock(datetime):
    value = stamp("2026-10-07T10:00:00-04:00")

    @classmethod
    def now(cls, tz=None):
        return cls.value.astimezone(tz) if tz else cls.value.replace(tzinfo=None)


class FakeBroker:
    def __init__(self):
        self.account_id = uuid.uuid4().hex
        self.book = {}
        self.submissions = []
        self.cancellations = []
        self.extra_positions = []
        self.extra_orders = []
        self.behavior = "new"
        self.market_open = True

    def account(self):
        return dict(id=self.account_id, status="ACTIVE", options_trading_level=2,
                    cash="100000", options_buying_power="100000")

    def clock(self):
        return dict(is_open=self.market_open,timestamp=Clock.now(timezone.utc).isoformat())

    def submit(self, payload):
        self.submissions.append(copy.deepcopy(payload))
        if self.behavior == "timeout_before":
            raise DataError("Timeout")
        if self.behavior == "rejected":
            raise OrderRejected("Rejected")
        cid = payload["client_order_id"]
        if cid in self.book:
            raise OrderRejected("Duplicate client id")
        self.book[cid] = dict(payload, id=uuid.uuid4().hex, status="new", filled_qty="0", filled_avg_price=None,
                             updated_at=Clock.now(timezone.utc).isoformat())
        if self.behavior == "filled":
            self.fill(cid, int(payload["qty"]), payload["limit_price"])
        if self.behavior == "timeout_after":
            raise DataError("Timeout after acceptance")
        return self.order(cid)

    def order(self, cid):
        if cid not in self.book:
            raise OrderNotFound("Not found")
        return copy.deepcopy(self.book[cid])

    def fill(self, cid, qty, price):
        order = self.book[cid]
        order.update(filled_qty=str(qty), filled_avg_price=str(price),
                     status="filled" if qty == int(order["qty"]) else "partially_filled",
                     filled_at=Clock.now(timezone.utc).isoformat(), updated_at=Clock.now(timezone.utc).isoformat())

    def positions(self):
        totals = {}
        for o in self.book.values():
            totals[o["symbol"]] = totals.get(o["symbol"],0) + int(o["filled_qty"])*(1 if o["side"] == "buy" else -1)
        return [dict(symbol=s, qty=str(q)) for s,q in totals.items() if q]+copy.deepcopy(self.extra_positions)

    def open_orders(self):
        return [copy.deepcopy(o) for o in self.book.values() if o["status"] not in ("filled","canceled","rejected","expired")]+self.extra_orders

    def cancel(self, order_id):
        self.cancellations.append(order_id)
        for o in self.book.values():
            if o["id"] == order_id:
                o["status"] = "pending_cancel"


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name)/"broker.sqlite3")
        Clock.value = stamp("2026-10-07T10:00:00-04:00")
        self.clock = patch("options_bot.paper_engine.datetime", Clock)
        self.clock.start()
        self.broker = FakeBroker()
        self.engine = PaperEngine(self.path, broker=self.broker)
        self.engine.connect()

    def tearDown(self):
        self.engine.close()
        self.clock.stop()
        self.tmp.cleanup()

    def f(self, minutes=0, bid="0.16", ask="0.17"):
        result = frame(bid=bid, ask=ask)
        Clock.value = stamp("2026-10-07T10:00:00-04:00")+timedelta(minutes=minutes)
        result.update(id="frame-"+str(minutes), at=Clock.value.isoformat())
        result["quotes"][0].update(feed="opra", at=result["at"])
        if minutes:
            result["bars"] = {}
        return result

    def order_id(self, index=-1):
        return self.broker.submissions[index]["client_order_id"]

    def test_acceptance_is_not_a_fill(self):
        self.engine.step(self.f())
        state=self.engine.summary()
        self.assertFalse(state["positions"])
        self.assertEqual(state["cash"], "1000.00")
        self.assertEqual(state["reserved"], "18.30")
        self.assertEqual(state["pending_orders"][0]["status"], "new")
        self.assertFalse(any(e["kind"]=="BUY_TO_OPEN" for e in self.engine.store.events()))

    def test_confirmed_buy_and_sell_update_ledger_once(self):
        self.engine.step(self.f())
        self.broker.fill(self.order_id(),1,"0.17")
        self.engine.reconcile()
        self.engine.reconcile()
        self.assertEqual(self.engine.summary()["cash"],"982.35")
        self.assertEqual(self.engine.summary()["equity"],"997.70")
        self.engine.step(self.f(1,"0.24","0.25"))
        self.assertEqual(self.broker.submissions[-1]["position_intent"],"sell_to_close")
        self.assertTrue(self.engine.summary()["positions"])
        self.broker.fill(self.order_id(),1,"0.24")
        self.engine.reconcile()
        self.assertFalse(self.engine.summary()["positions"])
        self.assertEqual(self.engine.summary()["equity"],"1005.70")

    def test_immediate_broker_fill_is_supported(self):
        self.broker.behavior="filled"
        self.engine.step(self.f())
        self.assertTrue(self.engine.summary()["positions"])

    def test_restart_reconciles_without_resubmission(self):
        self.engine.step(self.f())
        self.engine.close()
        self.broker.fill(self.order_id(),1,"0.17")
        self.engine=PaperEngine(self.path,broker=self.broker)
        self.engine.connect()
        self.assertEqual(len(self.broker.submissions),1)
        self.assertEqual(self.engine.summary()["cash"],"982.35")

    def test_timeout_after_acceptance_does_not_repeat_post(self):
        self.broker.behavior="timeout_after"
        with self.assertRaises(DataError): self.engine.step(self.f())
        self.assertEqual(self.engine.summary()["halt"],"order_submission_unknown")
        self.broker.behavior="new"
        self.engine.reconcile()
        self.assertEqual(len(self.broker.submissions),1)
        self.assertTrue(self.broker.cancellations)

    def test_unknown_timeout_never_blindly_retries(self):
        self.broker.behavior="timeout_before"
        with self.assertRaises(DataError): self.engine.step(self.f())
        with self.assertRaises(DataError): self.engine.reconcile()
        self.assertEqual(len(self.broker.submissions),1)
        self.assertEqual(self.engine.summary()["halt"],"order_status_unknown")

    def test_rejection_does_not_create_position(self):
        self.broker.behavior="rejected"
        self.engine.step(self.f())
        self.assertFalse(self.engine.summary()["positions"])
        self.assertEqual(self.engine.orders()[0]["status"],"rejected")
        self.assertEqual(self.engine.summary()["reserved"],"0.00")

    def test_client_id_deduplicates_same_signal_bar(self):
        self.broker.behavior="rejected"
        first=self.f()
        self.engine.step(first)
        second=copy.deepcopy(first)
        Clock.value+=timedelta(seconds=5)
        second.update(id="same-signal",at=Clock.value.isoformat())
        second["quotes"][0]["at"]=second["at"]
        self.engine.step(second)
        self.assertEqual(len(self.broker.submissions),1)

    def test_budget_rejection_with_large_broker_balance(self):
        self.engine.step(self.f(bid="4.30",ask="4.50"))
        self.assertEqual(len(self.broker.submissions),0)

    def test_no_market_or_short_option_orders(self):
        self.engine.step(self.f())
        payload=self.broker.submissions[0]
        self.assertEqual(payload["type"],"limit")
        self.assertEqual(payload["time_in_force"],"day")
        self.assertEqual(payload["position_intent"],"buy_to_open")

    def test_halt_cancels_entries_and_waits_for_confirmation(self):
        self.engine.step(self.f())
        self.engine.halt("manual")
        self.engine.reconcile()
        self.assertTrue(self.broker.cancellations)
        self.assertTrue(self.engine.summary()["pending_orders"])
        self.broker.book[self.order_id()]["status"]="canceled"
        self.engine.reconcile()
        self.assertFalse(self.engine.summary()["pending_orders"])

    def test_duplicate_frame_honors_kill_switch(self):
        f=self.f()
        self.engine.step(f)
        self.engine.step(f,kill=True)
        self.assertEqual(self.engine.summary()["halt"],"kill_switch")
        self.assertTrue(self.broker.cancellations)

    def test_unmanaged_positions_halt_without_liquidation(self):
        self.broker.extra_positions=[dict(symbol="AAPL",qty="1")]
        with self.assertRaises(DataError): self.engine.reconcile()
        self.assertEqual(self.engine.summary()["halt"],"broker_position_mismatch")
        self.assertFalse(self.broker.submissions)

    def test_unmanaged_orders_are_never_canceled(self):
        self.broker.extra_orders=[dict(client_order_id="manual")]
        with self.assertRaises(DataError): self.engine.reconcile()
        self.assertEqual(self.engine.summary()["halt"],"unmanaged_broker_order")
        self.assertFalse(self.broker.cancellations)

    def test_account_identity_binding(self):
        self.broker.account_id="different-account"
        with self.assertRaises(DataError): self.engine.reconcile()

    def test_same_account_cannot_have_two_controllers_on_this_host(self):
        other=PaperEngine(str(Path(self.tmp.name)/"other.sqlite3"),broker=self.broker)
        try:
            with self.assertRaises(DataError): other.connect()
        finally: other.close()

    def test_local_database_cannot_be_used_for_broker_orders(self):
        path=str(Path(self.tmp.name)/"local.sqlite3")
        local=Engine(path)
        local.close()
        with self.assertRaises(ValueError): PaperEngine(path,broker=self.broker)

    def test_broker_database_cannot_be_used_for_local_simulation(self):
        with self.assertRaises(ValueError): Engine(self.path)

    def test_market_closed_before_submission_cancels_local_intent(self):
        self.broker.market_open=False
        self.engine.step(self.f())
        self.assertFalse(self.broker.submissions)
        self.assertEqual(self.engine.orders()[0]["status"],"canceled")

    def test_historical_and_synthetic_frames_cannot_submit(self):
        with self.assertRaises(ValueError): self.engine.step(frame())
        f=self.f()
        Clock.value+=timedelta(days=1)
        with self.assertRaises(ValueError): self.engine.step(f)
        self.assertFalse(self.broker.submissions)

    def test_unfilled_entry_times_out_via_cancellation(self):
        self.engine.step(self.f())
        Clock.value+=timedelta(seconds=61)
        self.engine.reconcile()
        self.assertTrue(self.broker.cancellations)
        self.assertTrue(self.engine.summary()["pending_orders"])

    def test_fill_limit_violation_halts_processing(self):
        self.engine.step(self.f())
        self.broker.fill(self.order_id(),1,"0.99")
        with self.assertRaises(DataError): self.engine.reconcile()
        self.assertFalse(self.engine.summary()["positions"])

    def test_partial_buy_and_sell_fills(self):
        self.engine.close()
        # A 5000 budget allows multiple cheap fixture contracts.
        self.path=str(Path(self.tmp.name)/"larger.sqlite3")
        self.engine=PaperEngine(self.path,Config(capital=Decimal("5000")),self.broker)
        self.engine.connect()
        self.engine.step(self.f())
        buy=self.order_id()
        self.assertEqual(self.broker.submissions[0]["qty"],"5")
        self.broker.fill(buy,2,"0.17")
        self.engine.reconcile()
        self.assertEqual(self.engine.summary()["positions"][frame()["quotes"][0]["symbol"]]["qty"],2)
        self.broker.book[buy]["status"]="canceled"
        self.engine.reconcile()
        self.engine.step(self.f(1,"0.24","0.25"))
        sell=self.order_id()
        self.broker.fill(sell,1,"0.24")
        self.engine.reconcile()
        self.assertEqual(self.engine.summary()["positions"][frame()["quotes"][0]["symbol"]]["qty"],1)
        self.broker.fill(sell,2,"0.24")
        self.engine.reconcile()
        self.assertFalse(self.engine.summary()["positions"])
        self.assertEqual(self.engine.summary()["equity"],"5011.40")


class TransportTests(unittest.TestCase):
    def payload(self):
        return dict(symbol="SPY261014C00601000",qty="1",side="buy",type="limit",time_in_force="day",
                    limit_price="0.17",client_order_id="op-test",position_intent="buy_to_open")

    def test_request_host_cannot_be_selected_by_environment(self):
        client=AlpacaPaper("fake","fake")
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{}'
        with patch.dict("os.environ",{"APCA_API_BASE_URL":"https://api.alpaca.markets"}), \
             patch.object(client.opener,"open",return_value=Response()) as send:
            client.submit(self.payload())
        req=send.call_args.args[0]
        self.assertTrue(req.full_url.startswith("https://paper-api.alpaca.markets/"))
        self.assertEqual(req.method,"POST")

    def test_unapproved_payloads_and_endpoints_are_blocked(self):
        client=AlpacaPaper("fake","fake")
        for field,value in (("side","sell"),("type","market"),("qty","0.5"),("symbol","AAPL"),("limit_price","0")):
            payload=self.payload();payload[field]=value
            with self.assertRaises(DataError): client.submit(payload)
        with self.assertRaises(DataError): client.request("POST","https://api.alpaca.markets/v2/orders")
        with self.assertRaises(DataError): client.request("POST","/v2/positions")

    def test_errors_never_echo_credentials(self):
        client=AlpacaPaper("private-key","private-secret")
        with patch.object(client.opener,"open",side_effect=HTTPError("hidden",401,"private-secret",{},None)):
            with self.assertRaises(DataError) as exc: client.account()
        self.assertNotIn("private-secret",str(exc.exception))

    def test_account_requires_options_level_two(self):
        client=AlpacaPaper("fake","fake")
        with patch.object(client,"request",return_value=dict(id="x",status="ACTIVE",options_trading_level=1)):
            with self.assertRaises(DataError): client.account()


if __name__ == "__main__": unittest.main()
