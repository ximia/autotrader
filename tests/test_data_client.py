"""Tests for Data API response parsing (no network — httpx MockTransport)."""
import httpx

from app.polymarket.data_client import DataClient


def _client(handler) -> DataClient:
    transport = httpx.MockTransport(handler)
    return DataClient(base_url="https://data-api.test", client=httpx.Client(transport=transport))


def test_leaderboard_parsing():
    def handler(request):
        assert request.url.path == "/leaderboard"
        return httpx.Response(200, json=[
            {"proxyWallet": "0xABC", "userName": "whale", "pnl": "1234.5", "vol": "9999", "rank": 1},
            {"proxyWallet": "0xDEF", "userName": "shark", "pnl": 500, "vol": 100},
        ])

    dc = _client(handler)
    rows = dc.leaderboard(limit=2)
    assert len(rows) == 2
    assert rows[0].wallet == "0xabc"  # lower-cased
    assert rows[0].pnl == 1234.5
    assert rows[1].rank == 2  # filled from index


def test_trades_parsing_and_usd_size():
    def handler(request):
        return httpx.Response(200, json=[
            {"asset": "tok1", "conditionId": "c1", "side": "BUY", "price": "0.4",
             "size": "100", "timestamp": "1700000000", "title": "Will X?", "outcome": "Yes",
             "transactionHash": "0xhash"},
        ])

    dc = _client(handler)
    trades = dc.trades("0xabc")
    assert len(trades) == 1
    t = trades[0]
    assert t.token_id == "tok1"
    assert t.side == "BUY"
    assert t.usd_size == 40.0  # 0.4 * 100
    assert t.timestamp == 1700000000
    assert "0xhash" in t.id


def test_portfolio_value_handles_list_and_dict():
    def handler(request):
        return httpx.Response(200, json={"value": "750.25"})

    dc = _client(handler)
    assert dc.portfolio_value("0xabc") == 750.25
