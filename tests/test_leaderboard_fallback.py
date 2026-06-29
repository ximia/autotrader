"""The leaderboard must fall back to the next endpoint when one fails/empties."""
import httpx

from app.polymarket.data_client import DataClient


def test_falls_back_to_second_endpoint():
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.host == "data-api.polymarket.com":
            return httpx.Response(404, json={"error": "gone"})
        # lb-api responds with the ranked list.
        return httpx.Response(200, json=[
            {"proxyWallet": "0xAAA", "userName": "w", "pnl": 10, "vol": 100, "rank": 1},
        ])

    dc = DataClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    rows = dc.leaderboard(limit=1)

    assert "data-api.polymarket.com" in calls
    assert "lb-api.polymarket.com" in calls
    assert len(rows) == 1 and rows[0].wallet == "0xaaa"


def test_returns_empty_when_all_fail():
    def handler(request):
        return httpx.Response(500)

    dc = DataClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert dc.leaderboard() == []
