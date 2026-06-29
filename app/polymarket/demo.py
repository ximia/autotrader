"""Offline demo data sources.

When ``DEMO_MODE=true`` the engine uses ``DemoData`` / ``DemoGamma`` instead of
the real Polymarket clients. This lets the entire pipeline — trader selection,
trade copying, sizing, positions, PnL, and the live dashboard — run end-to-end
with **no network access and no wallet**, so you can see the product working
immediately. Demo mode is always paper (it never places real orders).

The synthetic data is driven by wall-clock time, so each loop surfaces a few
"new" trades and position prices drift, making the dashboard feel live.
"""
from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass

from app.polymarket.data_client import LeaderboardTrader, SourceTrade
from app.polymarket.gamma_client import MarketInfo


@dataclass
class _Market:
    question: str
    condition_id: str
    yes_token: str
    no_token: str
    base_price: float  # baseline price of the YES outcome


# A small catalogue of plausible markets the demo traders trade in.
DEMO_MARKETS: list[_Market] = [
    _Market("Will BTC close above $150k this year?", "0xc001", "10001", "10002", 0.42),
    _Market("Will the incumbent win the 2028 election?", "0xc002", "20001", "20002", 0.55),
    _Market("Will the Fed cut rates at the next meeting?", "0xc003", "30001", "30002", 0.63),
    _Market("Will Team A win the championship?", "0xc004", "40001", "40002", 0.31),
    _Market("Will ETH flip BTC by market cap this year?", "0xc005", "50001", "50002", 0.08),
    _Market("Will a major AI lab release a new model this month?", "0xc006", "60001", "60002", 0.71),
]

_TOKEN_INDEX = {m.yes_token: m for m in DEMO_MARKETS} | {m.no_token: m for m in DEMO_MARKETS}

# Synthetic top traders (stable wallets + names).
DEMO_TRADERS = [
    ("0xdemo0000000000000000000000000000000000a1", "MarketWhale", 184_500.0, 2_100_000.0),
    ("0xdemo0000000000000000000000000000000000b2", "AlphaSeeker", 122_300.0, 1_450_000.0),
    ("0xdemo0000000000000000000000000000000000c3", "OddsHunter", 98_700.0, 980_000.0),
    ("0xdemo0000000000000000000000000000000000d4", "EdgeFinder", 76_200.0, 720_000.0),
    ("0xdemo0000000000000000000000000000000000e5", "PolyKing", 61_900.0, 540_000.0),
    ("0xdemo0000000000000000000000000000000000f6", "SharpBettor", 48_400.0, 410_000.0),
    ("0xdemo000000000000000000000000000000000017", "QuantDegen", 35_100.0, 300_000.0),
    ("0xdemo000000000000000000000000000000000028", "ValueVulture", 27_800.0, 220_000.0),
]

_BUCKET_SECONDS = 60          # one potential trade per trader per minute
_HISTORY_BUCKETS = 8          # seed ~8 minutes of trades on first run


def _h(*parts) -> int:
    return int(hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest(), 16)


def current_price(token_id: str, now: float | None = None) -> float:
    """Price of an outcome token, drifting smoothly over time."""
    market = _TOKEN_INDEX.get(token_id)
    if market is None:
        return 0.5
    now = time.time() if now is None else now
    base = market.base_price if token_id == market.yes_token else (1 - market.base_price)
    drift = 0.04 * math.sin(now / 420.0 + _h(token_id) % 7)
    return min(max(base + drift, 0.03), 0.97)


class DemoData:
    """Drop-in replacement for ``DataClient`` (offline)."""

    def leaderboard(self, window="MONTH", category="OVERALL", limit=20) -> list[LeaderboardTrader]:
        return [
            LeaderboardTrader(wallet=w, username=name, pnl=pnl, volume=vol, rank=i + 1)
            for i, (w, name, pnl, vol) in enumerate(DEMO_TRADERS[:limit])
        ]

    def trades(self, user, limit=50, side=None, taker_only=True) -> list[SourceTrade]:
        now = time.time()
        cur_bucket = int(now // _BUCKET_SECONDS)
        fills: list[SourceTrade] = []
        for b in range(cur_bucket - _HISTORY_BUCKETS, cur_bucket + 1):
            # Each trader trades only on some buckets (~1 in 3).
            if _h(user, b) % 3 != 0:
                continue
            market = DEMO_MARKETS[_h(user, b, "mkt") % len(DEMO_MARKETS)]
            buy_yes = _h(user, b, "side") % 4 != 0  # mostly BUY YES
            token = market.yes_token if buy_yes else market.no_token
            price = current_price(token, now)  # price at "now" so slippage ~0
            usd = 50 + (_h(user, b, "usd") % 450)  # $50–$500 notional
            shares = round(usd / price, 2)
            fills.append(
                SourceTrade(
                    id=f"demo:{user}:{b}",
                    wallet=user,
                    token_id=token,
                    condition_id=market.condition_id,
                    side="BUY",  # demo copies opening BUYs
                    price=round(price, 3),
                    shares=shares,
                    timestamp=b * _BUCKET_SECONDS,
                    market_question=market.question,
                    outcome="Yes" if token == market.yes_token else "No",
                )
            )
        return fills[-limit:]

    def portfolio_value(self, user) -> float:
        return 20_000.0 + (_h(user, "pv") % 180_000)

    def fetch_all_trades(self, wallets, limit=50, since_ts=0, max_workers=8):
        return {w: self.trades(w, limit=limit) for w in wallets}

    def close(self) -> None:
        pass


class DemoGamma:
    """Drop-in replacement for ``GammaClient`` (offline)."""

    def market_for_token(self, token_id, use_cache=True) -> MarketInfo | None:
        market = _TOKEN_INDEX.get(token_id)
        if market is None:
            return None
        price = current_price(token_id)
        is_yes = token_id == market.yes_token
        return MarketInfo(
            token_id=token_id,
            condition_id=market.condition_id,
            question=market.question,
            outcome="Yes" if is_yes else "No",
            active=True,
            closed=False,
            accepting_orders=True,
            best_bid=round(max(price - 0.005, 0.01), 3),
            best_ask=round(min(price + 0.005, 0.99), 3),
        )

    def close(self) -> None:
        pass
