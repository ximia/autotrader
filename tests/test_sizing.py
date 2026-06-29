"""Tests for proportional-to-bankroll sizing and its clamps."""
from app.copier.sizing import proportional_size

BASE = dict(
    copy_ratio=1.0,
    min_order_usd=1.0,
    max_per_trade_usd=25.0,
    min_trade_usd=10.0,
)


def test_proportional_basic():
    # They put 10% of a $10k portfolio in; we have $1k -> ~$100, clamped to $25.
    r = proportional_size(their_trade_usd=1000, their_portfolio_usd=10000,
                          my_bankroll_usd=1000, **BASE)
    assert r.accepted
    assert r.usd == 25.0  # clamped to max_per_trade


def test_proportional_scales_with_bankroll():
    # 1% of portfolio, $1000 bankroll -> $10, within caps.
    r = proportional_size(their_trade_usd=100, their_portfolio_usd=10000,
                          my_bankroll_usd=1000, **BASE)
    assert r.accepted
    assert r.usd == 10.0


def test_copy_ratio_applied():
    r = proportional_size(their_trade_usd=100, their_portfolio_usd=10000,
                          my_bankroll_usd=1000, copy_ratio=0.5,
                          min_order_usd=1.0, max_per_trade_usd=25.0, min_trade_usd=10.0)
    assert r.usd == 5.0


def test_below_min_trade_rejected():
    r = proportional_size(their_trade_usd=5, their_portfolio_usd=10000,
                          my_bankroll_usd=1000, **BASE)
    assert not r.accepted
    assert "min" in r.reason


def test_min_order_floor():
    # Tiny fraction would size below $1; floored to min_order_usd.
    r = proportional_size(their_trade_usd=10, their_portfolio_usd=1_000_000,
                          my_bankroll_usd=1000, **BASE)
    assert r.accepted
    assert r.usd == 1.0


def test_zero_bankroll_rejected():
    r = proportional_size(their_trade_usd=100, their_portfolio_usd=10000,
                          my_bankroll_usd=0, **BASE)
    assert not r.accepted


def test_unknown_portfolio_clamps_to_max():
    # No portfolio info -> size off our caps (max per trade).
    r = proportional_size(their_trade_usd=100, their_portfolio_usd=0,
                          my_bankroll_usd=1000, **BASE)
    assert r.accepted
    assert r.usd == 25.0
