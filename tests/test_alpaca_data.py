from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from types import SimpleNamespace

from options_bot.alpaca_data import AlpacaData, DataError, credentials
from options_bot.demo import frame
from options_bot.engine import Engine
from options_bot.models import stamp
from options_bot.live import run


class FakeData(AlpacaData):
    def __init__(self):
        self.current = stamp("2026-10-07T10:00:01-04:00")
        super().__init__("fake", "fake", now=lambda: self.current)
        self.calls = []
        self.open = True
        self.quote = dict(bp="0.16", ap="0.17", bs=10, **{"as": 10}, t=self.current.isoformat())
        self.delta = .5
        self.raw = frame()
        self.symbol = self.raw["quotes"][0]["symbol"]

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path == "/v2/clock":
            return dict(timestamp=self.current.isoformat(), is_open=self.open,
                        next_open="2026-10-08T09:30:00-04:00")
        if path == "/v2/calendar":
            return [dict(date="2026-10-07", open="09:30", close="16:00")]
        if path == "/v2/stocks/bars":
            return dict(bars={"SPY": [dict(t=(stamp(b["end"])-timedelta(minutes=1)).isoformat(),
                o=b["open"], h=b["high"], l=b["low"], c=b["close"], v=b["volume"])
                for b in self.raw["bars"]["SPY"]]}, next_page_token=None)
        if path == "/v2/options/contracts":
            return dict(option_contracts=[dict(symbol=self.symbol, tradable=True, root_symbol="SPY",
                underlying_symbol="SPY", expiration_date="2026-10-14", type="call", size="100",
                open_interest="1500")], next_page_token=None)
        if path.startswith("/v1beta1/options/snapshots"):
            return dict(snapshots={self.symbol: dict(latestQuote=self.quote,
                        greeks={} if self.delta is None else dict(delta=self.delta))}, next_page_token=None)
        raise AssertionError(path)


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Engine(str(Path(self.tmp.name)/"account.sqlite3"))
        self.data = FakeData()

    def tearDown(self):
        self.engine.store.close()
        self.tmp.cleanup()

    def test_actual_api_field_mapping_and_simulated_fill(self):
        f, info = self.data.frame(self.engine)
        self.assertEqual(f["quotes"][0]["feed"], "opra")
        self.assertEqual(f["quotes"][0]["ask_size"], 10)
        self.assertEqual(f["bars"]["SPY"][-1]["end"], "2026-10-07T10:00:00-04:00")
        self.engine.step(f)
        self.assertEqual(self.engine.summary()["entries_today"], 1)
        self.assertEqual(info["quote_preview"][0]["one_contract_premium"], "17.00")

    def test_closed_market_never_fetches_chain_or_places_trade(self):
        self.data.open = False
        f, info = self.data.frame(self.engine)
        self.assertIsNone(f)
        self.assertEqual(info["market"], "closed")
        self.assertEqual(len(self.data.calls), 1)

    def test_feed_parameters_are_explicit(self):
        self.data.frame(self.engine)
        for path, params in self.data.calls:
            if path.startswith("/v1beta1/"):
                self.assertEqual(params["feed"], "opra")
            if path == "/v2/stocks/bars":
                self.assertEqual(params["feed"], "sip")

    def test_held_contract_refreshes_without_greeks_and_exits(self):
        f, _ = self.data.frame(self.engine)
        self.engine.step(f)
        self.engine.halt("manual_kill_switch")
        self.data.current += timedelta(seconds=5)
        self.data.delta = None
        self.data.quote.update(t=self.data.current.isoformat(), bp="0.24", ap="0.25")
        self.data.calls = []
        f, _ = self.data.frame(self.engine)
        self.engine.step(f)
        self.assertFalse(self.engine.summary()["positions"])
        self.assertFalse(any(path.endswith("/SPY") for path, _ in self.data.calls))

    def test_minute_bars_and_catalog_are_cached(self):
        self.data.frame(self.engine)
        self.data.current += timedelta(seconds=5)
        self.data.frame(self.engine)
        self.assertEqual(sum(path=="/v2/stocks/bars" for path, _ in self.data.calls), 1)
        self.assertEqual(sum(path=="/v2/options/contracts" for path, _ in self.data.calls), 1)

    def test_pagination_collects_every_page(self):
        data = AlpacaData("fake", "fake")
        pages = [dict(snapshots={"a": {}}, next_page_token="next"),
                 dict(snapshots={"b": {}}, next_page_token=None)]
        with patch.object(data, "get", side_effect=pages) as get:
            result = data.pages("/v1beta1/options/snapshots", {}, "snapshots")
            self.assertEqual(len(result), 2)
            self.assertEqual(get.call_args_list[1].args[1]["page_token"], "next")

    def test_repeating_page_token_fails(self):
        with patch.object(self.data, "get", return_value=dict(snapshots={}, next_page_token="same")):
            with self.assertRaises(DataError):
                self.data.pages("/v1beta1/options/snapshots", {}, "snapshots")

    def test_order_endpoints_are_blocked(self):
        data = AlpacaData("fake", "fake")
        for path in ("/v2/orders", "https://api.alpaca.markets/v2/orders", "/v2/positions"):
            with self.assertRaises(DataError):
                data.get(path)

    def test_http_failures_do_not_expose_keys(self):
        data = AlpacaData("secret-key", "secret-value")
        with patch.object(data.opener, "open", side_effect=HTTPError("private",403,"secret-value",{},None)):
            with self.assertRaises(DataError) as error:
                data.get("/v2/clock")
            self.assertNotIn("secret", str(error.exception))
            self.assertIn("403", str(error.exception))

    def test_file_credentials_and_environment_precedence(self):
        path = Path(self.tmp.name)/".env"
        path.write_text('APCA_API_KEY_ID="file-key"\nAPCA_API_SECRET_KEY=file-secret\n')
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(credentials(path), ["file-key", "file-secret"])
        with patch.dict("os.environ", {"APCA_API_KEY_ID":"env-key"}, clear=True):
            self.assertEqual(credentials(path), ["env-key", "file-secret"])

    def test_missing_credentials_produce_clear_message(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(DataError):
                credentials(Path(self.tmp.name)/"missing")

    def test_clock_drift_blocks_processing(self):
        real_get = self.data.get
        def shifted(path, params=None):
            result = real_get(path, params)
            if path == "/v2/clock":
                result["timestamp"] = (self.data.current-timedelta(minutes=1)).isoformat()
            return result
        with patch.object(self.data, "get", side_effect=shifted):
            with self.assertRaises(DataError):
                self.data.session()

    def test_live_cycle_records_frame_and_simulates_trade(self):
        args = SimpleNamespace(env_file=Path(self.tmp.name)/".env", command="live",
            record=Path(self.tmp.name)/"observed.jsonl", kill_file=Path(self.tmp.name)/"STOP_TRADING",
            once=True, max_cycles=0, interval=5)
        with patch("options_bot.live.credentials", return_value=["fake","fake"]), \
             patch("options_bot.live.AlpacaData", return_value=self.data), patch("builtins.print"):
            run(self.engine, args)
        self.assertEqual(self.engine.summary()["entries_today"], 1)
        self.assertEqual(len(args.record.read_text().splitlines()), 1)

    def test_provider_failure_persists_halt(self):
        args = SimpleNamespace(env_file=Path(self.tmp.name)/".env", command="live",
            record=Path(self.tmp.name)/"observed.jsonl", kill_file=Path(self.tmp.name)/"STOP_TRADING",
            once=True, max_cycles=0, interval=5)
        with patch("options_bot.live.credentials", return_value=["fake","fake"]), \
             patch("options_bot.live.AlpacaData", return_value=self.data), \
             patch.object(self.data, "frame", side_effect=DataError("Disconnected")), patch("builtins.print"):
            with self.assertRaises(DataError):
                run(self.engine, args)
        self.assertEqual(self.engine.summary()["halt"], "market_data_or_runtime_error")


if __name__ == "__main__":
    unittest.main()
