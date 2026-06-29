"""Tests for the paper executor's simulated fills."""
from app.copier.executor import PaperExecutor


def test_paper_buy_shares_from_usd():
    ex = PaperExecutor()
    r = ex.buy("tok", usd=10.0, ref_price=0.25)
    assert r.status == "filled"
    assert r.is_live is False
    assert r.fill_price == 0.25
    assert r.shares == 40.0  # 10 / 0.25
    assert r.usd == 10.0


def test_paper_sell_usd_from_shares():
    ex = PaperExecutor()
    r = ex.sell("tok", shares=40.0, ref_price=0.30)
    assert r.status == "filled"
    assert r.usd == 12.0  # 40 * 0.30


def test_price_clamped_to_valid_range():
    ex = PaperExecutor()
    r = ex.buy("tok", usd=10.0, ref_price=0.0)  # invalid -> default 0.5
    assert r.fill_price == 0.5
