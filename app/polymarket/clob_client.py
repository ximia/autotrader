"""Wrapper around ``py-clob-client-v2`` for live order placement.

Polymarket archived py-clob-client (v0.34.x) on 2026-05-25 and replaced it
with py-clob-client-v2. The new library uses a different signature type system
(SignatureTypeV2) and a new order format — which is why v0.34.x produces
"invalid order version" errors.

Key changes from v1:
- SignatureTypeV2.POLY_1271 (int=3) for EIP-7702 / EIP-1271 smart accounts
- SignatureTypeV2.POLY_PROXY (int=1) for old proxy wallets
- MarketOrderArgsV2.amount is USDC to spend (same as before)
- create_and_post_market_order combines sign + post in one call
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import get_settings

log = logging.getLogger(__name__)

_NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
_PUSD        = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # Polymarket V2 token
USDC_DECIMALS = 1_000_000


class ClobTrader:
    def __init__(self):
        settings = get_settings()
        ok, reason = settings.can_trade_live()
        if not ok:
            raise RuntimeError(f"cannot init live CLOB client: {reason}")

        from py_clob_client_v2 import ClobClient, SignatureTypeV2

        self._settings = settings

        # Map old signature types to v2 equivalents:
        # 0 = self-custody EOA → EOA
        # 1 = email/Magic     → POLY_PROXY (Polymarket managed proxy)
        # 2 = browser proxy   → POLY_PROXY (MetaMask browser proxy wallet)
        # 3 = POLY_1271       → POLY_1271  (EIP-7702 smart accounts)
        sig_map = {
            0: SignatureTypeV2.EOA,
            1: SignatureTypeV2.POLY_PROXY,
            2: SignatureTypeV2.POLY_PROXY,
            3: SignatureTypeV2.POLY_1271,
        }
        sig_type = sig_map.get(settings.signature_type, SignatureTypeV2.POLY_PROXY)

        # Derive L2 API credentials from private key.
        temp = ClobClient(
            host=settings.clob_api_url,
            chain_id=settings.chain_id,
            key=settings.private_key,
        )
        creds = temp.create_or_derive_api_key()

        self._client = ClobClient(
            host=settings.clob_api_url,
            chain_id=settings.chain_id,
            key=settings.private_key,
            creds=creds,
            signature_type=sig_type,
            funder=settings.wallet_address or None,
        )
        log.warning(
            "CLOB v2 client initialised: funder=%s sig=%s",
            settings.wallet_address, sig_type,
        )

    # ── identity ──────────────────────────────────────────────────────────────

    def signer_address(self) -> Optional[str]:
        try:
            return self._client.get_address()
        except Exception:
            return None

    def funder_address(self) -> str:
        return self._settings.wallet_address

    # ── balance / readiness ───────────────────────────────────────────────────

    def _usdc_balance_rpc(self) -> float:
        """Read native USDC balance directly from Polygon via RPC."""
        import httpx
        wallet = self._settings.wallet_address
        data = "0x70a08231" + wallet[2:].zfill(64)
        for rpc in [
            "https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon-rpc.com",
        ]:
            try:
                # Check pUSD first (Polymarket V2 token used by proxy wallets).
                pusd = int(httpx.post(
                    rpc,
                    json={"jsonrpc": "2.0", "method": "eth_call",
                          "params": [{"to": _PUSD, "data": data}, "latest"], "id": 1},
                    timeout=8,
                ).json().get("result", "0x0"), 16) / USDC_DECIMALS
                if pusd > 0:
                    return pusd
                # Fall back to native USDC.
                return int(httpx.post(
                    rpc,
                    json={"jsonrpc": "2.0", "method": "eth_call",
                          "params": [{"to": _NATIVE_USDC, "data": data}, "latest"], "id": 2},
                    timeout=8,
                ).json().get("result", "0x0"), 16) / USDC_DECIMALS
            except Exception:
                continue
        return 0.0

    def available_usdc(self) -> float:
        balance = self._usdc_balance_rpc()
        if balance > 0:
            return balance
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams
            ba = self._client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return _to_float(_get(ba, "balance")) / USDC_DECIMALS
        except Exception as exc:
            log.warning("balance lookup failed: %s", exc)
            return 0.0

    def readiness(self) -> tuple[bool, str]:
        balance = self._usdc_balance_rpc()
        if balance > 0:
            return True, f"ready (${balance:,.2f} USDC available)"
        return False, "wallet has no USDC on Polygon — deposit/fund it first"

    # ── pricing ───────────────────────────────────────────────────────────────

    def live_price(self, token_id: str, side: str) -> Optional[float]:
        try:
            book = self._client.get_order_book(token_id)
            bids = _get(book, "bids") or []
            asks = _get(book, "asks") or []
            bid = _to_float(_get(_first(bids), "price"))
            ask = _to_float(_get(_first(asks), "price"))
            if bid and ask:
                return (bid + ask) / 2
            return ask or bid or None
        except Exception as exc:
            log.debug("order book failed for %s: %s", token_id, exc)
            return None

    # ── orders ────────────────────────────────────────────────────────────────

    def market_buy(self, *, token_id: str, usd: float) -> dict:
        """Buy `usd` worth of `token_id` using the new v2 market order API."""
        from py_clob_client_v2 import MarketOrderArgsV2, OrderType, Side

        args = MarketOrderArgsV2(
            token_id=token_id,
            amount=round(usd, 2),
            side=Side.BUY,
            order_type=OrderType.FOK,
        )
        resp = self._client.create_and_post_market_order(args)
        return _normalise(resp)

    def limit_sell(self, *, token_id: str, shares: float, price: float) -> dict:
        """Sell `shares` via a marketable limit order."""
        from py_clob_client_v2 import OrderArgsV2, OrderType, PartialCreateOrderOptions, Side

        px = min(max(round(price, 3), 0.001), 0.999)
        args = OrderArgsV2(
            token_id=token_id,
            price=px,
            size=round(shares, 2),
            side=Side.SELL,
        )
        resp = self._client.create_and_post_order(
            args,
            options=PartialCreateOrderOptions(tick_size="0.01", neg_risk=False),
            order_type=OrderType.FOK,
        )
        out = _normalise(resp)
        out.setdefault("price", px)
        return out


# ── helpers ────────────────────────────────────────────────────────────────────

def _normalise(resp) -> dict:
    if isinstance(resp, dict):
        return resp
    out = {}
    for key in ("success", "orderID", "orderId", "errorMsg", "status", "price",
                "takingAmount", "makingAmount"):
        if hasattr(resp, key):
            out[key] = getattr(resp, key)
    return out or {"success": True, "raw": str(resp)}


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _first(seq):
    if isinstance(seq, (list, tuple)) and seq:
        return seq[0]
    return None


def _to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
