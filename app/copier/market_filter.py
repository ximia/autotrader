"""Market quality filters — reject trades in unfavourable market conditions.

Applied before sizing/execution. Returns (ok, reason) tuples so the caller
can record the exact skip reason.
"""
from __future__ import annotations

from typing import Optional

from app.config import Settings
from app.polymarket.gamma_client import MarketInfo


def check_market(
    market: Optional[MarketInfo],
    entry_price: float,
    settings: Settings,
) -> tuple[bool, str]:
    """Run all market-quality checks.  Returns (True, "ok") on pass.

    Checks (in order):
      1. Market is tradeable (active, not closed, accepting orders).
      2. Spread is within configured tolerance.
      3. Entry price is not above MAX_ENTRY_PRICE.
      4. Entry price is not below 0 or above 1.
    """
    if market is not None:
        if not market.tradeable:
            return False, "market_not_tradeable"

        # Spread guard.
        spread_pct = market.spread_pct
        if spread_pct is not None and spread_pct > settings.max_spread_pct:
            return False, f"spread_too_wide({spread_pct:.3f}>"  \
                          f"{settings.max_spread_pct:.3f})"

    # Entry price guards.
    if entry_price <= 0 or entry_price >= 1:
        return False, f"invalid_price({entry_price:.4f})"

    if entry_price > settings.max_entry_price:
        return False, f"price_too_high({entry_price:.3f}>{settings.max_entry_price:.3f})"

    return True, "ok"


def check_slippage(
    source_price: float,
    current_price: float,
    max_slippage_pct: float,
) -> tuple[bool, str]:
    """Reject if market price moved too far from the source trader's fill."""
    if source_price <= 0:
        return True, "ok"  # no reference price → skip check
    drift = abs(current_price - source_price) / source_price
    if drift > max_slippage_pct:
        return False, f"slippage({drift:.3f}>{max_slippage_pct:.3f})"
    return True, "ok"
