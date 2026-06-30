"""Fractional Kelly position sizing for binary prediction markets.

On Polymarket every outcome is a binary: you pay `price` per share and collect
$1 if the outcome resolves YES (or $0 if NO). The Kelly criterion for this
structure is:

    f* = (p_est - price) / (1 - price)

where:
  p_est  = our estimate of the true probability the outcome resolves YES
  price  = current market ask (what we pay per share)

f* is the fraction of bankroll to stake. For price > p_est (market overpriced)
f* is negative → no bet.

We then apply:
  1. A fractional multiplier (Kelly fraction, e.g. 0.25 for quarter-Kelly)
     to reduce variance and protect against model error.
  2. A hard cap expressed as a fraction of bankroll (max_kelly_bet_pct).
  3. Standard per-trade clamps (min_order_usd, max_per_trade_usd).

Edge estimation:
  We don't have a ground-truth probability model, so we estimate our
  "edge" from signal confidence:

      edge_pct = (confidence - 0.5) * edge_scale  (capped at edge_cap)

      p_est = price + edge_pct

  At minimum confidence (0.5): edge=0 → p_est=price → f*=0 → no bet.
  At high confidence (0.9): edge ≈ +4% above market price.

The intent is conservative: we only bet when our confidence is meaningfully
above 0.5, and we bet small fractions even then.
"""
from __future__ import annotations

from dataclasses import dataclass

# Confidence → estimated edge mapping.
# Higher scale means a 0.6-confidence signal (copy-trade consensus) produces
# a meaningful bet size even with a small bankroll.
_EDGE_SCALE = 0.60   # confidence 0.5→1.0 maps to edge 0→30%
_EDGE_CAP   = 0.30   # hard cap on estimated edge


@dataclass
class KellyResult:
    usd: float
    accepted: bool
    reason: str = "ok"
    kelly_f: float = 0.0      # raw full-Kelly fraction
    adjusted_f: float = 0.0   # after fractional multiplier
    edge_pct: float = 0.0


def kelly_size(
    *,
    signal_confidence: float,   # 0.5–1.0 (below 0.5 → no bet)
    market_price: float,         # current ask price (0–1)
    bankroll: float,
    kelly_fraction: float = 0.25,
    max_kelly_bet_pct: float = 0.05,
    min_order_usd: float = 1.0,
    max_per_trade_usd: float = 25.0,
) -> KellyResult:
    """Compute USD to stake based on signal confidence and current market price.

    Returns ``accepted=False`` when the computed size is below ``min_order_usd``
    or when the Kelly criterion yields a zero/negative bet (no edge).
    """
    if bankroll <= 0:
        return KellyResult(0.0, False, "bankroll is zero")

    if signal_confidence <= 0.5:
        return KellyResult(0.0, False, "confidence below threshold")

    if market_price <= 0 or market_price >= 1:
        return KellyResult(0.0, False, f"invalid market price: {market_price}")

    # Estimated edge above market price.
    edge_pct = min((signal_confidence - 0.5) * _EDGE_SCALE, _EDGE_CAP)
    p_est = min(market_price + edge_pct, 0.97)

    # Full Kelly fraction for a binary market.
    # f* = (p_est - price) / (1 - price)
    kelly_f = (p_est - market_price) / (1 - market_price)

    if kelly_f <= 0:
        return KellyResult(
            0.0, False, "no positive edge at current price",
            kelly_f=round(kelly_f, 4), edge_pct=round(edge_pct, 4),
        )

    # Apply fractional multiplier.
    adjusted_f = kelly_f * kelly_fraction

    # Convert to USD.
    usd_kelly = adjusted_f * bankroll
    usd_capped = min(usd_kelly, bankroll * max_kelly_bet_pct, max_per_trade_usd)
    usd = max(usd_capped, 0.0)

    if usd < min_order_usd:
        # Positive-edge signal but bet is below minimum — floor to min_order_usd
        # so valid signals always place at least the minimum trade.
        usd = min_order_usd

    return KellyResult(
        usd=round(usd, 2),
        accepted=True,
        kelly_f=round(kelly_f, 4),
        adjusted_f=round(adjusted_f, 4),
        edge_pct=round(edge_pct, 4),
    )
