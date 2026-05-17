"""
Basic API tests for Risk Calculator microservice.
Run: pytest tests/ -v
"""
import pytest
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.main import app

@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c

# ── Health ─────────────────────────────────────────────────
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"

# ── Position Size ───────────────────────────────────────────
def test_position_size(client):
    r = client.post("/api/risk/position-size", json={
        "account_size": 100000,
        "risk_pct": 1,
        "entry": 500,
        "stop_loss": 490,
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["shares"] == 100
    assert d["risk_per_share"] == 10.0

def test_position_size_missing_field(client):
    r = client.post("/api/risk/position-size", json={"account_size": 100000})
    assert r.status_code == 400

def test_position_size_zero_risk(client):
    r = client.post("/api/risk/position-size", json={
        "account_size": 100000, "risk_pct": 1,
        "entry": 500, "stop_loss": 500,   # same as entry
    })
    assert r.status_code == 400

# ── Risk/Reward ─────────────────────────────────────────────
def test_rr(client):
    r = client.post("/api/risk/rr", json={
        "entry": 500, "stop_loss": 485, "target": 545, "shares": 100
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["rr_ratio"] == 3.0
    assert d["win_pnl"] == 4500.0
    assert d["loss_pnl"] == -1500.0

# ── Stop Loss ───────────────────────────────────────────────
def test_stop_loss(client):
    r = client.post("/api/risk/stop-loss", json={
        "entry": 500, "risk_pct": 2,
        "account_size": 100000, "direction": "BUY"
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["sl_pct_based"] == 490.0

# ── Liquidation ─────────────────────────────────────────────
def test_liquidation(client):
    r = client.post("/api/risk/liquidation", json={
        "entry": 45000, "leverage": 10,
        "direction": "LONG", "maintenance_margin_pct": 0.5
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["liq_price"] < 45000  # LONG liq must be below entry

# ── Portfolio ───────────────────────────────────────────────
def test_portfolio(client):
    r = client.post("/api/risk/portfolio", json={
        "account_size": 500000,
        "positions": [
            {"symbol": "INFY", "entry": 1500, "current": 1550, "shares": 100, "stop_loss": 1450},
            {"symbol": "TCS",  "entry": 3500, "current": 3400, "shares": 50,  "stop_loss": 3400},
        ]
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["position_count"] == 2
    assert d["total_exposure"] > 0

# ── Drawdown ────────────────────────────────────────────────
def test_drawdown(client):
    r = client.post("/api/risk/drawdown", json={
        "peak_value": 150000, "current_value": 120000
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["drawdown"] == 30000
    assert d["drawdown_pct"] == 20.0

def test_drawdown_curve(client):
    r = client.post("/api/risk/drawdown", json={
        "equity_curve": [100000, 110000, 105000, 120000, 108000, 130000]
    })
    assert r.status_code == 200
    d = r.get_json()["data"]
    assert d["max_drawdown"] > 0

# ── Journal ─────────────────────────────────────────────────
def test_journal_add_and_get(client):
    r = client.post("/api/risk/journal", json={
        "symbol": "RELIANCE", "direction": "BUY",
        "entry": 2500, "exit": 2600, "shares": 10,
        "date": "2026-05-17"
    })
    assert r.status_code == 201
    trade = r.get_json()["data"]
    assert trade["pnl"] == 1000.0
    assert trade["symbol"] == "RELIANCE"

    r2 = client.get("/api/risk/journal")
    assert r2.status_code == 200
    assert r2.get_json()["data"]["summary"]["total_trades"] >= 1

# ── Export ──────────────────────────────────────────────────
def test_export_csv_empty(client):
    # Fresh state — journal might be empty
    r = client.get("/api/risk/export/csv")
    # Either 200 (with data) or 400 (empty)
    assert r.status_code in [200, 400]
