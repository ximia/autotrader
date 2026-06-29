"""FastAPI application: live dashboard + JSON API + control endpoints."""
from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from app import pnl
from app.config import get_settings
from app.db import get_state, init_db, session_scope
from app.models import (
    BotState, CopyTrade, PnLSnapshot, Position, SignalEvent, Trader,
)
from app.scheduler import get_scheduler, shutdown_scheduler, start_scheduler, trigger_now

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    start_scheduler()
    trigger_now()
    yield
    shutdown_scheduler()


app = FastAPI(title="Polymarket Copy-Trader", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "dashboard.html")


# ─────────────────────────────────────────────────────────────────────────────
# STATUS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status() -> dict:
    settings = get_settings()
    sched = get_scheduler()
    next_run = None
    if sched:
        job = sched.get_job("copy_loop")
        if job and job.next_run_time:
            next_run = job.next_run_time.isoformat()

    with session_scope() as s:
        state = get_state(s)
        return {
            "live_trading": settings.live_trading,
            "demo_mode": settings.demo_mode,
            "paused": state.paused,
            "circuit_breaker_active": state.circuit_breaker_active,
            "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
            "last_run_status": state.last_run_status,
            "runs_total": state.runs_total,
            "spent_today_usd": round(state.spent_today_usd, 2),
            "max_daily_spend_usd": settings.max_daily_spend_usd,
            "paper_cash_usd": round(state.paper_cash_usd, 2),
            "live_ready": state.live_ready,
            "live_reason": state.live_reason,
            "usdc_available": round(state.usdc_available, 2),
            "wallet_address": settings.wallet_address or None,
            "next_run_at": next_run,
            "poll_interval_min": settings.poll_interval_min,
            "consecutive_losses": state.consecutive_losses,
            "config": settings.redacted(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# TRADERS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/traders")
def api_traders() -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(
            select(Trader).where(Trader.tracked.is_(True)).order_by(Trader.rank)
        ).all()
        out = []
        for t in rows:
            copied = s.query(CopyTrade).filter(
                CopyTrade.trader_wallet == t.wallet,
                CopyTrade.status.in_(["filled", "submitted"]),
            ).count()
            out.append({
                "wallet": t.wallet,
                "username": t.username,
                "rank": t.rank,
                "pnl": round(t.pnl, 2),
                "volume": round(t.volume, 2),
                "composite_score": round(t.composite_score, 3),
                "roi_estimate": round(t.roi_estimate, 4),
                "win_rate_proxy": round(t.win_rate_proxy, 3),
                "sharpe_proxy": round(t.sharpe_proxy, 3),
                "trade_count": t.trade_count,
                "copied_trades": copied,
            })
        return out


# ─────────────────────────────────────────────────────────────────────────────
# TRADES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/trades")
def api_trades(limit: int = 50) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(
            select(CopyTrade).order_by(desc(CopyTrade.created_at)).limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "time": r.created_at.isoformat(),
                "trader": r.trader_wallet,
                "market": r.market_question,
                "outcome": r.outcome,
                "side": r.side,
                "source_size_usd": round(r.source_size_usd, 2),
                "our_usd": round(r.our_usd, 2),
                "fill_price": r.fill_price,
                "status": r.status,
                "skip_reason": r.skip_reason,
                "is_live": r.is_live,
                "confidence_score": round(r.confidence_score, 3) if r.confidence_score else None,
                "consensus_count": r.consensus_count,
                "slippage_pct": round(r.slippage_pct, 4) if r.slippage_pct else None,
                "execution_latency_ms": round(r.execution_latency_ms, 1) if r.execution_latency_ms else None,
                "signal_reasons": r.signal_reasons_list(),
            }
            for r in rows
        ]


# ─────────────────────────────────────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/positions")
def api_positions() -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(
            select(Position)
            .where(Position.closed.is_(False))
            .order_by(desc(Position.updated_at))
        ).all()
        out = []
        for p in rows:
            cur = p.cur_price or p.avg_price
            value = p.shares * cur
            unreal = value - p.cost_basis_usd
            out.append({
                "market": p.market_question,
                "outcome": p.outcome,
                "token_id": p.token_id,
                "shares": round(p.shares, 2),
                "avg_price": round(p.avg_price, 4),
                "cur_price": round(cur, 4),
                "peak_price": round(p.peak_price or p.avg_price, 4),
                "value_usd": round(value, 2),
                "cost_usd": round(p.cost_basis_usd, 2),
                "unrealized_pnl_usd": round(unreal, 2),
                "unrealized_pnl_pct": round(unreal / p.cost_basis_usd, 4) if p.cost_basis_usd else 0.0,
            })
        return out


# ─────────────────────────────────────────────────────────────────────────────
# PnL + EQUITY CURVE
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/pnl")
def api_pnl() -> dict:
    summary = pnl.compute_summary()
    with session_scope() as s:
        snaps = s.scalars(
            select(PnLSnapshot).order_by(desc(PnLSnapshot.ts)).limit(500)
        ).all()
        curve = [
            {
                "ts": snap.ts.isoformat(),
                "total_pnl_usd": snap.total_pnl_usd,
                "bankroll_usd": snap.bankroll_usd,
                "unrealized_pnl_usd": snap.unrealized_pnl_usd,
                "realized_pnl_usd": snap.realized_pnl_usd,
            }
            for snap in reversed(snaps)
        ]
    return {"summary": summary.__dict__, "curve": curve}


# ─────────────────────────────────────────────────────────────────────────────
# SIGNALS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/signals")
def api_signals(limit: int = 50) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(
            select(SignalEvent).order_by(desc(SignalEvent.ts)).limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "token_id": r.token_id,
                "market": r.market_question,
                "outcome": r.outcome,
                "side": r.side,
                "consensus_count": r.consensus_count,
                "confidence": round(r.confidence, 3),
                "participating_wallets": r.wallets_list(),
                "executed": r.executed,
                "skip_reason": r.skip_reason,
                "usd_executed": round(r.usd_executed, 2),
                "fill_price": round(r.fill_price, 4) if r.fill_price else None,
            }
            for r in rows
        ]


# ─────────────────────────────────────────────────────────────────────────────
# RISK STATE
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/risk")
def api_risk() -> dict:
    settings = get_settings()
    with session_scope() as s:
        state = get_state(s)
        return {
            "circuit_breaker_active": state.circuit_breaker_active,
            "consecutive_losses": state.consecutive_losses,
            "daily_start_equity": round(state.daily_start_equity, 2),
            "weekly_start_equity": round(state.weekly_start_equity, 2),
            "daily_loss_limit_pct": settings.daily_loss_limit_pct,
            "weekly_loss_limit_pct": settings.weekly_loss_limit_pct,
            "circuit_breaker_threshold": settings.circuit_breaker_losses,
            "cooldown_after_loss_min": settings.cooldown_after_loss_min,
        }


# ─────────────────────────────────────────────────────────────────────────────
# METRICS
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/metrics")
def api_metrics() -> dict:
    """Detailed execution quality metrics for the last 100 filled trades."""
    with session_scope() as s:
        filled = s.scalars(
            select(CopyTrade)
            .where(CopyTrade.status.in_(["filled", "submitted"]), CopyTrade.side == "BUY")
            .order_by(desc(CopyTrade.created_at))
            .limit(100)
        ).all()

        if not filled:
            return {"count": 0}

        latencies = [r.execution_latency_ms for r in filled if r.execution_latency_ms]
        slippages = [r.slippage_pct for r in filled if r.slippage_pct is not None]
        confidences = [r.confidence_score for r in filled if r.confidence_score]
        consensuses = [r.consensus_count for r in filled if r.consensus_count]

        def _stats(vals: list[float]) -> dict:
            if not vals:
                return {}
            vals_s = sorted(vals)
            n = len(vals_s)
            return {
                "mean": round(sum(vals_s) / n, 4),
                "median": round(vals_s[n // 2], 4),
                "p90": round(vals_s[int(n * 0.9)], 4),
                "min": round(vals_s[0], 4),
                "max": round(vals_s[-1], 4),
            }

        return {
            "count": len(filled),
            "latency_ms": _stats(latencies),
            "slippage_pct": _stats(slippages),
            "confidence": _stats(confidences),
            "consensus_count": _stats([float(c) for c in consensuses]),
        }


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def api_health() -> dict:
    sched = get_scheduler()
    with session_scope() as s:
        state = get_state(s)
        last_run = state.last_run_at
    last_run_utc = None
    if last_run:
        last_run_utc = (
            last_run.replace(tzinfo=dt.timezone.utc) if last_run.tzinfo is None else last_run
        )
    age_s = (dt.datetime.now(dt.timezone.utc) - last_run_utc).total_seconds() if last_run_utc else None
    return {
        "scheduler_running": bool(sched and sched.running),
        "last_run_age_s": round(age_s, 1) if age_s is not None else None,
        "db": "ok",
    }


# ─────────────────────────────────────────────────────────────────────────────
# CONTROLS
# ─────────────────────────────────────────────────────────────────────────────

class ControlRequest(BaseModel):
    action: str   # pause | resume | run_now | reset_circuit_breaker
    confirm: str | None = None
    value: bool | None = None


@app.post("/api/control")
def api_control(req: ControlRequest) -> dict:
    settings = get_settings()
    if req.action == "pause":
        with session_scope() as s:
            get_state(s).paused = True
        return {"ok": True, "paused": True}

    if req.action == "resume":
        with session_scope() as s:
            get_state(s).paused = False
        return {"ok": True, "paused": False}

    if req.action == "run_now":
        trigger_now()
        return {"ok": True, "triggered": True}

    if req.action == "reset_circuit_breaker":
        with session_scope() as s:
            state = get_state(s)
            state.circuit_breaker_active = False
            state.consecutive_losses = 0
        return {"ok": True, "circuit_breaker_active": False}

    if req.action == "set_live":
        if req.value and req.confirm != "GO LIVE":
            raise HTTPException(400, "type 'GO LIVE' to confirm")
        ok, reason = settings.can_trade_live()
        return {
            "ok": True,
            "live_trading": settings.live_trading,
            "live_ready": ok,
            "reason": reason,
            "note": "Live trading is controlled by LIVE_TRADING in .env, not the UI.",
        }

    raise HTTPException(400, f"unknown action: {req.action}")
