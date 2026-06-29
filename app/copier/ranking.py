"""Composite trader quality scoring.

Converts raw trade history + leaderboard data into a single composite score
that captures: profitability, conviction, consistency, recency, and activity.
Used by the signal engine to weight consensus signals and by the engine to
decide which traders deserve leaderboard slots.

All math operates on the SourceTrade list the data client already fetches,
so there are no additional API calls in steady state.
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import statistics
import time
from dataclasses import dataclass
from typing import Optional

from app.polymarket.data_client import LeaderboardTrader, SourceTrade

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TraderScore:
    wallet: str
    composite: float      # 0–1 overall quality score
    roi_estimate: float   # rough PnL / volume proxy (can be negative)
    win_rate_proxy: float # fraction of bets at "high conviction" prices
    sharpe_proxy: float   # edge-mean / edge-std (consistency)
    conviction: float     # mean |price - 0.5| * 2 — how decisive their bets are
    recency: float        # exponential decay from last trade (0–1)
    activity: int         # number of trades analysed
    volume: float         # total USD volume


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────

def score_trader(
    lt: LeaderboardTrader,
    recent_fills: list[SourceTrade],
    *,
    window_days: int = 30,
    min_trades: int = 3,
) -> TraderScore:
    """Compute a composite quality score for a single trader.

    ``lt`` provides the leaderboard-level PnL / volume.
    ``recent_fills`` are the trader's trades from the data API (used for
    conviction, consistency, and recency calculations).
    """
    now = time.time()
    cutoff = now - window_days * 86400

    # Only use fills within the scoring window.
    fills = [f for f in recent_fills if f.timestamp >= cutoff]
    activity = len(fills)

    if activity < min_trades:
        return TraderScore(
            wallet=lt.wallet,
            composite=0.0,
            roi_estimate=0.0,
            win_rate_proxy=0.0,
            sharpe_proxy=0.0,
            conviction=0.0,
            recency=0.0,
            activity=activity,
            volume=lt.volume,
        )

    # ── conviction: how far from 0.5 are their bets? ──────────────────────
    edges = [abs(f.price - 0.5) for f in fills]
    avg_edge = statistics.mean(edges)
    conviction = min(avg_edge * 2.0, 1.0)  # scale to 0–1

    # ── consistency (Sharpe-like): mean edge / std edge ───────────────────
    if len(edges) >= 2:
        std_edge = statistics.stdev(edges)
        sharpe_proxy = avg_edge / (std_edge + 1e-6)
    else:
        sharpe_proxy = 0.0
    sharpe_score = _sigmoid(sharpe_proxy, midpoint=2.0, scale=1.5)

    # ── ROI estimate from leaderboard data ────────────────────────────────
    if lt.volume > 0:
        roi = lt.pnl / lt.volume
    else:
        roi = 0.0
    roi_score = _sigmoid(roi, midpoint=0.05, scale=20.0)

    # ── win rate proxy: fraction of bets at "high conviction" (|p-0.5|>0.15) ──
    high_conv = sum(1 for e in edges if e > 0.15)
    win_rate_proxy = high_conv / activity

    # ── recency: most recent trade timestamp ─────────────────────────────
    if fills:
        last_ts = max(f.timestamp for f in fills)
        hours_ago = (now - last_ts) / 3600
        recency = math.exp(-hours_ago / 48)  # half-life ~48h
    else:
        recency = 0.0

    # ── volume score (more data = more reliable) ──────────────────────────
    volume_score = _sigmoid(math.log1p(lt.volume), midpoint=8.0, scale=1.5)

    # ── composite weighted score ──────────────────────────────────────────
    composite = (
        roi_score      * 0.30 +
        sharpe_score   * 0.20 +
        conviction     * 0.20 +
        recency        * 0.15 +
        win_rate_proxy * 0.10 +
        volume_score   * 0.05
    )

    return TraderScore(
        wallet=lt.wallet,
        composite=round(composite, 4),
        roi_estimate=round(roi, 4),
        win_rate_proxy=round(win_rate_proxy, 4),
        sharpe_proxy=round(sharpe_proxy, 4),
        conviction=round(conviction, 4),
        recency=round(recency, 4),
        activity=activity,
        volume=lt.volume,
    )


def score_all(
    leaderboard: list[LeaderboardTrader],
    fills_by_wallet: dict[str, list[SourceTrade]],
    window_days: int = 30,
    min_trades: int = 3,
) -> list[TraderScore]:
    """Score every trader and return sorted by composite (descending)."""
    scores = []
    for lt in leaderboard:
        fills = fills_by_wallet.get(lt.wallet, [])
        s = score_trader(lt, fills, window_days=window_days, min_trades=min_trades)
        if s.composite > 0:
            scores.append(s)

    scores.sort(key=lambda s: s.composite, reverse=True)
    return scores


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _sigmoid(x: float, midpoint: float = 0.0, scale: float = 1.0) -> float:
    """Squash any real value into (0, 1) via a scaled sigmoid."""
    try:
        return 1.0 / (1.0 + math.exp(-scale * (x - midpoint)))
    except OverflowError:
        return 1.0 if x > midpoint else 0.0
