"""Order transport fixed to Alpaca paper. No configurable trading host."""
import json
import re
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .alpaca_data import DataError, NoRedirect


class OrderNotFound(DataError):
    pass


class OrderRejected(DataError):
    pass


class AlpacaPaper:
    def __init__(self, key, secret):
        self.headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                        "Accept": "application/json", "Content-Type": "application/json"}
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, params=None, body=None):
        valid = ((method == "GET" and path in ("/v2/account", "/v2/positions", "/v2/orders",
                                                "/v2/orders:by_client_order_id", "/v2/clock"))
                 or (method == "POST" and path == "/v2/orders")
                 or (method == "DELETE" and re.fullmatch(r"/v2/orders/[a-zA-Z0-9-]+", path)))
        if not valid:
            raise DataError("Unsupported paper account endpoint")
        # Intentionally literal: environment variables cannot select a live account host.
        url = "https://paper-api.alpaca.markets" + path
        if params:
            url += "?" + urlencode(params)
        req = Request(url, headers=self.headers, method=method,
                      data=json.dumps(body).encode() if body is not None else None)
        try:
            with self.opener.open(req, timeout=8) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            if method == "GET" and path == "/v2/orders:by_client_order_id" and exc.code == 404:
                raise OrderNotFound("Paper order not found by client id") from None
            if method == "POST" and exc.code in (400, 403, 422):
                raise OrderRejected(f"Paper order rejected (HTTP {exc.code}); inspect the Alpaca paper account") from None
            raise DataError(f"Paper account HTTP {exc.code}; verify credentials, options permission and account status") from None
        except (URLError, OSError, ValueError):
            raise DataError("Paper account request failed; reconcile order status before any retry") from None

    def account(self):
        account = self.request("GET", "/v2/account")
        if not isinstance(account, dict):
            raise DataError("Invalid paper account response")
        if account.get("status") != "ACTIVE" or account.get("trading_blocked") or account.get("account_blocked"):
            raise DataError("Paper account is not active for trading")
        if int(account.get("options_trading_level") or 0) < 2:
            raise DataError("Paper account requires options trading level 2 for long calls and puts")
        if not account.get("id"):
            raise DataError("Paper account id is missing")
        return account

    def positions(self):
        rows = self.request("GET", "/v2/positions")
        if not isinstance(rows, list) or any(not isinstance(p, dict) or "symbol" not in p or "qty" not in p for p in rows):
            raise DataError("Invalid paper positions response")
        return rows

    def clock(self):
        clock = self.request("GET", "/v2/clock")
        if not isinstance(clock, dict) or not isinstance(clock.get("is_open"), bool) or not clock.get("timestamp"):
            raise DataError("Invalid paper market clock response")
        return clock

    def open_orders(self):
        rows = self.request("GET", "/v2/orders", {"status": "open", "limit": 500, "nested": "false"})
        if not isinstance(rows, list) or any(not isinstance(o, dict) or "client_order_id" not in o for o in rows):
            raise DataError("Invalid paper open orders response")
        if len(rows) >= 500:
            raise DataError("Too many open orders to reconcile safely")
        return rows

    def order(self, client_id):
        return self.request("GET", "/v2/orders:by_client_order_id", {"client_order_id": client_id})

    def submit(self, payload):
        allowed = {"symbol", "qty", "side", "type", "time_in_force", "limit_price",
                   "client_order_id", "position_intent"}
        qty = Decimal(payload.get("qty", "0"))
        if (set(payload) != allowed or payload.get("type") != "limit" or payload.get("time_in_force") != "day"
                or not re.fullmatch(r"(SPY|QQQ)\d{6}[CP]\d{8}", payload.get("symbol", ""))
                or not qty.is_finite() or qty < 1 or qty != qty.to_integral_value()
                or not Decimal(payload.get("limit_price", "0")).is_finite()
                or Decimal(payload.get("limit_price", "0")) <= 0
                or (payload.get("side"), payload.get("position_intent")) not in
                   (("buy", "buy_to_open"), ("sell", "sell_to_close"))):
            raise DataError("Only standard long option paper entries and closing limit orders are supported")
        return self.request("POST", "/v2/orders", body=payload)

    def cancel(self, order_id):
        return self.request("DELETE", "/v2/orders/" + order_id)
