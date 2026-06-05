import ccxt
import time
import logging
import os
import requests
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

from strategy import analyze, Signal, get_swing_points, calc_atr
from db import (init_db, save_trade, update_trade, load_trades, clear_trades as db_clear_trades,
                save_pending_signal, update_pending_signal_status)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class Trade:
    id: str
    symbol: str
    side: str
    setup_type: str
    entry_price: float
    quantity: float           # totale originele positie
    stop_loss: float          # dynamisch, verschuift mee
    tp1: float
    tp2: float
    tp3: float
    timestamp: str
    reason: str
    status: str = "open"      # open | partial_1 | partial_2 | partial_3 | closed
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    exit_price: Optional[float] = None
    realized_pnl: float = 0.0
    review_label: Optional[str] = None
    review_note: Optional[str] = None
    context_score: int = 0
    trade_mode: str = "daytrade"

@dataclass
class BotState:
    running: bool = False
    symbol: str = "BTC/USDT"
    risk_per_trade: float = 0.01
    sim_mode: bool = True
    sim_balance: float = 10000.0
    trades: list = field(default_factory=list)
    last_signal: str = "none"
    last_setup: str = "none"
    last_candle_time: str = ""
    balance: float = 0.0
    equity: float = 0.0
    total_pnl: float = 0.0
    # Circuit breaker
    consecutive_stops: int = 0
    circuit_breaker_until: float = 0.0   # Unix timestamp; 0.0 = inactief
    # Daily loss limit
    day_date: str = ""
    day_start_equity: float = 0.0
    # Equity history voor grafiek (max 500 punten)
    equity_history: list = field(default_factory=list)
    # Live price polling: timestamp van laatste 60s check
    last_live_check: float = 0.0
    # Setup gezondheid: setups die tijdelijk uitgeschakeld zijn
    disabled_setups: list = field(default_factory=list)  # ['rotation', 'range', ...]
    # Trade mode en human approval
    trade_mode: str = "daytrade"   # "daytrade" | "scalp" | "both"
    human_approval: bool = False
    pending_count: int = 0
    last_5m_ts: str = ""           # alleen gebruikt in 'both' mode

state = BotState()

import threading
_pending_lock = threading.Lock()
pending_signals: dict = {}  # signal_id → {signal, exchange, qty, candles_15m, expires_at}


def _record_equity():
    snap = (state.sim_balance + state.total_pnl) if state.sim_mode else state.equity
    state.equity_history.append({
        "ts": datetime.utcnow().strftime("%d/%m %H:%M"),
        "equity": round(snap, 2),
    })
    if len(state.equity_history) > 500:
        state.equity_history.pop(0)


def send_telegram(message: str):
    token   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        logger.warning(f"Telegram melding mislukt: {e}")


def send_approval_request(signal, exchange, qty, candles_15m_snap, trade_mode_override=None):
    """Stuur een goedkeuringsverzoek via Telegram voor een gevonden signaal."""
    token   = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '')
    if not token or not chat_id:
        logger.warning("Telegram niet geconfigureerd — goedkeuringsverzoek overgeslagen")
        return

    signal_id = f"S{int(time.time()) % 99999:05d}"
    now_t     = time.time()
    expires   = now_t + 600  # 10 minuten

    ps_record = {
        'id':                signal_id,
        'setup_type':        signal.setup_type,
        'side':              signal.side,
        'entry':             signal.entry,
        'stop_loss':         signal.stop_loss,
        'tp1':               signal.tp1,
        'tp2':               signal.tp2,
        'tp3':               signal.tp3,
        'reason':            signal.reason,
        'context_score':     signal.context_score,
        'context_breakdown': signal.context_breakdown,
        'symbol':            state.symbol,
        'quantity':          qty,
        'created_at':        now_t,
        'expires_at':        expires,
        'timestamp':         datetime.utcnow().isoformat(),
    }

    with _pending_lock:
        pending_signals[signal_id] = {
            'signal':               signal,
            'exchange':             exchange,
            'qty':                  qty,
            'candles_15m':          candles_15m_snap,
            'expires_at':           expires,
            'ps_record':            ps_record,
            'trade_mode_override':  trade_mode_override,
        }

    try:
        save_pending_signal(ps_record)
    except Exception as e:
        logger.warning(f"Kon pending signal niet opslaan in DB: {e}")

    breakdown = signal.context_breakdown
    message = (
        f"🔔 <b>SETUP GEVONDEN — GOEDKEURING NODIG</b>\n\n"
        f"<b>{signal.setup_type.upper()} {signal.side.upper()}</b> {state.symbol}\n"
        f"Entry: {signal.entry:.0f} | SL: {signal.stop_loss:.0f}\n"
        f"TP1: {signal.tp1:.0f} | TP2: {signal.tp2:.0f} | TP3: {signal.tp3:.0f}\n\n"
        f"📊 <b>Context Score: {signal.context_score}/100</b>\n"
        f"• 4H trend: {breakdown.get('trend_4h', 0)}pts\n"
        f"• 1H trend: {breakdown.get('trend_1h', 0)}pts\n"
        f"• Volume: {breakdown.get('volume', 0)}pts\n"
        f"• Level schoon: {breakdown.get('level_clean', 0)}pts\n"
        f"• Round number: {breakdown.get('round_number', 0)}pts\n"
        f"• Inside/Doji: {breakdown.get('inside_doji', 0)}pts\n"
        f"• ATR SL: {breakdown.get('atr_sl', 0)}pts\n\n"
        f"<i>{signal.reason}</i>\n\n"
        f"⏰ Vervalt in 10 minuten"
    )

    reply_markup = {"inline_keyboard": [[
        {"text": "✅ GOEDKEUREN", "callback_data": f"approve:{signal_id}"},
        {"text": "❌ OVERSLAAN",  "callback_data": f"skip:{signal_id}"},
    ]]}

    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id":      chat_id,
                "text":         message,
                "parse_mode":   "HTML",
                "reply_markup": reply_markup,
            },
            timeout=5,
        )
        logger.info(f"Goedkeuringsverzoek verstuurd: {signal_id} ({signal.setup_type} {signal.side})")
    except Exception as e:
        logger.warning(f"Telegram goedkeuringsverzoek mislukt: {e}")


def get_public_exchange():
    """OKX publieke API voor marktdata — geen auth nodig."""
    return ccxt.okx({'options': {'defaultType': 'spot'}})

def get_exchange():
    """OKX met auth — alleen nodig in LIVE mode voor orderplaatsing."""
    api_key    = os.environ.get('OKX_API_KEY', '')
    secret     = os.environ.get('OKX_SECRET', '')
    passphrase = os.environ.get('OKX_PASSPHRASE', '')
    sandbox    = os.environ.get('OKX_SANDBOX', 'false').lower() == 'true'

    params = {
        'apiKey':   api_key,
        'secret':   secret,
        'password': passphrase,
        'options':  {'defaultType': 'spot'},
    }
    if sandbox:
        params['headers'] = {'x-simulated-trading': '1'}

    # EEA gebruikers (Nederland etc.) moeten myokx gebruiken ipv okx
    try:
        exchange = ccxt.myokx(params)
    except AttributeError:
        exchange = ccxt.okx(params)

    return exchange

def get_candles(exchange, symbol: str, timeframe: str, limit: int = 100):
    return exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

def calculate_position_size(balance: float, entry: float, stop: float,
                             risk_pct: float, vol_scale: float = 1.0) -> float:
    """
    Positiegrootte op basis van risicobedrag.
    vol_scale < 1 bij hoge volatiliteit (ATR14 > ATR50), > 1 bij lage volatiliteit.
    Geclampt op [0.5, 2.0] zodat positie nooit meer dan verdubbelt of halveert.
    """
    risk_amount = balance * risk_pct * vol_scale
    risk_per_unit = abs(entry - stop)
    if risk_per_unit == 0:
        return 0
    return round(risk_amount / risk_per_unit, 6)

def place_order(exchange, symbol: str, signal: Signal, qty: float, candles_15m: list = None, trade_mode_override: str = None) -> Optional[Trade]:
    actual_trade_mode = trade_mode_override if trade_mode_override else state.trade_mode
    mode = "SIM" if state.sim_mode else "LIVE"
    if state.sim_mode:
        trade = Trade(
            id=f"SIM-{len(state.trades)+1:04d}",
            symbol=symbol,
            side=signal.side,
            setup_type=signal.setup_type,
            entry_price=signal.entry,
            quantity=qty,
            stop_loss=signal.stop_loss,
            tp1=signal.tp1,
            tp2=signal.tp2,
            tp3=signal.tp3,
            reason=signal.reason,
            timestamp=datetime.utcnow().isoformat(),
            context_score=getattr(signal, 'context_score', 0),
            trade_mode=actual_trade_mode,
        )
        logger.info(
            f"[SIM] [{signal.setup_type.upper()}] {signal.side.upper()} {qty:.4f} {symbol} @ {signal.entry:.0f} | "
            f"SL={signal.stop_loss:.0f} | TP1={signal.tp1:.0f} | TP2={signal.tp2:.0f} | TP3={signal.tp3:.0f}"
        )
        send_telegram(
            f"📈 <b>TRADE OPEN [{mode}]</b>\n"
            f"{signal.setup_type.upper()} {signal.side.upper()} {qty:.4f} {symbol}\n"
            f"Entry: {signal.entry:.0f} | SL: {signal.stop_loss:.0f}\n"
            f"TP1: {signal.tp1:.0f} | TP2: {signal.tp2:.0f} | TP3: {signal.tp3:.0f}\n"
            f"Sessie: {signal.session} | Geldig tot: {signal.valid_until}\n"
            f"<i>{signal.reason}</i>"
        )
        save_trade(asdict(trade))
        if candles_15m:
            from db import save_candle_snapshot
            save_candle_snapshot(trade.id, candles_15m[-100:], entry_ts=candles_15m[-1][0])
        return trade
    else:
        try:
            order = exchange.create_market_order(symbol, signal.side, qty)
            trade = Trade(
                id=order['id'],
                symbol=symbol,
                side=signal.side,
                setup_type=signal.setup_type,
                entry_price=signal.entry,
                quantity=qty,
                stop_loss=signal.stop_loss,
                tp1=signal.tp1,
                tp2=signal.tp2,
                tp3=signal.tp3,
                reason=signal.reason,
                timestamp=datetime.utcnow().isoformat(),
                context_score=getattr(signal, 'context_score', 0),
                trade_mode=actual_trade_mode,
            )
            logger.info(
                f"[LIVE] [{signal.setup_type.upper()}] {signal.side.upper()} {qty} {symbol} @ {signal.entry:.0f} | "
                f"SL={signal.stop_loss:.0f} | TP1={signal.tp1:.0f} | TP2={signal.tp2:.0f} | TP3={signal.tp3:.0f}"
            )
            send_telegram(
                f"📈 <b>TRADE OPEN [{mode}]</b>\n"
                f"{signal.setup_type.upper()} {signal.side.upper()} {qty} {symbol}\n"
                f"Entry: {signal.entry:.0f} | SL: {signal.stop_loss:.0f}\n"
                f"TP1: {signal.tp1:.0f} | TP2: {signal.tp2:.0f} | TP3: {signal.tp3:.0f}\n"
                f"Sessie: {signal.session} | Geldig tot: {signal.valid_until}\n"
                f"<i>{signal.reason}</i>"
            )
            save_trade(asdict(trade))
            if candles_15m:
                from db import save_candle_snapshot
                save_candle_snapshot(trade.id, candles_15m[-100:], entry_ts=candles_15m[-1][0])
            return trade
        except Exception as e:
            logger.error(f"Order mislukt: {e}")
            return None

def partial_close(exchange, trade: Trade, fraction: float, curr_price: float, label: str):
    """Sluit een deel van de positie — echt of gesimuleerd."""
    qty = round(trade.quantity * fraction, 6)

    if not state.sim_mode:
        try:
            close_side = "sell" if trade.side == "buy" else "buy"
            exchange.create_market_order(trade.symbol, close_side, qty)
        except Exception as e:
            logger.error(f"{label} order fout: {e}")
            return 0.0

    if trade.side == "buy":
        pnl = (curr_price - trade.entry_price) * qty
    else:
        pnl = (trade.entry_price - curr_price) * qty

    trade.realized_pnl += pnl
    state.total_pnl += pnl

    mode = "SIM" if state.sim_mode else "LIVE"
    logger.info(f"[{mode}] {label} ({fraction*100:.0f}% @ {curr_price:.0f}) | PnL = {pnl:.2f} USDT")
    return pnl

def trail_sl_to_structure(trade: Trade, candles: list, phase: int):
    """
    Verschuif SL naar relevante prijsactie na TP2 en TP3.
    phase 2 → laatste swing low/high
    phase 3 → nieuwste swing punt nog dichter bij prijs
    """
    swing_highs, swing_lows = get_swing_points(candles[:-1], lookback=3)

    if trade.side == "buy" and swing_lows:
        candidates = sorted(
            [p for _, p in swing_lows if p < candles[-1][4]],
            reverse=True
        )
        if candidates:
            new_sl = candidates[0] * 0.999  # net eronder
            if new_sl > trade.stop_loss:    # alleen omhoog verschuiven
                logger.info(f"SL verschoven naar swing low: {trade.stop_loss:.0f} → {new_sl:.0f} (fase {phase})")
                trade.stop_loss = new_sl

    elif trade.side == "sell" and swing_highs:
        candidates = sorted(
            [p for _, p in swing_highs if p > candles[-1][4]]
        )
        if candidates:
            new_sl = candidates[0] * 1.001  # net erboven
            if new_sl < trade.stop_loss:    # alleen omlaag verschuiven
                logger.info(f"SL verschoven naar swing high: {trade.stop_loss:.0f} → {new_sl:.0f} (fase {phase})")
                trade.stop_loss = new_sl

def manage_open_trades(exchange, candles_15m, curr_price: float = None):
    """
    4-tranche uitstap strategie:
    - TP1 (25%) → SL naar breakeven
    - TP2 (25%) → SL naar laatste swing low/high
    - TP3 (25%) → SL naar nieuwer swing punt
    - Runner (25%) → SL blijft trailen totdat SL geraakt wordt

    curr_price: optioneel — gebruik 5m close als die verser is dan de 15m close (scalp mode).
                SL trailing blijft altijd gebaseerd op 15m structuur.
    """
    if curr_price is None:
        curr_price = candles_15m[-1][4]

    for trade in state.trades:
        if trade.status == "closed":
            continue

        hit_sl = (
            (trade.side == "buy"  and curr_price <= trade.stop_loss) or
            (trade.side == "sell" and curr_price >= trade.stop_loss)
        )
        hit_tp1 = not trade.tp1_hit and (
            (trade.side == "buy"  and curr_price >= trade.tp1) or
            (trade.side == "sell" and curr_price <= trade.tp1)
        )
        hit_tp2 = trade.tp1_hit and not trade.tp2_hit and (
            (trade.side == "buy"  and curr_price >= trade.tp2) or
            (trade.side == "sell" and curr_price <= trade.tp2)
        )
        hit_tp3 = trade.tp2_hit and not trade.tp3_hit and (
            (trade.side == "buy"  and curr_price >= trade.tp3) or
            (trade.side == "sell" and curr_price <= trade.tp3)
        )

        if hit_sl:
            remaining = 1.0
            if trade.tp1_hit: remaining -= 0.25
            if trade.tp2_hit: remaining -= 0.25
            if trade.tp3_hit: remaining -= 0.25
            partial_close(exchange, trade, remaining, curr_price, "❌ SL")
            trade.status = "closed"
            trade.exit_price = curr_price
            update_trade(asdict(trade))
            from db import get_trade_candles, save_candle_snapshot
            snap = get_trade_candles(trade.id)
            entry_ts = snap.get("entry_ts") if isinstance(snap, dict) else None
            save_candle_snapshot(trade.id, candles_15m[-100:], entry_ts=entry_ts)
            _update_setup_health()

            _record_equity()
            state.consecutive_stops += 1
            send_telegram(
                f"❌ <b>SL HIT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} {trade.symbol}\n"
                f"Entry: {trade.entry_price:.0f} → Exit: {curr_price:.0f}\n"
                f"PnL: {trade.realized_pnl:+.2f} USDT | Stops op rij: {state.consecutive_stops}"
            )

            # Circuit breaker na 5 stops op rij
            if state.consecutive_stops >= 5:
                state.circuit_breaker_until = time.time() + 86400  # 24 uur
                resume = datetime.utcfromtimestamp(state.circuit_breaker_until).strftime('%Y-%m-%d %H:%M UTC')
                logger.warning(f"Circuit breaker actief tot {resume}")
                send_telegram(
                    f"🚨 <b>CIRCUIT BREAKER</b>\n"
                    f"5 stops op rij — bot gepauzeerd.\n"
                    f"Hervat om: {resume}"
                )

        elif hit_tp1:
            partial_close(exchange, trade, 0.25, curr_price, "✅ TP1")
            trade.tp1_hit = True
            trade.status = "partial_1"
            trade.stop_loss = trade.entry_price  # → breakeven
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            logger.info(f"SL verschoven naar breakeven: {trade.entry_price:.0f}")
            send_telegram(
                f"✅ <b>TP1 GERAAKT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT | SL → breakeven"
            )

        elif hit_tp2:
            partial_close(exchange, trade, 0.25, curr_price, "✅ TP2")
            trade.tp2_hit = True
            trade.status = "partial_2"
            trail_sl_to_structure(trade, candles_15m, phase=2)
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            send_telegram(
                f"✅ <b>TP2 GERAAKT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT | SL → swing PA"
            )

        elif hit_tp3:
            partial_close(exchange, trade, 0.25, curr_price, "✅ TP3")
            trade.tp3_hit = True
            trade.status = "partial_3"
            trail_sl_to_structure(trade, candles_15m, phase=3)
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            send_telegram(
                f"✅ <b>TP3 GERAAKT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT | Runner actief, SL trailend"
            )

        elif trade.tp3_hit:
            # Runner fase: SL continu trailen op elke nieuwe candle
            trail_sl_to_structure(trade, candles_15m, phase=4)
            update_trade(asdict(trade))

SETUP_TYPES = ['liquidity_sweep', 'rotation', 'breakout', 'continuation']
HEALTH_WINDOW      = 20   # aantal recente trades per setup om te beoordelen
DISABLE_THRESHOLD  = 0.40 # win rate onder deze grens → disable
RECOVERY_THRESHOLD = 0.50 # win rate boven deze grens → re-enable
MIN_TRADES_TO_JUDGE = 10  # minimaal nodig voordat we een oordeel vellen


def get_setup_health(setup: str) -> dict:
    """
    Bereken win rate en status voor een setup op basis van de laatste HEALTH_WINDOW trades.
    Geeft: {'win_rate': float, 'trades': int, 'status': 'healthy'|'degrading'|'disabled'}
    """
    closed = [t for t in state.trades if t.status == "closed" and t.setup_type == setup]
    recent = closed[-HEALTH_WINDOW:]
    n = len(recent)
    if n == 0:
        return {'win_rate': None, 'trades': 0, 'status': 'healthy'}

    wins = sum(1 for t in recent if t.realized_pnl > 0)
    win_rate = wins / n

    if setup in state.disabled_setups:
        status = 'disabled'
    elif n >= MIN_TRADES_TO_JUDGE and win_rate < DISABLE_THRESHOLD:
        status = 'degrading'
    else:
        status = 'healthy'

    return {'win_rate': round(win_rate, 3), 'trades': n, 'status': status}


def _update_setup_health():
    """
    Controleer na elke gesloten trade of een setup gedegradeerd of hersteld is.
    Schakelt automatisch uit bij win rate < 40% (≥10 trades) en weer in bij ≥50%.
    """
    for setup in SETUP_TYPES:
        health = get_setup_health(setup)
        n = health['trades']
        wr = health['win_rate']
        currently_disabled = setup in state.disabled_setups

        if not currently_disabled and wr is not None and n >= MIN_TRADES_TO_JUDGE and wr < DISABLE_THRESHOLD:
            state.disabled_setups.append(setup)
            msg = (
                f"⚠️ <b>SETUP UITGESCHAKELD: {setup.upper()}</b>\n"
                f"Win rate laatste {n} trades: {wr*100:.0f}% (drempel: {DISABLE_THRESHOLD*100:.0f}%)\n"
                f"Setup hervat automatisch zodra win rate ≥{RECOVERY_THRESHOLD*100:.0f}%"
            )
            logger.warning(f"Setup {setup} uitgeschakeld: win rate {wr*100:.0f}%")
            send_telegram(msg)

        elif currently_disabled and wr is not None and wr >= RECOVERY_THRESHOLD:
            state.disabled_setups.remove(setup)
            msg = (
                f"✅ <b>SETUP HERSTELD: {setup.upper()}</b>\n"
                f"Win rate laatste {n} trades: {wr*100:.0f}% — setup weer actief"
            )
            logger.info(f"Setup {setup} hersteld: win rate {wr*100:.0f}%")
            send_telegram(msg)


def _check_open_trades_live(exchange):
    """
    Live price check voor open trades — los van candle timing.
    Wordt elke 60s aangeroepen via state.last_live_check.
    Gebruikt dezelfde manage_open_trades logica maar met live ticker prijs.
    """
    open_trades = [t for t in state.trades if t.status != "closed"]
    if not open_trades:
        return
    try:
        ticker = exchange.fetch_ticker(state.symbol)
        live_price = ticker['last']
        # Maak een minimale fake candle met live prijs voor de TP/SL checks
        fake_candle = [0, live_price, live_price, live_price, live_price, 0]
        manage_open_trades(exchange, [fake_candle])
    except Exception as e:
        logger.warning(f"Live price check mislukt: {e}")


def run_bot():
    state.sim_mode     = os.environ.get('SIM_MODE', 'true').lower() == 'true'
    state.trade_mode   = os.environ.get('TRADE_MODE', 'daytrade').lower()
    state.human_approval = os.environ.get('HUMAN_APPROVAL', 'false').lower() == 'true'
    mode_label = "PAPER TRADING" if state.sim_mode else "LIVE TRADING"

    # DB initialiseren en bestaande trades laden
    init_db()
    saved_trades = load_trades()
    if saved_trades:
        from dataclasses import fields as dc_fields
        trade_fields = {f.name for f in dc_fields(Trade)}
        for td in saved_trades:
            t = Trade(**{k: v for k, v in td.items() if k in trade_fields})
            state.trades.append(t)
            if t.status == "closed":
                state.total_pnl += t.realized_pnl
        logger.info(f"{len(saved_trades)} trades hersteld uit database")

    exchange = get_public_exchange() if state.sim_mode else get_exchange()
    logger.info(f"DoopieCash Bot gestart | {state.symbol} | {mode_label}")

    if state.sim_mode:
        state.sim_balance = float(os.environ.get('SIM_BALANCE', '10000'))
        state.balance = state.sim_balance
        state.equity  = state.sim_balance
        logger.info(f"[SIM] Startkapitaal: ${state.balance:,.0f}")

    while state.running:
        try:
            # ── Circuit breaker ────────────────────────────────────────────────
            if state.circuit_breaker_until and time.time() < state.circuit_breaker_until:
                resume = datetime.utcfromtimestamp(state.circuit_breaker_until).strftime('%H:%M UTC')
                logger.info(f"Circuit breaker actief — hervat om {resume}")
                time.sleep(60)
                continue

            # ── Balance ophalen ────────────────────────────────────────────────
            if state.sim_mode:
                state.balance = state.sim_balance + state.total_pnl
                state.equity  = state.balance
            else:
                balance_info = exchange.fetch_balance()
                for currency in ['USDT', 'USDC', 'USD']:
                    if currency in balance_info and balance_info[currency]['total'] > 0:
                        state.balance = float(balance_info[currency]['free'])
                        state.equity  = float(balance_info[currency]['total'])
                        break

            # ── Daily loss limit ───────────────────────────────────────────────
            today = datetime.utcnow().strftime('%Y-%m-%d')
            if state.day_date != today:
                state.day_date = today
                state.day_start_equity = state.equity

            if state.day_start_equity > 0 and state.equity < state.day_start_equity * 0.97:
                daily_pct = (state.equity - state.day_start_equity) / state.day_start_equity * 100
                logger.warning(f"Daily loss limit bereikt ({daily_pct:.1f}%). Geen nieuwe trades vandaag.")
                time.sleep(60)
                continue

            # ── Live price polling (elke 60s, los van candle timing) ──────────
            if time.time() - state.last_live_check >= 60:
                state.last_live_check = time.time()
                _check_open_trades_live(exchange)

            # ── Marktdata ─────────────────────────────────────────────────────
            candles_5m  = get_candles(exchange, state.symbol, '5m',  limit=60)
            candles_15m = get_candles(exchange, state.symbol, '15m', limit=150)
            candles_1h  = get_candles(exchange, state.symbol, '1h',  limit=50)
            candles_4h  = get_candles(exchange, state.symbol, '4h',  limit=30)

            curr_5m_ts  = str(candles_5m[-1][0])
            curr_15m_ts = str(candles_15m[-1][0])

            # Expire pending signals
            now_t = time.time()
            with _pending_lock:
                expired_ids = [sid for sid, ps in pending_signals.items() if ps['expires_at'] < now_t]
                for sid in expired_ids:
                    try:
                        update_pending_signal_status(sid, 'expired')
                    except Exception:
                        pass
                    del pending_signals[sid]
                    send_telegram(f"⏰ Setup {sid} vervallen (10 min timeout)")
            state.pending_count = len(pending_signals)

            # Bepaal welke candles nieuw zijn
            new_15m = curr_15m_ts != state.last_candle_time
            new_5m  = curr_5m_ts  != state.last_5m_ts

            if state.trade_mode == 'both':
                triggered = new_15m or new_5m
            elif state.trade_mode == 'scalp':
                triggered = new_5m
            else:
                triggered = new_15m

            if triggered:
                # Timestamps bijwerken
                if state.trade_mode == 'both':
                    if new_5m:  state.last_5m_ts = curr_5m_ts
                    if new_15m: state.last_candle_time = curr_15m_ts
                elif state.trade_mode == 'scalp':
                    state.last_5m_ts = curr_5m_ts
                else:
                    state.last_candle_time = curr_15m_ts

                # Scalp/both: gebruik de verse 5m slotkoers als TP/SL check prijs,
                # zodat trades die op een 5m close raken niet wachten op de volgende 15m close.
                # SL trailing blijft gebaseerd op 15m structuur (via candles_15m).
                mgmt_price = (
                    candles_5m[-1][4]
                    if state.trade_mode in ('scalp', 'both') and new_5m
                    else None  # manage_open_trades valt terug op candles_15m[-1][4]
                )
                manage_open_trades(exchange, candles_15m, curr_price=mgmt_price)

                open_count = sum(1 for t in state.trades if t.status != "closed")
                signal         = None
                effective_mode = state.trade_mode

                if open_count == 0:
                    if state.trade_mode == 'both':
                        # Daytrade heeft prioriteit op nieuwe 15m candle
                        if new_15m:
                            signal = analyze(
                                candles_15m, candles_1h,
                                candles_4h=candles_4h,
                                candles_5m=candles_5m,
                                disabled_setups=state.disabled_setups,
                                session_filter=False,
                            )
                            if signal:
                                effective_mode = 'daytrade'
                        # Scalp als geen daytrade signal en er is een nieuwe 5m candle
                        if not signal and new_5m:
                            signal = analyze(
                                candles_5m, candles_15m,
                                candles_4h=None,
                                candles_5m=None,
                                disabled_setups=list(set(state.disabled_setups) | {'rotation', 'continuation'}),
                                session_filter=False,
                                scalp_mode=True,
                            )
                            if signal:
                                effective_mode = 'scalp'
                    elif state.trade_mode == 'scalp':
                        signal = analyze(
                            candles_5m, candles_15m,
                            candles_4h=None,
                            candles_5m=None,
                            disabled_setups=list(set(state.disabled_setups) | {'rotation', 'continuation'}),
                            session_filter=False,
                            scalp_mode=True,
                        )
                    else:
                        signal = analyze(
                            candles_15m, candles_1h,
                            candles_4h=candles_4h,
                            candles_5m=candles_5m,
                            disabled_setups=state.disabled_setups,
                            session_filter=False,
                        )

                # Signal expiry check: als entry >0.5% van huidige prijs afwijkt, verwerp
                if signal:
                    curr_price = candles_15m[-1][4]
                    entry_drift = abs(signal.entry - curr_price) / curr_price
                    if entry_drift > 0.005:
                        logger.info(
                            f"Signal vervallen: entry {signal.entry:.0f} vs prijs {curr_price:.0f} "
                            f"({entry_drift*100:.2f}% drift)"
                        )
                        signal = None

                if signal:
                    state.last_signal = signal.side
                    state.last_setup  = signal.setup_type

                    atr14 = calc_atr(candles_15m, 14)
                    atr50 = calc_atr(candles_15m, min(50, len(candles_15m)))
                    vol_scale = max(0.5, min(2.0, atr50 / atr14)) if atr14 > 0 else 1.0

                    qty = calculate_position_size(
                        state.balance, signal.entry,
                        signal.stop_loss, state.risk_per_trade, vol_scale
                    )
                    if qty > 0:
                        if state.human_approval:
                            send_approval_request(signal, exchange, qty, candles_15m, trade_mode_override=effective_mode)
                            state.pending_count = len([p for p in pending_signals.values() if p['expires_at'] > time.time()])
                        else:
                            trade = place_order(exchange, state.symbol, signal, qty, candles_15m, trade_mode_override=effective_mode)
                            if trade:
                                state.trades.append(trade)
                else:
                    state.last_signal = "none"
                    state.last_setup  = "none"

            time.sleep(10)

        except Exception as e:
            logger.error(f"Bot loop fout: {e}")
            time.sleep(30)

    logger.info("Bot gestopt.")
