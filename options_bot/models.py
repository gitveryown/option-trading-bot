from dataclasses import asdict, dataclass
from datetime import datetime, date
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
import math

D = Decimal
ET = ZoneInfo("America/New_York")


def money(value) -> Decimal:
    result = D(str(value))
    if not result.is_finite():
        raise ValueError("Money must be finite")
    return result.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def stamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamps must include a timezone")
    return result


@dataclass(frozen=True)
class Config:
    capital: Decimal = D("1000.00")
    premium_cap: Decimal = D("0.02")
    exposure_cap: Decimal = D("0.04")
    daily_loss_cap: Decimal = D("0.03")
    drawdown_cap: Decimal = D("0.10")
    fee_per_contract: Decimal = D("0.65")
    max_positions: int = 1
    max_trades: int = 3
    max_losses: int = 3
    stop: Decimal = D("0.20")
    target: Decimal = D("0.40")
    trail_activation: Decimal = D("0.20")
    trail_distance: Decimal = D("0.15")
    max_hold_minutes: int = 60
    quote_age_seconds: int = 10
    bar_age_seconds: int = 90
    max_spread: Decimal = D("0.10")
    min_open_interest: int = 100
    min_dte: int = 5
    max_dte: int = 14
    min_delta: float = 0.35
    max_delta: float = 0.65
    volume_ratio: float = 1.3
    symbols: tuple[str, ...] = ("SPY", "QQQ")
    mode: str = "local_paper"

    def __post_init__(self):
        # Fixed safety envelope for this first version.
        if self.mode != "local_paper":
            raise ValueError("Only local_paper mode exists")
        if not D("1000") <= self.capital <= D("5000"):
            raise ValueError("Capital must be between 1000 and 5000")
        if not D("0") < self.premium_cap <= D("0.02"):
            raise ValueError("Per position premium limit cannot exceed 2 percent")
        for name, maximum in (("exposure_cap", "0.04"), ("daily_loss_cap", "0.03"),
                              ("drawdown_cap", "0.10")):
            if not D("0") < getattr(self, name) <= D(maximum):
                raise ValueError(f"Invalid {name}")
        if not 1 <= self.max_positions <= 1 or not 1 <= self.max_trades <= 3:
            raise ValueError("At most one position and three entries per day")
        if not 1 <= self.max_losses <= 3:
            raise ValueError("Invalid loss streak limit")
        if not D("0") <= self.fee_per_contract <= D("10"):
            raise ValueError("Invalid simulated fee")
        for name in ("stop", "target", "trail_activation", "trail_distance", "max_spread"):
            if not D("0") < getattr(self, name) < D("1"):
                raise ValueError(f"Invalid {name}")
        if not 1 <= self.max_hold_minutes <= 60:
            raise ValueError("Holding time must be between one and sixty minutes")
        if not 1 <= self.quote_age_seconds <= 10 or not 1 <= self.bar_age_seconds <= 90:
            raise ValueError("Invalid freshness limit")
        if not 5 <= self.min_dte <= self.max_dte <= 14:
            raise ValueError("Expiration range must stay within 5 to 14 days")
        if not 0.35 <= self.min_delta <= self.max_delta <= 0.65:
            raise ValueError("Invalid delta range")
        if self.min_open_interest < 100 or not 1.3 <= self.volume_ratio <= 10:
            raise ValueError("Invalid liquidity or volume filter")
        if not self.symbols or not set(self.symbols) <= {"SPY", "QQQ"}:
            raise ValueError("Only SPY and QQQ are enabled")

    def serial(self):
        return {k: str(v) if isinstance(v, D) else v for k, v in asdict(self).items()}


@dataclass(frozen=True)
class Quote:
    symbol: str
    underlying: str
    expiration: date
    right: str
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    delta: float
    open_interest: int
    at: datetime
    multiplier: int = 100
    feed: str = "synthetic"

    @classmethod
    def parse(cls, raw):
        return cls(symbol=str(raw["symbol"]), underlying=str(raw["underlying"]),
                   expiration=date.fromisoformat(raw["expiration"]), right=raw["right"],
                   bid=money(raw["bid"]), ask=money(raw["ask"]),
                   bid_size=int(raw["bid_size"]), ask_size=int(raw["ask_size"]),
                   delta=float(raw["delta"]), open_interest=int(raw["open_interest"]),
                   at=stamp(raw["at"]), multiplier=int(raw.get("multiplier", 100)),
                   feed=raw.get("feed", "synthetic"))

    def usable(self, now, cfg):
        age = (now - self.at).total_seconds()
        return (0 <= age <= cfg.quote_age_seconds and self.bid >= 0
                and self.ask > 0 and self.bid <= self.ask and self.bid_size > 0
                and self.ask_size > 0 and self.multiplier == 100
                and self.right in ("call", "put") and math.isfinite(self.delta)
                and self.feed in ("synthetic", "opra"))


def validate_frame(frame):
    """Structural errors raise before mutation; replay runner latches a halt."""
    if not isinstance(frame["id"], str) or not frame["id"]:
        raise ValueError("Frame needs a nonempty id")
    now = stamp(frame["at"])
    session = frame["session"]
    opening, closing = stamp(session["open"]), stamp(session["close"])
    day = date.fromisoformat(session["date"])
    if (opening.astimezone(ET).date() != day or closing.astimezone(ET).date() != day
            or now.astimezone(ET).date() != day or opening >= closing):
        raise ValueError("Invalid exchange session")
    # Session times are authoritative input, including holidays and early closes.
    if not opening <= now <= closing:
        raise ValueError("Frame outside declared session")
    quotes = [Quote.parse(q) for q in frame.get("quotes", [])]
    if len({q.symbol for q in quotes}) != len(quotes):
        raise ValueError("Duplicate option symbols in frame")
    for bars in frame.get("bars", {}).values():
        previous = None
        for bar in bars:
            ending = stamp(bar["end"])
            if ending > now or ending < opening or (previous and ending <= previous):
                raise ValueError("Future, unsorted, or wrong session bars")
            values = [float(bar[k]) for k in ("open", "high", "low", "close", "volume")]
            o, h, low, c, volume = values
            if (not all(math.isfinite(v) for v in values) or min(o, h, low, c) <= 0
                    or volume < 0 or low > min(o, c) or h < max(o, c)):
                raise ValueError("Invalid OHLCV")
            previous = ending
    return now, opening, closing, day, quotes
