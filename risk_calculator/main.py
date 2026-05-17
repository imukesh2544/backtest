"""
Risk Calculator Microservice — Flask Backend
All calculation logic, REST API, export, journal, metrics
"""

import os, json, csv, time, uuid, logging, io
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, send_file, render_template, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
import jwt

# ── CONFIG ────────────────────────────────────────────────────
APP_PREFIX  = os.environ.get("APP_PREFIX", "/risk-calculator")
PORT        = int(os.environ.get("PORT", 6000))
LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO")
JWT_SECRET  = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod")
SECRET_KEY  = os.environ.get("SECRET_KEY", "dev-flask-secret")
EXPORT_DIR  = Path(os.environ.get("EXPORT_DIR", "./exports"))
CORS_ORIGINS= os.environ.get("CORS_ORIGINS", "*")
RATE_LIMIT  = os.environ.get("RATE_LIMIT", "100 per minute")

EXPORT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s","service":"risk-calculator"}'
)
log = logging.getLogger("risk-calc")

# ── APP ───────────────────────────────────────────────────────
app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.secret_key = SECRET_KEY
app.config["APPLICATION_ROOT"] = APP_PREFIX

CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}})

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[RATE_LIMIT],
    storage_uri="memory://",
)

# ── PROMETHEUS METRICS ────────────────────────────────────────
REQUEST_COUNT  = Counter("risk_requests_total", "Total requests", ["method", "endpoint", "status"])
REQUEST_LATENCY= Histogram("risk_request_duration_seconds", "Request duration", ["endpoint"])
CALC_COUNT     = Counter("risk_calculations_total", "Total calculations", ["calc_type"])

# ── IN-MEMORY JOURNAL (replace with DB in production) ─────────
_journal = []  # list of trade dicts

# ── HELPERS ───────────────────────────────────────────────────
def ok(data, code=200):
    return jsonify({"status": "ok", "data": data}), code

def err(msg, code=400):
    return jsonify({"status": "error", "error": msg}), code

def require_float(*keys, body=None):
    """Extract and validate floats from body dict."""
    body = body or {}
    result = {}
    for k in keys:
        try:
            result[k] = float(body[k])
        except (KeyError, TypeError, ValueError):
            return None, f"Missing or invalid field: {k}"
    return result, None

def log_request(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        start = time.time()
        response = f(*args, **kwargs)
        duration = time.time() - start
        status = response[1] if isinstance(response, tuple) else 200
        REQUEST_COUNT.labels(request.method, request.path, status).inc()
        REQUEST_LATENCY.labels(request.path).observe(duration)
        log.info(f"{request.method} {request.path} {status} {duration:.3f}s ip={request.remote_addr}")
        return response
    return decorated

# ── SECURITY HEADERS ─────────────────────────────────────────
@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]         = "SAMEORIGIN"
    response.headers["X-XSS-Protection"]        = "1; mode=block"
    response.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
    response.headers["Cache-Control"]            = "no-store"
    return response

# ════════════════════════════════════════════════════════════════
#  HEALTH + METRICS
# ════════════════════════════════════════════════════════════════
@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "risk-calculator", "ts": datetime.utcnow().isoformat()})

@app.route("/metrics")
def metrics():
    return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

# ════════════════════════════════════════════════════════════════
#  FRONTEND
# ════════════════════════════════════════════════════════════════
@app.route("/risk-calculator")
@app.route("/risk-calculator/")
def index():
    return render_template("risk_index.html", prefix=APP_PREFIX)

# ════════════════════════════════════════════════════════════════
#  API — POSITION SIZE CALCULATOR
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/position-size", methods=["POST"])
@limiter.limit(RATE_LIMIT)
@log_request
def position_size():
    """
    Calculate optimal position size based on risk %.
    Body: { account_size, risk_pct, entry, stop_loss }
    Returns: { shares, position_value, risk_amount, risk_per_share }
    """
    body = request.get_json(force=True, silent=True) or {}
    vals, e = require_float("account_size","risk_pct","entry","stop_loss", body=body)
    if e: return err(e)

    acc   = vals["account_size"]
    rpct  = vals["risk_pct"]
    entry = vals["entry"]
    sl    = vals["stop_loss"]

    if acc <= 0:  return err("account_size must be > 0")
    if rpct <= 0 or rpct > 100: return err("risk_pct must be between 0 and 100")
    if entry <= 0: return err("entry must be > 0")
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0: return err("entry and stop_loss cannot be equal")

    risk_amount    = acc * (rpct / 100)
    shares         = int(risk_amount / risk_per_share)
    position_value = round(shares * entry, 2)
    actual_risk    = round(shares * risk_per_share, 2)
    actual_risk_pct= round(actual_risk / acc * 100, 2)

    CALC_COUNT.labels("position_size").inc()
    return ok({
        "shares":          shares,
        "position_value":  position_value,
        "risk_amount":     actual_risk,
        "risk_pct_actual": actual_risk_pct,
        "risk_per_share":  round(risk_per_share, 2),
        "max_loss":        actual_risk,
    })

# ════════════════════════════════════════════════════════════════
#  API — RISK TO REWARD
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/rr", methods=["POST"])
@log_request
def risk_reward():
    """
    Body: { entry, stop_loss, target, shares (optional) }
    """
    body = request.get_json(force=True, silent=True) or {}
    vals, e = require_float("entry","stop_loss","target", body=body)
    if e: return err(e)

    entry  = vals["entry"]
    sl     = vals["stop_loss"]
    target = vals["target"]
    shares = int(body.get("shares", 1))

    risk   = abs(entry - sl)
    reward = abs(target - entry)
    rr     = round(reward / risk, 2) if risk > 0 else 0

    win_pnl  = round(reward * shares, 2)
    loss_pnl = round(-risk * shares, 2)

    # Breakeven win rate
    breakeven_wr = round(1 / (1 + rr) * 100, 2) if rr > 0 else 100

    CALC_COUNT.labels("rr").inc()
    return ok({
        "risk":            round(risk, 2),
        "reward":          round(reward, 2),
        "rr_ratio":        rr,
        "rr_label":        f"1 : {rr}",
        "win_pnl":         win_pnl,
        "loss_pnl":        loss_pnl,
        "breakeven_wr_pct":breakeven_wr,
        "entry":           entry,
        "stop_loss":       sl,
        "target":          target,
    })

# ════════════════════════════════════════════════════════════════
#  API — STOP LOSS CALCULATOR
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/stop-loss", methods=["POST"])
@log_request
def stop_loss_calc():
    """
    Body: { entry, risk_pct, account_size, direction (BUY/SELL) }
    Returns ATR-based and pct-based stop levels.
    """
    body = request.get_json(force=True, silent=True) or {}
    vals, e = require_float("entry","risk_pct","account_size", body=body)
    if e: return err(e)

    entry    = vals["entry"]
    risk_pct = vals["risk_pct"]
    acc      = vals["account_size"]
    direction= body.get("direction", "BUY").upper()
    atr      = float(body.get("atr", 0)) or None

    risk_amount = acc * (risk_pct / 100)

    # Percentage-based SL
    pct_sl_dist = entry * (risk_pct / 100)
    sl_pct = round(entry - pct_sl_dist, 2) if direction == "BUY" else round(entry + pct_sl_dist, 2)

    # ATR-based SL (1.5× ATR)
    sl_atr = None
    if atr:
        sl_atr = round(entry - 1.5*atr, 2) if direction == "BUY" else round(entry + 1.5*atr, 2)

    CALC_COUNT.labels("stop_loss").inc()
    return ok({
        "entry":          entry,
        "direction":      direction,
        "sl_pct_based":   sl_pct,
        "sl_atr_based":   sl_atr,
        "risk_amount":    round(risk_amount, 2),
        "risk_pct":       risk_pct,
    })

# ════════════════════════════════════════════════════════════════
#  API — LEVERAGE CALCULATOR
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/leverage", methods=["POST"])
@log_request
def leverage_calc():
    """
    Body: { margin, position_value } OR { position_value, leverage }
    """
    body = request.get_json(force=True, silent=True) or {}

    if "margin" in body and "position_value" in body:
        margin = float(body["margin"])
        pos_val= float(body["position_value"])
        leverage = round(pos_val / margin, 2) if margin > 0 else 0
        margin_used = margin
    elif "position_value" in body and "leverage" in body:
        pos_val  = float(body["position_value"])
        leverage = float(body["leverage"])
        margin_used = round(pos_val / leverage, 2) if leverage > 0 else 0
    else:
        return err("Provide (margin + position_value) or (position_value + leverage)")

    # Liquidation estimates (rough: liq when loss = margin)
    margin_pct = round(100 / leverage, 2) if leverage > 0 else 0

    CALC_COUNT.labels("leverage").inc()
    return ok({
        "leverage":         leverage,
        "position_value":   round(pos_val, 2),
        "margin_required":  margin_used,
        "margin_pct":       margin_pct,
        "effective_exposure": round(pos_val, 2),
    })

# ════════════════════════════════════════════════════════════════
#  API — FUTURES LIQUIDATION CALCULATOR
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/liquidation", methods=["POST"])
@log_request
def liquidation_calc():
    """
    Body: { entry, leverage, direction (LONG/SHORT), maintenance_margin_pct }
    """
    body = request.get_json(force=True, silent=True) or {}
    vals, e = require_float("entry","leverage", body=body)
    if e: return err(e)

    entry     = vals["entry"]
    leverage  = vals["leverage"]
    direction = body.get("direction", "LONG").upper()
    mm_pct    = float(body.get("maintenance_margin_pct", 0.5)) / 100  # default 0.5%

    if leverage <= 0: return err("leverage must be > 0")

    init_margin = entry / leverage

    # Simplified liquidation price formula
    if direction == "LONG":
        liq_price = round(entry * (1 - (1/leverage) + mm_pct), 2)
        liq_drop_pct = round((entry - liq_price) / entry * 100, 2)
    else:
        liq_price = round(entry * (1 + (1/leverage) - mm_pct), 2)
        liq_drop_pct = round((liq_price - entry) / entry * 100, 2)

    CALC_COUNT.labels("liquidation").inc()
    return ok({
        "entry":            entry,
        "direction":        direction,
        "leverage":         leverage,
        "liq_price":        liq_price,
        "liq_distance_pct": liq_drop_pct,
        "init_margin":      round(init_margin, 2),
        "margin_pct":       round(100/leverage, 2),
    })

# ════════════════════════════════════════════════════════════════
#  API — PORTFOLIO RISK CALCULATOR
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/portfolio", methods=["POST"])
@log_request
def portfolio_risk():
    """
    Body: {
      account_size: float,
      positions: [
        { symbol, entry, current, shares, stop_loss }
      ]
    }
    """
    body = request.get_json(force=True, silent=True) or {}
    acc  = float(body.get("account_size", 0))
    positions = body.get("positions", [])

    if acc <= 0: return err("account_size must be > 0")
    if not positions: return err("positions list is required")

    total_exposure = 0
    total_risk     = 0
    total_pnl      = 0
    pos_results    = []

    for p in positions:
        try:
            sym     = p.get("symbol", "?")
            entry   = float(p["entry"])
            current = float(p.get("current", entry))
            shares  = int(p["shares"])
            sl      = float(p.get("stop_loss", entry * 0.97))

            exposure    = entry * shares
            risk        = abs(entry - sl) * shares
            pnl         = (current - entry) * shares
            pnl_pct     = round((current - entry) / entry * 100, 2)
            risk_pct_acc= round(risk / acc * 100, 2)

            total_exposure += exposure
            total_risk     += risk
            total_pnl      += pnl

            pos_results.append({
                "symbol":       sym,
                "exposure":     round(exposure, 2),
                "risk":         round(risk, 2),
                "risk_pct_acc": risk_pct_acc,
                "pnl":          round(pnl, 2),
                "pnl_pct":      pnl_pct,
            })
        except Exception as ex:
            pos_results.append({"symbol": p.get("symbol","?"), "error": str(ex)})

    CALC_COUNT.labels("portfolio").inc()
    return ok({
        "account_size":        acc,
        "total_exposure":      round(total_exposure, 2),
        "total_risk":          round(total_risk, 2),
        "total_risk_pct":      round(total_risk / acc * 100, 2),
        "total_pnl":           round(total_pnl, 2),
        "total_pnl_pct":       round(total_pnl / acc * 100, 2),
        "exposure_pct":        round(total_exposure / acc * 100, 2),
        "positions":           pos_results,
        "position_count":      len(pos_results),
    })

# ════════════════════════════════════════════════════════════════
#  API — DRAWDOWN CALCULATOR
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/drawdown", methods=["POST"])
@log_request
def drawdown_calc():
    """
    Body: { peak_value, current_value } OR { equity_curve: [v1, v2, ...] }
    """
    body = request.get_json(force=True, silent=True) or {}

    if "equity_curve" in body:
        curve = [float(v) for v in body["equity_curve"]]
        if len(curve) < 2: return err("equity_curve needs at least 2 values")

        peak = curve[0]
        max_dd = 0
        max_dd_pct = 0
        dd_periods = 0
        in_dd = False

        for v in curve:
            if v > peak:
                peak = v
                in_dd = False
            else:
                dd = peak - v
                dd_pct = dd / peak * 100
                if dd > max_dd:
                    max_dd = dd
                    max_dd_pct = dd_pct
                in_dd = True
                dd_periods += 1

        current_dd = round(peak - curve[-1], 2)
        current_dd_pct = round((peak - curve[-1]) / peak * 100, 2)

        CALC_COUNT.labels("drawdown_curve").inc()
        return ok({
            "max_drawdown":      round(max_dd, 2),
            "max_drawdown_pct":  round(max_dd_pct, 2),
            "current_drawdown":  current_dd,
            "current_dd_pct":    current_dd_pct,
            "peak_value":        peak,
            "current_value":     curve[-1],
            "dd_periods":        dd_periods,
        })

    else:
        vals, e = require_float("peak_value","current_value", body=body)
        if e: return err(e)
        peak = vals["peak_value"]
        curr = vals["current_value"]
        dd   = round(peak - curr, 2)
        dd_pct = round(dd / peak * 100, 2) if peak > 0 else 0
        recovery_needed_pct = round(dd / curr * 100, 2) if curr > 0 else 0

        # Number of wins needed to recover (assuming avg win = risk * rr)
        rr = float(body.get("rr_ratio", 2))
        risk_per_trade_pct = float(body.get("risk_per_trade_pct", 1))
        risk_amt = curr * (risk_per_trade_pct / 100)
        wins_to_recover = int(dd / (risk_amt * rr)) + 1 if risk_amt > 0 else 0

        CALC_COUNT.labels("drawdown_simple").inc()
        return ok({
            "peak_value":           peak,
            "current_value":        curr,
            "drawdown":             dd,
            "drawdown_pct":         dd_pct,
            "recovery_needed_pct":  recovery_needed_pct,
            "wins_to_recover":      wins_to_recover,
        })

# ════════════════════════════════════════════════════════════════
#  API — TRADE JOURNAL
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/journal", methods=["GET"])
@log_request
def journal_get():
    sort_by = request.args.get("sort", "date")
    page    = int(request.args.get("page", 1))
    per_pg  = int(request.args.get("per_page", 20))

    data = sorted(_journal, key=lambda x: x.get(sort_by, ""), reverse=True)
    total = len(data)
    start = (page - 1) * per_pg
    page_data = data[start:start + per_pg]

    wins   = [t for t in _journal if t.get("pnl", 0) > 0]
    losses = [t for t in _journal if t.get("pnl", 0) < 0]
    net_pnl= round(sum(t.get("pnl", 0) for t in _journal), 2)
    win_rate = round(len(wins)/len(_journal)*100, 1) if _journal else 0

    return ok({
        "trades":    page_data,
        "total":     total,
        "page":      page,
        "per_page":  per_pg,
        "summary": {
            "total_trades": total,
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     win_rate,
            "net_pnl":      net_pnl,
            "avg_win":      round(sum(t["pnl"] for t in wins)/len(wins), 2) if wins else 0,
            "avg_loss":     round(sum(t["pnl"] for t in losses)/len(losses), 2) if losses else 0,
        }
    })

@app.route("/api/risk/journal", methods=["POST"])
@log_request
def journal_add():
    body = request.get_json(force=True, silent=True) or {}
    required = ["symbol","direction","entry","exit","shares","date"]
    for f in required:
        if f not in body:
            return err(f"Missing field: {f}")

    entry  = float(body["entry"])
    exit_p = float(body["exit"])
    shares = int(body["shares"])
    pnl    = round((exit_p - entry) * shares if body["direction"].upper() == "BUY" else (entry - exit_p) * shares, 2)

    trade = {
        "id":        str(uuid.uuid4()),
        "symbol":    body["symbol"].upper(),
        "direction": body["direction"].upper(),
        "entry":     entry,
        "exit":      exit_p,
        "shares":    shares,
        "pnl":       pnl,
        "date":      body["date"],
        "notes":     body.get("notes", ""),
        "strategy":  body.get("strategy", ""),
        "setup":     body.get("setup", ""),
        "created_at":datetime.utcnow().isoformat(),
    }
    _journal.append(trade)
    return ok(trade, 201)

@app.route("/api/risk/journal/<trade_id>", methods=["DELETE"])
@log_request
def journal_delete(trade_id):
    global _journal
    before = len(_journal)
    _journal = [t for t in _journal if t["id"] != trade_id]
    if len(_journal) == before:
        return err("Trade not found", 404)
    return ok({"deleted": trade_id})

# ════════════════════════════════════════════════════════════════
#  API — CALCULATE (unified endpoint)
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/calculate", methods=["POST"])
@log_request
def calculate():
    """
    Unified calculator endpoint.
    Body: { type: "position_size|rr|stop_loss|leverage|liquidation", ...params }
    """
    body = request.get_json(force=True, silent=True) or {}
    calc_type = body.get("type", "").lower()

    routes = {
        "position_size": position_size,
        "rr":            risk_reward,
        "risk_reward":   risk_reward,
        "stop_loss":     stop_loss_calc,
        "leverage":      leverage_calc,
        "liquidation":   liquidation_calc,
        "drawdown":      drawdown_calc,
    }

    if calc_type not in routes:
        return err(f"Unknown calc type. Choose: {list(routes.keys())}")

    # Delegate to appropriate handler
    with app.test_request_context(
        "/api/risk/calculate", method="POST",
        json=body, content_type="application/json"
    ):
        return routes[calc_type]()

# ════════════════════════════════════════════════════════════════
#  API — EXPORT
# ════════════════════════════════════════════════════════════════
@app.route("/api/risk/export/csv", methods=["GET"])
@log_request
def export_csv():
    """Export trade journal as CSV."""
    if not _journal:
        return err("No trades to export")

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id","date","symbol","direction","entry","exit","shares","pnl","strategy","setup","notes"
    ])
    writer.writeheader()
    for t in _journal:
        writer.writerow({k: t.get(k,"") for k in writer.fieldnames})

    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"trade_journal_{datetime.now().strftime('%Y%m%d')}.csv"
    )

@app.route("/api/risk/export/json", methods=["GET"])
@log_request
def export_json():
    """Export trade journal as JSON."""
    if not _journal:
        return err("No trades to export")
    output = json.dumps({"trades": _journal, "exported_at": datetime.utcnow().isoformat()}, indent=2)
    return send_file(
        io.BytesIO(output.encode()),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"trade_journal_{datetime.now().strftime('%Y%m%d')}.json"
    )

# ════════════════════════════════════════════════════════════════
#  ERROR HANDLERS
# ════════════════════════════════════════════════════════════════
@app.errorhandler(404)
def not_found(e):
    return err("Endpoint not found", 404)

@app.errorhandler(429)
def rate_limited(e):
    return err("Rate limit exceeded. Try again later.", 429)

@app.errorhandler(500)
def server_error(e):
    log.error(f"500 error: {e}")
    return err("Internal server error", 500)

# ════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info(f"Risk Calculator starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
