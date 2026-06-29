"""End-to-end copy-engine tests with fake clients and the paper executor."""
from __future__ import annotations

import pytest

from app.config import Settings
from app.copier.engine import CopyEngine
from app.copier.executor import PaperExecutor
from app.db import get_state, session_scope
from app.models import CopyTrade, Position, Trader
from app.polymarket.data_client import LeaderboardTrader, SourceTrade
from app.polymarket.gamma_client import MarketInfo


class FakeData:
    def __init__(self, board, trades_by_wallet, portfolio=10000.0):
        self._board = board
        self._trades = trades_by_wallet
        self._portfolio = portfolio

    def leaderboard(self, window="MONTH", category="OVERALL", limit=20):
        return self._board

    def trades(self, user, limit=50, side=None, taker_only=True):
        return list(self._trades.get(user, []))

    def portfolio_value(self, user):
        return self._portfolio

    def close(self):
        pass


class FakeGamma:
    def __init__(self, tradeable=True, bid=0.39, ask=0.41):
        self._tradeable = tradeable
        self._bid, self._ask = bid, ask

    def market_for_token(self, token_id, use_cache=True):
        return MarketInfo(
            token_id=token_id, condition_id="c1", question="Will X happen?",
            outcome="Yes", active=self._tradeable, closed=not self._tradeable,
            accepting_orders=self._tradeable, best_bid=self._bid, best_ask=self._ask,
        )

    def close(self):
        pass


def _settings(**over):
    base = dict(
        live_trading=False, top_n=2, min_trade_usd=10.0, min_order_usd=1.0,
        max_per_trade_usd=25.0, max_daily_spend_usd=200.0, max_open_positions=25,
        max_slippage_pct=0.05, copy_ratio=1.0, copy_buys_only=True, paper_bankroll=1000.0,
    )
    base.update(over)
    return Settings(**base)


def _seed_trader(wallet, ts=0):
    with session_scope() as s:
        s.add(Trader(wallet=wallet, last_seen_ts=ts, tracked=True, portfolio_value=10000.0))


def _buy(wallet, token, ts, price=0.40, shares=250):
    return SourceTrade(
        id=f"{token}:{ts}", wallet=wallet, token_id=token, condition_id="c1",
        side="BUY", price=price, shares=shares, timestamp=ts,
        market_question="Will X happen?", outcome="Yes",
    )


def test_copy_buy_creates_position_and_spends_cash(temp_db):
    _seed_trader("0xwhale")
    board = [LeaderboardTrader(wallet="0xwhale", username="whale", pnl=1000, volume=5000, rank=1)]
    # $100 trade (0.40 * 250) -> 1% of 10k portfolio -> $10 of $1000 bankroll.
    fills = {"0xwhale": [_buy("0xwhale", "tok1", ts=2_000_000_000)]}
    eng = CopyEngine(data=FakeData(board, fills), gamma=FakeGamma(), executor=PaperExecutor(),
                     settings=_settings())

    report = eng.run_once()

    assert report.copies_made == 1
    with session_scope() as s:
        ct = s.query(CopyTrade).filter(CopyTrade.status == "filled").one()
        assert ct.side == "BUY"
        assert ct.our_usd == 10.0
        pos = s.get(Position, "tok1")
        assert pos is not None and pos.shares > 0
        state = get_state(s)
        assert round(state.paper_cash_usd, 2) == 990.0  # 1000 - 10
        assert round(state.spent_today_usd, 2) == 10.0


def test_idempotent_no_double_copy(temp_db):
    _seed_trader("0xwhale")
    board = [LeaderboardTrader(wallet="0xwhale", username="whale", pnl=1, volume=1, rank=1)]
    fills = {"0xwhale": [_buy("0xwhale", "tok1", ts=2_000_000_000)]}
    eng = CopyEngine(data=FakeData(board, fills), gamma=FakeGamma(), executor=PaperExecutor(),
                     settings=_settings())

    eng.run_once()
    second = eng.run_once()  # same fill, cursor already advanced

    assert second.copies_made == 0
    with session_scope() as s:
        assert s.query(CopyTrade).count() == 1


def test_sell_with_no_position_is_ignored(temp_db):
    # A trader's SELL on a token we don't hold opens nothing and clutters nothing.
    _seed_trader("0xwhale")
    board = [LeaderboardTrader(wallet="0xwhale", username="w", pnl=1, volume=1, rank=1)]
    sell = SourceTrade(id="s1", wallet="0xwhale", token_id="tok1", condition_id="c1",
                       side="SELL", price=0.4, shares=250, timestamp=2_000_000_000)
    eng = CopyEngine(data=FakeData(board, {"0xwhale": [sell]}), gamma=FakeGamma(),
                     executor=PaperExecutor(), settings=_settings())

    report = eng.run_once()

    assert report.copies_made == 0
    with session_scope() as s:
        assert s.query(CopyTrade).count() == 0
        assert s.get(Position, "tok1") is None


def test_slippage_guard_skips(temp_db):
    _seed_trader("0xwhale")
    board = [LeaderboardTrader(wallet="0xwhale", username="w", pnl=1, volume=1, rank=1)]
    # Their fill at 0.40 but current ask is 0.60 -> 50% slippage > 5% cap.
    fills = {"0xwhale": [_buy("0xwhale", "tok1", ts=2_000_000_000, price=0.40)]}
    eng = CopyEngine(data=FakeData(board, fills), gamma=FakeGamma(bid=0.59, ask=0.61),
                     executor=PaperExecutor(), settings=_settings())

    report = eng.run_once()

    assert report.copies_made == 0
    with session_scope() as s:
        ct = s.query(CopyTrade).one()
        assert ct.status == "skipped" and "slippage" in ct.skip_reason


def test_paused_does_nothing(temp_db):
    with session_scope() as s:
        get_state(s).paused = True
    board = [LeaderboardTrader(wallet="0xwhale", username="w", pnl=1, volume=1, rank=1)]
    fills = {"0xwhale": [_buy("0xwhale", "tok1", ts=2_000_000_000)]}
    eng = CopyEngine(data=FakeData(board, fills), gamma=FakeGamma(), executor=PaperExecutor(),
                     settings=_settings())

    report = eng.run_once()

    assert "paused" in report.errors
    with session_scope() as s:
        assert s.query(CopyTrade).count() == 0


def test_closed_market_skipped(temp_db):
    _seed_trader("0xwhale")
    board = [LeaderboardTrader(wallet="0xwhale", username="w", pnl=1, volume=1, rank=1)]
    fills = {"0xwhale": [_buy("0xwhale", "tok1", ts=2_000_000_000)]}
    eng = CopyEngine(data=FakeData(board, fills), gamma=FakeGamma(tradeable=False),
                     executor=PaperExecutor(), settings=_settings())

    report = eng.run_once()

    assert report.copies_made == 0
    with session_scope() as s:
        ct = s.query(CopyTrade).one()
        assert ct.status == "skipped" and "closed" in ct.skip_reason
