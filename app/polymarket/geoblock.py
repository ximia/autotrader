"""Polymarket geoblock check.

Calls the official geoblock endpoint before placing any orders.
Result is cached for CACHE_TTL seconds so it doesn't slow down every cycle.

Endpoint: GET https://polymarket.com/api/geoblock
Returns:  { "blocked": bool, "ip": str, "country": str, "region": str }
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger(__name__)

_GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
CACHE_TTL = 300  # re-check every 5 minutes


@dataclass
class GeoblockResult:
    blocked: bool
    ip: str
    country: str
    region: str
    checked_at: float = 0.0
    error: Optional[str] = None

    @property
    def reason(self) -> str:
        if self.error:
            return f"geoblock check failed: {self.error}"
        if self.blocked:
            reg = f"/{self.region}" if self.region else ""
            return f"trading blocked in {self.country}{reg} — run the bot from an allowed region (see docs.polymarket.com/developers/CLOB/geoblock)"
        return "ok"


_cache: Optional[GeoblockResult] = None


def check(client: Optional[httpx.Client] = None) -> GeoblockResult:
    """Return the geoblock status for this server's IP.  Result is cached."""
    global _cache
    now = time.monotonic()

    if _cache and (now - _cache.checked_at) < CACHE_TTL:
        return _cache

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=10.0)

    try:
        resp = client.get(_GEOBLOCK_URL)
        resp.raise_for_status()
        data = resp.json()
        result = GeoblockResult(
            blocked=bool(data.get("blocked", False)),
            ip=data.get("ip", ""),
            country=data.get("country", ""),
            region=data.get("region", ""),
            checked_at=now,
        )
    except Exception as exc:
        log.warning("geoblock check failed: %s", exc)
        # Fail open — don't block trading if the geoblock endpoint is down.
        result = GeoblockResult(
            blocked=False,
            ip="",
            country="",
            region="",
            checked_at=now,
            error=str(exc),
        )
    finally:
        if own_client:
            client.close()

    _cache = result
    if result.blocked:
        log.error(
            "GEOBLOCK: trading blocked from %s (%s/%s) — move the bot to an allowed region",
            result.ip, result.country, result.region,
        )
    else:
        log.info("geoblock: OK (ip=%s country=%s)", result.ip, result.country)

    return result


def invalidate() -> None:
    """Force a fresh check on the next call."""
    global _cache
    _cache = None
