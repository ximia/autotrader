"""The leaderboard should be re-fetched on a cadence, not every cycle."""
from app.config import Settings
from app.copier.engine import CopyEngine
from app.copier.executor import PaperExecutor
from app.polymarket.data_client import LeaderboardTrader
from app.polymarket.gamma_client import MarketInfo


class CountingData:
    def __init__(self):
        self.leaderboard_calls = 0

    def leaderboard(self, window="MONTH", category="OVERALL", limit=20):
        self.leaderboard_calls += 1
        return [LeaderboardTrader(wallet="0xw", username="w", pnl=1, volume=1, rank=1)]

    def trades(self, user, limit=50, side=None, taker_only=True):
        return []

    def portfolio_value(self, user):
        return 10000.0

    def close(self):
        pass


class FakeGamma:
    def market_for_token(self, token_id, use_cache=True):
        return MarketInfo(token_id, "c1", "Q?", "Yes", True, False, True, 0.4, 0.4)

    def close(self):
        pass


def _settings():
    return Settings(demo_mode=False, live_trading=False, top_n=1,
                    leaderboard_refresh_min=15.0, paper_bankroll=1000.0)


def test_leaderboard_cached_between_cycles(temp_db):
    data = CountingData()
    eng = CopyEngine(data=data, gamma=FakeGamma(), executor=PaperExecutor(),
                     settings=_settings())

    eng.run_once()
    eng.run_once()
    eng.run_once()

    # Fetched once; subsequent cycles reuse the tracked set from the DB.
    assert data.leaderboard_calls == 1
