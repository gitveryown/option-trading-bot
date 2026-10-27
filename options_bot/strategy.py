from dataclasses import dataclass
from .models import stamp


def ema(values, period):
    result = values[0]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def rsi(values, period=14):
    # Simple rolling RSI, deliberately distinct from Wilder smoothing.
    changes = [b - a for a, b in zip(values, values[1:])][-period:]
    gains = sum(max(x, 0) for x in changes)
    losses = sum(max(-x, 0) for x in changes)
    if gains == losses == 0:
        return 50.0
    return 100.0 if losses == 0 else 100 - 100 / (1 + gains / losses)


@dataclass(frozen=True)
class Signal:
    underlying: str
    right: str
    reason: str


def momentum(symbol, bars, now, opening, cfg):
    if len(bars) < 25:
        return None
    times = [stamp(b["end"]) for b in bars]
    if (now - times[-1]).total_seconds() > cfg.bar_age_seconds:
        return None
    # Require complete session history through the current completed minute.
    if (times[0] - opening).total_seconds() != 60:
        return None
    if any((b - a).total_seconds() != 60 for a, b in zip(times, times[1:])):
        return None
    closes = [float(b["close"]) for b in bars]
    fast, slow = ema(closes, 9), ema(closes, 21)
    volume = sum(float(b["volume"]) for b in bars)
    if volume == 0:
        return None
    vwap = sum((float(b["high"]) + float(b["low"]) + float(b["close"]))
               / 3 * float(b["volume"]) for b in bars) / volume
    baseline = sum(float(b["volume"]) for b in bars[-21:-1]) / 20
    relative_volume = float(bars[-1]["volume"]) / baseline if baseline > 0 else 0
    strength = rsi(closes)
    previous = bars[-6:-1]
    close = closes[-1]
    if relative_volume < cfg.volume_ratio:
        return None
    right = None
    if (close > max(float(b["high"]) for b in previous) and close > vwap
            and fast > slow and 55 <= strength <= 80):
        right = "call"
    if (close < min(float(b["low"]) for b in previous) and close < vwap
            and fast < slow and 20 <= strength <= 45):
        right = "put"
    if right:
        return Signal(symbol, right, f"5 bar breakout; EMA9/21; VWAP; "
                      f"RSI={strength:.1f}; relative volume={relative_volume:.2f}")
    return None
