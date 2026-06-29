"""Dry connection test for your Polymarket wallet — places NO orders.

Run it after filling in `.env` and before enabling live trading:

    python -m app.tools.check_wallet

It connects read-only, prints who you are (signer + funder), your USDC balance,
and whether approvals are set, then gives a clear READY / what-to-fix verdict.
This is the safe way to confirm your wallet is wired up correctly.
"""
from __future__ import annotations

import sys

from app.config import get_settings


def main() -> int:
    s = get_settings()
    print("=" * 60)
    print(" Polymarket wallet connection check (no orders placed)")
    print("=" * 60)

    sig_names = {0: "self-custody EOA", 1: "email/Magic", 2: "browser proxy"}
    print(f"  signature_type : {s.signature_type} ({sig_names.get(s.signature_type, '?')})")
    print(f"  funder (USDC)  : {s.wallet_address or '— NOT SET —'}")
    print(f"  private key    : {'set' if s.private_key else '— NOT SET —'}")
    print(f"  CLOB host      : {s.clob_api_url}")
    print("-" * 60)

    if not s.private_key or not s.wallet_address:
        print("  ✗ PRIVATE_KEY and WALLET_ADDRESS must both be set in .env.")
        print("    See the README 'Which wallet do I have?' section.")
        return 1

    try:
        from app.polymarket.clob_client import ClobTrader
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ could not import the CLOB client: {exc}")
        print("    Run: pip install -r requirements.txt")
        return 1

    # Temporarily allow construction even if LIVE_TRADING is still false — this
    # is a read-only check and the user hasn't flipped the live switch yet.
    try:
        trader = _connect(s)
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ failed to connect / authenticate: {exc}")
        print("    Double-check the private key and signature_type for your wallet.")
        return 1

    signer = trader.signer_address()
    print(f"  signer address : {signer or '(unknown)'}")

    ok, reason = trader.readiness()
    balance = trader.available_usdc()
    print(f"  USDC balance   : ${balance:,.2f}")
    print("-" * 60)
    if ok:
        print(f"  ✓ READY TO TRADE — {reason}")
        print("    You can set LIVE_TRADING=true and start with small caps.")
        return 0
    print(f"  ✗ NOT READY — {reason}")
    return 2


def _connect(settings):
    """Build a ClobTrader for a read-only check, bypassing the live-flag gate."""
    from app.polymarket.clob_client import ClobTrader

    # can_trade_live() blocks when LIVE_TRADING is off; for a read-only check we
    # construct directly with a temporary live flag.
    original = settings.live_trading
    settings.live_trading = True
    try:
        return ClobTrader()
    finally:
        settings.live_trading = original


if __name__ == "__main__":
    sys.exit(main())
