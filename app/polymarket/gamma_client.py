"""Polymarket Gamma API client.

Resolves outcome token IDs into human-readable market metadata and live
pricing. Results are cached in-process since market metadata changes rarely;
price data uses a shorter TTL.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Optional

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_META_TTL = 300    # seconds — market metadata (question, outcome, active flag)
_PRICE_TTL = 15    # seconds — bid/ask prices


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MarketInfo:
    token_id: str
    condition_id: Optional[str]
    question: Optional[str]
    outcome: Optional[str]
    active: bool
    closed: bool
    accepting_orders: bool
    tick_size: float = 0.01
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    # Estimated depth at the best bid/ask levels (not always available).
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None
    # Last time price data was refreshed (epoch seconds).
    price_fetched_at: float = field(default_factory=time.time)

    @property
    def tradeable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return None

    @property
    def spread_pct(self) -> Optional[float]:
        if self.best_bid and self.best_ask and self.best_ask > 0:
            return (self.best_ask - self.best_bid) / self.best_ask
        return None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2
        return self.best_ask or self.best_bid

    @property
    def book_volume_usd(self) -> float:
        """Rough depth estimate from available size data."""
        bid_val = (self.bid_size or 0) * (self.best_bid or 0)
        ask_val = (self.ask_size or 0) * (self.best_ask or 0)
        return bid_val + ask_val


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class GammaClient:
    def __init__(self, base_url: Optional[str] = None, client: Optional[httpx.Client] = None):
        settings = get_settings()
        self.base_url = (base_url or settings.gamma_api_url).rstrip("/")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            follow_redirects=True,
        )
        self._cache: dict[str, MarketInfo] = {}
        self._lock = Lock()

    def close(self) -> None:
        self._client.close()

    def market_for_token(self, token_id: str, use_cache: bool = True) -> Optional[MarketInfo]:
        """Lookup market metadata + current pricing for an outcome token."""
        with self._lock:
            cached = self._cache.get(token_id)

        if cached and use_cache:
            # Serve stale metadata but refresh prices if TTL expired.
            age = time.time() - cached.price_fetched_at
            if age < _PRICE_TTL:
                return cached
            # Price stale — re-fetch from API (metadata stays cached).
            return self._refresh_prices(cached)

        return self._fetch(token_id)

    def _fetch(self, token_id: str) -> Optional[MarketInfo]:
        try:
            resp = self._client.get(
                f"{self.base_url}/markets",
                params={"clob_token_ids": token_id},
            )
            resp.raise_for_status()
            rows = resp.json()
        except httpx.HTTPError as exc:
            log.debug("gamma lookup failed for token %s: %s", token_id, exc)
            return None

        rows = rows if isinstance(rows, list) else rows.get("data", []) or []
        if not rows:
            return None

        info = _parse_market(rows[0], token_id)
        if info:
            with self._lock:
                self._cache[token_id] = info
        return info

    def _refresh_prices(self, info: MarketInfo) -> MarketInfo:
        """Re-fetch only pricing for an already-cached market."""
        try:
            resp = self._client.get(
                f"{self.base_url}/markets",
                params={"clob_token_ids": info.token_id},
            )
            resp.raise_for_status()
            rows = resp.json()
            rows = rows if isinstance(rows, list) else rows.get("data", []) or []
            if rows:
                new = _parse_market(rows[0], info.token_id)
                if new:
                    info.best_bid = new.best_bid
                    info.best_ask = new.best_ask
                    info.bid_size = new.bid_size
                    info.ask_size = new.ask_size
                    info.accepting_orders = new.accepting_orders
                    info.price_fetched_at = time.time()
        except Exception:
            pass
        return info

    def invalidate(self, token_id: str) -> None:
        """Remove a token from cache (e.g. after a fill)."""
        with self._lock:
            self._cache.pop(token_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# PARSING
# ─────────────────────────────────────────────────────────────────────────────

def _parse_market(row: dict, token_id: str) -> Optional[MarketInfo]:
    token_ids = _as_list(row.get("clobTokenIds"))
    outcomes = _as_list(row.get("outcomes"))

    outcome = None
    if token_id in token_ids:
        idx = token_ids.index(token_id)
        if idx < len(outcomes):
            outcome = outcomes[idx]

    # Best-effort bid/ask size parsing (Gamma API surface varies).
    bid_size = _opt_float(row.get("bestBidSize") or row.get("bidSize"))
    ask_size = _opt_float(row.get("bestAskSize") or row.get("askSize"))

    return MarketInfo(
        token_id=token_id,
        condition_id=row.get("conditionId"),
        question=row.get("question"),
        outcome=outcome,
        active=bool(row.get("active", True)),
        closed=bool(row.get("closed", False)),
        accepting_orders=bool(row.get("acceptingOrders", row.get("active", True))),
        tick_size=_to_float(row.get("orderPriceMinTickSize"), 0.01),
        best_bid=_opt_float(row.get("bestBid")),
        best_ask=_opt_float(row.get("bestAsk")),
        bid_size=bid_size,
        ask_size=ask_size,
        price_fetched_at=time.time(),
    )


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else [parsed]
        except json.JSONDecodeError:
            return [value]
    return [value]


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _opt_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
