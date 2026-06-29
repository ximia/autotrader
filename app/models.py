"""SQLAlchemy ORM models — the bot's local state."""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# TRADERS
# ─────────────────────────────────────────────────────────────────────────────

class Trader(Base):
    """A trader we are tracking (from the leaderboard or allowlist)."""

    __tablename__ = "traders"

    wallet: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    volume: Mapped[float] = mapped_column(Float, default=0.0)
    portfolio_value: Mapped[float] = mapped_column(Float, default=0.0)
    trade_count: Mapped[int] = mapped_column(Integer, default=0)

    # Composite quality score (updated by the ranking module).
    composite_score: Mapped[float] = mapped_column(Float, default=0.0)
    roi_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate_proxy: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe_proxy: Mapped[float] = mapped_column(Float, default=0.0)
    last_scored_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    # Cursor: only copy source trades with timestamp strictly greater than this.
    last_seen_ts: Mapped[int] = mapped_column(Integer, default=0)
    tracked: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    copy_trades: Mapped[list["CopyTrade"]] = relationship(back_populates="trader")
    score_history: Mapped[list["TraderScoreHistory"]] = relationship(back_populates="trader")


class FollowedTrader(Base):
    """Persisted follow list derived from the /v1/leaderboard consistency filter.

    This is the source-of-truth for *which* wallets the copy pipeline tracks.
    The copy engine reads active (not banned) rows from here instead of
    re-deriving the leaderboard every cycle.

    pin=True  → always follow regardless of future leaderboard results.
    banned=True → never follow regardless of leaderboard (overrides pin).
    dropped_at set → wallet no longer on the leaderboard but kept for history.
    """

    __tablename__ = "followed_traders"

    proxy_wallet: Mapped[str] = mapped_column(String, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    verified_badge: Mapped[bool] = mapped_column(Boolean, default=False)

    # Snapshot of stats at the last leaderboard refresh.
    pnl: Mapped[float] = mapped_column(Float, default=0.0)
    vol: Mapped[float] = mapped_column(Float, default=0.0)
    best_rank: Mapped[int] = mapped_column(Integer, default=0)
    # JSON list of windows this wallet appeared in, e.g. '["WEEK","MONTH"]'.
    windows_seen: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Manual overrides.
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)

    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)
    # Set when wallet drops off the leaderboard. Pinned wallets are kept active.
    dropped_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)


class TraderScoreHistory(Base):
    """Periodic snapshot of a trader's composite quality score."""

    __tablename__ = "trader_scores"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    wallet: Mapped[str] = mapped_column(ForeignKey("traders.wallet"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    composite_score: Mapped[float] = mapped_column(Float, default=0.0)
    roi_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate_proxy: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe_proxy: Mapped[float] = mapped_column(Float, default=0.0)
    conviction_score: Mapped[float] = mapped_column(Float, default=0.0)
    recency_score: Mapped[float] = mapped_column(Float, default=0.0)

    trader: Mapped["Trader"] = relationship(back_populates="score_history")


# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

class SignalEvent(Base):
    """A consensus signal detected across tracked traders in a single cycle."""

    __tablename__ = "signal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)
    token_id: Mapped[str] = mapped_column(String, index=True)
    market_question: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    side: Mapped[str] = mapped_column(String, default="BUY")

    # Consensus metadata.
    consensus_count: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    participating_wallets: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    # Execution outcome.
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    skip_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    usd_executed: Mapped[float] = mapped_column(Float, default=0.0)
    fill_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    def wallets_list(self) -> list[str]:
        if not self.participating_wallets:
            return []
        try:
            return json.loads(self.participating_wallets)
        except Exception:
            return []


# ─────────────────────────────────────────────────────────────────────────────
# TRADES & POSITIONS
# ─────────────────────────────────────────────────────────────────────────────

class CopyTrade(Base):
    """A trade we executed (or skipped) based on a detected signal."""

    __tablename__ = "copy_trades"
    __table_args__ = (
        UniqueConstraint("source_trade_id", name="uq_source_trade"),
        Index("ix_copy_trades_token_status", "token_id", "status"),
        Index("ix_copy_trades_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_trade_id: Mapped[str] = mapped_column(String, index=True)
    trader_wallet: Mapped[str] = mapped_column(ForeignKey("traders.wallet"), index=True)

    token_id: Mapped[str] = mapped_column(String, index=True)
    condition_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    market_question: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    side: Mapped[str] = mapped_column(String)  # BUY | SELL

    # Source trader's trade.
    source_price: Mapped[float] = mapped_column(Float, default=0.0)
    source_size_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Our execution.
    our_usd: Mapped[float] = mapped_column(Float, default=0.0)
    our_shares: Mapped[float] = mapped_column(Float, default=0.0)
    fill_price: Mapped[float] = mapped_column(Float, default=0.0)

    # Signal quality metadata.
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    consensus_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    slippage_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    execution_latency_ms: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    signal_reasons: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON

    # Status.
    status: Mapped[str] = mapped_column(String, default="simulated", index=True)
    skip_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    order_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, index=True)

    trader: Mapped["Trader"] = relationship(back_populates="copy_trades")

    def signal_reasons_list(self) -> list[str]:
        if not self.signal_reasons:
            return []
        try:
            return json.loads(self.signal_reasons)
        except Exception:
            return []


class Position(Base):
    """Our net position in a single outcome token (paper or live)."""

    __tablename__ = "positions"

    token_id: Mapped[str] = mapped_column(String, primary_key=True)
    condition_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    market_question: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    shares: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    cost_basis_usd: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cur_price: Mapped[float] = mapped_column(Float, default=0.0)

    # Peak price since entry (for trailing stop tracking).
    peak_price: Mapped[float] = mapped_column(Float, default=0.0)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


# ─────────────────────────────────────────────────────────────────────────────
# PnL SNAPSHOTS
# ─────────────────────────────────────────────────────────────────────────────

class PnLSnapshot(Base):
    """Periodic equity-curve point for the dashboard chart."""

    __tablename__ = "pnl_snapshots"
    __table_args__ = (Index("ix_pnl_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_utcnow)
    bankroll_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cash_usd: Mapped[float] = mapped_column(Float, default=0.0)
    positions_value_usd: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    unrealized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    total_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)


# ─────────────────────────────────────────────────────────────────────────────
# BOT STATE (singleton, id=1)
# ─────────────────────────────────────────────────────────────────────────────

class BotState(Base):
    """Singleton (id=1) holding mutable runtime state and risk counters."""

    __tablename__ = "bot_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    # Paper cash ledger (USD). Seeded from PAPER_BANKROLL on first run.
    paper_cash_usd: Mapped[float] = mapped_column(Float, default=0.0)
    paper_initialized: Mapped[bool] = mapped_column(Boolean, default=False)

    # Daily spend tracking.
    spent_today_usd: Mapped[float] = mapped_column(Float, default=0.0)
    spend_day: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # YYYY-MM-DD

    # Risk: daily / weekly equity baseline for loss-limit checks.
    daily_start_equity: Mapped[float] = mapped_column(Float, default=0.0)
    weekly_start_equity: Mapped[float] = mapped_column(Float, default=0.0)
    daily_start_day: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    weekly_start_week: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Risk: circuit breaker.
    consecutive_losses: Mapped[int] = mapped_column(Integer, default=0)
    last_loss_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    circuit_breaker_active: Mapped[bool] = mapped_column(Boolean, default=False)

    # Leaderboard refresh cadence.
    leaderboard_refreshed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    # Live pre-flight readiness (funded + approved).
    live_ready: Mapped[bool] = mapped_column(Boolean, default=True)
    live_reason: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    usdc_available: Mapped[float] = mapped_column(Float, default=0.0)

    # Slow leaderboard refresh (separate from fast copy-loop cadence).
    follow_list_refreshed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)

    # Run stats.
    last_run_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_run_status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    runs_total: Mapped[int] = mapped_column(Integer, default=0)
