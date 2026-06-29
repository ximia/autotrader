"""APScheduler wiring for the periodic copy loop.

A single background job runs ``CopyEngine.run_once()`` followed by a PnL
snapshot every ``POLL_INTERVAL_MIN`` minutes. The engine is rebuilt each tick
so config/executor changes (e.g. flipping to live) take effect without a
restart.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app import pnl
from app.config import get_settings
from app.copier.engine import build_default_engine

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
JOB_ID = "copy_loop"


def run_cycle() -> dict:
    """One full cycle: copy loop + price refresh + PnL snapshot."""
    engine = build_default_engine()
    try:
        report = engine.run_once()
        try:
            # Reuse the engine's gamma client so demo mode stays fully offline.
            pnl.refresh_position_prices(gamma=engine.gamma)
            summary = pnl.snapshot()
        except Exception:  # noqa: BLE001
            log.exception("pnl snapshot failed")
            summary = None
    finally:
        engine.data.close()
        engine.gamma.close()
    log.info("cycle complete: %s", report.summary())
    return {"report": report, "summary": summary}


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler
    settings = get_settings()
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        run_cycle,
        "interval",
        minutes=settings.poll_interval_min,
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        next_run_time=None,  # first run triggered manually after startup
    )
    _scheduler.start()
    log.info("scheduler started (every %.1f min)", settings.poll_interval_min)
    return _scheduler


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def trigger_now() -> None:
    """Schedule an immediate one-off run of the cycle."""
    import datetime as dt

    if _scheduler and _scheduler.running:
        _scheduler.add_job(
            run_cycle,
            "date",
            run_date=dt.datetime.now(dt.timezone.utc),
            id="run_now",
            replace_existing=True,
            misfire_grace_time=60,
        )


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
