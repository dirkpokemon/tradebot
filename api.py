from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os
import threading
import time
import requests
import sqlite3
from datetime import datetime
from bot import state, run_bot, get_setup_health, SETUP_TYPES, get_public_exchange, get_exchange
from dataclasses import asdict
from db import (clear_trades as db_clear_trades, get_trade_candles, save_review, load_reviews_summary,
                get_learning_proposals, save_learning_proposals, decide_proposal,
                get_learned_params, set_learned_param, delete_learned_param,
                get_learned_params_meta, count_suppressed_proposals,
                clear_rejected_proposals)
from learn import analyze_for_proposals
from autotune import run_autotune, autotune_state
from backtest import (
    BacktestConfig, backtest_state, run_backtest,
    monte_carlo_state, run_monte_carlo,
)
import math

app = FastAPI(title="BTC Trading Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

bot_thread: threading.Thread = None

class BotConfig(BaseModel):
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "15m"
    risk_per_trade: float = 0.01

@app.get("/status")
def get_status():
    trades_serialized = [asdict(t) for t in state.trades]
    return {
        "running": state.running,
        "sim_mode": state.sim_mode,
        "symbol": state.symbol,
        "timeframe": "15m",
        "risk_per_trade": state.risk_per_trade,
        "last_signal": state.last_signal,
        "last_setup": state.last_setup,
        "last_candle_time": state.last_candle_time,
        "balance": state.balance,
        "equity": state.equity,
        "total_pnl": state.total_pnl,
        "trades": trades_serialized,
        "open_trades": sum(1 for t in state.trades if t.status != "closed"),
        "closed_trades": sum(1 for t in state.trades if t.status == "closed"),
        "winning_trades": sum(1 for t in state.trades if t.status == "closed" and t.realized_pnl and t.realized_pnl > 0),
        "consecutive_stops": state.consecutive_stops,
        "circuit_breaker_active": bool(state.circuit_breaker_until and time.time() < state.circuit_breaker_until),
        "circuit_breaker_until": state.circuit_breaker_until if state.circuit_breaker_until and time.time() < state.circuit_breaker_until else None,
        "daily_loss_pct": round((state.equity - state.day_start_equity) / state.day_start_equity * 100, 2) if state.day_start_equity > 0 else 0.0,
        "disabled_setups": state.disabled_setups,
        "setup_health": {s: get_setup_health(s) for s in SETUP_TYPES},
        "trade_mode": state.trade_mode,
        "trade_direction": state.trade_direction,
        "last_analysis": state.last_analysis,
    }

@app.post("/start")
def start_bot(config: BotConfig):
    global bot_thread
    if state.running:
        raise HTTPException(status_code=400, detail="Bot is already running")
    state.symbol = config.symbol
    state.risk_per_trade = config.risk_per_trade
    state.running = True
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    return {"message": "Bot started", "config": config}

@app.post("/stop")
def stop_bot():
    if not state.running:
        raise HTTPException(status_code=400, detail="Bot is not running")
    state.running = False
    return {"message": "Bot stopping..."}

@app.get("/stats")
def get_stats():
    closed = [t for t in state.trades if t.status == "closed"]

    # Per-setup statistieken + gezondheid
    setup_stats = {}
    for setup in SETUP_TYPES:
        ts = [t for t in closed if t.setup_type == setup]
        wins   = [t for t in ts if t.realized_pnl > 0]
        losses = [t for t in ts if t.realized_pnl <= 0]
        gross_profit = sum(t.realized_pnl for t in wins)
        gross_loss   = abs(sum(t.realized_pnl for t in losses))
        health = get_setup_health(setup)
        setup_stats[setup] = {
            "count": len(ts),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(ts) * 100) if ts else 0,
            "avg_pnl": round(sum(t.realized_pnl for t in ts) / len(ts), 2) if ts else 0,
            "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
            "health": health['status'],
            "recent_win_rate": round(health['win_rate'] * 100) if health['win_rate'] is not None else None,
            "recent_trades": health['trades'],
        }

    # Dagelijkse PnL gegroepeerd op datum (YYYY-MM-DD)
    daily: dict = {}
    for t in closed:
        day = t.timestamp[:10]
        daily[day] = round(daily.get(day, 0.0) + t.realized_pnl, 2)
    daily_pnl = [{"date": k, "pnl": v} for k, v in sorted(daily.items())]

    # Sharpe ratio op basis van dagelijkse PnL (annualized, ≥2 dagen nodig)
    sharpe = None
    if len(daily_pnl) >= 2:
        returns = [d['pnl'] for d in daily_pnl]
        n = len(returns)
        mean_r = sum(returns) / n
        variance = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
        std_r = math.sqrt(variance) if variance > 0 else 0
        if std_r > 0:
            sharpe = round(mean_r / std_r * math.sqrt(252), 2)

    # Max drawdown vanuit equity history
    max_drawdown = None
    if len(state.equity_history) >= 2:
        equities = [e['equity'] for e in state.equity_history]
        peak = equities[0]
        max_dd = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        max_drawdown = round(max_dd * 100, 2)  # als percentage

    return {
        "equity_history": state.equity_history,
        "setup_stats": setup_stats,
        "daily_pnl": daily_pnl,
        "sharpe_ratio": sharpe,
        "max_drawdown_pct": max_drawdown,
    }


_TF_OKX = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m","1h":"1H","4h":"4H","1d":"1D"}
_TF_BINANCE = {"1m":"1m","5m":"5m","15m":"15m","30m":"30m","1h":"1h","4h":"4h","1d":"1d"}


def _candles_okx(timeframe: str, limit: int) -> list:
    """Directe OKX REST-aanroep — geen ccxt marktlijst nodig."""
    bar = _TF_OKX.get(timeframe, timeframe)
    url = f"https://www.okx.com/api/v5/market/candles?instId=BTC-USDT-SWAP&bar={bar}&limit={limit}"
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != "0":
        raise ValueError(f"OKX API fout: {data.get('msg', data)}")
    rows = data["data"]  # nieuwste eerst → omdraaien
    return [
        {"time": int(row[0]) // 1000, "open": float(row[1]), "high": float(row[2]),
         "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
        for row in reversed(rows)
    ]


def _candles_binance(timeframe: str, limit: int) -> list:
    """Binance futures als fallback — altijd publiek beschikbaar."""
    interval = _TF_BINANCE.get(timeframe, timeframe)
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval={interval}&limit={limit}"
    r = requests.get(url, timeout=8)
    r.raise_for_status()
    rows = r.json()
    return [
        {"time": int(row[0]) // 1000, "open": float(row[1]), "high": float(row[2]),
         "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])}
        for row in rows
    ]


@app.get("/candles")
def get_live_candles(timeframe: str = "15m", limit: int = 150):
    last_err = None
    for fn in [_candles_okx, _candles_binance]:
        try:
            return fn(timeframe, limit)
        except Exception as e:
            last_err = e
    raise HTTPException(status_code=500, detail=f"candles mislukt: {last_err}")


@app.get("/trades")
def get_trades():
    return [asdict(t) for t in state.trades]

@app.delete("/trades")
def clear_trades():
    if state.running:
        raise HTTPException(status_code=400, detail="Stop the bot before clearing trades")
    state.trades.clear()
    state.total_pnl = 0.0
    db_clear_trades()
    return {"message": "Trade history cleared"}


class ReviewRequest(BaseModel):
    label: str  # "good_entry" | "too_early" | "wrong_setup" | "bad_rr" | "false_signal"
    note: str = ""

@app.get("/trades/{trade_id}/candles")
def get_candles_for_trade(trade_id: str):
    candles, entry_ts = get_trade_candles(trade_id)
    if candles is None:
        raise HTTPException(status_code=404, detail="Geen candle snapshot beschikbaar")
    return {"candles": candles, "entry_ts": entry_ts}

@app.post("/trades/{trade_id}/review")
def post_review(trade_id: str, req: ReviewRequest):
    valid = {"good_entry", "too_early", "wrong_setup", "bad_rr", "false_signal", "good", "marginal", "bad"}
    if req.label not in valid:
        raise HTTPException(status_code=400, detail=f"Ongeldig label. Kies uit: {valid}")
    save_review(trade_id, req.label, req.note)
    # Also update the in-memory trade so the next /status poll reflects the review
    for t in state.trades:
        if t.id == trade_id:
            t.review_label = req.label
            t.review_note  = req.note
            break
    return {"message": "Review opgeslagen", "trade_id": trade_id, "label": req.label}

@app.get("/reviews/summary")
def get_reviews_summary():
    rows = load_reviews_summary()
    return {"reviews": rows, "total": len(rows)}


# ── Backtest endpoints ────────────────────────────────────────────────────────

class BacktestRequest(BaseModel):
    symbol: str           = "BTC/USDT:USDT"
    days: int             = 90
    test_pct: float       = 0.30
    risk_per_trade: float = 0.01
    starting_balance: float = 10000.0
    session_filter: bool  = True
    trade_mode: str       = "daytrade"  # 'daytrade' | 'scalp' | 'both'

def _run_backtest_thread(config: BacktestConfig):
    try:
        backtest_state.running  = True
        backtest_state.error    = ""
        backtest_state.result   = None
        backtest_state.progress = 0.0
        exchange = get_public_exchange() if state.sim_mode else get_exchange()
        result = run_backtest(config, exchange)
        backtest_state.result = asdict(result)
    except Exception as e:
        backtest_state.error = str(e)
        import logging; logging.getLogger(__name__).error(f"Backtest mislukt: {e}")
    finally:
        backtest_state.running  = False
        backtest_state.progress = 1.0

@app.post("/backtest")
def start_backtest(req: BacktestRequest):
    if backtest_state.running:
        raise HTTPException(status_code=400, detail="Backtest is al bezig")
    if req.trade_mode not in ('daytrade', 'scalp', 'both'):
        raise HTTPException(status_code=400, detail="trade_mode moet 'daytrade', 'scalp' of 'both' zijn")
    config = BacktestConfig(
        symbol=req.symbol,
        days=req.days,
        test_pct=req.test_pct,
        risk_per_trade=req.risk_per_trade,
        starting_balance=req.starting_balance,
        session_filter=req.session_filter,
        trade_mode=req.trade_mode,
    )
    t = threading.Thread(target=_run_backtest_thread, args=(config,), daemon=True)
    t.start()
    return {"message": f"Backtest gestart: {req.symbol} | {req.days}d | test={int(req.test_pct*100)}% | mode={req.trade_mode}"}

@app.get("/backtest")
def get_backtest():
    return {
        "running":  backtest_state.running,
        "progress": round(backtest_state.progress * 100),
        "error":    backtest_state.error,
        "result":   backtest_state.result,
    }


# ── Monte Carlo endpoints ─────────────────────────────────────────────────────

class MonteCarloRequest(BaseModel):
    n_simulations: int = 1000

def _run_mc_thread(trade_pnls: list, starting_balance: float, n_simulations: int):
    try:
        monte_carlo_state.running  = True
        monte_carlo_state.error    = ""
        monte_carlo_state.result   = None
        monte_carlo_state.progress = 0.0
        from dataclasses import asdict as _asdict
        result = run_monte_carlo(trade_pnls, starting_balance, n_simulations)
        monte_carlo_state.result = _asdict(result)
    except Exception as e:
        monte_carlo_state.error = str(e)
        import logging; logging.getLogger(__name__).error(f"Monte Carlo mislukt: {e}")
    finally:
        monte_carlo_state.running  = False
        monte_carlo_state.progress = 1.0

@app.post("/monte-carlo")
def start_monte_carlo(req: MonteCarloRequest):
    if monte_carlo_state.running:
        raise HTTPException(status_code=400, detail="Monte Carlo is al bezig")
    if not backtest_state.result:
        raise HTTPException(status_code=400, detail="Voer eerst een backtest uit")
    trades = backtest_state.result.get("trades", [])
    pnls   = [t["realized_pnl"] for t in trades if t.get("realized_pnl") is not None]
    if len(pnls) < 5:
        raise HTTPException(status_code=400, detail=f"Te weinig trades ({len(pnls)}) — minimaal 5 nodig")
    starting_balance = backtest_state.result.get("config", {}).get("starting_balance", 10000.0)
    t = threading.Thread(
        target=_run_mc_thread,
        args=(pnls, starting_balance, req.n_simulations),
        daemon=True,
    )
    t.start()
    return {"message": f"Monte Carlo gestart: {req.n_simulations} simulaties op {len(pnls)} trades"}

@app.get("/monte-carlo")
def get_monte_carlo():
    return {
        "running":  monte_carlo_state.running,
        "progress": round(monte_carlo_state.progress * 100),
        "error":    monte_carlo_state.error,
        "result":   monte_carlo_state.result,
    }


@app.on_event("startup")
async def start_monthly_autotune():
    t = threading.Thread(target=_monthly_autotune_loop, daemon=True)
    t.start()


@app.on_event("startup")
async def register_webhook():
    token    = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    rail_url = os.environ.get('RAILWAY_URL', '')
    if token and rail_url:
        webhook_url = f"{rail_url.rstrip('/')}/telegram/webhook"
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={"url": webhook_url}, timeout=5)
            import logging
            logging.getLogger(__name__).info(f"Telegram webhook geregistreerd: {webhook_url} → {r.json()}")
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Webhook registratie mislukt: {e}")


@app.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(request: Request):
    return {"ok": True}


@app.get("/learning_stats")
def get_learning_stats():
    db_path = os.environ.get('DB_PATH', 'trades.db')
    with sqlite3.connect(db_path) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM signal_reviews ORDER BY timestamp DESC").fetchall()
        trade_reviews = c.execute(
            "SELECT COUNT(*) FROM trades WHERE review_label IS NOT NULL"
        ).fetchone()[0]
    reviews = [dict(r) for r in rows]
    n = len(reviews)
    total = n + trade_reviews
    if n < 5:
        return {
            "message": f"Niet genoeg data ({total}/30 beoordelingen — {trade_reviews} via dashboard, {n} via Telegram)",
            "reviews": total,
            "trade_reviews": trade_reviews,
            "signal_reviews": n,
        }

    approved = [r for r in reviews if r['approved']]
    skipped  = [r for r in reviews if not r['approved']]
    factors  = ['score_atr_sl', 'score_trend_4h', 'score_trend_1h', 'score_volume',
                'score_level_clean', 'score_round_number', 'score_inside_doji']

    def avg_factor(lst, f):
        vals = [r[f] for r in lst if r[f] is not None]
        return round(sum(vals) / len(vals), 1) if vals else 0

    rejection_counts = {}
    for r in skipped:
        rr = r.get('rejection_reason') or 'unknown'
        rejection_counts[rr] = rejection_counts.get(rr, 0) + 1

    factor_comparison = {
        f: {
            'avg_approved': avg_factor(approved, f),
            'avg_skipped':  avg_factor(skipped, f),
        }
        for f in factors
    }

    suggestions = sorted(
        [(f, abs(v['avg_approved'] - v['avg_skipped'])) for f, v in factor_comparison.items()],
        key=lambda x: -x[1]
    )

    return {
        "total_reviews": n,
        "approved": len(approved),
        "skipped": len(skipped),
        "approval_rate": round(len(approved) / n * 100) if n else 0,
        "factor_comparison": factor_comparison,
        "rejection_reasons": rejection_counts,
        "top_differentiating_factors": [s[0] for s in suggestions[:3]],
        "ready_for_learning": n >= 30,
    }


# ── Self-learning endpoints ───────────────────────────────────────────────────

@app.get("/learning/proposals")
def get_proposals():
    proposals = get_learning_proposals()
    return {
        "proposals": proposals,
        "pending": sum(1 for p in proposals if p["status"] == "pending"),
        # Adviezen die je eerder afwees en daarom niet opnieuw worden voorgesteld
        "suppressed": count_suppressed_proposals(),
    }


@app.post("/learning/proposals/allow-rejected")
def allow_rejected_proposals():
    """Hef de onderdrukking op: eerder afgewezen adviezen mogen weer voorgesteld worden."""
    n = clear_rejected_proposals()
    return {"message": f"{n} eerdere afwijzing(en) gewist — deze adviezen kunnen weer terugkomen"}


@app.post("/learning/analyze")
def trigger_analysis():
    closed = sum(1 for t in state.trades if t.status == "closed")
    if closed < 30:
        return {"message": f"Niet genoeg data: {closed} gesloten trades (minimaal 30 nodig)", "proposals": 0}
    proposals = analyze_for_proposals(min_trades=30)
    if not proposals:
        return {
            "message": (f"Analyse voltooid over {closed} trades: geen verbetervoorstellen. "
                        f"De bot presteert op de gemeten punten (richting, modus, score) al goed genoeg."),
            "proposals": 0,
        }
    saved = save_learning_proposals(proposals)
    skipped = len(proposals) - saved
    if saved == 0:
        return {
            "message": (f"Analyse voltooid over {closed} trades: alleen adviezen die je eerder al "
                        f"afwees ({skipped}). Die worden niet opnieuw voorgesteld."),
            "proposals": 0,
        }
    msg = f"{saved} nieuwe voorstellen gegenereerd uit {closed} trades"
    if skipped:
        msg += f" ({skipped} eerder afgewezen advies overgeslagen)"
    return {"message": msg, "proposals": saved}


@app.post("/learning/proposals/{proposal_id}/accept")
def accept_proposal(proposal_id: str):
    proposals = get_learning_proposals()
    p = next((x for x in proposals if x["id"] == proposal_id), None)
    if not p:
        raise HTTPException(status_code=404, detail="Voorstel niet gevonden")
    if p["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Voorstel heeft al status: {p['status']}")
    ptype = p["type"]
    if ptype == "min_score_global":
        set_learned_param("min_score_global", p["proposed_value"])
    elif ptype == "disable_setup":
        params = get_learned_params()
        disabled = params.get("disabled_setups", [])
        if p["setup_type"] not in disabled:
            disabled.append(p["setup_type"])
        set_learned_param("disabled_setups", disabled)
        if p["setup_type"] not in state.disabled_setups:
            state.disabled_setups.append(p["setup_type"])
    elif ptype == "min_score_setup":
        params = get_learned_params()
        scores = params.get("setup_min_scores", {})
        scores[p["setup_type"]] = p["proposed_value"]
        set_learned_param("setup_min_scores", scores)
    elif ptype == "trade_direction":
        set_learned_param("trade_direction", p["proposed_value"])
        state.trade_direction = p["proposed_value"]
    elif ptype == "trade_mode":
        set_learned_param("trade_mode", p["proposed_value"])
        state.trade_mode = p["proposed_value"]
    elif ptype == "backtest_param":
        # setup_type draagt de parameternaam (sl_atr_mult | min_rr)
        if p["setup_type"] in ("sl_atr_mult", "min_rr"):
            set_learned_param(p["setup_type"], p["proposed_value"])
    elif ptype == "factor_insight":
        pass  # informatief — accepteren is alleen bevestigen, wijzigt geen parameters
    decide_proposal(proposal_id, "accepted")
    return {"message": f"Toegepast: {p['description']}", "type": ptype}


@app.post("/learning/proposals/{proposal_id}/reject")
def reject_proposal(proposal_id: str):
    proposals = get_learning_proposals()
    p = next((x for x in proposals if x["id"] == proposal_id), None)
    if not p:
        raise HTTPException(status_code=404, detail="Voorstel niet gevonden")
    decide_proposal(proposal_id, "rejected")
    return {"message": f"Afgewezen: {p['description']}"}


@app.get("/learning/params")
def get_active_params():
    """
    Actieve geleerde instellingen. `history` is op datum gesorteerd (nieuwste eerst)
    zodat het dashboard kan tonen welke wijziging als laatste is toegepast.
    Defaults staan erbij zodat zichtbaar is wat geldt als er nog niets geleerd is.
    """
    # Interne boekhouding (bv. wanneer autotune laatst draaide) is geen
    # strategie-instelling en hoort niet in het overzicht thuis.
    bookkeeping = {"last_autotune"}
    return {
        "params":   get_learned_params(),
        "history":  [h for h in get_learned_params_meta() if h["key"] not in bookkeeping],
        "defaults": {
            "sl_atr_mult":      1.5,
            "min_rr":           2.0,
            "min_score_global": 50,
            "trade_direction":  "both",
            "risk_per_trade":   state.risk_per_trade,
        },
    }


# ── Autotune (backtest-gedreven parameter-tuning) ────────────────────────────

def _run_autotune_thread():
    try:
        exchange = get_public_exchange() if state.sim_mode else get_exchange()
        proposals = run_autotune(exchange)
        if proposals:
            saved = save_learning_proposals(proposals)
            skipped = len(proposals) - saved
            if skipped:
                import logging
                logging.getLogger(__name__).info(
                    f"Autotune: {skipped} advies/adviezen overgeslagen (eerder afgewezen)"
                )
        set_learned_param("last_autotune", datetime.utcnow().isoformat())
    except Exception as e:
        import logging; logging.getLogger(__name__).error(f"Autotune-thread mislukt: {e}")


@app.post("/learning/autotune")
def start_autotune():
    if autotune_state.running:
        raise HTTPException(status_code=400, detail="Autotune draait al")
    if backtest_state.running:
        raise HTTPException(status_code=400, detail="Er draait al een backtest — wacht tot die klaar is")
    t = threading.Thread(target=_run_autotune_thread, daemon=True)
    t.start()
    return {"message": "Backtest-tuning gestart — dit duurt enkele minuten (5 backtests over 150 dagen)"}


@app.get("/learning/autotune")
def get_autotune_status():
    return {
        "running":  autotune_state.running,
        "progress": round(autotune_state.progress * 100),
        "step":     autotune_state.step,
        "error":    autotune_state.error,
        "last_run": autotune_state.last_run or get_learned_params().get("last_autotune"),
        "summary":  autotune_state.summary,
    }


def _monthly_autotune_loop():
    """Draait autotune automatisch als de laatste run ≥30 dagen geleden is (check elk uur)."""
    import logging
    log = logging.getLogger(__name__)
    time.sleep(120)  # startup even laten settelen
    while True:
        try:
            if not autotune_state.running and not backtest_state.running:
                last = get_learned_params().get("last_autotune")
                due = True
                if last:
                    try:
                        last_dt = datetime.fromisoformat(last)
                        due = (datetime.utcnow() - last_dt).days >= 30
                    except ValueError:
                        due = True
                if due:
                    log.info("Maandelijkse autotune gestart (laatste run ≥30 dagen geleden)")
                    _run_autotune_thread()
        except Exception as e:
            log.warning(f"Maandelijkse autotune-check mislukt: {e}")
        time.sleep(3600)


# ── SPA static files (dashboard/dist, built by nixpacks) ─────────────────────

_DIST = os.path.join(os.path.dirname(__file__), "dashboard", "dist")

@app.get("/", include_in_schema=False)
async def spa_root():
    index = os.path.join(_DIST, "index.html")
    if not os.path.isfile(index):
        return {"status": "API online", "dashboard": "not built — run: npm --prefix dashboard run build"}
    return FileResponse(index)

@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    index = os.path.join(_DIST, "index.html")
    if not os.path.isfile(index):
        return {"status": "API online", "dashboard": "not built"}
    file = os.path.join(_DIST, full_path)
    if os.path.isfile(file):
        return FileResponse(file)
    return FileResponse(index)
