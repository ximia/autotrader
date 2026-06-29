"""Protective exit rules — when to close an open position.

Supports:
  - Take-profit: sell when unrealized gain >= take_profit_pct
  - Stop-loss:   sell when unrealized loss >= stop_loss_pct
  - Trailing stop: sell when price falls trailing_stop_pct below its peak
  - Break-even stop: once a position is up take_profit_pct/2, move stop to cost
  - Mirror exit: sell when a copied trader we follow sells the same token

Pure logic — no DB access, no network. The engine supplies current prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional


@dataclass
class ExitDecision:
    token_id: str
    shares: float
    reason: str   # take_profit | stop_loss | trailing_stop | mirror_exit | break_even
    cur_price: float


def evaluate_exits(
    positions: Iterable,
    *,
    price_fn: Callable[[str], Optional[float]],
    take_profit_pct: float,
    stop_loss_pct: float,
    trailing_stop_pct: float,
    break_even_stop: bool,
    enable_tp_sl: bool,
    mirror_exits: bool,
    trader_sold_tokens: set[str],
) -> list[ExitDecision]:
    """Return exit decisions for open positions that hit any exit rule.

    Each position yields at most one decision; priority order:
      mirror_exit > trailing_stop > stop_loss > break_even > take_profit
    """
    decisions: list[ExitDecision] = []

    for pos in positions:
        if pos.shares <= 0:
            continue

        price = price_fn(pos.token_id)
        if price is None or price <= 0:
            continue

        avg = pos.avg_price or price
        change = (price / avg) - 1.0 if avg > 0 else 0.0
        peak = pos.peak_price if pos.peak_price and pos.peak_price >= avg else avg

        reason: Optional[str] = None

        # 1. Mirror exit — follow the source trader out.
        if mirror_exits and pos.token_id in trader_sold_tokens:
            reason = "mirror_exit"

        # 2. Trailing stop — fell trailing_stop_pct below peak.
        elif trailing_stop_pct > 0 and price < peak * (1 - trailing_stop_pct):
            reason = "trailing_stop"

        # 3. Hard stop-loss.
        elif enable_tp_sl and stop_loss_pct > 0 and change <= -stop_loss_pct:
            reason = "stop_loss"

        # 4. Break-even stop — if we were profitable, protect cost.
        elif (
            break_even_stop
            and take_profit_pct > 0
            and change >= take_profit_pct / 2     # we reached half-way to TP
            and change < 0                         # now down from cost basis
        ):
            reason = "break_even"

        # 5. Take-profit.
        elif enable_tp_sl and take_profit_pct > 0 and change >= take_profit_pct:
            reason = "take_profit"

        if reason:
            decisions.append(ExitDecision(pos.token_id, pos.shares, reason, price))

    return decisions
