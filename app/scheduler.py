"""APScheduler wiring for two jobs:

  copy_loop  (fast)  — CopyEngine.run_once() + PnL snapshot, every POLL_INTERVAL_MIN.
  lb_refresh (slow)  — Leaderboard consistency filter + FollowedTrader upsert,
                       every LEADERBOARD_SLOW_REFRESH_MIN (default 60 min).

The two jobs run independently so the leaderboard re-ranking never blocks trades.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select, or_

from app import pnl
from app.config import get_settings
from app.copier.engine import build_default_engine
from app.db import get_state, session_scope
from app.models import BotState, FollowedTrader

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
JOB_COPY   = "copy_loop"
JOB_LB     = "lb_refresh"


# ─────────────────────────────────────────────────────────────────────────────
# FAST JOB — copy loop
# ─────────────────────────────────────────────────────────────────────────────

def run_cycle() -> dict:
    """One full cycle: copy loop + price refresh + PnL snapshot."""
    engine = build_default_engine()
    try:
        report = engine.run_once()
        try:
            pnl.refresh_position_prices(gamma=engine.gamma)
            summary = pnl.snapshot()
        except Exception:
            log.exception("pnl snapshot failed")
            summary = None
    finally:
        engine.data.close()
        engine.gamma.close()
    log.info("cycle complete: %s", report.summary())
    return {"report": report, "summary": summary}


# ─────────────────────────────────────────────────────────────────────────────
# SLOW JOB — leaderboard refresh + FollowedTrader persist
# ─────────────────────────────────────────────────────────────────────────────

def refresh_follow_list() -> None:
    """Pull the /v1/leaderboard, apply the consistency filter, and persist the
    follow list to the ``followed_traders`` table.

    Wallets that fall off the leaderboard are marked with ``dropped_at``.
    Pinned wallets are kept active regardless. Banned wallets are never removed.
    """
    settings = get_settings()
    from app.polymarket.leaderboard_client import LeaderboardClient

    log.info(
        "leaderboard refresh starting (windows=%s, ranks=%d, order=%s)",
        settings.consistency_windows,
        settings.leaderboard_ranks_to_pull,
        settings.leaderboard_order_by,
    )

    client = LeaderboardClient()
    try:
        candidates = client.build_follow_list(
            windows=settings.consistency_windows,
            order_by=settings.leaderboard_order_by,
            category=settings.leaderboard_category,
            max_ranks=settings.leaderboard_ranks_to_pull,
            min_pnl=settings.leaderboard_min_pnl,
            min_vol=settings.leaderboard_min_vol,
            verified_only=settings.leaderboard_verified_only,
        )
    except Exception:
        log.exception("leaderboard fetch failed — follow list unchanged")
        return
    finally:
        client.close()

    now = dt.datetime.now(dt.timezone.utc)
    fresh_wallets = {c.proxy_wallet for c in candidates}

    with session_scope() as s:
        existing: dict[str, FollowedTrader] = {
            row.proxy_wallet: row
            for row in s.scalars(select(FollowedTrader)).all()
        }

        # Upsert fresh candidates.
        for c in candidates:
            row = existing.get(c.proxy_wallet)
            if row is None:
                row = FollowedTrader(
                    proxy_wallet=c.proxy_wallet,
                    first_seen_at=now,
                )
                s.add(row)
            row.username      = c.username
            row.verified_badge = c.verified_badge
            row.pnl           = c.pnl
            row.vol           = c.vol
            row.best_rank     = c.best_rank
            row.windows_seen  = json.dumps(c.windows_seen)
            row.last_seen_at  = now
            row.dropped_at    = None   # back on the board

        # Mark wallets that dropped off (unless pinned).
        for wallet, row in existing.items():
            if wallet not in fresh_wallets and not row.pinned and row.dropped_at is None:
                row.dropped_at = now
                log.info("trader dropped from leaderboard: %s", wallet)

        # Record refresh timestamp.
        state = get_state(s)
        state.follow_list_refreshed_at = now

    log.info(
        "follow list refreshed: %d active, %d dropped",
        len(fresh_wallets),
        sum(1 for r in existing.values() if r.proxy_wallet not in fresh_wallets and not r.pinned),
    )


# ─────────────────────────────────────────────────────────────────────────────
# SCHEDULER LIFECYCLE
# ─────────────────────────────────────────────────────────────────────────────

def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone="UTC")

    # Fast copy loop.
    _scheduler.add_job(
        run_cycle,
        "interval",
        minutes=settings.poll_interval_min,
        id=JOB_COPY,
        max_instances=1,
        coalesce=True,
        next_run_time=None,
    )

    # Slow leaderboard refresh.
    _scheduler.add_job(
        refresh_follow_list,
        "interval",
        minutes=settings.leaderboard_slow_refresh_min,
        id=JOB_LB,
        max_instances=1,
        coalesce=True,
        next_run_time=None,  # first run triggered manually after startup
    )

    _scheduler.start()
    log.info(
        "scheduler started (copy=%.1fmin, lb_refresh=%.0fmin)",
        settings.poll_interval_min,
        settings.leaderboard_slow_refresh_min,
    )
    return _scheduler


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def trigger_now() -> None:
    """Trigger both jobs immediately on startup."""
    if not (_scheduler and _scheduler.running):
        return
    now = dt.datetime.now(dt.timezone.utc)

    _scheduler.add_job(
        run_cycle,
        "date",
        run_date=now,
        id="run_now",
        replace_existing=True,
        misfire_grace_time=60,
    )
    # Run leaderboard refresh a few seconds after startup so the follow list
    # is populated before the first copy cycle completes.
    _scheduler.add_job(
        refresh_follow_list,
        "date",
        run_date=now + dt.timedelta(seconds=3),
        id="lb_now",
        replace_existing=True,
        misfire_grace_time=60,
    )


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
