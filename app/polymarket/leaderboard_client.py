"""Polymarket leaderboard client — /v1/leaderboard endpoint.

Fetches the verified public leaderboard, applies a multi-window consistency
filter (wallet must appear in ALL configured time windows) to reduce
survivorship bias, and returns a clean follow list.

This module is intentionally read-only and secret-free — no private key,
no auth headers needed. It exists purely to derive the follow list that the
copy pipeline uses.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_BASE = "https://data-api.polymarket.com"
_LEADERBOARD_PATH = "/v1/leaderboard"
_PAGE_SIZE = 50  # hard max per the API spec


# ─────────────────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LeaderboardEntry:
    proxy_wallet: str
    rank: int
    username: Optional[str]
    pnl: float
    vol: float
    verified_badge: bool
    x_username: Optional[str] = None


@dataclass
class FollowCandidate:
    """A wallet that passed the consistency filter, ready to persist."""
    proxy_wallet: str
    username: Optional[str]
    pnl: float
    vol: float
    best_rank: int                      # best rank seen across all windows
    verified_badge: bool
    windows_seen: list[str] = field(default_factory=list)  # ["WEEK","MONTH"]


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────────────

class LeaderboardClient:
    def __init__(self, client: Optional[httpx.Client] = None):
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0),
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    # ── RAW FETCH ─────────────────────────────────────────────────────────────

    def fetch_board(
        self,
        *,
        time_period: str,
        order_by: str = "PNL",
        category: str = "OVERALL",
        max_ranks: int = 100,
    ) -> list[LeaderboardEntry]:
        """Fetch up to ``max_ranks`` entries from one time window.

        ``rank`` is returned as a string by the API — we parse it to int here.
        ``time_period`` is always set explicitly (never rely on the API default).
        """
        entries: list[LeaderboardEntry] = []
        offset = 0

        while len(entries) < max_ranks:
            limit = min(_PAGE_SIZE, max_ranks - len(entries))
            try:
                resp = self._client.get(
                    f"{_BASE}{_LEADERBOARD_PATH}",
                    params={
                        "timePeriod": time_period,
                        "orderBy": order_by,
                        "category": category,
                        "limit": limit,
                        "offset": offset,
                    },
                )
                resp.raise_for_status()
                page: list[dict] = resp.json()
            except httpx.HTTPStatusError as exc:
                log.error(
                    "leaderboard fetch failed (%s %s): %s",
                    exc.response.status_code, time_period, exc.response.text[:200],
                )
                break
            except Exception as exc:
                log.error("leaderboard fetch error (%s): %s", time_period, exc)
                break

            if not page:
                break

            for row in page:
                wallet = (row.get("proxyWallet") or "").lower()
                if not wallet:
                    continue
                try:
                    rank = int(row.get("rank") or 0)
                except (ValueError, TypeError):
                    rank = 0
                entries.append(
                    LeaderboardEntry(
                        proxy_wallet=wallet,
                        rank=rank,
                        username=row.get("userName") or None,
                        pnl=float(row.get("pnl") or 0),
                        vol=float(row.get("vol") or 0),
                        verified_badge=bool(row.get("verifiedBadge")),
                        x_username=row.get("xUsername") or None,
                    )
                )

            if len(page) < limit:
                break  # last page
            offset += limit

        log.info("leaderboard(%s) fetched %d entries", time_period, len(entries))
        return entries

    # ── CONSISTENCY FILTER ────────────────────────────────────────────────────

    def build_follow_list(
        self,
        *,
        windows: list[str],
        order_by: str = "PNL",
        category: str = "OVERALL",
        max_ranks: int = 100,
        min_pnl: float = 0.0,
        min_vol: float = 0.0,
        verified_only: bool = False,
    ) -> list[FollowCandidate]:
        """Pull multiple time windows and return only wallets present in all of them.

        This cross-window intersection filters out traders who had one lucky
        bet in a short window but aren't consistently good.
        """
        if not windows:
            windows = ["MONTH"]

        # Fetch each window.
        boards: dict[str, dict[str, LeaderboardEntry]] = {}
        for window in windows:
            entries = self.fetch_board(
                time_period=window,
                order_by=order_by,
                category=category,
                max_ranks=max_ranks,
            )
            boards[window] = {e.proxy_wallet: e for e in entries}
            time.sleep(0.25)  # polite rate-limiting between pages

        if not boards:
            return []

        # Intersect: keep only wallets present in every window.
        wallet_sets = [set(b.keys()) for b in boards.values()]
        common = wallet_sets[0].intersection(*wallet_sets[1:])
        log.info(
            "consistency filter: %s boards → %d common wallets (from %s total)",
            windows,
            len(common),
            [len(b) for b in boards.values()],
        )

        candidates: list[FollowCandidate] = []
        for wallet in common:
            # Use the stats from the first window as the primary snapshot.
            primary = boards[windows[0]][wallet]

            if verified_only and not primary.verified_badge:
                continue
            if primary.pnl < min_pnl:
                continue
            if primary.vol < min_vol:
                continue

            best_rank = min(
                boards[w][wallet].rank
                for w in windows
                if wallet in boards[w] and boards[w][wallet].rank > 0
            )

            candidates.append(
                FollowCandidate(
                    proxy_wallet=wallet,
                    username=primary.username,
                    pnl=primary.pnl,
                    vol=primary.vol,
                    best_rank=best_rank,
                    verified_badge=primary.verified_badge,
                    windows_seen=list(windows),
                )
            )

        # Sort by best rank ascending (rank 1 = best).
        candidates.sort(key=lambda c: c.best_rank)
        return candidates
