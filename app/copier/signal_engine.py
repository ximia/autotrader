"""Consensus signal engine.

Instead of blindly copying every trade from every tracked trader, we:

1. Collect all recent fills from ALL tracked traders in parallel.
2. Group fills by (token_id, side="BUY") — looking for convergence.
3. Score each group on a composite confidence metric:
     - consensus weight  (more unique traders → higher)
     - trader quality    (composite scores of participating traders)
     - average conviction (how decisive the trades are — far from 0.5)
     - price quality     (not too close to certain resolution)
     - liquidity         (spread + book depth from Gamma)
4. Emit a Signal only when consensus_count ≥ min_consensus and
   confidence ≥ min_signal_confidence.

This means the bot takes FEWER, HIGHER-QUALITY trades rather than
blindly mirroring every move.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from app.polymarket.data_client import SourceTrade
from app.polymarket.gamma_client import MarketInfo

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    token_id: str
    side: str                          # BUY | SELL
    confidence: float                  # 0–1 composite score
    consensus_count: int               # unique traders who agree
    participating_wallets: list[str]   # deduplicated
    avg_price: float                   # average fill price across participating trades
    total_source_usd: float            # combined USD the source traders put in
    market_question: Optional[str] = None
    outcome: Optional[str] = None
    condition_id: Optional[str] = None
    reasons: list[str] = field(default_factory=list)  # scoring factors


# ─────────────────────────────────────────────────────────────────────────────
# ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class SignalEngine:
    """Pure-function consensus detector — stateless, no DB, no network."""

    def generate_signals(
        self,
        fills_by_wallet: dict[str, list[SourceTrade]],
        trader_scores: dict[str, float],            # wallet → composite score (0–1)
        market_info_fn,                             # token_id → MarketInfo | None
        *,
        min_consensus: int = 2,
        min_confidence: float = 0.55,
    ) -> list[Signal]:
        """Aggregate fills across wallets and emit scored consensus signals.

        Args:
            fills_by_wallet: recent fills per wallet (already deduped against cursors).
            trader_scores:   composite quality score per wallet.
            market_info_fn:  callable(token_id) → MarketInfo | None.
            min_consensus:   minimum unique wallets that must agree.
            min_confidence:  minimum composite score to emit a signal.
        """
        # ── group fills by (token_id, side) ──────────────────────────────
        groups: dict[tuple[str, str], list[tuple[str, SourceTrade]]] = {}
        for wallet, fills in fills_by_wallet.items():
            for fill in fills:
                key = (fill.token_id, fill.side)
                groups.setdefault(key, []).append((wallet, fill))

        signals: list[Signal] = []

        for (token_id, side), wallet_fills in groups.items():
            # Deduplicate to unique wallets.
            seen_wallets: set[str] = set()
            unique: list[tuple[str, SourceTrade]] = []
            for wallet, fill in wallet_fills:
                if wallet not in seen_wallets:
                    seen_wallets.add(wallet)
                    unique.append((wallet, fill))

            if len(unique) < min_consensus:
                continue

            # ── score this consensus group ────────────────────────────────
            market = market_info_fn(token_id)
            score, reasons = self._score_group(
                unique, trader_scores, market, side
            )

            if score < min_confidence:
                log.debug(
                    "signal %s %s skipped: confidence=%.2f < %.2f  reasons=%s",
                    token_id[:12], side, score, min_confidence, reasons,
                )
                continue

            # ── build Signal ──────────────────────────────────────────────
            fill_objects = [f for _, f in unique]
            avg_price = (
                sum(f.price * f.usd_size for f in fill_objects)
                / sum(f.usd_size for f in fill_objects)
                if fill_objects else 0.0
            )
            first = fill_objects[0]

            signals.append(
                Signal(
                    token_id=token_id,
                    side=side,
                    confidence=round(score, 4),
                    consensus_count=len(unique),
                    participating_wallets=[w for w, _ in unique],
                    avg_price=round(avg_price, 4),
                    total_source_usd=round(sum(f.usd_size for f in fill_objects), 2),
                    market_question=market.question if market else first.market_question,
                    outcome=market.outcome if market else first.outcome,
                    condition_id=market.condition_id if market else first.condition_id,
                    reasons=reasons,
                )
            )

        # Sort by confidence descending so the engine processes best signals first.
        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals

    # ── SCORING ──────────────────────────────────────────────────────────────

    def _score_group(
        self,
        unique: list[tuple[str, SourceTrade]],
        trader_scores: dict[str, float],
        market: Optional[MarketInfo],
        side: str,
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        n = len(unique)

        # 1. Consensus weight — more traders = stronger signal.
        #    Scores: 2→0.50, 3→0.67, 4→0.75, 5→0.80, ...
        consensus_w = n / (n + 2)
        reasons.append(f"consensus={n}(w={consensus_w:.2f})")

        # 2. Trader quality — average composite score of participating traders.
        scores = [trader_scores.get(w, 0.5) for w, _ in unique]
        avg_score = sum(scores) / len(scores)
        trader_quality_w = avg_score
        reasons.append(f"quality={avg_score:.2f}")

        # 3. Conviction — how decisive their bets are (far from 0.5 = more certain).
        fills = [f for _, f in unique]
        avg_edge = sum(abs(f.price - 0.5) for f in fills) / len(fills)
        conviction_w = min(avg_edge * 2.0, 1.0)
        reasons.append(f"conviction={conviction_w:.2f}")

        # 4. Price quality — penalize if price is near 0 or 1 (little upside left
        #    or outcome already near-certain by market consensus).
        avg_price = sum(f.price for f in fills) / len(fills)
        # Peaks at 0.5 (most uncertainty), fades near extremes.
        # We still want to trade at e.g. 0.7 (decent upside) but not 0.95+.
        if side == "BUY":
            price_q = max(0.0, 1 - max(0.0, avg_price - 0.5) / 0.5)
        else:
            price_q = max(0.0, 1 - max(0.0, (1 - avg_price) - 0.5) / 0.5)
        reasons.append(f"price_quality={price_q:.2f}(p={avg_price:.2f})")

        # 5. Liquidity — tight spread + adequate book depth (if Gamma available).
        liquidity_w = 0.5  # neutral default when Gamma unavailable
        if market:
            if not market.tradeable:
                return 0.0, reasons + ["market_not_tradeable"]
            spread_pct = market.spread_pct
            if spread_pct is not None:
                # score 1.0 at 0% spread, ~0.5 at 5%, ~0.2 at 10%
                liquidity_w = math.exp(-spread_pct * 15)
            reasons.append(f"spread={spread_pct:.3f}" if spread_pct else "spread=?")

        # ── weighted composite ────────────────────────────────────────────
        confidence = (
            consensus_w      * 0.30 +
            trader_quality_w * 0.25 +
            conviction_w     * 0.20 +
            price_q          * 0.15 +
            liquidity_w      * 0.10
        )
        return confidence, reasons
