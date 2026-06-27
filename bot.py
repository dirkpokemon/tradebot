import ccxt
import time
import logging
import os
import requests
from datetime import datetime
from typing import Optional
from dataclasses import dataclass, field, asdict

from strategy import analyze, Signal, get_swing_points, calc_atr, get_last_analysis
from db import init_db, save_trade, update_trade, load_trades, clear_trades as db_clear_trades
from db import get_learned_params, save_learning_proposals, set_learned_param

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
    counter_trend: bool = False   # True = setup ging tegen de 4h voorkeursrichting in

@dataclass
class BotState:
    running: bool = False
    symbol: str = "BTC/USDT:USDT"
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
    disabled_setups: list = field(default_factory=list)  # ['rotation', 'continuation', ...]
    sl_cooldown_candles: int = 0  # candles sinds laatste SL; 1 = cooldown actief, 0 = vrij
    trade_mode: str = "daytrade"   # "daytrade" | "scalp" | "both"
    trade_direction: str = "both"  # "both" | "long_only" | "short_only"
    last_5m_ts: str = ""           # alleen gebruikt in 'both' mode
    # Diagnostiek: wat zag de laatste analyse per modus (voor dashboard/status)
    last_analysis: dict = field(default_factory=dict)

state = BotState()


def _get_learned_analyze_kwargs() -> dict:
    """Load active learned params and return kwargs for analyze()."""
    try:
        params = get_learned_params()
        result = {}
        if "min_score_global" in params:
            result["min_score_override"] = params["min_score_global"]
        if "setup_min_scores" in params:
            result["setup_min_scores"] = params["setup_min_scores"]
        # Merge learned disabled setups with state disabled setups
        learned_disabled = params.get("disabled_setups", [])
        result["disabled_setups"] = list(set(state.disabled_setups + learned_disabled))
        return result
    except Exception:
        return {"disabled_setups": state.disabled_setups}


def _maybe_run_learning():
    """Trigger learning analysis after every 10 new closed trades (min 30 total)."""
    try:
        closed_count = sum(1 for t in state.trades if t.status == "closed")
        if closed_count < 30 or closed_count % 10 != 0:
            return
        from learn import analyze_for_proposals
        proposals = analyze_for_proposals(min_trades=30)
        if proposals:
            save_learning_proposals(proposals)
            logger.info(f"Leeranalyse: {len(proposals)} nieuwe voorstellen opgeslagen")
    except Exception as e:
        logger.warning(f"Leeranalyse mislukt: {e}")


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


def get_public_exchange():
    """OKX publieke API voor marktdata — geen auth nodig."""
    return ccxt.okx({'options': {'defaultType': 'swap'}})

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
        'options':  {'defaultType': 'swap'},
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
    ct_tag = " ⚖️ COUNTER-TREND (halve grootte, max 1.5R)" if getattr(signal, 'is_counter_trend', False) else ""
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
            counter_trend=getattr(signal, 'is_counter_trend', False),
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
            f"Sessie: {signal.session} | Geldig tot: {signal.valid_until}{ct_tag}\n"
            f"<i>{signal.reason}</i>"
        )
        save_trade(asdict(trade))
        if candles_15m:
            from db import save_candle_snapshot
            # [-2] = laatste gesloten candle (trigger voor de entry); [-1] is de forming candle
            entry_ts = candles_15m[-2][0] if len(candles_15m) >= 2 else candles_15m[-1][0]
            save_candle_snapshot(trade.id, candles_15m[-100:], entry_ts=entry_ts)
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
                counter_trend=getattr(signal, 'is_counter_trend', False),
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
                entry_ts = candles_15m[-2][0] if len(candles_15m) >= 2 else candles_15m[-1][0]
                save_candle_snapshot(trade.id, candles_15m[-100:], entry_ts=entry_ts)
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
    Uitstap strategie — afhankelijk van trade-type:

    Normale trades (mét de 4h trend) — 4-tranche exit:
    - TP1 (25%) → SL naar breakeven
    - TP2 (25%) → SL naar laatste swing low/high
    - TP3 (25%) → SL naar nieuwer swing punt
    - Runner (25%) → SL blijft trailen totdat SL geraakt wordt

    Counter-trend trades (tegen de 4h trend, max 1.5R) — 2-tranche exit, geen TP3/runner:
    - TP1 = 1R (50%) → SL naar breakeven
    - TP2 = 1.5R (50%) → volledige exit

    curr_price: optioneel — gebruik 5m close als die verser is dan de 15m close (scalp mode).
                SL trailing blijft altijd gebaseerd op 15m structuur.
    """
    if curr_price is None:
        curr_price = candles_15m[-1][4]

    for trade in state.trades:
        if trade.status == "closed":
            continue

        is_ct = getattr(trade, 'counter_trend', False)
        tp_fraction = 0.5 if is_ct else 0.25

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
        # Counter-trend trades hebben geen TP3/runner-fase (max 1.5R, sluit volledig op TP2)
        hit_tp3 = (not is_ct) and trade.tp2_hit and not trade.tp3_hit and (
            (trade.side == "buy"  and curr_price >= trade.tp3) or
            (trade.side == "sell" and curr_price <= trade.tp3)
        )

        if hit_sl:
            remaining = 1.0
            if trade.tp1_hit: remaining -= tp_fraction
            if trade.tp2_hit: remaining -= tp_fraction
            if not is_ct and trade.tp3_hit: remaining -= 0.25

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
            state.sl_cooldown_candles = 1  # start cooldown (daytrade: 2 candles op 15m, scalp: 1 candle op 5m)
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
            # TP1 (25%): uitstap zonder SL-to-BE — SL pas naar BE als prijs echt een
            # nieuw niveau heeft gewonnen (bewezen bij TP2 hit). Zo wordt geen
            # premature BE gezet op een enkele candle die TP1 raakt.
            partial_close(exchange, trade, tp_fraction, curr_price, "✅ TP1")
            trade.tp1_hit = True
            trade.status = "partial_1"
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            send_telegram(
                f"✅ <b>TP1 GERAAKT</b> ({tp_fraction*100:.0f}%) — SL ongewijzigd\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT"
                + (" | counter-trend: max 1.5R" if is_ct else "")
            )

        elif hit_tp2 and is_ct:
            # Counter-trend: TP2 = max target (1.5R) → volledige exit, geen TP3/runner
            partial_close(exchange, trade, tp_fraction, curr_price, "✅ TP2 (volledige exit)")
            trade.tp2_hit = True
            trade.tp3_hit = True  # markeer als afgerond zodat geen runner-fase volgt
            trade.status = "closed"
            trade.exit_price = curr_price
            update_trade(asdict(trade))
            from db import get_trade_candles, save_candle_snapshot
            snap = get_trade_candles(trade.id)
            entry_ts = snap.get("entry_ts") if isinstance(snap, dict) else None
            save_candle_snapshot(trade.id, candles_15m[-100:], entry_ts=entry_ts)
            _update_setup_health()
            _record_equity()
            state.consecutive_stops = 0
            send_telegram(
                f"✅ <b>TP2 GERAAKT — VOLLEDIGE EXIT</b> (counter-trend, max 1.5R)\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} {trade.symbol}\n"
                f"Entry: {trade.entry_price:.0f} → Exit: {curr_price:.0f}\n"
                f"PnL: {trade.realized_pnl:+.2f} USDT"
            )

        elif hit_tp2:
            # TP2: prijs heeft een nieuw niveau gewonnen → nu SL naar breakeven
            partial_close(exchange, trade, tp_fraction, curr_price, "✅ TP2")
            trade.tp2_hit = True
            trade.status = "partial_2"
            trade.stop_loss = trade.entry_price  # → breakeven (prijs bewees zichzelf)
            trail_sl_to_structure(trade, candles_15m, phase=2)
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            logger.info(f"SL verschoven naar breakeven: {trade.entry_price:.0f}")
            send_telegram(
                f"✅ <b>TP2 GERAAKT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT | SL → breakeven + swing PA"
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

        elif trade.tp3_hit and not is_ct:
            # Runner fase: SL continu trailen op elke nieuwe candle (alleen mét-trend trades)
            trail_sl_to_structure(trade, candles_15m, phase=4)
            update_trade(asdict(trade))

SETUP_TYPES = ['rotation', 'continuation']
HEALTH_WINDOW      = 20   # aantal recente trades per setup om te beoordelen
DISABLE_THRESHOLD  = 0.40 # win rate onder deze grens → waarschuwing
RECOVERY_THRESHOLD = 0.50 # win rate boven deze grens → waarschuwing opgeheven
MIN_TRADES_TO_JUDGE = 10  # minimaal nodig voordat we een oordeel vellen

# Setups waarvoor al een "verslechtert"-waarschuwing is verstuurd (voorkomt spam).
_degraded_warned: set = set()


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
    Bewaakt de gezondheid van elke setup en stuurt een waarschuwing bij verslechtering.

    Schakelt setups NIET meer automatisch uit. Er zijn maar twee kernsetups
    (rotation, continuation); één daarvan automatisch uitzetten legt de bot grotendeels
    stil. Bovendien kon een uitgeschakelde setup zichzelf nooit herstellen — hij nam
    geen trades meer, dus de win rate bleef bevroren onder de drempel (deadlock).

    Uitschakelen verloopt nu uitsluitend via het leerrapport, waar jij per voorstel
    beslist. De gezondheidsstatus (healthy/degrading) blijft zichtbaar op het dashboard.
    """
    for setup in SETUP_TYPES:
        health = get_setup_health(setup)
        n  = health['trades']
        wr = health['win_rate']
        if wr is None:
            continue

        if n >= MIN_TRADES_TO_JUDGE and wr < DISABLE_THRESHOLD and setup not in _degraded_warned:
            _degraded_warned.add(setup)
            logger.warning(f"Setup {setup} verslechtert: win rate {wr*100:.0f}% over {n} trades")
            send_telegram(
                f"⚠️ <b>SETUP VERSLECHTERT: {setup.upper()}</b>\n"
                f"Win rate laatste {n} trades: {wr*100:.0f}% (drempel: {DISABLE_THRESHOLD*100:.0f}%)\n"
                f"De setup blijft actief. Bekijk het leerrapport voor een eventueel voorstel."
            )
        elif wr >= RECOVERY_THRESHOLD:
            _degraded_warned.discard(setup)


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
    state.sim_mode        = os.environ.get('SIM_MODE', 'true').lower() == 'true'
    state.trade_mode      = os.environ.get('TRADE_MODE', 'daytrade').lower()
    state.trade_direction = os.environ.get('TRADE_DIRECTION', 'both').lower()
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

    # Kernsetups (rotation, continuation) nooit uitgeschakeld laten staan: het zijn de
    # enige twee setups, dus uitschakelen legt de bot stil. Verwijder ze uit een eerder
    # opgebouwde (auto- of geleerde) disable-lijst zodat de bot altijd kan blijven traden.
    state.disabled_setups = [s for s in state.disabled_setups if s not in SETUP_TYPES]
    try:
        learned_disabled = get_learned_params().get('disabled_setups', [])
        cleaned = [s for s in learned_disabled if s not in SETUP_TYPES]
        if cleaned != learned_disabled:
            set_learned_param('disabled_setups', cleaned)
            removed = set(learned_disabled) - set(cleaned)
            logger.info(f"Kernsetups uit geleerde disable-lijst verwijderd: {', '.join(removed)}")
    except Exception as e:
        logger.warning(f"Kon geleerde disable-lijst niet opschonen: {e}")

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
            # 30m voor daytrade secundair timeframe (15m + 30m)
            candles_30m = get_candles(exchange, state.symbol, '30m', limit=80) \
                if state.trade_mode in ('daytrade', 'both') else None
            # 1m voor scalp secundair timeframe (5m + 1m)
            candles_1m  = get_candles(exchange, state.symbol, '1m', limit=90) \
                if state.trade_mode in ('scalp', 'both') else None

            curr_5m_ts  = str(candles_5m[-1][0])
            curr_15m_ts = str(candles_15m[-1][0])

            # Bepaal welke candles nieuw zijn
            new_15m = curr_15m_ts != state.last_candle_time
            new_5m  = curr_5m_ts  != state.last_5m_ts

            # BELANGRIJK: de laatste candle in elke reeks is de NET GEOPENDE
            # (forming) candle — body ≈ 0, close ≈ open. Entry-condities (body
            # ≥50%, gain the level) moeten op de zojuist GESLOTEN candle worden
            # beoordeeld, anders faalt vrijwel elke setup. Daarom strippen we de
            # forming candle van alle entry-timeframes voor analyze().
            closed_15m = candles_15m[:-1]
            closed_5m  = candles_5m[:-1]
            closed_30m = candles_30m[:-1] if candles_30m else None
            closed_1m  = candles_1m[:-1]  if candles_1m  else None

            # Daytrade en 'both' werken identiek: 15m heeft prioriteit, 5m is fallback
            # wanneer 15m geen setup oplevert (DoopieCash punt 9 — meer trades genereren
            # door ook lagere timeframes te doorzoeken i.p.v. te wachten op 15m).
            multi_tf = state.trade_mode in ('daytrade', 'both')

            if state.trade_mode == 'scalp':
                triggered = new_5m
            else:
                triggered = new_15m or new_5m

            if triggered:
                # Timestamps bijwerken
                if state.trade_mode == 'scalp':
                    state.last_5m_ts = curr_5m_ts
                else:
                    if new_5m:  state.last_5m_ts = curr_5m_ts
                    if new_15m: state.last_candle_time = curr_15m_ts

                # Scalp/multi-tf: gebruik de verse 5m slotkoers als TP/SL check prijs,
                # zodat trades die op een 5m close raken niet wachten op de volgende 15m close.
                # SL trailing blijft gebaseerd op 15m structuur (via candles_15m).
                mgmt_price = (
                    candles_5m[-1][4]
                    if (state.trade_mode == 'scalp' or multi_tf) and new_5m
                    else None  # manage_open_trades valt terug op candles_15m[-1][4]
                )
                manage_open_trades(exchange, candles_15m, curr_price=mgmt_price)

                open_count = sum(1 for t in state.trades if t.status != "closed")
                signal         = None
                effective_mode = state.trade_mode

                # SL-cooldown bijhouden: tel de candle op die bij de actieve modus hoort
                # scalp = 5m candles (1 candle cooldown), anders 15m (3 candles)
                cooldown_tick = new_5m if state.trade_mode == 'scalp' else new_15m
                if cooldown_tick and state.sl_cooldown_candles > 0:
                    state.sl_cooldown_candles += 1
                    if state.sl_cooldown_candles >= 3:
                        state.sl_cooldown_candles = 0
                        logger.info("SL-cooldown voorbij — nieuwe setups worden weer gezocht")

                # Per type bijhouden hoeveel posities open zijn (max 1 daytrade + 1 scalp)
                open_daytrade = sum(1 for t in state.trades
                                    if t.status != "closed" and getattr(t, 'trade_mode', 'daytrade') == 'daytrade')
                open_scalp    = sum(1 for t in state.trades
                                    if t.status != "closed" and getattr(t, 'trade_mode', 'daytrade') == 'scalp')

                atr14     = calc_atr(candles_15m, 14)
                atr50     = calc_atr(candles_15m, min(50, len(candles_15m)))
                vol_scale = max(0.5, min(2.0, atr50 / atr14)) if atr14 > 0 else 1.0

                # Helper: filter + expiry + grootte + order plaatsen voor één signal
                def _try_place(sig, mode_label_str, eff_mode):
                    if not sig:
                        return None
                    # Richting-filter
                    if state.trade_direction != 'both':
                        allowed = 'buy' if state.trade_direction == 'long_only' else 'sell'
                        if sig.side != allowed:
                            logger.info(f"Signal verworpen: TRADE_DIRECTION={state.trade_direction}")
                            return None
                    # Expiry: entry mag max 0.5% van huidige prijs afwijken.
                    # Referentie = live (forming) close van het eigen timeframe.
                    ref_price = candles_5m[-1][4] if eff_mode == 'scalp' else candles_15m[-1][4]
                    if abs(sig.entry - ref_price) / ref_price > 0.005:
                        logger.info(f"Signal vervallen: {sig.entry:.0f} vs {ref_price:.0f}")
                        return None
                    risk_pct = state.risk_per_trade * (0.5 if getattr(sig, 'is_counter_trend', False) else 1.0)
                    qty = calculate_position_size(state.balance, sig.entry, sig.stop_loss, risk_pct, vol_scale)
                    if qty <= 0:
                        return None
                    return place_order(exchange, state.symbol, sig, qty, candles_15m, trade_mode_override=eff_mode)

                placed_any = False

                if multi_tf:
                    # ── Daytrade pad: nieuwe 15m candle + geen daytrade open ──────────────
                    if new_15m and open_daytrade == 0:
                        dt_sig = analyze(
                            closed_15m, candles_1h,
                            cooldown_candles=state.sl_cooldown_candles,
                            candles_4h=candles_4h,
                            candles_5m=closed_5m,
                            candles_30m=closed_30m,
                            disabled_setups=state.disabled_setups,
                            session_filter=False,
                        )
                        state.last_analysis['daytrade'] = get_last_analysis()
                        trade = _try_place(dt_sig, 'DAYTRADE', 'daytrade')
                        if trade:
                            state.trades.append(trade)
                            state.last_signal = dt_sig.side
                            state.last_setup  = dt_sig.setup_type
                            placed_any = True

                    # ── Scalp pad: nieuwe 5m candle + geen scalp open (onafhankelijk) ─────
                    if new_5m and open_scalp == 0:
                        sc_sig = analyze(
                            closed_5m, closed_15m,
                            cooldown_candles=state.sl_cooldown_candles,
                            min_cooldown_candles=1,
                            candles_4h=candles_4h,
                            candles_5m=None,
                            candles_1m=closed_1m,
                            disabled_setups=state.disabled_setups,
                            session_filter=False,
                            scalp_mode=True,
                        )
                        state.last_analysis['scalp'] = get_last_analysis()
                        trade = _try_place(sc_sig, 'SCALP', 'scalp')
                        if trade:
                            state.trades.append(trade)
                            state.last_signal = sc_sig.side
                            state.last_setup  = sc_sig.setup_type
                            placed_any = True

                else:
                    # ── Pure daytrade of pure scalp: max 1 open positie ───────────────────
                    open_count = open_daytrade + open_scalp
                    if open_count == 0:
                        if state.trade_mode == 'scalp':
                            sig = analyze(
                                closed_5m, closed_15m,
                                cooldown_candles=state.sl_cooldown_candles,
                                min_cooldown_candles=1,
                                candles_4h=candles_4h,
                                candles_5m=None,
                                candles_1m=closed_1m,
                                disabled_setups=state.disabled_setups,
                                session_filter=False,
                                scalp_mode=True,
                            )
                            eff = 'scalp'
                        else:
                            sig = analyze(
                                closed_15m, candles_1h,
                                cooldown_candles=state.sl_cooldown_candles,
                                candles_4h=candles_4h,
                                candles_5m=closed_5m,
                                disabled_setups=state.disabled_setups,
                                session_filter=False,
                            )
                            eff = 'daytrade'
                        state.last_analysis[eff] = get_last_analysis()
                        trade = _try_place(sig, state.trade_mode.upper(), eff)
                        if trade:
                            state.trades.append(trade)
                            state.last_signal = sig.side
                            state.last_setup  = sig.setup_type
                            placed_any = True
                        elif not sig:
                            state.last_signal = "none"
                            state.last_setup  = "none"

                if not placed_any and not (open_daytrade + open_scalp):
                    state.last_signal = "none"
                    state.last_setup  = "none"

            time.sleep(10)

        except Exception as e:
            logger.error(f"Bot loop fout: {e}")
            time.sleep(30)

    logger.info("Bot gestopt.")
