"""Position sizing — proportional to bankroll.

We mirror the *conviction* of the source trader: the fraction of their portfolio
they put into a trade, applied to our own bankroll, scaled by COPY_RATIO and
clamped to per-trade limits.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SizingResult:
    usd: float
    accepted: bool
    reason: str = "ok"


def proportional_size(
    *,
    their_trade_usd: float,
    their_portfolio_usd: float,
    my_bankroll_usd: float,
    copy_ratio: float,
    min_order_usd: float,
    max_per_trade_usd: float,
    min_trade_usd: float,
) -> SizingResult:
    """Compute our USD order size for a copied trade.

    Returns ``accepted=False`` when the trade should be skipped (too small, or
    we have no bankroll). When accepted, ``usd`` is clamped to
    ``[min_order_usd, max_per_trade_usd]``.
    """
    if their_trade_usd < min_trade_usd:
        return SizingResult(0.0, False, f"source trade ${their_trade_usd:.2f} < min ${min_trade_usd:.2f}")

    if my_bankroll_usd <= 0:
        return SizingResult(0.0, False, "bankroll is zero")

    # Fraction of their portfolio committed. Fall back to a neutral fraction
    # when we can't read their portfolio value so we still size off our caps.
    if their_portfolio_usd > 0:
        fraction = their_trade_usd / their_portfolio_usd
    else:
        fraction = max_per_trade_usd / my_bankroll_usd  # -> clamps to max below

    raw = fraction * my_bankroll_usd * copy_ratio
    clamped = max(min_order_usd, min(raw, max_per_trade_usd))

    # Never risk more than the bankroll we actually have.
    clamped = min(clamped, my_bankroll_usd)
    if clamped < min_order_usd:
        return SizingResult(0.0, False, "insufficient bankroll for min order")

    return SizingResult(round(clamped, 2), True)
