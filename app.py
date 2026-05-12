"""
StratEdge — Universal Backtesting Backend
Flask API server with yfinance data fetching, disk caching,
indicator calculation, and full backtest engine.
"""

import os, json, time, hashlib, logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS

# ─── CONFIG ──────────────────────────────────────────────────────────────────
CACHE_DIR   = Path(os.environ.get("CACHE_DIR", "./cache"))
STOCKS_FILE = Path(os.environ.get("STOCKS_FILE", "./stocks.json"))
CACHE_TTL   = int(os.environ.get("CACHE_TTL_HOURS", "6")) * 3600   # 6h default
LOG_LEVEL   = os.environ.get("LOG_LEVEL", "INFO")

CACHE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("stratedge")

app = Flask(__name__, template_folder="templates")
CORS(app)

# ─── INTERVAL MAPPING ────────────────────────────────────────────────────────
# Maps UI label → (yfinance interval, yfinance period)
INTERVAL_MAP = {
    "1d":  ("1d",  "10y"),
    "1wk": ("1wk", "10y"),
    "1mo": ("1mo", "10y"),
    "60m": ("60m", "60d"),    # Yahoo only allows ~60 days for intraday
    "15m": ("15m", "60d"),
    "5m":  ("5m",  "60d"),
}

# ─── LOAD STOCK UNIVERSE ─────────────────────────────────────────────────────
def load_stocks():
    if STOCKS_FILE.exists():
        with open(STOCKS_FILE) as f:
            return json.load(f)
    log.warning("stocks.json not found — returning empty universe")
    return []

STOCKS = load_stocks()
# Build search index: [(name_lower, symbol_lower, entry), ...]
SEARCH_INDEX = [
    (s["name"].lower(), s["symbol"].lower(), s)
    for s in STOCKS
]

# ─── DISK CACHE ──────────────────────────────────────────────────────────────
def _cache_path(symbol: str, interval: str) -> Path:
    key = hashlib.md5(f"{symbol}:{interval}".encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


def _cache_valid(path: Path) -> bool:
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < CACHE_TTL


def cache_read(symbol: str, interval: str):
    p = _cache_path(symbol, interval)
    if _cache_valid(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return None


def cache_write(symbol: str, interval: str, data: dict):
    p = _cache_path(symbol, interval)
    try:
        with open(p, "w") as f:
            json.dump(data, f, separators=(",", ":"))
    except Exception as e:
        log.warning(f"Cache write failed: {e}")


def cache_bust(symbol: str, interval: str):
    p = _cache_path(symbol, interval)
    if p.exists():
        p.unlink()

# ─── DATA FETCHING ───────────────────────────────────────────────────────────
def fetch_ohlcv(symbol: str, interval: str) -> dict:
    """
    Fetch OHLCV data from Yahoo Finance.
    Returns dict with keys: symbol, interval, bars (list of OHLCV dicts)
    """
    yf_interval, yf_period = INTERVAL_MAP.get(interval, ("1d", "10y"))

    log.info(f"Fetching {symbol} interval={yf_interval} period={yf_period}")
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=yf_period, interval=yf_interval, auto_adjust=True)

        if df is None or df.empty:
            raise ValueError(f"No data returned for {symbol}")

        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        df = df[~df.index.duplicated(keep="first")]
        df = df.sort_index()

        bars = []
        for ts, row in df.iterrows():
            if hasattr(ts, "date"):
                t = ts.date().isoformat()
            else:
                t = str(ts)[:10]
            bars.append({
                "t": t,
                "o": round(float(row["Open"]),  2),
                "h": round(float(row["High"]),   2),
                "l": round(float(row["Low"]),    2),
                "c": round(float(row["Close"]),  2),
                "v": int(row.get("Volume", 0) or 0),
            })

        # Deduplicate by date (keep first)
        seen = set()
        unique = []
        for b in bars:
            if b["t"] not in seen:
                seen.add(b["t"])
                unique.append(b)

        return {
            "symbol":   symbol,
            "interval": interval,
            "fetched":  datetime.utcnow().isoformat(),
            "bars":     unique,
        }

    except Exception as e:
        log.error(f"fetch_ohlcv error for {symbol}: {e}")
        raise


# ─── INDICATORS ──────────────────────────────────────────────────────────────
def calc_ema(closes: list, period: int) -> list:
    k = 2 / (period + 1)
    result = [None] * len(closes)
    if len(closes) < period:
        return result
    ema = sum(closes[:period]) / period
    result[period - 1] = round(ema, 2)
    for i in range(period, len(closes)):
        ema = closes[i] * k + ema * (1 - k)
        result[i] = round(ema, 2)
    return result


def calc_rsi(closes: list, period: int = 14) -> list:
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0)
        losses.append(abs(d) if d < 0 else 0)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    rs = avg_gain / avg_loss if avg_loss else 100
    result[period] = round(100 - 100 / (1 + rs), 2)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0
        lo = abs(d) if d < 0 else 0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + lo) / period
        rs = avg_gain / avg_loss if avg_loss else 100
        result[i] = round(100 - 100 / (1 + rs), 2)
    return result


def add_indicators(bars: list, ema_period: int, rsi_period: int) -> list:
    """Attach ema and rsi values to each bar dict (returns new list, doesn't mutate)."""
    bars = [dict(b) for b in bars]   # shallow copy so we don't dirty the cache
    closes = [b["c"] for b in bars]
    emas   = calc_ema(closes, ema_period)
    rsis   = calc_rsi(closes, rsi_period)
    for i, b in enumerate(bars):
        b["ema"] = emas[i]
        b["rsi"] = rsis[i]
    return bars


# ─── BACKTEST ENGINE ─────────────────────────────────────────────────────────
def run_backtest(bars: list, params: dict) -> dict:
    """
    Full backtest engine.

    params keys:
      ema_period     int   (default 5)
      rsi_period     int   (default 14)
      rsi_threshold  float (default 30)
      rr_ratio       float (default 3.0)
      capital        float (default 100000)

    Returns full result dict including annotated bars for the frontend chart.
    """
    ema_period    = int(params.get("ema_period",    5))
    rsi_period    = int(params.get("rsi_period",    14))
    rsi_threshold = float(params.get("rsi_threshold", 30.0))
    rr_ratio      = float(params.get("rr_ratio",    3.0))
    capital_start = float(params.get("capital",     100000.0))

    # Attach indicators (returns a new list — cache is untouched)
    bars = add_indicators(bars, ema_period, rsi_period)

    capital = capital_start
    state   = "IDLE"
    pending = None
    active  = None
    trades  = []
    signals = []
    cancels = []
    carry   = []

    for i, day in enumerate(bars):
        ema = day["ema"]
        rsi = day["rsi"]

        # ── IDLE ─────────────────────────────────────────────────────────
        if state == "IDLE":
            if (ema is not None and rsi is not None
                    and day["h"] < ema and rsi < rsi_threshold):
                state = "PENDING"
                pending = {
                    "signal_date": day["t"],
                    "signal_high": day["h"],
                    "signal_low":  day["l"],
                    "signal_ema":  ema,
                    "signal_rsi":  rsi,
                    "signal_idx":  i,
                }
                signals.append({
                    "date": day["t"], "type": "SIGNAL",
                    "high": day["h"], "low": day["l"],
                    "ema": ema, "rsi": rsi,
                })

        # ── PENDING ──────────────────────────────────────────────────────
        elif state == "PENDING":
            tH = pending["signal_high"]
            sl = pending["signal_low"]

            ep = None; et = ""
            if day["o"] > tH:
                ep = day["o"]; et = "GAP_UP"
            elif day["h"] > tH:
                ep = tH;       et = "NORMAL"

            if ep is not None:
                risk   = round(ep - sl, 2)
                target = round(ep + rr_ratio * risk, 2)
                shares = max(1, int(capital / ep))

                active = {
                    "id":          len(trades) + 1,
                    "signal_date": pending["signal_date"],
                    "signal_high": pending["signal_high"],
                    "signal_low":  pending["signal_low"],
                    "signal_ema":  pending["signal_ema"],
                    "signal_rsi":  pending["signal_rsi"],
                    "entry_date":  day["t"],
                    "entry_price": ep,
                    "entry_type":  et,
                    "entry_ema":   ema,
                    "entry_rsi":   rsi,
                    "sl":          sl,
                    "target":      target,
                    "risk":        risk,
                    "shares":      shares,
                    "rr_ratio":    rr_ratio,
                    "entry_idx":   i,
                }
                pending = None
                state   = "IN_TRADE"

                # Same-day exit check
                exited = False
                if day["l"] <= sl:
                    xp = day["o"] if (et == "GAP_UP" and day["o"] <= sl) else sl
                    pnl = round((xp - ep) * shares, 2)
                    capital = round(capital + pnl, 2)
                    trades.append({**active,
                        "exit_date": day["t"], "exit_price": xp,
                        "exit_reason": "SL", "pnl": pnl,
                        "capital_after": capital, "hold_days": 0,
                    })
                    state = "IDLE"; active = None; exited = True

                elif day["h"] >= target:
                    xp = day["o"] if (et == "GAP_UP" and day["o"] >= target) else target
                    pnl = round((xp - ep) * shares, 2)
                    capital = round(capital + pnl, 2)
                    trades.append({**active,
                        "exit_date": day["t"], "exit_price": xp,
                        "exit_reason": "TARGET", "pnl": pnl,
                        "capital_after": capital, "hold_days": 0,
                    })
                    state = "IDLE"; active = None; exited = True

            else:
                ema_ok = (ema is not None and day["h"] < ema)
                rsi_ok = (rsi is not None and rsi < rsi_threshold)

                if ema_ok and rsi_ok:
                    carry.append({"date": day["t"], "signal": pending["signal_date"]})
                else:
                    reason = (
                        f"EMA breach: H={day['h']} ≥ EMA={ema}"
                        if not ema_ok
                        else f"RSI={rsi} ≥ {rsi_threshold}"
                    )
                    cancels.append({
                        "date": day["t"],
                        "signal_date": pending["signal_date"],
                        "reason": reason,
                    })
                    signals.append({"date": day["t"], "type": "CANCEL", "reason": reason})
                    state = "IDLE"; pending = None

        # ── IN_TRADE ─────────────────────────────────────────────────────
        elif state == "IN_TRADE":
            e  = active["entry_price"]
            sl = active["sl"]
            tg = active["target"]
            sh = active["shares"]

            xp = None; xr = ""
            if   day["o"] <= sl:  xp = day["o"]; xr = "SL (Gap Down)"
            elif day["o"] >= tg:  xp = day["o"]; xr = "TARGET (Gap Up)"
            elif day["l"] <= sl:  xp = sl;        xr = "SL"
            elif day["h"] >= tg:  xp = tg;        xr = "TARGET"

            if xp is not None:
                pnl = round((xp - e) * sh, 2)
                capital = round(capital + pnl, 2)
                entry_i = active.get("entry_idx", i)
                trades.append({**active,
                    "exit_date":     day["t"],
                    "exit_price":    xp,
                    "exit_reason":   xr,
                    "pnl":           pnl,
                    "capital_after": capital,
                    "hold_days":     i - entry_i,
                })
                state = "IDLE"; active = None

    # Close open trade at last bar
    if state == "IN_TRADE" and active:
        last = bars[-1]
        pnl  = round((last["c"] - active["entry_price"]) * active["shares"], 2)
        capital = round(capital + pnl, 2)
        entry_i = active.get("entry_idx", len(bars) - 1)
        trades.append({**active,
            "exit_date":     last["t"],
            "exit_price":    last["c"],
            "exit_reason":   "EOD (Open P&L)",
            "pnl":           pnl,
            "capital_after": capital,
            "hold_days":     len(bars) - 1 - entry_i,
            "is_open":       True,
        })

    # ── METRICS ──────────────────────────────────────────────────────────────
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0 and not t.get("is_open")]

    net_pnl  = round(capital - capital_start, 2)
    ret_pct  = round(net_pnl / capital_start * 100, 2)
    win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0.0
    avg_win  = round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0.0
    avg_loss = round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0
    pf_ratio = round(abs(avg_win / avg_loss), 2) if avg_loss else 0.0

    max_dd = 0.0
    peak   = capital_start
    for t in trades:
        if t["capital_after"] > peak:
            peak = t["capital_after"]
        dd = t["capital_after"] - peak
        if dd < max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    equity = [{"date": bars[0]["t"], "capital": capital_start}]
    for t in trades:
        equity.append({"date": t["exit_date"], "capital": t["capital_after"]})

    return {
        "params": {
            "ema_period":    ema_period,
            "rsi_period":    rsi_period,
            "rsi_threshold": rsi_threshold,
            "rr_ratio":      rr_ratio,
            "capital":       capital_start,
        },
        "summary": {
            "total_trades":   len(trades),
            "wins":           len(wins),
            "losses":         len(losses),
            "win_rate":       win_rate,
            "net_pnl":        net_pnl,
            "return_pct":     ret_pct,
            "avg_win":        avg_win,
            "avg_loss":       avg_loss,
            "profit_factor":  pf_ratio,
            "max_drawdown":   max_dd,
            "total_signals":  len([s for s in signals if s["type"] == "SIGNAL"]),
            "cancels":        len(cancels),
            "final_capital":  round(capital, 2),
        },
        "trades":   trades,
        "signals":  signals,
        "cancels":  cancels,
        "equity":   equity,
        # ── FIX: include indicator-annotated bars so frontend can render chart
        "bars":     bars,
    }


# ─── API ROUTES ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def search():
    """GET /api/search?q=DABUR&limit=10"""
    q     = request.args.get("q", "").strip().lower()
    limit = int(request.args.get("limit", 10))

    if len(q) < 1:
        return jsonify([])

    results = []
    for name, symbol, entry in SEARCH_INDEX:
        if symbol.startswith(q) or symbol.replace(".ns","").replace(".bo","") == q:
            results.append(entry)
    for name, symbol, entry in SEARCH_INDEX:
        if entry not in results and q in name:
            results.append(entry)
    for name, symbol, entry in SEARCH_INDEX:
        if entry not in results and q in symbol:
            results.append(entry)

    return jsonify(results[:limit])


@app.route("/api/data")
def get_data():
    """GET /api/data?symbol=DABUR.NS&interval=1d&refresh=false"""
    symbol   = request.args.get("symbol", "").strip().upper()
    interval = request.args.get("interval", "1d").strip()
    refresh  = request.args.get("refresh", "false").lower() == "true"

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    if interval not in INTERVAL_MAP:
        return jsonify({"error": f"interval must be one of {list(INTERVAL_MAP.keys())}"}), 400

    if refresh:
        cache_bust(symbol, interval)

    cached = cache_read(symbol, interval)
    if cached:
        log.info(f"Cache hit: {symbol} {interval}")
        bars = add_indicators(cached["bars"], 5, 14)
        cached["bars"] = bars
        return jsonify(cached)

    try:
        data = fetch_ohlcv(symbol, interval)
        cache_write(symbol, interval, data)
        data["bars"] = add_indicators(data["bars"], 5, 14)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/backtest", methods=["POST"])
def backtest():
    """
    POST /api/backtest
    {
      "symbol": "DABUR.NS", "interval": "1d",
      "ema_period": 5, "rsi_period": 14, "rsi_threshold": 30,
      "rr_ratio": 3.0, "capital": 100000
    }
    Returns full backtest result INCLUDING annotated bars for chart rendering.
    """
    body = request.get_json(force=True, silent=True) or {}

    symbol   = body.get("symbol", "").strip().upper()
    interval = body.get("interval", "1d").strip()

    if not symbol:
        return jsonify({"error": "symbol is required"}), 400

    params = {
        "ema_period":    body.get("ema_period",    5),
        "rsi_period":    body.get("rsi_period",    14),
        "rsi_threshold": body.get("rsi_threshold", 30.0),
        "rr_ratio":      body.get("rr_ratio",      3.0),
        "capital":       body.get("capital",       100000.0),
    }

    cached = cache_read(symbol, interval)
    if not cached:
        try:
            cached = fetch_ohlcv(symbol, interval)
            cache_write(symbol, interval, cached)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    result = run_backtest(list(cached["bars"]), params)
    result["symbol"]     = symbol
    result["interval"]   = interval
    result["total_bars"] = len(cached["bars"])

    return jsonify(result)


@app.route("/api/batch", methods=["POST"])
def batch_backtest():
    """
    POST /api/batch
    { "group": "NIFTY50", "interval": "1d", ... params ... }
    OR
    { "symbols": ["DABUR.NS", ...], ... }
    Returns summary rows — no full bars (keeps response small).
    """
    body = request.get_json(force=True, silent=True) or {}

    group    = body.get("group", "")
    symbols  = body.get("symbols", [])
    interval = body.get("interval", "1d")

    if group:
        symbols = [s["symbol"] for s in STOCKS if group in s.get("groups", [])]

    if not symbols:
        return jsonify({"error": "No symbols provided"}), 400

    params = {
        "ema_period":    body.get("ema_period",    5),
        "rsi_period":    body.get("rsi_period",    14),
        "rsi_threshold": body.get("rsi_threshold", 30.0),
        "rr_ratio":      body.get("rr_ratio",      3.0),
        "capital":       body.get("capital",       100000.0),
    }

    results = []
    for sym in symbols:
        sym = sym.strip().upper()
        try:
            cached = cache_read(sym, interval)
            if not cached:
                cached = fetch_ohlcv(sym, interval)
                cache_write(sym, interval, cached)
            bt = run_backtest(list(cached["bars"]), params)
            results.append({
                "symbol":        sym,
                "total_trades":  bt["summary"]["total_trades"],
                "wins":          bt["summary"]["wins"],
                "losses":        bt["summary"]["losses"],
                "win_rate":      bt["summary"]["win_rate"],
                "net_pnl":       bt["summary"]["net_pnl"],
                "return_pct":    bt["summary"]["return_pct"],
                "profit_factor": bt["summary"]["profit_factor"],
                "max_drawdown":  bt["summary"]["max_drawdown"],
                "total_signals": bt["summary"]["total_signals"],
                "status":        "ok",
            })
        except Exception as e:
            results.append({"symbol": sym, "status": "error", "error": str(e)})

    results.sort(key=lambda x: x.get("return_pct", -9999), reverse=True)
    return jsonify({"results": results, "count": len(results)})


@app.route("/api/groups")
def get_groups():
    """GET /api/groups — returns available stock group names + counts."""
    groups = {}
    for s in STOCKS:
        for g in s.get("groups", []):
            groups.setdefault(g, 0)
            groups[g] += 1
    return jsonify([{"name": k, "count": v} for k, v in sorted(groups.items())])


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
    """POST /api/cache/clear — clears all cached OHLCV data files."""
    count = 0
    for f in CACHE_DIR.glob("*.json"):
        f.unlink()
        count += 1
    return jsonify({"cleared": count})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "stocks": len(STOCKS), "cache_dir": str(CACHE_DIR)})


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "false").lower() == "true"
    log.info(f"StratEdge starting on port {port}  debug={debug}  stocks={len(STOCKS)}")
    app.run(host="0.0.0.0", port=port, debug=debug)
