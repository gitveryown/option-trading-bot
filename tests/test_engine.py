import copy
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from options_bot.cli import main
from options_bot.demo import frame, sequence
from options_bot.engine import Engine
from options_bot.models import Config, stamp
from options_bot.strategy import Signal, momentum


class TradingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "paper.sqlite3")
        self.engine = Engine(self.path)

    def tearDown(self):
        self.engine.store.close()
        self.tmp.cleanup()

    def events(self, kind):
        return [e for e in self.engine.store.events() if e["kind"] == kind]

    def later(self, first, minutes=1, bid="0.16", ask="0.17"):
        f = copy.deepcopy(first)
        at = stamp(first["at"]) + timedelta(minutes=minutes)
        f.update(id=f"frame-{at.isoformat()}", at=at.isoformat(), bars={})
        f["quotes"][0].update(at=f["at"], bid=bid, ask=ask)
        return f

    def test_real_strategy_call_and_put(self):
        for right in ("call", "put"):
            f = frame(right=right)
            result = momentum("SPY", f["bars"]["SPY"], stamp(f["at"]),
                              stamp(f["session"]["open"]), Config())
            self.assertEqual(result.right, right)

    def test_flat_and_insufficient_volume_no_signal(self):
        f = frame()
        f["bars"]["SPY"][-1]["volume"] = 1000
        self.engine.step(f)
        self.assertFalse(self.events("BUY_TO_OPEN"))

    def test_profit_is_net_of_both_fees(self):
        for f in sequence():
            self.engine.step(f)
        self.assertEqual(self.engine.summary()["equity"], "1005.70")
        self.assertEqual(self.events("SELL_TO_CLOSE")[0]["pnl"], "5.70")
        self.assertEqual(self.engine.summary()["unsettled"], "23.35")

    def test_full_premium_cap_skips_expensive_contract(self):
        self.engine.step(frame(bid="4.30", ask="4.50"))
        self.assertFalse(self.events("BUY_TO_OPEN"))
        self.assertTrue(self.events("SKIP"))

    def test_fee_reserve_prevents_twenty_dollar_contract(self):
        self.engine.step(frame(bid="0.19", ask="0.20"))
        self.assertFalse(self.events("BUY_TO_OPEN"))

    def test_gap_stop_is_not_a_guaranteed_twenty_percent_loss(self):
        for f in sequence("gap"):
            self.engine.step(f)
        self.assertEqual(self.engine.summary()["equity"], "982.70")
        self.assertEqual(self.events("SELL_TO_CLOSE")[0]["reason"], "premium_stop")

    def test_idempotency_survives_restart(self):
        f = frame()
        self.engine.step(f)
        self.engine.store.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.step(f), "duplicate")
        self.assertEqual(len(self.events("BUY_TO_OPEN")), 1)

    def test_mutated_duplicate_rejected(self):
        f = frame()
        self.engine.step(f)
        f["quotes"][0]["ask"] = "0.18"
        with self.assertRaises(ValueError):
            self.engine.step(f)
        self.assertEqual(len(self.events("BUY_TO_OPEN")), 1)

    def test_config_change_rejected(self):
        with self.assertRaises(ValueError):
            Engine(self.path, Config(capital=Decimal("2000")))

    def test_live_mode_rejected(self):
        with self.assertRaises(ValueError):
            Config(mode="live")

    def test_stale_quote_prevents_entry(self):
        f = frame()
        f["quotes"][0]["at"] = "2026-10-07T09:59:00-04:00"
        self.engine.step(f)
        self.assertFalse(self.events("BUY_TO_OPEN"))

    def test_future_quote_prevents_entry(self):
        f = frame()
        f["quotes"][0]["at"] = "2026-10-07T10:01:00-04:00"
        self.engine.step(f)
        self.assertFalse(self.events("BUY_TO_OPEN"))

    def test_invalid_chain_filters(self):
        for field, value in (("expiration", "2026-10-08"), ("delta", .9),
                             ("delta", -.5), ("multiplier", 10), ("open_interest", 5),
                             ("feed", "indicative"), ("ask_size", 0), ("ask", "0.50")):
            with self.subTest(field=field, value=value):
                db = str(Path(self.tmp.name) / f"filter-{field}-{value}.sqlite3")
                engine = Engine(db)
                try:
                    f = frame()
                    f["quotes"][0][field] = value
                    engine.step(f)
                    self.assertEqual(engine.summary()["entries_today"], 0)
                finally:
                    engine.store.close()

    def test_unpriced_position_halts_then_closes_on_fresh_quote(self):
        f = frame()
        self.engine.step(f)
        second = self.later(f)
        second["quotes"] = []
        self.engine.step(second)
        self.assertEqual(self.engine.summary()["halt"], "unpriced_position")
        self.assertTrue(self.engine.summary()["positions"])
        self.engine.step(self.later(f, minutes=2))
        self.assertFalse(self.engine.summary()["positions"])
        self.assertEqual(self.engine.summary()["halt"], "unpriced_position")

    def test_kill_closes_and_persists(self):
        f = frame()
        self.engine.step(f)
        self.engine.halt("manual_kill_switch")
        self.engine.step(self.later(f))
        self.assertFalse(self.engine.summary()["positions"])
        self.engine.store.close()
        self.engine = Engine(self.path)
        self.assertEqual(self.engine.summary()["halt"], "manual_kill_switch")

    def test_insufficient_bid_size_leaves_pending_exit(self):
        f = frame()
        self.engine.step(f)
        self.engine.halt("manual_kill_switch")
        second = self.later(f)
        second["quotes"][0]["bid_size"] = 0
        self.engine.step(second)
        self.assertTrue(self.engine.summary()["positions"])
        self.assertTrue(self.events("EXIT_PENDING"))

    def test_one_position_prevents_averaging_down(self):
        f = frame()
        self.engine.step(f)
        with patch("options_bot.engine.momentum", return_value=Signal("SPY", "call", "test")):
            self.engine.step(self.later(f))
        self.assertEqual(len(self.events("BUY_TO_OPEN")), 1)

    def test_daily_room_reserves_entire_next_premium(self):
        f = frame()
        self.engine.step(f)
        self.engine.step(self.later(f, bid="0.01", ask="0.02"))
        with patch("options_bot.engine.momentum", return_value=Signal("SPY", "call", "test")):
            self.engine.step(self.later(f, minutes=2))
        # First loss of 17.30 leaves only 12.70 of the 30.00 daily loss budget.
        self.assertEqual(len(self.events("BUY_TO_OPEN")), 1)

    def test_daily_and_drawdown_circuit_breakers(self):
        with self.engine.store.transaction():
            s = self.engine.store.state()
            s.update(day="2026-10-07", cash="970.00")
            self.engine.store.save(s)
        self.engine.step(frame())
        self.assertEqual(self.engine.summary()["day_halt"], "daily_loss")
        with self.engine.store.transaction():
            s = self.engine.store.state()
            s["cash"] = "900.00"
            self.engine.store.save(s)
        self.engine.step(self.later(frame()))
        self.assertEqual(self.engine.summary()["halt"], "account_drawdown")

    def test_loss_streak_blocks_entry(self):
        with self.engine.store.transaction():
            s = self.engine.store.state()
            s.update(day="2026-10-07", loss_streak=3)
            self.engine.store.save(s)
        self.engine.step(frame())
        self.assertEqual(self.engine.summary()["day_halt"], "loss_streak")
        self.assertFalse(self.events("BUY_TO_OPEN"))

    def test_trade_count_limit(self):
        with self.engine.store.transaction():
            s = self.engine.store.state()
            s.update(day="2026-10-07", entries=3)
            self.engine.store.save(s)
        self.engine.step(frame())
        self.assertFalse(self.events("BUY_TO_OPEN"))

    def test_close_before_early_session_end(self):
        f = frame()
        f["session"]["close"] = "2026-10-07T13:00:00-04:00"
        self.engine.step(f)
        second = self.later(f, minutes=165)
        self.engine.step(second)
        self.assertFalse(self.engine.summary()["positions"])
        self.assertEqual(self.events("SELL_TO_CLOSE")[0]["reason"], "session_flatten")

    def test_maximum_holding_time(self):
        f = frame()
        self.engine.step(f)
        self.engine.step(self.later(f, minutes=60))
        self.assertEqual(self.events("SELL_TO_CLOSE")[0]["reason"], "maximum_holding_time")

    def test_trailing_exit(self):
        f = frame()
        self.engine.step(f)
        self.engine.step(self.later(f, bid="0.22", ask="0.23"))
        self.engine.step(self.later(f, minutes=2, bid="0.18", ask="0.19"))
        self.assertEqual(self.events("SELL_TO_CLOSE")[0]["reason"], "trailing_exit")

    def test_unsettled_cash_credited_only_next_session(self):
        for f in sequence():
            self.engine.step(f)
        tomorrow = frame()
        tomorrow["id"] = "tomorrow"
        tomorrow["at"] = "2026-10-08T10:00:00-04:00"
        tomorrow["session"] = dict(date="2026-10-08", open="2026-10-08T09:30:00-04:00",
                                   close="2026-10-08T16:00:00-04:00")
        tomorrow["bars"], tomorrow["quotes"] = {}, []
        self.engine.step(tomorrow)
        self.assertEqual(self.engine.summary()["cash"], "1005.70")
        self.assertEqual(self.engine.summary()["unsettled"], "0.00")

    def test_future_bar_rejected_atomically(self):
        f = frame()
        f["bars"]["SPY"][-1]["end"] = "2026-10-07T10:01:00-04:00"
        with self.assertRaises(ValueError):
            self.engine.step(f)
        self.assertFalse(self.engine.store.events())

    def test_reversed_time_rejected(self):
        self.engine.step(frame())
        f = frame()
        f.update(id="out-of-order", bars={}, at="2026-10-07T09:59:00-04:00")
        with self.assertRaises(ValueError):
            self.engine.step(f)

    def test_cli_invalid_json_persists_halt(self):
        path = Path(self.tmp.name) / "broken.jsonl"
        path.write_text("{not json}\n")
        self.assertEqual(main(["replay", "--input", str(path), "--db", self.path]), 1)
        self.assertEqual(self.engine.summary()["halt"], "input_or_runtime_error")

    def test_transaction_rollback_on_write_failure(self):
        before = self.engine.summary()
        with patch.object(self.engine.store, "save", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.engine.step(frame())
        self.assertEqual(self.engine.summary(), before)
        self.assertFalse(self.engine.store.events())


if __name__ == "__main__":
    unittest.main()
