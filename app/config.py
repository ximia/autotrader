"""Typed application configuration — all values come from environment / .env.

Nothing here ever logs the private key. ``Settings.redacted()`` is the only
representation safe to expose in a UI or log line.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── execution mode ──────────────────────────────────────────────────────
    live_trading: bool = False
    demo_mode: bool = False
    poll_interval_min: float = Field(default=1.0, gt=0)
    leaderboard_refresh_min: float = Field(default=15.0, gt=0)

    # ── wallet (live only) ───────────────────────────────────────────────────
    private_key: str = ""
    wallet_address: str = ""
    signature_type: int = 0

    # ── trader selection ─────────────────────────────────────────────────────
    top_n: int = Field(default=10, ge=1, le=50)
    leaderboard_window: str = "MONTH"
    leaderboard_category: str = "OVERALL"
    trader_allowlist: str = ""
    trader_blocklist: str = ""

    # ── leaderboard v1 (dedicated slow-refresh job) ───────────────────────────
    # How many total ranks to pull (paginated in blocks of 50).
    leaderboard_ranks_to_pull: int = Field(default=100, ge=1, le=1000)
    # Comma-separated time windows that must ALL contain a wallet for it to pass
    # the consistency filter. E.g. "WEEK,MONTH" means the wallet must appear in
    # both the WEEK board and the MONTH board.
    leaderboard_consistency_windows: str = "WEEK,MONTH"
    # Rank wallets by profit (PNL) or volume (VOL).
    leaderboard_order_by: str = "PNL"
    # Minimum PnL (USD) a wallet must have to enter the follow list.
    leaderboard_min_pnl: float = Field(default=0.0, ge=0)
    # Minimum volume (USD) a wallet must have to enter the follow list.
    leaderboard_min_vol: float = Field(default=0.0, ge=0)
    # Only follow wallets with a verified Polymarket badge.
    leaderboard_verified_only: bool = False
    # How often (minutes) to run the slow leaderboard refresh job.
    leaderboard_slow_refresh_min: float = Field(default=60.0, gt=0)

    @property
    def consistency_windows(self) -> list[str]:
        return [w.strip().upper() for w in self.leaderboard_consistency_windows.split(",") if w.strip()]

    # ── signal engine ────────────────────────────────────────────────────────
    # Minimum confidence score (0–1) required to execute a signal.
    min_signal_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    # Minimum unique traders that must agree on a market/outcome to fire a signal.
    min_consensus_count: int = Field(default=2, ge=1)
    # Look-back window for consensus detection (minutes).
    signal_window_min: float = Field(default=30.0, gt=0)
    # Minimum minutes between firing the same signal (same token_id + BUY).
    signal_cooldown_min: float = Field(default=60.0, ge=0)

    # ── position sizing (fractional Kelly) ───────────────────────────────────
    kelly_fraction: float = Field(default=0.25, gt=0, le=10.0)
    max_kelly_bet_pct: float = Field(default=0.05, gt=0, le=1.0)
    # Maximum fraction of bankroll in any single market across all positions.
    max_exposure_per_market_pct: float = Field(default=0.15, gt=0, le=1.0)
    # Maximum fraction of bankroll in positions opened via any single tracked trader.
    max_exposure_per_trader_pct: float = Field(default=0.30, gt=0, le=1.0)
    # Override caps (hard limits, still respected alongside Kelly).
    min_order_usd: float = Field(default=1.0, gt=0)
    max_per_trade_usd: float = Field(default=25.0, gt=0)
    max_daily_spend_usd: float = Field(default=100.0, gt=0)
    max_open_positions: int = Field(default=25, ge=1)
    copy_ratio: float = Field(default=1.0, gt=0)

    # ── entry filters ────────────────────────────────────────────────────────
    max_slippage_pct: float = Field(default=0.03, ge=0)
    max_entry_price: float = Field(default=0.97, gt=0, le=1.0)
    min_trade_usd: float = Field(default=5.0, ge=0)
    copy_buys_only: bool = True

    # ── market quality filters ───────────────────────────────────────────────
    min_book_volume_usd: float = Field(default=100.0, ge=0)
    max_spread_pct: float = Field(default=0.08, ge=0)  # 8% max spread

    # ── risk management ──────────────────────────────────────────────────────
    # Halt all trading if the day's P&L drops below this fraction of start equity.
    daily_loss_limit_pct: float = Field(default=0.05, ge=0, le=1.0)
    # Halt if the week's P&L drops below this fraction of start-of-week equity.
    weekly_loss_limit_pct: float = Field(default=0.10, ge=0, le=1.0)
    # Activate circuit breaker after this many consecutive losing trades (0 = off).
    circuit_breaker_losses: int = Field(default=5, ge=0)
    # Wait this many minutes after ANY loss before the next trade (0 = off).
    cooldown_after_loss_min: float = Field(default=0.0, ge=0)

    # ── protective exits ─────────────────────────────────────────────────────
    enable_auto_exits: bool = True
    take_profit_pct: float = Field(default=0.5, ge=0)
    stop_loss_pct: float = Field(default=0.3, ge=0)
    mirror_exits: bool = True
    # Trailing stop: sell when price falls this fraction below its peak (0 = off).
    trailing_stop_pct: float = Field(default=0.0, ge=0)
    # Move stop to break-even once position is up take_profit_pct/2.
    break_even_stop: bool = False

    # ── paper mode ───────────────────────────────────────────────────────────
    paper_bankroll: float = Field(default=1000.0, gt=0)
    initial_lookback_min: float = Field(default=0.0, ge=0)

    # ── endpoints ────────────────────────────────────────────────────────────
    data_api_url: str = "https://data-api.polymarket.com"
    gamma_api_url: str = "https://gamma-api.polymarket.com"
    clob_api_url: str = "https://clob.polymarket.com"
    chain_id: int = 137
    database_url: str = "sqlite:///./autotrader.db"

    # ── derived helpers ──────────────────────────────────────────────────────

    @property
    def allowlist(self) -> list[str]:
        return _parse_addrs(self.trader_allowlist)

    @property
    def blocklist(self) -> list[str]:
        return _parse_addrs(self.trader_blocklist)

    def can_trade_live(self) -> tuple[bool, str]:
        if self.demo_mode:
            return False, "DEMO_MODE is enabled (paper only)"
        if not self.live_trading:
            return False, "LIVE_TRADING is disabled"
        if not self.private_key:
            return False, "PRIVATE_KEY is not set"
        if not self.wallet_address:
            return False, "WALLET_ADDRESS is not set"
        return True, "ok"

    def redacted(self) -> dict:
        """Safe-to-display config — no secrets."""
        return {
            "live_trading": self.live_trading,
            "demo_mode": self.demo_mode,
            "poll_interval_min": self.poll_interval_min,
            "leaderboard_refresh_min": self.leaderboard_refresh_min,
            "signature_type": self.signature_type,
            "wallet_address": self.wallet_address or None,
            "has_private_key": bool(self.private_key),
            "top_n": self.top_n,
            "leaderboard_window": self.leaderboard_window,
            "leaderboard_category": self.leaderboard_category,
            # Signal engine.
            "min_signal_confidence": self.min_signal_confidence,
            "min_consensus_count": self.min_consensus_count,
            "signal_window_min": self.signal_window_min,
            # Sizing.
            "kelly_fraction": self.kelly_fraction,
            "max_kelly_bet_pct": self.max_kelly_bet_pct,
            "min_order_usd": self.min_order_usd,
            "max_per_trade_usd": self.max_per_trade_usd,
            "max_daily_spend_usd": self.max_daily_spend_usd,
            "max_open_positions": self.max_open_positions,
            # Entry filters.
            "max_slippage_pct": self.max_slippage_pct,
            "max_entry_price": self.max_entry_price,
            "min_trade_usd": self.min_trade_usd,
            "copy_buys_only": self.copy_buys_only,
            # Risk.
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "weekly_loss_limit_pct": self.weekly_loss_limit_pct,
            "circuit_breaker_losses": self.circuit_breaker_losses,
            # Exits.
            "enable_auto_exits": self.enable_auto_exits,
            "take_profit_pct": self.take_profit_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "mirror_exits": self.mirror_exits,
            "trailing_stop_pct": self.trailing_stop_pct,
            "break_even_stop": self.break_even_stop,
            # Paper.
            "paper_bankroll": self.paper_bankroll,
        }

    @field_validator("leaderboard_window")
    @classmethod
    def _valid_window(cls, v: str) -> str:
        v = v.upper()
        allowed = {"DAY", "WEEK", "MONTH", "ALL"}
        if v not in allowed:
            raise ValueError(f"leaderboard_window must be one of {allowed}")
        return v


def _parse_addrs(raw: str) -> list[str]:
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
