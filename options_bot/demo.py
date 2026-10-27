"""Contrived fixtures for behavior testing, never historical market evidence."""
from datetime import datetime, timedelta
import copy


def bars(right="call"):
    opening = datetime.fromisoformat("2026-10-07T09:30:00-04:00")
    # Sawtooth trend with mixed gains and losses keeps RSI within the filter.
    price = 600.0
    result = []
    moves = [0.12, 0.12, -0.10] * 10
    moves[-1] = 0.30
    for i, move in enumerate(moves):
        if right == "put":
            move = -move
        o, c = price, price + move
        result.append(dict(end=(opening + timedelta(minutes=i + 1)).isoformat(),
                           open=round(o, 4), close=round(c, 4), high=round(max(o, c) + .01, 4),
                           low=round(min(o, c) - .01, 4), volume=2000 if i == 29 else 1000))
        price = c
    return result


def frame(bid="0.16", ask="0.17", right="call"):
    return dict(id="synthetic-1", at="2026-10-07T10:00:00-04:00",
                session=dict(date="2026-10-07", open="2026-10-07T09:30:00-04:00",
                             close="2026-10-07T16:00:00-04:00"), bars={"SPY": bars(right)},
                quotes=[dict(symbol="SPY261014C00601000" if right == "call" else "SPY261014P00601000",
                             underlying="SPY", expiration="2026-10-14", right=right,
                             bid=bid, ask=ask, bid_size=10, ask_size=10,
                             delta=.5 if right == "call" else -.5, open_interest=1500,
                             at="2026-10-07T10:00:00-04:00", multiplier=100, feed="synthetic")])


def sequence(scenario="mechanics"):
    first = frame()
    if scenario == "budget":
        first["quotes"][0].update(bid="4.30", ask="4.50")
    second = copy.deepcopy(first)
    second.update(id="synthetic-2", at="2026-10-07T10:01:00-04:00")
    second["bars"] = {}  # No new signals needed to manage an existing position.
    second["quotes"][0]["at"] = second["at"]
    if scenario == "mechanics":
        second["quotes"][0].update(bid="0.24", ask="0.25")
    elif scenario == "gap":
        second["quotes"][0].update(bid="0.01", ask="0.02")
    last = copy.deepcopy(second)
    last.update(id="synthetic-3", at="2026-10-07T15:45:00-04:00")
    last["quotes"][0]["at"] = last["at"]
    return [first, second, last]
