"""Authenticated GET requests only. Real data, local simulated execution."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .models import ET, stamp


class DataError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("Unexpected redirect blocked")


def credentials(path=Path(".env")):
    """Read only these two settings; never execute a config file as shell code."""
    values = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip() in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
                values[key.strip()] = value.strip().strip("\"'")
    keys = [os.environ.get(k) or values.get(k) for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY")]
    if not all(keys) or any(str(k).startswith("YOUR_") for k in keys):
        raise DataError("Add your paper API key and secret to .env; see QUICKSTART.md")
    return keys


class AlpacaData:
    PAPER = "https://paper-api.alpaca.markets"
    DATA = "https://data.alpaca.markets"

    def __init__(self, key, secret, now=None):
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                        "Accept": "application/json"}
        self.opener = build_opener(NoRedirect())
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.cache = {}

    def get(self, path, params=None):
        allowed = {"/v2/clock", "/v2/calendar", "/v2/options/contracts", "/v2/stocks/bars",
                   "/v1beta1/options/snapshots"}
        if path not in allowed and path not in (
                "/v1beta1/options/snapshots/SPY", "/v1beta1/options/snapshots/QQQ"):
            raise DataError("Endpoint is outside the read only allowlist")
        base = self.DATA if path == "/v2/stocks/bars" or path.startswith("/v1beta1/") else self.PAPER
        url = base + path + ("?" + urlencode(params) if params else "")
        req = Request(url, headers=self.headers, method="GET")
        try:
            with self.opener.open(req, timeout=8) as response:
                return json.load(response)
        except HTTPError as exc:
            guidance = {401: "Check your paper API credentials", 403: "Check OPRA and SIP data access",
                        429: "Rate limited; wait before retrying"}.get(exc.code, "Data provider request failed")
            # Do not echo response bodies or request headers containing credentials.
            raise DataError(f"HTTP {exc.code}: {guidance}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise DataError("Data request failed or returned invalid JSON") from None

    def pages(self, path, params, field):
        seen, token, result = set(), None, []
        for _ in range(30):
            p = dict(params)
            if token:
                p["page_token"] = token
            body = self.get(path, p)
            value = body.get(field) or ([] if field == "option_contracts" else {})
            if isinstance(value, dict):
                result.append(value)
            else:
                result.extend(value)
            token = body.get("next_page_token") or body.get("page_token")
            if not token:
                return result
            if token in seen:
                raise DataError("Repeated pagination token")
            seen.add(token)
        raise DataError("Pagination exceeded safety limit")

    def session(self):
        clock = self.get("/v2/clock")
        if abs((self.now() - stamp(clock["timestamp"])).total_seconds()) > 5:
            raise DataError("Computer clock differs from broker clock by more than five seconds")
        if not clock["is_open"]:
            return None, clock
        day = stamp(clock["timestamp"]).astimezone(ET).date().isoformat()
        cache_key = ("session", day)
        if cache_key not in self.cache:
            calendar = self.get("/v2/calendar", {"start": day, "end": day})
            if len(calendar) != 1 or calendar[0]["date"] != day:
                raise DataError("No authoritative exchange session for today")
            item = calendar[0]
            self.cache[cache_key] = dict(date=day, **{
                k: datetime.fromisoformat(f"{day}T{item[k]}").replace(tzinfo=ET).isoformat()
                for k in ("open", "close")})
        return self.cache[cache_key], clock

    def bars(self, session, symbols):
        boundary = self.now().replace(second=0, microsecond=0)
        key = ("bars", session["date"], boundary.isoformat())
        if key not in self.cache:
            pages = self.pages("/v2/stocks/bars", dict(symbols=",".join(symbols), timeframe="1Min",
                start=session["open"], end=(boundary-timedelta(microseconds=1)).isoformat(),
                feed="sip", adjustment="raw", sort="asc", limit=10000), "bars")
            result = {s: [] for s in symbols}
            for page in pages:
                for symbol, rows in page.items():
                    if symbol not in result:
                        continue
                    for b in rows:
                        end = stamp(b["t"]) + timedelta(minutes=1)
                        if stamp(session["open"]) < end <= boundary:
                            result[symbol].append(dict(end=end.isoformat(), open=b["o"], high=b["h"],
                                                      low=b["l"], close=b["c"], volume=b["v"]))
            # Replace old minute cache to avoid growing indefinitely.
            self.cache = {k: v for k, v in self.cache.items() if k[0] != "bars"}
            self.cache[key] = result
        return self.cache[key]

    def catalog(self, session, cfg):
        day = datetime.fromisoformat(session["date"]).date()
        bucket = int(self.now().timestamp()) // 600
        key = ("catalog", str(day), bucket)
        if key not in self.cache:
            rows = self.pages("/v2/options/contracts", dict(underlying_symbols=",".join(cfg.symbols),
                expiration_date_gte=str(day+timedelta(days=cfg.min_dte)),
                expiration_date_lte=str(day+timedelta(days=cfg.max_dte)), status="active", limit=10000),
                "option_contracts")
            result = {}
            for c in rows:
                if c.get("tradable") is not True or c.get("root_symbol") != c.get("underlying_symbol"):
                    continue
                if c.get("size") != "100" and c.get("size") != 100:
                    continue
                # Verify OCC symbol matches its catalog expiration, type and underlying.
                match = re.fullmatch(r"(SPY|QQQ)(\d{6})([CP])(\d{8})", c["symbol"])
                if not match or match[1] != c["underlying_symbol"]:
                    continue
                expected = datetime.strptime(match[2], "%y%m%d").date().isoformat()
                if c["expiration_date"] != expected or c["type"] != ("call" if match[3] == "C" else "put"):
                    continue
                result[c["symbol"]] = c
            self.cache = {k: v for k, v in self.cache.items() if k[0] != "catalog"}
            self.cache[key] = result
        return self.cache[key]

    def frame(self, engine):
        session, clock = self.session()
        if session is None:
            return None, dict(market="closed", next_open=clock["next_open"])
        cfg = engine.cfg
        bars = self.bars(session, cfg.symbols)
        catalog = self.catalog(session, cfg)
        state = engine.store.state()
        snapshots = {}
        quotes, shortlisted = [], []
        # Do not scan for entries while an existing position needs management.
        if not state["positions"] and not state["halt"] and not state["day_halt"]:
            for symbol in cfg.symbols:
                if not bars.get(symbol):
                    continue
                underlying_price = Decimal(str(bars[symbol][-1]["close"]))
                pages = self.pages(f"/v1beta1/options/snapshots/{symbol}", dict(feed="opra", limit=1000,
                    expiration_date_gte=(stamp(session["open"])+timedelta(days=cfg.min_dte)).date().isoformat(),
                    expiration_date_lte=(stamp(session["open"])+timedelta(days=cfg.max_dte)).date().isoformat(),
                    root_symbol=symbol, strike_price_gte=str(underlying_price*Decimal("0.97")),
                    strike_price_lte=str(underlying_price*Decimal("1.03"))), "snapshots")
                for page in pages:
                    snapshots.update(page)
            for symbol, snap in snapshots.items():
                c = catalog.get(symbol)
                delta = (snap.get("greeks") or {}).get("delta")
                if c and delta is not None and cfg.min_delta <= abs(float(delta)) <= cfg.max_delta:
                    shortlisted.append(symbol)
            shortlisted.sort(key=lambda s: (abs(abs(float(snapshots[s]["greeks"]["delta"]))-.5), s))
            shortlisted = shortlisted[:40]
        # Held contracts are requested explicitly, even when they leave the DTE scan range.
        requested = sorted(set(shortlisted) | set(state["positions"]))
        if requested:
            refreshed = self.pages("/v1beta1/options/snapshots", dict(symbols=",".join(requested),
                                   feed="opra", limit=1000), "snapshots")
            snapshots = {}
            for page in refreshed:
                snapshots.update(page)
            for symbol in requested:
                snap = snapshots.get(symbol) or {}
                q, greeks = snap.get("latestQuote") or {}, snap.get("greeks") or {}
                c = catalog.get(symbol)
                held = state["positions"].get(symbol)
                if c is None and held:
                    c = dict(underlying_symbol=held["underlying"], expiration_date=held["expiration"],
                             type=held["right"], open_interest="0")
                if c is None or not q:
                    continue
                delta = greeks.get("delta")
                if delta is None and not held:
                    continue
                quotes.append(dict(symbol=symbol, underlying=c["underlying_symbol"],
                    expiration=c["expiration_date"], right=c["type"], bid=q["bp"], ask=q["ap"],
                    bid_size=q["bs"], ask_size=q["as"], at=q["t"], delta=delta if delta is not None else 0,
                    open_interest=int(c.get("open_interest") or 0), multiplier=100, feed="opra"))
        now = self.now()
        if not stamp(session["open"]) <= now <= stamp(session["close"]):
            return None, dict(market="closed", next_open=clock["next_open"])
        preview = [dict(symbol=q["symbol"], bid=q["bid"], ask=q["ask"], at=q["at"],
                        one_contract_premium=str(Decimal(str(q["ask"]))*100))
                   for q in sorted(quotes, key=lambda q: Decimal(str(q["ask"])))[:5]]
        return dict(id="alpaca-"+now.isoformat(), at=now.isoformat(), session=session,
                    bars=bars, quotes=quotes), dict(market="open", quotes=len(quotes),
                    source="OPRA quotes and SIP bars", catalog_contracts=len(catalog),
                    quote_preview=preview)

    def check(self):
        clock = self.get("/v2/clock")
        self.get("/v2/stocks/bars", dict(symbols="SPY", timeframe="1Min", feed="sip", limit=1))
        self.get("/v1beta1/options/snapshots/SPY", dict(feed="opra", limit=1))
        return dict(credentials="accepted", data_access="OPRA and SIP requests accepted",
                    market_open=clock["is_open"], next_open=clock["next_open"],
                    execution="LOCAL PAPER ONLY; no broker orders")
