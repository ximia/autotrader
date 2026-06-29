"""Risk management layer.

All checks return (allowed: bool, reason: str).
The engine consults this layer before every execution and after every fill
to update risk counters.

Risk controls:
  - Daily loss limit     — halt if equity dropped X% from day-start
  - Weekly loss limit    — halt if equity dropped X% from week-start
  - Circuit breaker      — halt after N consecutive losses
  - Cooldown             — pause N minutes after any loss
  - Per-market exposure  — cap fraction of bankroll in one market
  - Per-trader exposure  — cap fraction of bankroll per source trader
  - Max open positions   — cap total simultaneous positions
  - Max daily spend      — hard cap on USD deployed per day
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from sqlalchemy import func, select

from app.config import Settings
from app.db import get_state, session_scope
from app.models import BotState, CopyTrade, Position

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# PRE-TRADE CHECKS
# ─────────────────────────────────────────────────────────────────────────────

def check_all(
    settings: Settings,
    bankroll: float,
    token_id: str,
    trader_wallet: str,
    proposed_usd: float,
) -> tuple[bool, str]:
    """Run all pre-trade risk checks in order.  Returns first failure."""

    ok, reason = check_circuit_breaker(settings)
    if not ok:
        return False, reason

    ok, reason = check_daily_loss_limit(settings, bankroll)
    if not ok:
        return False, reason

    ok, reason = check_weekly_loss_limit(settings, bankroll)
    if not ok:
        return False, reason

    ok, reason = check_cooldown(settings)
    if not ok:
        return False, reason

    ok, reason = check_daily_spend(settings)
    if not ok:
        return False, reason

    ok, reason = check_open_positions(settings)
    if not ok:
        return False, reason

    ok, reason = check_market_exposure(token_id, proposed_usd, bankroll, settings)
    if not ok:
        return False, reason

    ok, reason = check_trader_exposure(trader_wallet, proposed_usd, bankroll, settings)
    if not ok:
        return False, reason

    return True, "ok"


def check_circuit_breaker(settings: Settings) -> tuple[bool, str]:
    if settings.circuit_breaker_losses <= 0:
        return True, "ok"
    with session_scope() as s:
        state = get_state(s)
        if state.circuit_breaker_active:
            return False, f"circuit_breaker_active(after_{settings.circuit_breaker_losses}_losses)"
    return True, "ok"


def check_daily_loss_limit(settings: Settings, current_equity: float) -> tuple[bool, str]:
    if settings.daily_loss_limit_pct <= 0:
        return True, "ok"
    with session_scope() as s:
        state = get_state(s)
        start = state.daily_start_equity
    if start <= 0:
        return True, "ok"
    loss_pct = (start - current_equity) / start
    if loss_pct >= settings.daily_loss_limit_pct:
        return (
            False,
            f"daily_loss_limit({loss_pct:.1%}>={settings.daily_loss_limit_pct:.1%})",
        )
    return True, "ok"


def check_weekly_loss_limit(settings: Settings, current_equity: float) -> tuple[bool, str]:
    if settings.weekly_loss_limit_pct <= 0:
        return True, "ok"
    with session_scope() as s:
        state = get_state(s)
        start = state.weekly_start_equity
    if start <= 0:
        return True, "ok"
    loss_pct = (start - current_equity) / start
    if loss_pct >= settings.weekly_loss_limit_pct:
        return (
            False,
            f"weekly_loss_limit({loss_pct:.1%}>={settings.weekly_loss_limit_pct:.1%})",
        )
    return True, "ok"


def check_cooldown(settings: Settings) -> tuple[bool, str]:
    if settings.cooldown_after_loss_min <= 0:
        return True, "ok"
    with session_scope() as s:
        state = get_state(s)
        last_loss = state.last_loss_at
    if last_loss is None:
        return True, "ok"
    if last_loss.tzinfo is None:
        last_loss = last_loss.replace(tzinfo=dt.timezone.utc)
    elapsed = (dt.datetime.now(dt.timezone.utc) - last_loss).total_seconds() / 60
    if elapsed < settings.cooldown_after_loss_min:
        remaining = settings.cooldown_after_loss_min - elapsed
        return False, f"cooldown({remaining:.1f}min_remaining)"
    return True, "ok"


def check_daily_spend(settings: Settings) -> tuple[bool, str]:
    with session_scope() as s:
        state = get_state(s)
        if state.spent_today_usd >= settings.max_daily_spend_usd:
            return (
                False,
                f"daily_cap(spent={state.spent_today_usd:.2f}>={settings.max_daily_spend_usd:.2f})",
            )
    return True, "ok"


def check_open_positions(settings: Settings) -> tuple[bool, str]:
    with session_scope() as s:
        count = (
            s.query(Position)
            .filter(Position.closed.is_(False), Position.shares > 0)
            .count()
        )
    if count >= settings.max_open_positions:
        return False, f"max_positions({count}>={settings.max_open_positions})"
    return True, "ok"


def check_market_exposure(
    token_id: str,
    proposed_usd: float,
    bankroll: float,
    settings: Settings,
) -> tuple[bool, str]:
    if settings.max_exposure_per_market_pct <= 0 or bankroll <= 0:
        return True, "ok"
    with session_scope() as s:
        pos = s.get(Position, token_id)
        current = pos.cost_basis_usd if pos and not pos.closed else 0.0
    total = current + proposed_usd
    pct = total / bankroll
    if pct > settings.max_exposure_per_market_pct:
        return (
            False,
            f"market_exposure({pct:.1%}>{settings.max_exposure_per_market_pct:.1%})",
        )
    return True, "ok"


def check_trader_exposure(
    trader_wallet: str,
    proposed_usd: float,
    bankroll: float,
    settings: Settings,
) -> tuple[bool, str]:
    if settings.max_exposure_per_trader_pct <= 0 or bankroll <= 0:
        return True, "ok"
    with session_scope() as s:
        # Sum cost basis across all open positions opened via this trader.
        result = s.execute(
            select(func.sum(CopyTrade.our_usd))
            .where(
                CopyTrade.trader_wallet == trader_wallet,
                CopyTrade.status.in_(["filled", "submitted"]),
                CopyTrade.side == "BUY",
            )
        ).scalar()
        current = float(result or 0)
    total = current + proposed_usd
    pct = total / bankroll
    if pct > settings.max_exposure_per_trader_pct:
        return (
            False,
            f"trader_exposure({pct:.1%}>{settings.max_exposure_per_trader_pct:.1%})",
        )
    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# POST-FILL UPDATES
# ─────────────────────────────────────────────────────────────────────────────

def record_fill_outcome(
    is_win: bool,
    settings: Settings,
    state: BotState,
) -> None:
    """Update risk counters after a position is closed.

    ``is_win`` = True if the position closed in profit.
    Must be called inside an existing session_scope (state is already loaded).
    """
    if is_win:
        state.consecutive_losses = 0
    else:
        state.consecutive_losses += 1
        state.last_loss_at = dt.datetime.now(dt.timezone.utc)
        if (
            settings.circuit_breaker_losses > 0
            and state.consecutive_losses >= settings.circuit_breaker_losses
        ):
            state.circuit_breaker_active = True
            log.warning(
                "CIRCUIT BREAKER ACTIVATED after %d consecutive losses",
                state.consecutive_losses,
            )


def update_equity_baselines(state: BotState, current_equity: float) -> None:
    """Reset daily / weekly equity baselines on period boundaries.

    Called at the start of each cycle (already inside a session_scope).
    """
    now = dt.datetime.now(dt.timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week = now.strftime("%Y-W%W")

    if state.daily_start_day != today:
        state.daily_start_day = today
        state.daily_start_equity = current_equity

    if state.weekly_start_week != week:
        state.weekly_start_week = week
        state.weekly_start_equity = current_equity
