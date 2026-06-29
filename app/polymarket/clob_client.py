"""Wrapper around ``py-clob-client`` (v0.34.x) for live order placement.

Imports of ``py_clob_client`` are deferred to construction time so the rest of
the app (and the paper-trading path) does not require the dependency or a
private key. This class is only instantiated when live trading is enabled and
fully configured.

Key facts this wrapper encodes for Polymarket's CLOB:
- A market BUY's ``amount`` is **USDC to spend**; a SELL is placed as a
  marketable limit order sized in **shares** (the market-SELL path in the client
  is unreliable — see py-clob-client issue #145).
- USDC is 6-decimals. ``get_balance_allowance`` needs a ``BalanceAllowanceParams``.
- ``signature_type`` + ``funder`` select how orders are attributed:
  0 = self-custody EOA (funder = your address), 1 = email/Magic, 2 = browser proxy
  (funder = your Polymarket deposit/proxy address).
"""
from __future__ import annotations

import logging
from typing import Optional

# Polymarket migrated from USDC.e to native USDC in 2024.
# py-clob-client v0.34.x still ships the old USDC.e address — patch it in
# BOTH the config module AND client module (which imports it directly via
# "from .config import get_contract_config", so the config-level patch alone
# has no effect on the already-bound reference inside client.py).
_NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
try:
    import py_clob_client.config as _clob_cfg
    import py_clob_client.client as _clob_client_mod
    import py_clob_client.clob_types as _clob_types

    _orig = _clob_cfg.get_contract_config

    def _patched(chainID: int, neg_risk: bool = False):
        cfg = _orig(chainID, neg_risk)
        if chainID == 137:
            cfg = _clob_types.ContractConfig(
                exchange=cfg.exchange,
                collateral=_NATIVE_USDC,
                conditional_tokens=cfg.conditional_tokens,
            )
        return cfg

    _clob_cfg.get_contract_config = _patched
    _clob_client_mod.get_contract_config = _patched  # patch the bound reference
except Exception:
    pass

from app.config import get_settings

log = logging.getLogger(__name__)

USDC_DECIMALS = 1_000_000


class ClobTrader:
    def __init__(self):
        settings = get_settings()
        ok, reason = settings.can_trade_live()
        if not ok:
            raise RuntimeError(f"cannot init live CLOB client: {reason}")

        # Deferred imports — only needed for live trading.
        from py_clob_client.client import ClobClient

        self._settings = settings
        kwargs = dict(
            host=settings.clob_api_url,
            key=settings.private_key,
            chain_id=settings.chain_id,
            signature_type=settings.signature_type,
        )
        # funder is the address holding USDC (the EOA itself for signature_type 0,
        # the Polymarket proxy/deposit address for types 1 and 2).
        if settings.wallet_address:
            kwargs["funder"] = settings.wallet_address

        self._client = ClobClient(**kwargs)
        # L2 auth: derive API creds from the private key.
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        log.warning("CLOB client initialised for funder=%s (live)", settings.wallet_address)

    # ----------------------------- identity -------------------------------- #
    def signer_address(self) -> Optional[str]:
        try:
            return self._client.get_address()
        except Exception:  # noqa: BLE001
            return None

    def funder_address(self) -> str:
        return self._settings.wallet_address

    # ------------------------- balance / readiness ------------------------- #

    def _usdc_balance_rpc(self) -> float:
        """Read native USDC balance directly from Polygon via RPC.

        EIP-7702 smart accounts (MetaMask Smart Accounts) don't set traditional
        ERC-20 allowances, so the CLOB API incorrectly returns balance=0 for
        these wallets even when funds are present. Reading directly from the
        chain bypasses this issue.
        """
        import httpx

        wallet = self._settings.wallet_address
        # balanceOf(address) selector + padded wallet address
        data = "0x70a08231" + wallet[2:].zfill(64)
        rpcs = [
            "https://polygon-bor-rpc.publicnode.com",
            "https://rpc.ankr.com/polygon",
            "https://polygon-rpc.com",
        ]
        for rpc in rpcs:
            try:
                r = httpx.post(
                    rpc,
                    json={"jsonrpc": "2.0", "method": "eth_call",
                          "params": [{"to": _NATIVE_USDC, "data": data}, "latest"], "id": 1},
                    timeout=8,
                )
                raw = r.json().get("result", "0x0")
                return int(raw, 16) / USDC_DECIMALS
            except Exception:
                continue
        return 0.0

    def _collateral_balance_allowance(self) -> tuple[float, float]:
        """Return (usdc_balance, usdc_allowance) for the collateral (USDC)."""
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

        ba = self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance = _to_float(_get(ba, "balance")) / USDC_DECIMALS
        allowance = _to_float(_get(ba, "allowance")) / USDC_DECIMALS
        return balance, allowance

    def available_usdc(self) -> float:
        # Use direct RPC balance check — more reliable than the CLOB API for
        # EIP-7702 smart accounts (MetaMask Smart Accounts) which don't set
        # traditional ERC-20 allowances.
        balance = self._usdc_balance_rpc()
        if balance > 0:
            return balance
        # Fall back to CLOB API for traditional wallets.
        try:
            balance, _ = self._collateral_balance_allowance()
            return balance
        except Exception as exc:  # noqa: BLE001
            log.warning("balance lookup failed: %s", exc)
            return 0.0

    def readiness(self) -> tuple[bool, str]:
        """Whether the wallet can actually place orders. Never raises."""
        # Check on-chain balance directly first (works for EIP-7702 accounts).
        balance = self._usdc_balance_rpc()
        if balance > 0:
            return True, f"ready (${balance:,.2f} USDC available)"
        # Fall back to CLOB API balance check for traditional wallets.
        try:
            balance, allowance = self._collateral_balance_allowance()
        except Exception as exc:  # noqa: BLE001
            return False, f"could not read wallet ({exc})"
        if balance <= 0:
            return False, "wallet has no USDC on Polygon — deposit/fund it first"
        if allowance <= 0:
            return False, (
                "USDC not approved for the exchange — place one manual trade on "
                "polymarket.com with this wallet to set approvals"
            )
        return True, f"ready (${balance:,.2f} USDC available)"

    # ------------------------------- pricing ------------------------------- #
    def live_price(self, token_id: str, side: str) -> Optional[float]:
        """Fresh price for a token from the CLOB (best price for the side)."""
        try:
            resp = self._client.get_price(token_id, side.upper())
            price = _to_float(_get(resp, "price"))
            if price > 0:
                return price
        except Exception as exc:  # noqa: BLE001
            log.debug("get_price failed for %s: %s", token_id, exc)
        # Fall back to the order-book midpoint.
        try:
            book = self._client.get_order_book(token_id)
            bid = _to_float(_get(_first(_get(book, "bids")), "price"))
            ask = _to_float(_get(_first(_get(book, "asks")), "price"))
            if bid and ask:
                return (bid + ask) / 2
            return ask or bid or None
        except Exception as exc:  # noqa: BLE001
            log.debug("order book failed for %s: %s", token_id, exc)
            return None

    # ------------------------------- orders -------------------------------- #
    def market_buy(self, *, token_id: str, usd: float) -> dict:
        """Buy `usd` worth of `token_id` with a Fill-Or-Kill market order."""
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        args = MarketOrderArgs(
            token_id=token_id, amount=round(usd, 2), side=BUY, order_type=OrderType.FOK
        )
        signed = self._client.create_market_order(args)
        resp = self._client.post_order(signed, OrderType.FOK)
        return _normalise(resp)

    def limit_sell(self, *, token_id: str, shares: float, price: float) -> dict:
        """Sell `shares` via a marketable limit order at/just below `price`.

        A limit order priced into the bid behaves like a market sell but avoids
        the unreliable market-SELL path in the client.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        px = min(max(round(price, 3), 0.001), 0.999)
        args = OrderArgs(token_id=token_id, price=px, size=round(shares, 2), side=SELL)
        signed = self._client.create_order(args)
        resp = self._client.post_order(signed, OrderType.FOK)
        out = _normalise(resp)
        out.setdefault("price", px)
        return out


# ------------------------------- helpers ---------------------------------- #
def _normalise(resp) -> dict:
    """Coerce a py-clob-client response into a plain dict."""
    if isinstance(resp, dict):
        return resp
    out = {}
    for key in ("success", "orderID", "orderId", "errorMsg", "status", "price"):
        if hasattr(resp, key):
            out[key] = getattr(resp, key)
    return out or {"success": True, "raw": str(resp)}


def _get(obj, key, default=None):
    """Read a key from a dict or attribute from an object."""
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
