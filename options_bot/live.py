import json
from pathlib import Path
import time

from .alpaca_data import AlpacaData, DataError, credentials
from .alpaca_paper import AlpacaPaper


def run(engine, args):
    keys = credentials(args.env_file)
    provider = AlpacaData(*keys)
    remote = getattr(args, "execution", "local") == "alpaca_paper"
    if remote:
        engine.broker = AlpacaPaper(*keys)
        print(json.dumps(engine.connect()), flush=True)
    print(json.dumps(provider.check()), flush=True)
    if args.command == "check":
        return
    count = 0
    try:
        while True:
            if args.kill_file.exists():
                engine.halt("kill_switch")
            if remote:
                engine.reconcile()
            frame, info = provider.frame(engine)
            if frame is not None:
                # Save the actual observed input for replay, before simulating fills.
                with args.record.open("a") as stream:
                    stream.write(json.dumps(frame)+"\n")
                engine.step(frame, kill=args.kill_file.exists())
            print(json.dumps(dict(data=info, account=engine.summary())), flush=True)
            count += 1
            if args.once or (args.max_cycles and count >= args.max_cycles):
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        engine.halt("operator_stopped")
        if remote:
            try:
                engine.reconcile()
            except DataError:
                print("Could not confirm pending order cancellation. Inspect the Alpaca paper dashboard.", flush=True)
        print("Stopped. Open simulated positions need fresh quotes to close; resume the same database.", flush=True)
    except Exception:
        engine.halt("market_data_or_runtime_error")
        if remote:
            try:
                engine.reconcile()
            except Exception:
                pass
        raise
