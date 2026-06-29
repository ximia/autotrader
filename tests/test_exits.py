"""Tests for protective-exit rules (pure) and engine exit execution."""
from dataclasses import dataclass

from app.config import Settings
from app.copier.engine import CopyEngine
from app.copier.executor import PaperExecutor
from app.copier.exits import evaluate_exits
from app.db import get_state, session_scope
from app.models import CopyTrade, Position, Trader
from app.polymarket.data_client import LeaderboardTrader, SourceTrade
from app.polymarket.gamma_client import MarketInfo


@dataclass
class _Pos:
    token_id: str
    shares: float
    avg_price: float


def _prices(mapping):
    return lambda tid: mapping.get(tid)


def test_take_profit_triggers():
    pos = _Pos("t1", 100, 0.40)
    out = evaluate_exits([pos], price_fn=_prices({"t1": 0.62}),
                         take_profit_pct=0.5, stop_loss_pct=0.3,
                         enable_tp_sl=True, mirror_exits=True, trader_sold_tokens=set())
    assert len(out) == 1 and out[0].reason == "take_profit"


def test_stop_loss_triggers():
    pos = _Pos("t1", 100, 0.40)
    out = evaluate_exits([pos], price_fn=_prices({"t1": 0.27}),
                         take_profit_pct=0.5, stop_loss_pct=0.3,
                         enable_tp_sl=True, mirror_exits=True, trader_sold_tokens=set())
    assert len(out) == 1 and out[0].reason == "stop_loss"


def test_mirror_exit_takes_priority():
    pos = _Pos("t1", 100, 0.40)
    out = evaluate_exits([pos], price_fn=_prices({"t1": 0.41}),
                         take_profit_pct=0.5, stop_loss_pct=0.3,
                         enable_tp_sl=True, mirror_exits=True, trader_sold_tokens={"t1"})
    assert len(out) == 1 and out[0].reason == "mirror_exit"


def test_no_exit_within_band():
    pos = _Pos("t1", 100, 0.40)
    out = evaluate_exits([pos], price_fn=_prices({"t1": 0.45}),
                         take_profit_pct=0.5, stop_loss_pct=0.3,
                         enable_tp_sl=True, mirror_exits=True, trader_sold_tokens=set())
    assert out == []


def test_tp_sl_disabled_but_mirror_on():
    pos = _Pos("t1", 100, 0.40)
    out = evaluate_exits([pos], price_fn=_prices({"t1": 0.90}),  # would be TP
                         take_profit_pct=0.5, stop_loss_pct=0.3,
                         enable_tp_sl=False, mirror_exits=True, trader_sold_tokens=set())
    assert out == []  # TP/SL off and not mirrored


# ----------------------------- engine integration ----------------------------
class FakeData:
    def __init__(self, board, trades):
        self._board, self._trades = board, trades

    def leaderboard(self, window="MONTH", category="OVERALL", limit=20):
        return self._board

    def trades(self, user, limit=50, side=None, taker_only=True):
        return list(self._trades.get(user, []))

    def portfolio_value(self, user):
        return 10000.0

    def close(self):
        pass


class FakeGamma:
    def __init__(self, price=0.40):
        self.price = price

    def market_for_token(self, token_id, use_cache=True):
        return MarketInfo(token_id=token_id, condition_id="c1", question="Q?", outcome="Yes",
                          active=True, closed=False, accepting_orders=True,
                          best_bid=self.price - 0.005, best_ask=self.price + 0.005)

    def close(self):
        pass


def _settings(**over):
    base = dict(demo_mode=False, live_trading=False, top_n=2, min_trade_usd=10.0,
                min_order_usd=1.0, max_per_trade_usd=25.0, max_daily_spend_usd=500.0,
                max_open_positions=25, max_slippage_pct=0.05, copy_ratio=1.0,
                paper_bankroll=1000.0, enable_auto_exits=True, take_profit_pct=0.5,
                stop_loss_pct=0.3, mirror_exits=True, max_entry_price=0.97)
    base.update(over)
    return Settings(**base)


def _buy(wallet, token, ts, price=0.40, shares=250):
    return SourceTrade(id=f"{token}:{ts}", wallet=wallet, token_id=token, condition_id="c1",
                       side="BUY", price=price, shares=shares, timestamp=ts,
                       market_question="Q?", outcome="Yes")


def test_engine_take_profit_closes_position(temp_db):
    with session_scope() as s:
        s.add(Trader(wallet="0xw", last_seen_ts=0, tracked=True, portfolio_value=10000.0))
    board = [LeaderboardTrader(wallet="0xw", username="w", pnl=1, volume=1, rank=1)]
    fills = {"0xw": [_buy("0xw", "tok1", 2_000_000_000)]}

    # Cycle 1: open the position at ~0.40.
    eng = CopyEngine(data=FakeData(board, fills), gamma=FakeGamma(0.40),
                     executor=PaperExecutor(), settings=_settings())
    eng.run_once()
    with session_scope() as s:
        assert s.get(Position, "tok1").shares > 0

    # Cycle 2: price jumps to 0.70 (+75%) -> take-profit should close it.
    eng2 = CopyEngine(data=FakeData(board, {}), gamma=FakeGamma(0.70),
                      executor=PaperExecutor(), settings=_settings())
    report = eng2.run_once()

    assert report.exits_made == 1
    with session_scope() as s:
        pos = s.get(Position, "tok1")
        assert pos.closed and pos.shares == 0
        assert pos.realized_pnl_usd > 0  # sold higher than avg cost
        sell = s.query(CopyTrade).filter(CopyTrade.side == "SELL").one()
        assert "take_profit" in sell.skip_reason
        # paper cash back above the post-buy level
        assert get_state(s).paper_cash_usd > 990
