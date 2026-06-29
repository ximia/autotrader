"""Live-mode guards: cash clamp and the funded/approved readiness gate."""
from app.config import Settings
from app.copier.engine import CopyEngine
from app.db import get_state, session_scope
from app.models import CopyTrade, Position, Trader
from app.polymarket.data_client import LeaderboardTrader, SourceTrade
from app.polymarket.gamma_client import MarketInfo


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
    def market_for_token(self, token_id, use_cache=True):
        return MarketInfo(token_id=token_id, condition_id="c1", question="Q?", outcome="Yes",
                          active=True, closed=False, accepting_orders=True,
                          best_bid=0.395, best_ask=0.405)

    def close(self):
        pass


class FakeLiveExecutor:
    """Stands in for LiveExecutor: configurable cash + readiness, records buys."""
    is_live = True

    def __init__(self, cash, ready=(True, "ready")):
        self._cash, self._ready = cash, ready
        self.buys = []

    def available_cash(self):
        return self._cash

    def readiness(self):
        return self._ready

    def live_price(self, token_id, side):
        return None

    def buy(self, token_id, usd, ref_price):
        from app.copier.executor import FillResult
        self.buys.append((token_id, usd))
        shares = usd / ref_price if ref_price else 0
        return FillResult("submitted", ref_price, shares, usd, order_id="x", is_live=True)

    def sell(self, token_id, shares, ref_price):
        from app.copier.executor import FillResult
        return FillResult("submitted", ref_price, shares, shares * ref_price, order_id="y", is_live=True)


def _settings(**over):
    base = dict(demo_mode=False, live_trading=True, wallet_address="0xfunder",
                private_key="0xkey", top_n=2, min_trade_usd=10.0, min_order_usd=1.0,
                max_per_trade_usd=25.0, max_daily_spend_usd=500.0, max_open_positions=25,
                max_slippage_pct=0.05, copy_ratio=1.0, enable_auto_exits=False,
                mirror_exits=False, max_entry_price=0.97)
    base.update(over)
    return Settings(**base)


def _seed(wallet):
    with session_scope() as s:
        s.add(Trader(wallet=wallet, last_seen_ts=0, tracked=True, portfolio_value=10000.0))


def _buy(wallet, token, ts):
    return SourceTrade(id=f"{token}:{ts}", wallet=wallet, token_id=token, condition_id="c1",
                       side="BUY", price=0.40, shares=250, timestamp=ts,
                       market_question="Q?", outcome="Yes")


def test_not_ready_blocks_all_orders(temp_db):
    _seed("0xw")
    board = [LeaderboardTrader(wallet="0xw", username="w", pnl=1, volume=1, rank=1)]
    ex = FakeLiveExecutor(cash=100.0, ready=(False, "wallet has no USDC on Polygon"))
    eng = CopyEngine(data=FakeData(board, {"0xw": [_buy("0xw", "tok1", 2_000_000_000)]}),
                     gamma=FakeGamma(), executor=ex, settings=_settings())

    report = eng.run_once()

    assert ex.buys == []  # nothing placed
    with session_scope() as s:
        assert s.query(CopyTrade).count() == 0
        state = get_state(s)
        assert state.live_ready is False
        assert "USDC" in state.live_reason


def test_order_clamped_to_available_cash(temp_db):
    # Their trade is a big fraction of their portfolio, so proportional sizing
    # wants more than the $4 USDC on hand -> clamped to $4.
    with session_scope() as s:
        s.add(Trader(wallet="0xw", last_seen_ts=0, tracked=True, portfolio_value=200.0))
    board = [LeaderboardTrader(wallet="0xw", username="w", pnl=1, volume=1, rank=1)]
    ex = FakeLiveExecutor(cash=4.0)
    # copy_ratio amplifies so sizing (>= $4) exceeds available cash.
    eng = CopyEngine(data=FakeData(board, {"0xw": [_buy("0xw", "tok1", 2_000_000_000)]}),
                     gamma=FakeGamma(), executor=ex, settings=_settings(copy_ratio=10.0))

    eng.run_once()

    assert len(ex.buys) == 1
    assert ex.buys[0][1] == 4.0  # clamped to available cash


def test_insufficient_cash_skips(temp_db):
    _seed("0xw")
    board = [LeaderboardTrader(wallet="0xw", username="w", pnl=1, volume=1, rank=1)]
    ex = FakeLiveExecutor(cash=0.50)  # below min_order_usd=1.0
    eng = CopyEngine(data=FakeData(board, {"0xw": [_buy("0xw", "tok1", 2_000_000_000)]}),
                     gamma=FakeGamma(), executor=ex, settings=_settings())

    report = eng.run_once()

    assert ex.buys == []
    with session_scope() as s:
        ct = s.query(CopyTrade).filter(CopyTrade.status == "skipped").one()
        assert "insufficient cash" in ct.skip_reason
