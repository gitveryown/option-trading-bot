"""Loopback dashboard for local simulation or Alpaca paper account execution."""
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
from urllib.parse import urlsplit
import uuid

from .alpaca_data import AlpacaData, DataError, credentials
from .demo import sequence
from .engine import Engine
from .models import Config, money
from .execution import make_engine
from .alpaca_paper import AlpacaPaper


class Controller:
    def __init__(self, args):
        self.args = args
        self.cfg = Config(capital=args.capital)
        self.real_db = Path(args.db).resolve()
        self.db = self.real_db
        self.mode = "real_quotes"
        self.info = {"market": "not_connected", "quote_preview": []}
        self.error = None
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        self.csrf = secrets.token_urlsafe(32)
        engine = self.open_engine()
        engine.store.close()

    def open_engine(self):
        if self.mode == "synthetic_test":
            return Engine(str(self.db), self.cfg)
        return make_engine(self.args, self.db, self.cfg)

    def state(self):
        with self.lock:
            engine = self.open_engine()
            try:
                # Read one consistent account/ledger view while the worker writes.
                engine.store.db.execute("BEGIN")
                checkpoint = engine.store.state()
                summary = engine.summary()
                rows = engine.store.db.execute(
                    "SELECT id,at,kind,body FROM events WHERE kind != 'SNAPSHOT' ORDER BY id DESC LIMIT 100"
                ).fetchall()
                chart_rows = engine.store.db.execute(
                    "SELECT at,body FROM events WHERE kind = 'SNAPSHOT' ORDER BY id DESC LIMIT 240"
                ).fetchall()
                trades = engine.store.db.execute(
                    "SELECT body FROM events WHERE kind = 'SELL_TO_CLOSE'"
                ).fetchall()
                engine.store.db.execute("COMMIT")
                events = [dict(id=r[0], at=r[1], kind=r[2], **json.loads(r[3])) for r in rows]
                chart = [dict(at=r[0], equity=json.loads(r[1])["equity"]) for r in reversed(chart_rows)]
                equity = Decimal(summary["equity"])
                basis = min(self.cfg.capital, equity, Decimal(checkpoint["day_equity"]))
                exposure = sum((Decimal(p["cost"]) for p in summary["positions"].values()), Decimal("0")) + Decimal(summary.get("reserved", "0"))
                realized = sum((Decimal(json.loads(r[0])["pnl"]) for r in trades), Decimal("0"))
                loss = max(Decimal("0"), Decimal(checkpoint["day_equity"]) - equity)
                return dict(csrf=self.csrf, mode=self.mode, execution=summary.get("execution_backend", "local"), account=summary, events=events, chart=chart,
                    data=self.info, error=self.error,
                    running=bool(self.thread and self.thread.is_alive()),
                    last_observed=checkpoint["last_at"], capital=str(self.cfg.capital),
                    net_change=str(money(equity-self.cfg.capital)), realized=str(money(realized)),
                    day_change=str(money(equity-Decimal(checkpoint["day_equity"]))),
                    exposure=str(money(exposure)),
                    limits=dict(position_budget=str(money(basis*self.cfg.premium_cap)),
                        exposure_ceiling=str(money(basis*self.cfg.exposure_cap)),
                        daily_loss_threshold=str(money(Decimal(checkpoint["day_equity"])*self.cfg.daily_loss_cap)),
                        daily_loss_used=str(money(loss)), max_positions=self.cfg.max_positions,
                        max_entries=self.cfg.max_trades, min_dte=self.cfg.min_dte, max_dte=self.cfg.max_dte,
                        stop_percent=str(self.cfg.stop*100), target_percent=str(self.cfg.target*100)))
            finally:
                engine.close()

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.db, self.mode = self.real_db, "real_quotes"
            self.error = None
            self.info = {"market": "connecting", "quote_preview": []}
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._worker, daemon=True, name="paper-quotes")
            self.thread.start()

    def _worker(self):
        engine = None
        authenticated = False
        try:
            engine = make_engine(self.args, self.real_db, self.cfg)
            keys = credentials(self.args.env_file)
            provider = AlpacaData(*keys)
            remote = getattr(self.args, "execution", "local") == "alpaca_paper"
            if remote:
                engine.broker = AlpacaPaper(*keys)
                engine.connect()
            provider.check()
            authenticated = True
            while not self.stop_event.is_set():
                if self.args.kill_file.exists():
                    engine.halt("kill_switch")
                if remote:
                    engine.reconcile()
                frame, info = provider.frame(engine)
                if frame is not None:
                    with self.args.record.open("a") as stream:
                        stream.write(json.dumps(frame)+"\n")
                    engine.step(frame, kill=self.args.kill_file.exists())
                with self.lock:
                    self.info = dict(info, received_at=datetime.now(timezone.utc).isoformat())
                if self.stop_event.wait(self.args.interval):
                    break
        except Exception as exc:
            # A setup failure with an untouched account should be correctable.
            # A running account or carried position must retain a safety halt.
            if engine and (authenticated or engine.summary()["positions"] or engine.summary().get("pending_orders")):
                engine.halt("market_data_or_runtime_error")
                if getattr(engine, "broker", None):
                    try:
                        engine.reconcile()
                    except Exception:
                        pass  # Leave a persistent halt; never hide a pending order or invent its outcome.
            with self.lock:
                self.error = str(exc) if isinstance(exc, DataError) else "The data runner stopped unexpectedly."
                self.info = {"market": "connection_error", "quote_preview": []}
        finally:
            if engine:
                if getattr(engine, "broker", None) and self.stop_event.is_set():
                    try:
                        engine.halt("dashboard_shutdown")
                        engine.reconcile()
                    except Exception:
                        pass
                engine.close()

    def halt(self):
        with self.lock:
            engine = self.open_engine()
            try:
                engine.halt("operator_requested", datetime.now(timezone.utc).isoformat())
            finally:
                engine.close()

    def demo(self, scenario):
        if scenario not in ("mechanics", "gap", "budget"):
            raise ValueError("Unknown scenario")
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("The real quote runner is active. Keep this account visible while it runs.")
            folder = self.real_db.parent / "paper_runs"
            folder.mkdir(exist_ok=True)
            self.db = folder / ("sample_"+uuid.uuid4().hex+".sqlite3")
            self.mode = "synthetic_test"
            self.error = None
            engine = self.open_engine()
            try:
                for frame in sequence(scenario):
                    engine.step(frame)
                self.info = dict(market="synthetic", scenario=scenario, quote_preview=[],
                                 notice="Generated prices for software testing. No market performance evidence.")
            finally:
                engine.close()

    def select_real(self):
        with self.lock:
            if self.mode != "real_quotes":
                self.db, self.mode = self.real_db, "real_quotes"
                self.info = {"market": "not_connected", "quote_preview": []}
                self.error = None

    def shutdown(self):
        if self.thread and self.thread.is_alive():
            self.halt()
            self.stop_event.set()
            self.thread.join(timeout=10)


def make_server(controller, port=8765):
    page = Path(__file__).with_name("dashboard.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def trusted_host(self):
            actual_port = self.server.server_address[1]
            return self.headers.get("Host") in (f"127.0.0.1:{actual_port}", f"localhost:{actual_port}")

        def respond(self, status, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.trusted_host():
                return self.respond(403, {"error": "Local host required"})
            path = urlsplit(self.path).path
            if path == "/":
                self.respond(200, page, "text/html; charset=utf-8")
            elif path == "/api/state":
                try:
                    self.respond(200, controller.state())
                except (sqlite3.Error, ValueError, OSError):
                    self.respond(503, {"error": "Account is busy or unavailable. Retry shortly."})
            else:
                self.respond(404, {"error": "Not found"})

        def do_POST(self):
            port = self.server.server_address[1]
            origin = self.headers.get("Origin")
            if (not self.trusted_host() or self.headers.get("X-Paper-Token") != controller.csrf
                    or (origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"))):
                return self.respond(403, {"error": "Local dashboard token required"})
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "JSON required"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 2048:
                    return self.respond(413, {"error": "Request too large"})
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("JSON object required")
                path = urlsplit(self.path).path
                if path == "/api/start":
                    controller.start()
                elif path == "/api/halt":
                    controller.halt()
                elif path == "/api/demo":
                    controller.demo(body.get("scenario", "mechanics"))
                elif path == "/api/real":
                    controller.select_real()
                else:
                    return self.respond(404, {"error": "Not found"})
                self.respond(200, {"ok": True})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)})
            except (sqlite3.Error, OSError):
                self.respond(503, {"error": "Account is busy or unavailable. Retry shortly."})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(args):
    controller = Controller(args)
    server = make_server(controller, args.port)
    print(f"Paper dashboard: http://127.0.0.1:{server.server_address[1]}", flush=True)
    execution = "Alpaca paper orders" if getattr(args,"execution","local") == "alpaca_paper" else "Local simulated orders"
    print(f"Execution: {execution}. Open the URL in your browser. Real money trading is disabled.", flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        print("Stopping. Pending simulated exits require a later run with fresh quotes.", flush=True)
    finally:
        controller.shutdown()
        server.server_close()
