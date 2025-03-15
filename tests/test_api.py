import pandas as pd
import pytest
from fastapi.testclient import TestClient

from perplexify.api.app import create_app
from perplexify.api.service import DataService


class FakeService(DataService):
    """Offline data service: synthetic panels plus canned live snapshots."""

    def __init__(self, tmp):
        super().__init__(store=None)
        self.store = None
        self.tmp = tmp

    def get_market_data(self, exchange, symbols, interval, start=None, end=None):
        return super().get_market_data("synthetic", symbols, interval, start, end)

    def exchanges(self):
        return ["synthetic", "fakex"]

    def markets(self, exchange):
        if exchange != "fakex":
            raise KeyError(exchange)
        return pd.DataFrame(
            {
                "symbol": ["BTC", "DOGE", "DUST"],
                "mark_price": [80000.0, 0.2, 1.0],
                "index_price": [79990.0, 0.2, 1.0],
                "funding_rate": [0.0001, -0.0010, 0.01],
                "funding_interval_hours": [8.0, 8.0, 8.0],
                "funding_apr": [0.1095, -1.095, 10.95],
                "next_funding_time": [pd.Timestamp("2026-01-01", tz="UTC")] * 3,
                "open_interest": [1e9, 1e7, float("nan")],
                "volume_24h": [5e9, 2e8, 100.0],
            }
        )

    def funding_history(self, exchange, symbol, days):
        ts = pd.date_range("2026-01-01", periods=10, freq="8h", tz="UTC")
        return pd.DataFrame({"timestamp": ts, "funding_rate": 0.0001, "premium": float("nan"), "index_price": float("nan")}), 8.0

    def predicted_fundings(self):
        return pd.DataFrame(
            {
                "symbol": ["BTC", "BTC", "BTC", "ETH"],
                "venue": ["binance", "bybit", "hyperliquid", "binance"],
                "funding_rate": [0.0001, 0.0003, 0.0000125, 0.0001],
                "funding_interval_hours": [8, 8, 1, 8],
                "funding_apr": [0.1095, 0.3285, 0.1095, 0.1095],
                "next_funding_time": [pd.NaT] * 4,
            }
        )


@pytest.fixture
def client(tmp_path):
    app = create_app(FakeService(tmp_path))
    with TestClient(app) as c:
        yield c


def test_health_and_catalog(client):
    assert client.get("/health").json()["status"] == "ok"
    names = {s["name"] for s in client.get("/strategies").json()}
    assert "funding_carry" in names and "cross_sectional_carry" in names


def test_markets_and_opportunities(client):
    rows = client.get("/markets/fakex").json()
    assert rows[0]["symbol"] == "BTC" and rows[2]["open_interest"] is None
    assert client.get("/markets/nope").status_code == 404
    opp = client.get("/opportunities/fakex", params={"min_apr": 0.5, "min_volume": 1000}).json()
    assert [o["symbol"] for o in opp] == ["DOGE"] and opp[0]["side"] == "long_perp"
    xv = client.get("/opportunities/cross-venue/scan", params={"min_spread_apr": 0.1}).json()
    assert xv[0]["symbol"] == "BTC" and xv[0]["short_venue"] == "bybit" and xv[0]["spread_apr"] == pytest.approx(0.219)
    hist = client.get("/funding/fakex/btc/history", params={"days": 5}).json()
    assert len(hist["points"]) == 10 and hist["mean_apr"] == pytest.approx(0.1095)


def test_backtest_endpoint(client):
    body = {
        "exchange": "synthetic", "symbols": ["BTC", "ETH"], "interval": "8h",
        "strategy": {"name": "funding_carry", "params": {"entry_apr": 0.08}},
        "engine": {"risk": {"max_drawdown": 0.1}}, "max_points": 50,
    }
    r = client.post("/backtest", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["strategy"] == "funding_carry" and out["params"]["entry_apr"] == 0.08
    assert "annualized_return" in out["metrics"] and len(out["curve"]) <= 51
    assert out["data"]["symbols"] == ["BTC", "ETH"]
    bad = client.post("/backtest", json={**body, "strategy": {"name": "funding_carry", "params": {"zzz": 1}}})
    assert bad.status_code == 400
    assert client.post("/backtest", json={**body, "strategy": {"name": "unknown"}}).status_code == 400


def test_compare_endpoint_with_walk_forward(client):
    body = {
        "exchange": "synthetic", "symbols": ["BTC", "ETH", "SOL"], "interval": "8h",
        "strategies": [
            {"name": "funding_carry", "params": {"entry_apr": 0.05}},
            {"name": "cross_sectional_carry", "params": {"top_n": 2}},
            {"name": "buy_and_hold", "label": "bh"},
        ],
        "walk_forward_split": 0.6,
    }
    r = client.post("/backtest/compare", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert len(out["table"]) == 3 and {row["label"] for row in out["table"]} >= {"bh"}
    assert set(out["walk_forward"]["bh"]) == {"in_sample", "out_of_sample"}
    assert client.post("/backtest/compare", json={**body, "strategies": []}).status_code == 400


def test_bot_lifecycle(client, tmp_path):
    assert client.get("/bot/status").json() == {"running": False}
    body = {"exchange": "synthetic", "symbols": ["BTC", "ETH"], "interval": "8h", "poll_seconds": 3600,
            "strategy": {"name": "always_carry"}}
    r = client.post("/bot/start", json=body)
    assert r.status_code == 200, r.text
    assert client.post("/bot/start", json=body).status_code == 409
    st = client.post("/bot/tick").json()
    assert st["ticks"] >= 1 and st["fees_paid"] > 0
    pos = client.get("/bot/positions").json()
    assert pos["BTC"]["perp_weight"] < 0 < pos["BTC"]["hedge_weight"]
    st = client.post("/bot/tick").json()
    assert st["ticks"] >= 2
    assert len(client.get("/bot/ledger").json()) >= 2
    assert client.post("/bot/stop").json()["running"] is False
    assert client.post("/bot/stop").status_code == 409
