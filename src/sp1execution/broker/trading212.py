from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sp1execution.config import Settings


class LiveTradingDisabled(RuntimeError):
    pass


class Trading212Client:
    INSTRUMENT_CACHE_TTL_SECONDS = 600
    SHORT_CACHE_TTL_SECONDS = 5

    def __init__(self, settings: Settings, timeout: float = 15.0):
        self.settings = settings
        self.timeout = timeout

    def _cache_path(self, name: str) -> Path:
        state = Path("state")
        state.mkdir(parents=True, exist_ok=True)
        return state / f"t212_{self.settings.t212_env}_{name}_cache.json"

    def _request(self, method: str, path: str, body: dict | None = None):
        credentials = f"{self.settings.api_key}:{self.settings.api_secret}".encode()
        auth = base64.b64encode(credentials).decode("ascii")
        data = None if body is None else json.dumps(body).encode()
        req = Request(
            self.settings.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/json",
                "User-Agent": "SP1Execution/0.3.3.3",
            },
        )
        try:
            with urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw.decode()) if raw else None
        except HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            retry_after = exc.headers.get("Retry-After")
            reset = exc.headers.get("x-ratelimit-reset")
            details = []
            if retry_after:
                details.append(f"retry_after={retry_after}")
            if reset:
                details.append(f"ratelimit_reset={reset}")
            suffix = (" " + " ".join(details)) if details else ""
            raise RuntimeError(f"Trading212 HTTP {exc.code}: {raw}{suffix}") from exc

    def _read_cache(self, name: str, ttl: float):
        path = self._cache_path(name)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text())
            fetched_at = float(payload["fetched_at"])
            value = payload["value"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        age = time.time() - fetched_at
        if 0 <= age <= ttl:
            return value
        return None

    def _write_cache(self, name: str, value) -> None:
        path = self._cache_path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"fetched_at": time.time(), "value": value},
                separators=(",", ":"),
            )
        )
        tmp.replace(path)

    def account_summary(self, force_refresh: bool = False):
        if not force_refresh:
            cached = self._read_cache("account", self.SHORT_CACHE_TTL_SECONDS)
            if cached is not None:
                return cached
        value = self._request("GET", "/equity/account/summary")
        self._write_cache("account", value)
        return value

    def positions(self, force_refresh: bool = False):
        if not force_refresh:
            cached = self._read_cache("positions", self.SHORT_CACHE_TTL_SECONDS)
            if cached is not None:
                return cached
        value = self._request("GET", "/equity/positions")
        self._write_cache("positions", value)
        return value

    def instruments(self, force_refresh: bool = False):
        if not force_refresh:
            cached = self._read_cache("instruments", self.INSTRUMENT_CACHE_TTL_SECONDS)
            if cached is not None:
                return cached
        instruments = self._request("GET", "/equity/metadata/instruments")
        if not isinstance(instruments, list):
            raise TypeError("Trading212 instruments endpoint returned non-list payload.")
        self._write_cache("instruments", instruments)
        return instruments

    def pending_orders(self):
        return self._request("GET", "/equity/orders")

    def historical_orders(self, limit: int = 50):
        if not 1 <= limit <= 50:
            raise ValueError("historical order limit must be 1..50")
        return self._request("GET", f"/equity/history/orders?limit={limit}")

    def order(self, order_id: str | int):
        return self._request("GET", f"/equity/orders/{order_id}")

    def market_order_demo_only(self, ticker: str, quantity: float):
        if self.settings.t212_env != "demo":
            raise LiveTradingDisabled("SP1Execution v0.3.3 refuses all live order submission.")
        if quantity == 0:
            raise ValueError("quantity cannot be zero")
        return self._request(
            "POST",
            "/equity/orders/market",
            {
                "ticker": ticker,
                "quantity": quantity,
                "extendedHours": False,
            },
        )
