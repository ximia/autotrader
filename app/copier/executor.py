"""Trade executors.

PaperExecutor — simulates fills at the reference price, no network writes.
LiveExecutor  — places real orders via the CLOB client.

Both track execution latency and compute actual slippage for analytics.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Protocol

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# RESULT TYPE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FillResult:
    status: str           # filled | submitted | failed
    fill_price: float
    shares: float
    usd: float
    order_id: Optional[str] = None
    error: Optional[str] = None
    is_live: bool = False
    latency_ms: float = 0.0
    slippage_pct: float = 0.0   # (fill_price - ref_price) / ref_price


# ─────────────────────────────────────────────────────────────────────────────
# PROTOCOL
# ─────────────────────────────────────────────────────────────────────────────

class Executor(Protocol):
    is_live: bool

    def buy(self, token_id: str, usd: float, ref_price: float) -> FillResult: ...
    def sell(self, token_id: str, shares: float, ref_price: float) -> FillResult: ...
    def available_cash(self) -> float: ...
    def live_price(self, token_id: str, side: str) -> Optional[float]: ...
    def readiness(self) -> tuple[bool, str]: ...


# ─────────────────────────────────────────────────────────────────────────────
# PAPER EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

class PaperExecutor:
    """Simulates a fill at the reference price. No orders are sent."""

    is_live = False

    def buy(self, token_id: str, usd: float, ref_price: float) -> FillResult:
        t0 = time.monotonic()
        price = _safe_price(ref_price)
        shares = usd / price if price > 0 else 0.0
        latency = (time.monotonic() - t0) * 1000
        return FillResult(
            "filled", price, shares, usd,
            is_live=False, latency_ms=round(latency, 2),
        )

    def sell(self, token_id: str, shares: float, ref_price: float) -> FillResult:
        t0 = time.monotonic()
        price = _safe_price(ref_price)
        usd = shares * price
        latency = (time.monotonic() - t0) * 1000
        return FillResult(
            "filled", price, shares, usd,
            is_live=False, latency_ms=round(latency, 2),
        )

    def available_cash(self) -> float:
        from app.db import get_state, session_scope
        with session_scope() as s:
            return get_state(s).paper_cash_usd

    def live_price(self, token_id: str, side: str) -> Optional[float]:
        return None

    def readiness(self) -> tuple[bool, str]:
        return True, "paper mode"


# ─────────────────────────────────────────────────────────────────────────────
# LIVE EXECUTOR
# ─────────────────────────────────────────────────────────────────────────────

class LiveExecutor:
    """Places real orders through the CLOB client wrapper."""

    is_live = True

    def __init__(self, clob):  # app.polymarket.clob_client.ClobTrader
        self._clob = clob

    def available_cash(self) -> float:
        return self._clob.available_usdc()

    def live_price(self, token_id: str, side: str) -> Optional[float]:
        return self._clob.live_price(token_id, side)

    def readiness(self) -> tuple[bool, str]:
        return self._clob.readiness()

    def buy(self, token_id: str, usd: float, ref_price: float) -> FillResult:
        t0 = time.monotonic()
        try:
            resp = self._clob.market_buy(token_id=token_id, usd=usd)
        except Exception as exc:
            log.exception("live buy failed for %s", token_id)
            return FillResult(
                "failed", 0.0, 0.0, usd,
                error=str(exc), is_live=True,
                latency_ms=round((time.monotonic() - t0) * 1000, 2),
            )
        latency = (time.monotonic() - t0) * 1000
        fill_price = float(resp.get("price") or 0) or _safe_price(ref_price)
        shares = usd / fill_price if fill_price > 0 else 0.0
        slippage = (fill_price - ref_price) / ref_price if ref_price > 0 else 0.0
        return FillResult(
            "submitted" if resp.get("success", True) else "failed",
            fill_price, shares, usd,
            order_id=resp.get("orderID") or resp.get("orderId"),
            error=resp.get("errorMsg"),
            is_live=True,
            latency_ms=round(latency, 2),
            slippage_pct=round(slippage, 4),
        )

    def sell(self, token_id: str, shares: float, ref_price: float) -> FillResult:
        t0 = time.monotonic()
        price = _safe_price(ref_price)
        try:
            resp = self._clob.limit_sell(token_id=token_id, shares=shares, price=price)
        except Exception as exc:
            log.exception("live sell failed for %s", token_id)
            return FillResult(
                "failed", 0.0, 0.0, shares * price,
                error=str(exc), is_live=True,
                latency_ms=round((time.monotonic() - t0) * 1000, 2),
            )
        latency = (time.monotonic() - t0) * 1000
        fill_price = float(resp.get("price") or price)
        usd = shares * fill_price
        slippage = (fill_price - ref_price) / ref_price if ref_price > 0 else 0.0
        return FillResult(
            "submitted" if resp.get("success", True) else "failed",
            fill_price, shares, usd,
            order_id=resp.get("orderID") or resp.get("orderId"),
            error=resp.get("errorMsg"),
            is_live=True,
            latency_ms=round(latency, 2),
            slippage_pct=round(slippage, 4),
        )


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _safe_price(price: float) -> float:
    if price is None or price <= 0:
        return 0.5
    return min(max(float(price), 0.001), 0.999)
