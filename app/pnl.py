"""PnL and win-rate computation, plus equity-curve snapshots.

Works off our own ledger (``Position`` + ``CopyTrade``) so it is identical in
paper and live mode. Open positions are marked to the latest known price
(refreshed from Gamma when available).
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass

from sqlalchemy import select

from app.config import get_settings
from app.db import get_state, session_scope
from app.models import CopyTrade, PnLSnapshot, Position
from app.polymarket.gamma_client import GammaClient

log = logging.getLogger(__name__)


@dataclass
class PnLSummary:
    bankroll_usd: float
    cash_usd: float
    positions_value_usd: float
    cost_basis_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    win_rate: float
    open_positions: int
    closed_positions: int


def refresh_position_prices(gamma: GammaClient | None = None) -> None:
    """Mark open positions to current market price and detect resolutions."""
    gamma = gamma or GammaClient()
    with session_scope() as session:
        open_positions = list(
            session.scalars(select(Position).where(Position.closed.is_(False)))
        )
        for pos in open_positions:
            info = gamma.market_for_token(pos.token_id, use_cache=False)
            if info is None:
                continue

            mid = _mid_price(info)
            if mid is not None:
                pos.cur_price = mid
                # Track peak price for trailing stop.
                if (pos.peak_price or 0.0) < mid:
                    pos.peak_price = mid

            # Detect market resolution — closed markets have prices near 0 or 1.
            if info.closed and mid is not None:
                final_price = 1.0 if mid >= 0.95 else 0.0
                proceeds = pos.shares * final_price
                realized = proceeds - pos.cost_basis_usd
                pos.realized_pnl_usd += realized
                pos.cost_basis_usd = 0.0
                pos.shares = 0.0
                pos.cur_price = final_price
                pos.closed = True
                log.info(
                    "market resolved: %s → price=%.0f  pnl=%.2f  (%s)",
                    pos.token_id[:16], final_price, realized,
                    pos.market_question or "",
                )


def compute_summary() -> PnLSummary:
    settings = get_settings()
    with session_scope() as session:
        state = get_state(session)
        positions = list(session.scalars(select(Position)))
        open_pos = [p for p in positions if not p.closed and p.shares > 0]
        closed_pos = [p for p in positions if p.closed or p.shares <= 0]

        positions_value = sum(p.shares * (p.cur_price or p.avg_price) for p in open_pos)
        cost_basis = sum(p.cost_basis_usd for p in open_pos)
        realized = sum(p.realized_pnl_usd for p in positions)
        unrealized = positions_value - cost_basis

        # Use the balance updated by the engine each cycle — avoids a blocking
        # network call on every dashboard refresh.
        cash = state.usdc_available if settings.live_trading else state.paper_cash_usd
        bankroll = cash + positions_value

        # Win rate over positions that have realised something (closed or trimmed).
        decided = [p for p in positions if p.realized_pnl_usd != 0.0]
        wins = sum(1 for p in decided if p.realized_pnl_usd > 0)
        win_rate = (wins / len(decided)) if decided else 0.0

        return PnLSummary(
            bankroll_usd=round(bankroll, 2),
            cash_usd=round(cash, 2),
            positions_value_usd=round(positions_value, 2),
            cost_basis_usd=round(cost_basis, 2),
            realized_pnl_usd=round(realized, 2),
            unrealized_pnl_usd=round(unrealized, 2),
            total_pnl_usd=round(realized + unrealized, 2),
            win_rate=round(win_rate, 4),
            open_positions=len(open_pos),
            closed_positions=len(closed_pos),
        )


def snapshot(summary: PnLSummary | None = None) -> PnLSummary:
    """Persist a PnL snapshot for the equity curve and return the summary."""
    summary = summary or compute_summary()
    with session_scope() as session:
        session.add(
            PnLSnapshot(
                ts=dt.datetime.now(dt.timezone.utc),
                bankroll_usd=summary.bankroll_usd,
                cash_usd=summary.cash_usd,
                positions_value_usd=summary.positions_value_usd,
                realized_pnl_usd=summary.realized_pnl_usd,
                unrealized_pnl_usd=summary.unrealized_pnl_usd,
                total_pnl_usd=summary.total_pnl_usd,
                win_rate=summary.win_rate,
            )
        )
    return summary


def _mid_price(info) -> float | None:
    if info.best_bid and info.best_ask:
        return (info.best_bid + info.best_ask) / 2
    return info.best_ask or info.best_bid
