import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

from .demo import sequence
from .models import money
from .alpaca_data import DataError
from .execution import make_engine


def main(argv=None):
    parser = argparse.ArgumentParser(description="Options bot with local simulation or Alpaca paper execution; real money execution absent")
    parser.add_argument("command", choices=["demo", "replay", "status", "halt", "check", "live", "dashboard"])
    parser.add_argument("--capital", type=money, default=Decimal("1000.00"))
    parser.add_argument("--db")
    parser.add_argument("--execution", choices=["local", "alpaca_paper"], help="Dashboard and live default to Alpaca paper")
    parser.add_argument("--input", type=Path, help="JSONL snapshots for replay")
    parser.add_argument("--scenario", choices=["mechanics", "budget", "gap"], default="mechanics")
    parser.add_argument("--target", type=money, default=Decimal("2000.00"), help="Reporting goal only; does not change risk")
    parser.add_argument("--kill-file", type=Path, default=Path("STOP_TRADING"))
    parser.add_argument("--report", type=Path, help="Write result and ledger as JSON")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--record", type=Path, default=Path("observed_quotes.jsonl"))
    parser.add_argument("--interval", type=int, default=5, help="Polling interval in seconds, 5 to 60")
    parser.add_argument("--once", action="store_true", help="Fetch and process one live cycle")
    parser.add_argument("--max-cycles", type=int, default=0, help="Zero runs until interrupted")
    parser.add_argument("--port", type=int, default=8765, help="Local dashboard port")
    args = parser.parse_args(argv)
    args.execution = args.execution or ("alpaca_paper" if args.command in ("dashboard", "live", "check") else "local")
    args.db = args.db or ("alpaca_paper.sqlite3" if args.execution == "alpaca_paper" else "live_paper.sqlite3" if args.command == "dashboard" else "paper.sqlite3")
    if args.command in ("demo", "replay") and args.execution != "local":
        parser.error("Demo and replay are local only; historical input cannot submit brokerage orders")
    if args.command == "replay" and args.input is None:
        parser.error("replay requires --input")
    if args.target <= 0:
        parser.error("target must be positive")
    if not 5 <= args.interval <= 60 or args.max_cycles < 0:
        parser.error("Invalid polling interval or cycle count")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid dashboard port")
    if args.command == "dashboard":
        from .dashboard import serve
        try:
            serve(args)
            return 0
        except (ValueError, OSError) as exc:
            print(f"Cannot start dashboard: {exc}", file=sys.stderr)
            return 1
    engine = None
    try:
        engine = make_engine(args)
        if args.command in ("live", "check"):
            from .live import run
            run(engine, args)
        elif args.command == "halt":
            engine.halt("manual_kill_switch")
        elif args.command in ("demo", "replay"):
            frames = sequence(args.scenario) if args.command == "demo" else read_frames(args.input)
            for frame in frames:
                engine.step(frame, kill=args.kill_file.exists())
        summary = engine.summary()
        report = dict(summary=summary, starting_capital=str(args.capital), target_equity=str(args.target),
                      target_reached=Decimal(summary["equity"]) >= args.target,
                      net_change=str(money(Decimal(summary["equity"]) - args.capital)),
                      events=engine.store.events(),
                      data_notice="SYNTHETIC MECHANICS TEST; no evidence of market profitability"
                      if args.command == "demo" else "Actual OPRA quotes requested; fills remain locally simulated"
                      if args.command in ("live", "check") and args.execution == "local"
                      else "Alpaca paper execution; broker confirmed fills, strategy budget and modeled fees"
                      if args.execution == "alpaca_paper" else "Input data supplied by operator; provenance not verified")
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0
    except (DataError, ValueError, KeyError, TypeError, OSError, ArithmeticError) as exc:
        if engine:
            state = engine.summary()
            untouched_remote = (args.execution == "alpaca_paper" and not state["positions"]
                and not state.get("pending_orders") and not state.get("broker_orders")
                and not engine.store.state()["last_at"])
            if not untouched_remote:
                engine.halt("input_or_runtime_error")
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        if engine:
            engine.close()


def read_frames(path):
    with path.open() as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)
