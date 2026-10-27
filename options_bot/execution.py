from .engine import Engine
from .models import Config


def make_engine(args, path=None, cfg=None):
    cfg = cfg or Config(capital=args.capital)
    if getattr(args, "execution", "local") == "alpaca_paper":
        from .paper_engine import PaperEngine
        return PaperEngine(str(path or args.db), cfg)
    return Engine(str(path or args.db), cfg)
