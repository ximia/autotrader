"""Tests for offline demo mode — the full pipeline must run with no network."""
from app.config import Settings
from app.copier.engine import CopyEngine
from app.copier.executor import PaperExecutor
from app.db import session_scope
from app.models import CopyTrade, Position, Trader
from app.polymarket.demo import DEMO_MARKETS, DemoData, DemoGamma, current_price


def _settings(**over):
    base = dict(
        demo_mode=True, live_trading=False, top_n=8, min_trade_usd=10.0,
        min_order_usd=1.0, max_per_trade_usd=25.0, max_daily_spend_usd=500.0,
        max_open_positions=25, max_slippage_pct=0.05, copy_ratio=1.0,
        copy_buys_only=True, paper_bankroll=1000.0,
    )
    base.update(over)
    return Settings(**base)


def test_demo_leaderboard_has_traders():
    rows = DemoData().leaderboard(limit=8)
    assert len(rows) == 8
    assert rows[0].rank == 1
    assert all(r.wallet.startswith("0xdemo") for r in rows)


def test_demo_trades_resolve_to_markets():
    data, gamma = DemoData(), DemoGamma()
    # At least one demo trader should have recent fills, all resolvable in gamma.
    any_fills = False
    for wallet, *_ in [(r.wallet,) for r in data.leaderboard()]:
        for f in data.trades(wallet):
            any_fills = True
            assert 0 < f.price < 1
            assert gamma.market_for_token(f.token_id) is not None
    assert any_fills


def test_demo_price_in_range():
    for m in DEMO_MARKETS:
        assert 0.0 < current_price(m.yes_token) < 1.0
        assert 0.0 < current_price(m.no_token) < 1.0


def test_demo_engine_runs_end_to_end(temp_db):
    eng = CopyEngine(data=DemoData(), gamma=DemoGamma(), executor=PaperExecutor(),
                     settings=_settings())
    report = eng.run_once()

    assert report.traders_tracked == 8
    assert report.copies_made > 0  # demo always surfaces some trades
    with session_scope() as s:
        assert s.query(Trader).filter(Trader.tracked.is_(True)).count() == 8
        assert s.query(Position).count() > 0
        filled = s.query(CopyTrade).filter(CopyTrade.status == "filled").count()
        assert filled > 0
