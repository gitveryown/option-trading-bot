import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from options_bot.alpaca_data import DataError
from options_bot.dashboard import Controller, make_server
from tests.test_alpaca_data import FakeData
from tests.test_paper_execution import FakeBroker, Clock


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.args = SimpleNamespace(capital=1000, db=root/"real.sqlite3", env_file=root/".env",
            record=root/"observed.jsonl", kill_file=root/"STOP_TRADING", interval=5)
        # Production CLI supplies a Decimal.
        from decimal import Decimal
        self.args.capital = Decimal("1000.00")
        self.controller = Controller(self.args)
        self.server = make_server(self.controller, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.controller.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            result = conn.getresponse()
            status, response_headers, data = result.status, dict(result.getheaders()), result.read()
            return status, response_headers, data
        finally:
            conn.close()

    def post(self, path, body=None, extra=None):
        headers = {"Content-Type":"application/json", "X-Paper-Token":self.controller.csrf}
        headers.update(extra or {})
        return self.request("POST", path, json.dumps(body or {}), headers)

    def test_initial_account_has_no_generated_quotes_or_trades(self):
        status, _, data = self.request("GET", "/api/state")
        body = json.loads(data)
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "real_quotes")
        self.assertEqual(body["account"]["equity"], "1000.00")
        self.assertEqual(body["events"], [])
        self.assertEqual(body["chart"], [])
        self.assertEqual(body["data"]["quote_preview"], [])

    def test_demo_separate_from_real_account_and_labeled(self):
        self.assertEqual(self.post("/api/demo")[0], 200)
        demo = self.controller.state()
        self.assertEqual(demo["mode"], "synthetic_test")
        self.assertEqual(demo["account"]["equity"], "1005.70")
        self.assertEqual(len(demo["chart"]), 3)
        self.assertEqual(demo["data"]["quote_preview"], [])
        self.post("/api/real")
        real = self.controller.state()
        self.assertEqual(real["account"]["equity"], "1000.00")
        self.assertEqual(real["events"], [])

    def test_gap_and_budget_scenarios(self):
        for scenario, expected in (("gap", "982.70"), ("budget", "1000.00")):
            self.post("/api/demo", {"scenario":scenario})
            self.assertEqual(self.controller.state()["account"]["equity"], expected)

    def test_halt_persists_across_new_controller(self):
        self.assertEqual(self.post("/api/halt")[0], 200)
        again = Controller(self.args)
        self.assertEqual(again.state()["account"]["halt"], "operator_requested")

    def test_missing_token_blocks_mutation(self):
        result = self.request("POST", "/api/halt", "{}", {"Content-Type":"application/json"})
        self.assertEqual(result[0], 403)
        self.assertIsNone(self.controller.state()["account"]["halt"])

    def test_cross_origin_post_blocked(self):
        result = self.post("/api/halt", extra={"Origin":"https://another-site.example"})
        self.assertEqual(result[0], 403)

    def test_foreign_host_blocked(self):
        self.assertEqual(self.request("GET", "/api/state", headers={"Host":"another-site.example"})[0], 403)

    def test_credentials_never_served(self):
        self.args.env_file.write_text("APCA_API_KEY_ID=private-key\nAPCA_API_SECRET_KEY=private-secret\n")
        for path in ("/.env", "/../.env", "/api/credentials"):
            self.assertEqual(self.request("GET", path)[0], 404)
        self.assertNotIn("private-secret", self.request("GET", "/api/state")[2].decode())

    def test_setup_error_does_not_latch_halt_in_untouched_account(self):
        with patch("options_bot.dashboard.credentials", side_effect=DataError("Credentials missing")):
            self.controller._worker()
        state = self.controller.state()
        self.assertEqual(state["error"], "Credentials missing")
        self.assertIsNone(state["account"]["halt"])

    def test_setup_error_halts_carried_position(self):
        from options_bot.demo import frame
        engine = self.controller.open_engine()
        engine.step(frame())
        engine.store.close()
        with patch("options_bot.dashboard.credentials", side_effect=DataError("Credentials missing")):
            self.controller._worker()
        self.assertEqual(self.controller.state()["account"]["halt"], "market_data_or_runtime_error")

    def test_authenticated_cycle_records_real_mode_input(self):
        provider = FakeData()
        actual_frame = provider.frame
        def one_frame(engine):
            result = actual_frame(engine)
            self.controller.stop_event.set()
            return result
        with patch("options_bot.dashboard.credentials", return_value=["fake","fake"]), \
             patch("options_bot.dashboard.AlpacaData", return_value=provider), \
             patch.object(provider, "frame", side_effect=one_frame):
            self.controller._worker()
        state = self.controller.state()
        self.assertEqual(state["account"]["entries_today"], 1)
        self.assertEqual(state["data"]["source"], "OPRA quotes and SIP bars")
        self.assertTrue(self.args.record.exists())

    def test_failure_after_data_check_halts_account(self):
        provider = FakeData()
        with patch("options_bot.dashboard.credentials", return_value=["fake","fake"]), \
             patch("options_bot.dashboard.AlpacaData", return_value=provider), \
             patch.object(provider, "frame", side_effect=DataError("Disconnected")):
            self.controller._worker()
        self.assertEqual(self.controller.state()["account"]["halt"], "market_data_or_runtime_error")

    def test_repeated_start_does_not_create_second_runner(self):
        release = threading.Event()
        with patch.object(self.controller, "_worker", side_effect=lambda:release.wait(2)):
            self.controller.start()
            first = self.controller.thread
            self.controller.start()
            self.assertIs(self.controller.thread, first)
            release.set()
            first.join(timeout=3)

    def test_demo_cannot_hide_active_real_runner(self):
        release = threading.Event()
        with patch.object(self.controller, "_worker", side_effect=lambda:release.wait(2)):
            self.controller.start()
            self.assertEqual(self.post("/api/demo")[0], 400)
            self.assertEqual(self.controller.state()["mode"], "real_quotes")
            release.set()
            self.controller.thread.join(timeout=3)

    def test_dashboard_headers_and_asset(self):
        status, headers, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"Your options desk", page)

    def test_invalid_request_bodies_rejected(self):
        self.assertEqual(self.post("/api/demo", {"scenario":"unknown"})[0], 400)
        self.assertEqual(self.request("POST", "/api/halt", "[]", {
            "Content-Type":"application/json", "X-Paper-Token":self.controller.csrf})[0], 400)
        self.assertEqual(self.request("POST", "/api/halt", "{}", {
            "Content-Type":"text/plain", "X-Paper-Token":self.controller.csrf})[0], 415)

    def remote_controller(self):
        args=SimpleNamespace(**vars(self.args))
        args.execution="alpaca_paper"
        args.db=Path(self.tmp.name)/"alpaca.sqlite3"
        return Controller(args)

    def test_remote_dashboard_declares_broker_execution(self):
        controller=self.remote_controller()
        state=controller.state()
        self.assertEqual(state["execution"],"alpaca_paper")
        self.assertEqual(state["account"]["mode"],"ALPACA PAPER EXECUTION")
        self.assertEqual(state["account"]["broker_orders"],[])

    def test_remote_demo_uses_local_database_only(self):
        controller=self.remote_controller()
        controller.demo("mechanics")
        self.assertEqual(controller.state()["execution"],"local")
        self.assertEqual(controller.state()["account"]["equity"],"1005.70")
        controller.select_real()
        self.assertEqual(controller.state()["execution"],"alpaca_paper")
        self.assertEqual(controller.state()["account"]["equity"],"1000.00")

    def test_remote_worker_submits_without_inventing_a_fill(self):
        controller=self.remote_controller()
        provider=FakeData()
        broker=FakeBroker()
        original=provider.frame
        def one_frame(engine):
            result=original(engine)
            controller.stop_event.set()
            return result
        Clock.value=provider.current
        with patch("options_bot.paper_engine.datetime",Clock), \
             patch("options_bot.dashboard.credentials",return_value=["fake","fake"]), \
             patch("options_bot.dashboard.AlpacaData",return_value=provider), \
             patch("options_bot.dashboard.AlpacaPaper",return_value=broker), \
             patch.object(provider,"frame",side_effect=one_frame):
            controller._worker()
        state=controller.state()
        self.assertEqual(len(broker.submissions),1)
        self.assertEqual(state["account"]["positions"],{})
        self.assertEqual(state["account"]["cash"],"1000.00")
        self.assertTrue(state["account"]["pending_orders"])
        self.assertTrue(broker.cancellations)


if __name__ == "__main__":
    unittest.main()
