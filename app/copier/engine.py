"""Copy engine — consensus-driven, risk-managed trade execution.

Architecture:
  1. Paused / paper-init / risk-baseline setup.
  2. Leaderboard refresh (rate-limited).
  3. Parallel fetch of recent fills from ALL tracked traders.
  4. Composite trader scoring (ranking module).
  5. Protective exits (TP / SL / trailing / mirror).
  6. Consensus signal detection (signal engine).
  7. Per-signal: risk checks → Kelly sizing → execution → DB writes.
  8. Update run stats.

The key design shift from naive copy-trading: trades are only executed
when multiple top traders independently enter the same market/outcome
AND the composite confidence score clears a configurable threshold.
This trades volume for quality.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import or_, select

from app.config import Settings, get_settings
from app.copier import risk as risk_mgr
from app.polymarket import geoblock as _geoblock
from app.copier.executor import Executor, FillResult, PaperExecutor
from app.copier.exits import evaluate_exits
from app.copier.kelly import kelly_size
from app.copier.market_filter import check_market, check_slippage
from app.copier.ranking import LeaderboardTrader, score_all
from app.copier.signal_engine import Signal, SignalEngine
from app.db import get_state, reset_daily_spend_if_needed, session_scope
from app.models import BotState, CopyTrade, FollowedTrader, Position, SignalEvent, Trader, TraderScoreHistory
from app.polymarket.data_client import DataClient, SourceTrade
from app.polymarket.gamma_client import GammaClient

log = logging.getLogger(__name__)

_signal_engine = SignalEngine()
_dead_tokens: set[str] = set()  # tokens with no CLOB orderbook (404) — skip these


# ─────────────────────────────────────────────────────────────────────────────
# RUN REPORT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RunReport:
    started_at: dt.datetime
    signals_evaluated: int = 0
    signals_executed: int = 0
    signals_skipped: int = 0
    trades_seen: int = 0
    skipped_reasons: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"signals={self.signals_evaluated} "
            f"executed={self.signals_executed} "
            f"skipped={self.signals_skipped}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# COPY ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CopyEngine:
    def __init__(self, *, data: DataClient, gamma: GammaClient, executor: Executor, settings: Settings | None = None):
        self.data = data
        self.gamma = gamma
        self.executor = executor
        self.settings = settings or get_settings()

    # ── MAIN LOOP ────────────────────────────────────────────────────────────

    def run_once(self) -> RunReport:
        report = RunReport(started_at=dt.datetime.now(dt.timezone.utc))
        settings = self.settings

        # ── setup / guard checks ──────────────────────────────────────────
        with session_scope() as s:
            state = get_state(s)
            reset_daily_spend_if_needed(state)

            if state.paused:
                log.info("bot is paused — skipping cycle")
                return report

            # Paper bankroll initialization.
            if not settings.live_trading and not state.paper_initialized:
                state.paper_cash_usd = settings.paper_bankroll
                state.paper_initialized = True

            # Geoblock check — halt immediately if this IP is blocked.
            if settings.live_trading:
                geo = _geoblock.check()
                if geo.blocked:
                    state.live_ready = False
                    state.live_reason = geo.reason
                    state.last_run_at = dt.datetime.now(dt.timezone.utc)
                    state.last_run_status = f"GEOBLOCKED ({geo.country}/{geo.region})"
                    state.runs_total += 1
                    log.error("halting cycle: %s", geo.reason)
                    return report

            # Update live-readiness banner.
            if settings.live_trading:
                ok, reason = self.executor.readiness()
                state.live_ready = ok
                state.live_reason = reason
                if ok:
                    state.usdc_available = self.executor.available_cash()

            # Compute current equity for risk baselines.
            # Read from the in-session state rather than opening a new session
            # (outer session hasn't committed yet, so a nested session would
            # return stale data for paper_cash_usd).
            if settings.live_trading:
                try:
                    _equity = self.executor.available_cash()
                except Exception:
                    _equity = state.paper_cash_usd
            else:
                _equity = state.paper_cash_usd
            risk_mgr.update_equity_baselines(state, _equity)

        # ── leaderboard refresh ───────────────────────────────────────────
        wallets, trader_scores = self._maybe_refresh_leaderboard(settings)

        if not wallets:
            log.warning("no tracked traders — skipping cycle")
            return report

        # ── global scan — single fast API call replaces 24 slow wallet fetches ──
        # One request gets 200 recent trades from ALL wallets on the platform.
        # Per-wallet fetches took 1-3 minutes; this takes ~2 seconds.
        now_ts = int(time.time())
        fills_by_wallet: dict[str, list[SourceTrade]] = {}
        try:
            fills_by_wallet = self.data.global_momentum_scan(
                limit=1000,
                since_ts=now_ts - int(settings.signal_window_min * 60),
            )
            report.trades_seen = sum(len(v) for v in fills_by_wallet.values())
            log.info("global scan: %d wallets / %d trades", len(fills_by_wallet), report.trades_seen)
        except Exception:
            log.warning("global scan failed — skipping cycle")
            return report

        # ── protective exits (check for sells in the global scan) ─────────
        if settings.enable_auto_exits or settings.mirror_exits:
            trader_sold = {
                f.token_id
                for fills in fills_by_wallet.values()
                for f in fills
                if f.side == "SELL"
            }
            self._process_exits(trader_sold, settings)

        # ── filter dead tokens (no CLOB orderbook) ───────────────────────
        if _dead_tokens:
            fills_by_wallet = {
                w: [f for f in fills if f.token_id not in _dead_tokens]
                for w, fills in fills_by_wallet.items()
            }

        # ── consensus signal generation ───────────────────────────────────
        # Filter to BUY fills for signal detection.
        if settings.copy_buys_only:
            buy_fills = {w: [f for f in fills if f.side == "BUY"] for w, fills in fills_by_wallet.items()}
        else:
            buy_fills = fills_by_wallet

        signals = _signal_engine.generate_signals(
            buy_fills,
            trader_scores,
            market_info_fn=lambda tid: self.gamma.market_for_token(tid, use_cache=True),
            min_consensus=settings.min_consensus_count,
            min_confidence=settings.min_signal_confidence,
        )

        report.signals_evaluated = len(signals)

        # ── per-signal: risk check → Kelly → execute ──────────────────────
        bankroll = self._bankroll()
        for sig in signals:
            executed = self._execute_signal(sig, bankroll, report, settings)
            if executed:
                bankroll = self._bankroll()  # refresh after each spend

        # ── update run stats ──────────────────────────────────────────────
        with session_scope() as s:
            state = get_state(s)
            state.last_run_at = dt.datetime.now(dt.timezone.utc)
            state.last_run_status = report.summary()
            state.runs_total += 1

        return report

    # ── LEADERBOARD / FOLLOW LIST ────────────────────────────────────────────

    def _maybe_refresh_leaderboard(self, settings: Settings) -> tuple[list[str], dict[str, float]]:
        """Return (wallet_list, trader_scores) sourced from the FollowedTrader
        table that is maintained by the slow lb_refresh scheduler job.

        Falls back to the legacy inline leaderboard fetch when the table is
        empty (e.g. first startup before the slow job has run).
        """
        wallets, scores = self._wallets_from_follow_list(settings)
        if wallets:
            return wallets, scores

        # Follow list not populated yet — use legacy path to avoid an empty
        # first cycle. The slow job will populate FollowedTrader shortly.
        log.info("follow list empty, using legacy leaderboard fetch for this cycle")
        return self._legacy_refresh_leaderboard(settings)

    def _wallets_from_follow_list(self, settings: Settings) -> tuple[list[str], dict[str, float]]:
        """Read active FollowedTrader rows and sync them into the Trader table."""
        with session_scope() as s:
            active_followed = s.scalars(
                select(FollowedTrader).where(
                    FollowedTrader.banned.is_(False),
                    or_(
                        FollowedTrader.dropped_at.is_(None),
                        FollowedTrader.pinned.is_(True),
                    ),
                ).order_by(FollowedTrader.best_rank)
            ).all()

            if not active_followed:
                return [], {}

            # Upsert into Trader table so the copy engine's cursor/scoring
            # state is maintained per wallet.
            tracked_wallets = set()
            for ft in active_followed:
                t = s.get(Trader, ft.proxy_wallet)
                if t is None:
                    t = Trader(wallet=ft.proxy_wallet, last_seen_ts=0)
                    s.add(t)
                t.username = ft.username
                t.rank = ft.best_rank
                t.pnl = ft.pnl
                t.volume = ft.vol
                t.tracked = True
                tracked_wallets.add(ft.proxy_wallet)

            # Un-track Trader rows that are no longer in the follow list.
            for t in s.scalars(select(Trader).where(Trader.tracked.is_(True))).all():
                if t.wallet not in tracked_wallets and t.wallet not in settings.allowlist:
                    t.tracked = False

            # Always keep allowlisted wallets tracked.
            for addr in settings.allowlist:
                t = s.get(Trader, addr.lower())
                if t is None:
                    t = Trader(wallet=addr.lower(), last_seen_ts=0)
                    s.add(t)
                t.tracked = True
                tracked_wallets.add(addr.lower())

            wallets = [ft.proxy_wallet for ft in active_followed] + [
                a for a in settings.allowlist if a.lower() not in {f.proxy_wallet for f in active_followed}
            ]
            # Composite scores: use pnl normalised 0–1 as a proxy until the
            # ranking module has run (it runs in the slow job, not here).
            max_pnl = max((ft.pnl for ft in active_followed), default=1.0) or 1.0
            scores = {ft.proxy_wallet: min(ft.pnl / max_pnl, 1.0) for ft in active_followed}

        log.info(
            "follow list: %d active traders (top: %s)",
            len(active_followed),
            [ft.username or ft.proxy_wallet[:10] for ft in active_followed[:3]],
        )
        return list(dict.fromkeys(wallets)), scores

    def _legacy_refresh_leaderboard(self, settings: Settings) -> tuple[list[str], dict[str, float]]:
        """Inline leaderboard fetch used only when FollowedTrader table is empty."""
        board = self.data.leaderboard(
            window=settings.leaderboard_window,
            category=settings.leaderboard_category,
            limit=settings.top_n,
        )
        if not board:
            with session_scope() as s:
                wallets = list(s.scalars(
                    select(Trader.wallet).where(Trader.tracked.is_(True)).order_by(Trader.rank)
                ))
                scores = {
                    t.wallet: t.composite_score
                    for t in s.scalars(select(Trader).where(Trader.tracked.is_(True))).all()
                }
            return wallets, scores

        fill_wallets = [lt.wallet for lt in board]
        fills_by_wallet = self.data.fetch_all_trades(fill_wallets, limit=50, since_ts=0)
        trader_scores_obj = score_all(board, fills_by_wallet)
        scores_map: dict[str, float] = {s.wallet: s.composite for s in trader_scores_obj}

        with session_scope() as s:
            state = get_state(s)
            for t in s.scalars(select(Trader).where(Trader.tracked.is_(True))).all():
                t.tracked = False
            for lt in board:
                score = scores_map.get(lt.wallet, 0.0)
                ts_obj = next((x for x in trader_scores_obj if x.wallet == lt.wallet), None)
                t = s.get(Trader, lt.wallet)
                if t is None:
                    t = Trader(wallet=lt.wallet, last_seen_ts=0)
                    s.add(t)
                t.username = lt.username
                t.rank = lt.rank
                t.pnl = lt.pnl
                t.volume = lt.volume
                t.composite_score = score
                t.tracked = True
                if ts_obj:
                    s.add(TraderScoreHistory(
                        wallet=lt.wallet,
                        composite_score=ts_obj.composite,
                        roi_estimate=ts_obj.roi_estimate,
                        win_rate_proxy=ts_obj.win_rate_proxy,
                        sharpe_proxy=ts_obj.sharpe_proxy,
                        conviction_score=ts_obj.conviction,
                        recency_score=ts_obj.recency,
                    ))
            for addr in settings.allowlist:
                t = s.get(Trader, addr.lower())
                if t is None:
                    t = Trader(wallet=addr.lower(), last_seen_ts=0)
                    s.add(t)
                t.tracked = True
            state.leaderboard_refreshed_at = dt.datetime.now(dt.timezone.utc)

        log.info("legacy leaderboard: %d traders", len(board))
        return [lt.wallet for lt in board] + settings.allowlist, scores_map

    # ── EXITS ────────────────────────────────────────────────────────────────

    def _process_exits(self, trader_sold: set[str], settings: Settings) -> None:
        with session_scope() as s:
            open_positions = list(
                s.scalars(select(Position).where(Position.closed.is_(False)))
            )

        decisions = evaluate_exits(
            open_positions,
            price_fn=self._current_price,
            take_profit_pct=settings.take_profit_pct,
            stop_loss_pct=settings.stop_loss_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            break_even_stop=settings.break_even_stop,
            enable_tp_sl=settings.enable_auto_exits,
            mirror_exits=settings.mirror_exits,
            trader_sold_tokens=trader_sold,
        )

        for d in decisions:
            # Check if market already resolved — if so, settle directly without selling.
            market = self.gamma.market_for_token(d.token_id, use_cache=False)
            if market and market.closed:
                mid = market.mid_price
                final_price = 1.0 if (mid and mid >= 0.95) else 0.0
                with session_scope() as s:
                    pos = s.get(Position, d.token_id)
                    if pos and pos.shares > 0:
                        proceeds = pos.shares * final_price
                        pnl = proceeds - pos.cost_basis_usd
                        pos.realized_pnl_usd += pnl
                        pos.cost_basis_usd = 0.0
                        pos.shares = 0.0
                        pos.closed = True
                        state = get_state(s)
                        risk_mgr.record_fill_outcome(pnl >= 0, settings, state)
                        log.info("market resolved: %s final=%.0f pnl=%.2f",
                                 d.token_id[:16], final_price, pnl)
                continue

            try:
                result = self.executor.sell(d.token_id, d.shares, d.cur_price)
            except Exception as exc:
                log.warning("sell failed for %s: %s", d.token_id[:16], exc)
                continue

            with session_scope() as s:
                pos = s.get(Position, d.token_id)
                if not pos or pos.shares <= 0:
                    continue

                ratio = min(d.shares / pos.shares, 1.0)
                proceeds = result.usd
                cost_sold = pos.cost_basis_usd * ratio
                pnl = proceeds - cost_sold

                pos.realized_pnl_usd += pnl
                pos.cost_basis_usd = max(pos.cost_basis_usd - cost_sold, 0.0)
                pos.shares = max(pos.shares - d.shares, 0.0)
                if pos.shares <= 0.001:
                    pos.shares = 0.0
                    pos.closed = True

                if not settings.live_trading:
                    get_state(s).paper_cash_usd += proceeds

                # Update risk counters.
                state = get_state(s)
                risk_mgr.record_fill_outcome(pnl >= 0, settings, state)

                # Record the exit CopyTrade.
                orig = s.scalars(
                    select(CopyTrade)
                    .where(CopyTrade.token_id == d.token_id, CopyTrade.side == "BUY")
                    .order_by(CopyTrade.created_at)
                    .limit(1)
                ).first()
                if orig:
                    uid = f"exit:{d.token_id}:{int(time.time()*1000)}"
                    s.add(CopyTrade(
                        source_trade_id=uid,
                        trader_wallet=orig.trader_wallet,
                        token_id=d.token_id,
                        condition_id=pos.condition_id,
                        market_question=pos.market_question,
                        outcome=pos.outcome,
                        side="SELL",
                        source_price=d.cur_price,
                        source_size_usd=result.usd,
                        our_usd=result.usd,
                        our_shares=d.shares,
                        fill_price=result.fill_price,
                        status=result.status,
                        skip_reason=f"auto-exit: {d.reason}",
                        slippage_pct=result.slippage_pct,
                        execution_latency_ms=result.latency_ms,
                        is_live=result.is_live,
                    ))

            log.info("exit %s %.2f @ %.3f (%s) → pnl=%.2f",
                     d.token_id[:12], d.shares, d.cur_price, d.reason,
                     result.usd - (d.shares * d.cur_price))

    # ── SIGNAL EXECUTION ─────────────────────────────────────────────────────

    def _execute_signal(
        self,
        sig: Signal,
        bankroll: float,
        report: RunReport,
        settings: Settings,
    ) -> bool:
        """Execute a single consensus signal through full risk + sizing checks."""

        # Check signal cooldown (same token within cooldown window).
        if settings.signal_cooldown_min > 0:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=settings.signal_cooldown_min)
            with session_scope() as s:
                recent = s.scalars(
                    select(SignalEvent)
                    .where(
                        SignalEvent.token_id == sig.token_id,
                        SignalEvent.side == sig.side,
                        SignalEvent.executed.is_(True),
                        SignalEvent.ts >= cutoff,
                    )
                    .limit(1)
                ).first()
            if recent:
                self._record_signal(sig, executed=False, skip_reason="signal_cooldown")
                report.signals_skipped += 1
                report.skipped_reasons.append("cooldown")
                return False

        # Blocklist check.
        if any(w in settings.blocklist for w in sig.participating_wallets):
            self._record_signal(sig, executed=False, skip_reason="blocklisted_trader")
            report.signals_skipped += 1
            return False

        # Market quality check.
        market = self.gamma.market_for_token(sig.token_id, use_cache=True)
        cur_price = self._current_price(sig.token_id) or sig.avg_price
        ok, reason = check_market(market, cur_price, settings)
        if not ok:
            self._record_signal(sig, executed=False, skip_reason=reason)
            report.signals_skipped += 1
            report.skipped_reasons.append(reason)
            return False

        # Slippage check (current price vs source traders' average fill).
        ok, reason = check_slippage(sig.avg_price, cur_price, settings.max_slippage_pct)
        if not ok:
            self._record_signal(sig, executed=False, skip_reason=reason)
            report.signals_skipped += 1
            report.skipped_reasons.append(reason)
            return False

        # Kelly sizing.
        kelly = kelly_size(
            signal_confidence=sig.confidence,
            market_price=cur_price,
            bankroll=bankroll,
            kelly_fraction=settings.kelly_fraction,
            max_kelly_bet_pct=settings.max_kelly_bet_pct,
            min_order_usd=settings.min_order_usd,
            max_per_trade_usd=settings.max_per_trade_usd,
        )
        if not kelly.accepted:
            self._record_signal(sig, executed=False, skip_reason=f"kelly:{kelly.reason}")
            report.signals_skipped += 1
            report.skipped_reasons.append("kelly")
            return False

        usd = kelly.usd

        # Apply copy_ratio scaling.
        usd = round(usd * settings.copy_ratio, 2)
        usd = max(min(usd, settings.max_per_trade_usd), settings.min_order_usd)

        # Risk management checks.
        primary_trader = sig.participating_wallets[0] if sig.participating_wallets else "unknown"
        ok, reason = risk_mgr.check_all(settings, bankroll, sig.token_id, primary_trader, usd)
        if not ok:
            self._record_signal(sig, executed=False, skip_reason=reason)
            report.signals_skipped += 1
            report.skipped_reasons.append(reason)
            return False

        # Cash clamp — never spend more than we have.
        available = self.executor.available_cash()
        usd = min(usd, available)
        if usd < settings.min_order_usd:
            self._record_signal(sig, executed=False, skip_reason="insufficient_cash")
            report.signals_skipped += 1
            report.skipped_reasons.append("no_cash")
            return False

        # ── execute ───────────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            result = self.executor.buy(sig.token_id, usd, cur_price)
        except Exception as exc:
            err = str(exc)
            if "orderbook" in err.lower() or "404" in err:
                # Cache dead token so signal engine skips it next cycle.
                _dead_tokens.add(sig.token_id)
                self._record_signal(sig, executed=False, skip_reason="no_orderbook")
            else:
                self._record_signal(sig, executed=False, skip_reason=f"exec:{err[:60]}")
            report.signals_skipped += 1
            return False
        total_latency_ms = round((time.monotonic() - t0) * 1000, 2)

        slippage = (result.fill_price - cur_price) / cur_price if cur_price > 0 else 0.0
        reasons_json = json.dumps(sig.reasons)

        if result.status in ("filled", "submitted"):
            with session_scope() as s:
                state = get_state(s)
                if not settings.live_trading:
                    state.paper_cash_usd = max(state.paper_cash_usd - result.usd, 0.0)
                state.spent_today_usd += result.usd

                # Upsert position.
                pos = s.get(Position, sig.token_id)
                if pos is None:
                    pos = Position(
                        token_id=sig.token_id,
                        condition_id=sig.condition_id,
                        market_question=sig.market_question,
                        outcome=sig.outcome,
                        shares=0.0, avg_price=0.0,
                        cost_basis_usd=0.0, realized_pnl_usd=0.0,
                        cur_price=0.0, peak_price=0.0, closed=False,
                    )
                    s.add(pos)
                old_shares = pos.shares or 0.0
                total_shares = old_shares + result.shares
                if total_shares > 0:
                    pos.avg_price = (
                        (pos.avg_price or 0.0) * old_shares
                        + result.fill_price * result.shares
                    ) / total_shares
                pos.shares = total_shares
                pos.cost_basis_usd = (pos.cost_basis_usd or 0.0) + result.usd
                pos.cur_price = result.fill_price
                pos.peak_price = max(pos.peak_price or 0.0, result.fill_price)
                pos.closed = False

                # Record one CopyTrade per participating trader (ties fills to traders).
                for wallet in sig.participating_wallets:
                    source_id = f"sig:{sig.token_id}:{wallet}:{int(time.time())}"
                    # Check if already exists (shouldn't, but be safe).
                    if s.scalars(
                        select(CopyTrade).where(CopyTrade.source_trade_id == source_id).limit(1)
                    ).first():
                        continue
                    s.add(CopyTrade(
                        source_trade_id=source_id,
                        trader_wallet=wallet,
                        token_id=sig.token_id,
                        condition_id=sig.condition_id,
                        market_question=sig.market_question,
                        outcome=sig.outcome,
                        side=sig.side,
                        source_price=sig.avg_price,
                        source_size_usd=sig.total_source_usd / max(len(sig.participating_wallets), 1),
                        our_usd=result.usd / max(len(sig.participating_wallets), 1),
                        our_shares=result.shares / max(len(sig.participating_wallets), 1),
                        fill_price=result.fill_price,
                        status=result.status,
                        order_id=result.order_id,
                        confidence_score=sig.confidence,
                        consensus_count=sig.consensus_count,
                        slippage_pct=round(slippage, 4),
                        execution_latency_ms=total_latency_ms,
                        signal_reasons=reasons_json,
                        is_live=result.is_live,
                    ))

            self._record_signal(sig, executed=True, usd=result.usd, fill_price=result.fill_price)
            report.signals_executed += 1

            log.info(
                "SIGNAL EXECUTED: %s %s consensus=%d conf=%.2f usd=%.2f @ %.3f "
                "kelly_f=%.4f edge=%.3f lat=%.1fms",
                sig.side, sig.token_id[:16], sig.consensus_count, sig.confidence,
                result.usd, result.fill_price,
                kelly.kelly_f, kelly.edge_pct, total_latency_ms,
            )
            return True
        else:
            self._record_signal(sig, executed=False, skip_reason=f"exec:{result.status} {result.error or ''}")
            report.signals_skipped += 1
            return False

    # ── HELPERS ──────────────────────────────────────────────────────────────

    def _bankroll(self) -> float:
        if self.executor.is_live:
            return float(self.executor.available_cash())
        with session_scope() as s:
            return get_state(s).paper_cash_usd

    def _current_price(self, token_id: str) -> Optional[float]:
        info = self.gamma.market_for_token(token_id, use_cache=True)
        return info.mid_price if info else None

    def _record_signal(
        self,
        sig: Signal,
        *,
        executed: bool,
        skip_reason: Optional[str] = None,
        usd: float = 0.0,
        fill_price: Optional[float] = None,
    ) -> None:
        with session_scope() as s:
            s.add(SignalEvent(
                token_id=sig.token_id,
                market_question=sig.market_question,
                outcome=sig.outcome,
                side=sig.side,
                consensus_count=sig.consensus_count,
                confidence=sig.confidence,
                participating_wallets=json.dumps(sig.participating_wallets),
                executed=executed,
                skip_reason=skip_reason,
                usd_executed=usd,
                fill_price=fill_price,
            ))


# ─────────────────────────────────────────────────────────────────────────────
# FACTORY
# ─────────────────────────────────────────────────────────────────────────────

def build_default_engine() -> CopyEngine:
    """Construct a CopyEngine using the active settings."""
    settings = get_settings()

    if settings.demo_mode:
        from app.polymarket.demo import DemoData, DemoGamma
        data = DemoData()   # type: ignore[assignment]
        gamma = DemoGamma() # type: ignore[assignment]
    else:
        data = DataClient()
        gamma = GammaClient()

    if not settings.demo_mode and settings.live_trading and settings.private_key:
        from app.polymarket.clob_client import ClobTrader
        from app.copier.executor import LiveExecutor
        executor: Executor = LiveExecutor(ClobTrader())
    else:
        executor = PaperExecutor()

    return CopyEngine(data=data, gamma=gamma, executor=executor, settings=settings)
