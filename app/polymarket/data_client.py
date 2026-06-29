"""Polymarket Data API client.

Provides:
- leaderboard()  — top traders (real API first, quality-scored fallback)
- trades()       — recent fills for a wallet
- portfolio_value() — on-chain portfolio value
- fetch_all_trades() — parallel fetch for multiple wallets

All network calls share a single persistent HTTP session (connection reuse).
Results are cached short-term where the API response is stable.
"""
from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import Any, Optional

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

_LEADERBOARD_CACHE_TTL = 120  # seconds


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LeaderboardTrader:
    wallet: str
    username: Optional[str]
    pnl: float
    volume: float
    rank: Optional[int] = None


@dataclass
class SourceTrade:
    id: str
    wallet: str
    token_id: str
    condition_id: Optional[str]
    side: str           # BUY | SELL
    price: float
    shares: float
    timestamp: int
    market_question: Optional[str] = None
    outcome: Optional[str] = None

    @property
    def usd_size(self) -> float:
        return self.price * self.shares


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class DataClient:
    def __init__(self, base_url: Optional[str] = None, client: Optional[httpx.Client] = None):
        settings = get_settings()
        self.base_url = (base_url or settings.data_api_url).rstrip("/")
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            follow_redirects=True,
        )
        self._lb_cache: tuple[float, list[LeaderboardTrader]] | None = None
        self._lb_lock = Lock()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        clean = {k: v for k, v in params.items() if v is not None}
        try:
            resp = self._client.get(url, params=clean)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            log.warning("HTTP %s for %s: %s", exc.response.status_code, url, exc.response.text[:200])
            raise
        except httpx.RequestError as exc:
            log.warning("request error for %s: %s", url, exc)
            raise

    # ── LEADERBOARD ──────────────────────────────────────────────────────────

    def leaderboard(
        self,
        window: str = "MONTH",
        category: str = "OVERALL",
        limit: int = 20,
    ) -> list[LeaderboardTrader]:
        """Return top traders. Tries the real /leaderboard endpoint first;
        falls back to a quality-scored reconstruction from recent trades."""
        # Short-term cache avoids redundant calls within the same cycle.
        with self._lb_lock:
            if self._lb_cache:
                ts, cached = self._lb_cache
                if time.time() - ts < _LEADERBOARD_CACHE_TTL:
                    return cached[:limit]

        result = self._leaderboard_from_api(window, category, limit)
        if not result:
            result = self._leaderboard_from_trades(limit)

        with self._lb_lock:
            self._lb_cache = (time.time(), result)

        return result[:limit]

    def _leaderboard_from_api(
        self, window: str, category: str, limit: int
    ) -> list[LeaderboardTrader]:
        try:
            data = self._get(
                "/leaderboard",
                {"window": window, "category": category, "limit": limit},
            )
        except Exception as exc:
            log.debug("leaderboard API unavailable (%s), falling back to trade scan", exc)
            return []

        rows = data if isinstance(data, list) else data.get("data", [])
        if not rows:
            return []

        out: list[LeaderboardTrader] = []
        for i, r in enumerate(rows[:limit]):
            wallet = (r.get("proxyWallet") or r.get("proxy_wallet") or "").lower()
            if not wallet:
                continue
            out.append(
                LeaderboardTrader(
                    wallet=wallet,
                    username=r.get("name") or r.get("username") or None,
                    pnl=float(r.get("pnl") or 0),
                    volume=float(r.get("volume") or 0),
                    rank=i + 1,
                )
            )
        log.debug("leaderboard API returned %d traders", len(out))
        return out

    def _leaderboard_from_trades(self, limit: int) -> list[LeaderboardTrader]:
        """Build a quality-scored leaderboard from recent global trade activity."""
        try:
            data = self._get("/trades", {"limit": 500, "takerOnly": "true"})
        except Exception as exc:
            log.error("trade-scan fallback also failed: %s", exc)
            return []

        rows = data if isinstance(data, list) else data.get("data", [])
        now = time.time()
        stats: dict[str, dict] = {}

        for r in rows:
            wallet = (r.get("proxyWallet") or r.get("user") or "").lower()
            if not wallet:
                continue
            price = float(r.get("price") or 0)
            size = float(r.get("size") or r.get("shares") or 0)
            ts = int(r.get("timestamp") or now)
            if price <= 0 or size <= 0:
                continue

            w = stats.setdefault(wallet, {
                "volume": 0.0, "activity": 0, "edge_sum": 0.0,
                "edge_sq": 0.0, "last_ts": 0, "pnl": 0.0,
            })
            value = price * size
            w["volume"] += value
            w["activity"] += 1
            edge = abs(price - 0.5)
            w["edge_sum"] += edge
            w["edge_sq"] += edge * edge
            w["last_ts"] = max(w["last_ts"], ts)

        def _score(w: dict) -> float:
            if w["activity"] < 3:
                return 0.0
            avg_edge = w["edge_sum"] / w["activity"]
            variance = (w["edge_sq"] / w["activity"]) - (avg_edge ** 2)
            stability = 1 / (1 + max(variance, 0))
            recency = math.exp(-(now - w["last_ts"]) / 86400)
            return (avg_edge * 3.0 + math.log1p(w["volume"]) * 0.5 + stability * 2.0) * recency

        ranked = sorted(
            [
                LeaderboardTrader(
                    wallet=w,
                    username=None,
                    pnl=_score(s),
                    volume=s["volume"],
                )
                for w, s in stats.items()
                if s["activity"] >= 3
            ],
            key=lambda x: x.pnl,
            reverse=True,
        )
        for i, r in enumerate(ranked[:limit]):
            r.rank = i + 1
        return ranked[:limit]

    # ── TRADES ───────────────────────────────────────────────────────────────

    def trades(
        self,
        user: str,
        limit: int = 50,
        side: Optional[str] = None,
        taker_only: bool = True,
        since_ts: Optional[int] = None,
    ) -> list[SourceTrade]:
        """Fetch recent trades for a single wallet."""
        params: dict[str, Any] = {
            "user": user,
            "limit": limit,
            "takerOnly": str(taker_only).lower(),
        }
        if side:
            params["side"] = side

        try:
            data = self._get("/trades", params)
        except Exception as exc:
            log.warning("trades fetch failed for %s: %s", user, exc)
            return []

        rows = data if isinstance(data, list) else data.get("data", [])
        out: list[SourceTrade] = []

        for row in rows:
            token_id = str(row.get("asset") or "")
            if not token_id:
                continue
            ts = int(row.get("timestamp") or 0)
            if since_ts and ts <= since_ts:
                continue
            price = float(row.get("price") or 0)
            shares = float(row.get("size") or 0)
            if price <= 0 or shares <= 0:
                continue
            out.append(
                SourceTrade(
                    id=f"{row.get('transactionHash') or 'tx'}:{token_id}:{ts}",
                    wallet=(row.get("proxyWallet") or user).lower(),
                    token_id=token_id,
                    condition_id=row.get("conditionId"),
                    side=str(row.get("side", "")).upper() or "BUY",
                    price=price,
                    shares=shares,
                    timestamp=ts,
                    market_question=row.get("title"),
                    outcome=row.get("outcome"),
                )
            )
        return out

    def fetch_all_trades(
        self,
        wallets: list[str],
        limit: int = 50,
        since_ts: int = 0,
        max_workers: int = 8,
    ) -> dict[str, list[SourceTrade]]:
        """Fetch trades for multiple wallets in parallel."""
        results: dict[str, list[SourceTrade]] = {}
        if not wallets:
            return results

        with ThreadPoolExecutor(max_workers=min(max_workers, len(wallets))) as pool:
            future_to_wallet = {
                pool.submit(self.trades, w, limit, None, True, since_ts): w
                for w in wallets
            }
            for future in as_completed(future_to_wallet):
                wallet = future_to_wallet[future]
                try:
                    results[wallet] = future.result()
                except Exception as exc:
                    log.warning("parallel trades fetch failed for %s: %s", wallet, exc)
                    results[wallet] = []

        return results

    # ── PORTFOLIO VALUE ───────────────────────────────────────────────────────

    def portfolio_value(self, user: str) -> float:
        try:
            data = self._get("/value", {"user": user})
        except Exception:
            return 0.0
        if isinstance(data, dict):
            return float(data.get("value") or 0)
        if isinstance(data, list):
            return sum(float(x.get("value") or 0) for x in data)
        return 0.0
