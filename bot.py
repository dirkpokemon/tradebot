import ccxt
import time
import logging
import os
import re
import requests
import threading
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
    # De markten die de bot volgt. `symbol` is de eerste daarvan en blijft bestaan
    # als "hoofdmarkt" voor de backtest-standaard en de koptekst van het dashboard.
    symbols: list = field(default_factory=lambda: ["BTC/USDT:USDT"])
    symbol: str = "BTC/USDT:USDT"
    risk_per_trade: float = 0.01
    sim_mode: bool = True
    sim_balance: float = 10000.0
    trades: list = field(default_factory=list)
    last_signal: str = "none"
    last_setup: str = "none"
    last_candle_time: str = ""
    # Per markt: welke candle als laatste is verwerkt, de SL-cooldown en wat de
    # laatste analyse zag. Dit hoort NIET globaal te zijn — anders legt een stop
    # op BTC ook ETH stil en lopen de candle-administraties door elkaar.
    # Vorm: {symbol: {"last_15m": str, "last_5m": str, "cooldown": int, "analysis": dict}}
    markets: dict = field(default_factory=dict)
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
        # Backtest-getunede parameters (via autotune-voorstellen goedgekeurd)
        if params.get("sl_atr_mult"):
            result["sl_atr_mult"] = params["sl_atr_mult"]
        if params.get("min_rr"):
            result["min_rr"] = params["min_rr"]
        # Merge learned disabled setups with state disabled setups
        learned_disabled = params.get("disabled_setups", [])
        result["disabled_setups"] = list(set(state.disabled_setups + learned_disabled))
        return result
    except Exception:
        return {"disabled_setups": state.disabled_setups}


LEARN_EVERY_N_TRADES = 10


def _maybe_run_learning():
    """
    Laat het leerrapport draaien zodra er 10 nieuwe gesloten trades bij zijn
    (vanaf 30 in totaal), en laat de bot getalsmatige adviezen meteen zelf
    toepassen.

    De teller wordt in de database bijgehouden, niet afgeleid uit `count % 10`:
    deze functie draait per candle, dus met een modulo-test zou hij bij een stand
    van precies 40 trades élke candle opnieuw analyseren. Opslaan betekent ook dat
    een redeploy de analyse niet nog een keer overdoet.
    """
    try:
        closed_count = sum(1 for t in state.trades if t.status == "closed")
        if closed_count < 30:
            return
        last = get_learned_params().get("last_learn_trades") or 0
        if closed_count - int(last) < LEARN_EVERY_N_TRADES:
            return

        from learn import analyze_for_proposals
        from autolearn import maybe_auto_apply
        set_learned_param("last_learn_trades", closed_count)

        proposals = analyze_for_proposals(min_trades=30)
        if not proposals:
            logger.info(f"Leeranalyse over {closed_count} trades: geen verbeterpunten gevonden")
            return

        saved = save_learning_proposals(proposals)
        skipped = len(proposals) - saved
        logger.info(
            f"Leeranalyse over {closed_count} trades: {saved} nieuwe voorstellen"
            + (f" ({skipped} eerder afgewezen, overgeslagen)" if skipped else "")
        )
        # Getalsmatige adviezen binnen vaste grenzen voert de bot zelf door, via
        # dezelfde nulmeting-en-terugdraai-machinerie als de autotune. Structurele
        # adviezen (setup uitschakelen, richtingfilter, handelsmodus) blijven
        # bewust op een menselijke beslissing wachten.
        maybe_auto_apply(proposals)
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


def _strip_html(text: str) -> str:
    """Berichten zijn in Telegram-HTML geschreven; dit is de leesbare platte versie."""
    return re.sub(r"<[^>]+>", "", text)


def _mail_parts(message: str):
    """Onderwerp, platte tekst en HTML — gedeeld door beide verzendwegen."""
    plain = _strip_html(message)
    # De eerste regel is in elk bericht de kop ("📈 TRADE OPEN [DAYTRADE]") en
    # is daarmee precies wat je op je telefoon wilt zien zonder de mail te openen.
    subject = plain.split("\n", 1)[0].strip()[:120] or "TradeBot"
    html = (
        "<html><body style=\"font-family:-apple-system,Segoe UI,sans-serif;font-size:15px\">"
        + message.replace("\n", "<br>")
        + "</body></html>"
    )
    return subject, plain, html


def _send_ntfy_now(message: str):
    """
    Pushmelding via ntfy. Geen account en geen sleutel: je abonneert je in de
    ntfy-app op een kanaalnaam, en wie naar die naam publiceert bereikt je
    telefoon. Gaat over HTTPS, dus Railway laat het gewoon door.

    Publiceren gaat als JSON naar de basis-URL in plaats van via HTTP-headers:
    headers moeten ASCII zijn en onze meldingen bevatten emoji en accenten.
    """
    topic = os.environ.get('NTFY_TOPIC', '')
    if not topic:
        return

    server = os.environ.get('NTFY_SERVER', 'https://ntfy.sh').rstrip('/')
    plain  = _strip_html(message)
    parts  = plain.split("\n", 1)
    title  = parts[0].strip()[:120] or "TradeBot"
    body   = (parts[1].strip() if len(parts) > 1 else "") or title

    headers = {}
    token = os.environ.get('NTFY_TOKEN', '')
    if token:
        headers["Authorization"] = f"Bearer {token}"

    r = requests.post(
        server,
        json={"topic": topic, "title": title, "message": body, "priority": 4},
        headers=headers,
        timeout=10,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"ntfy gaf {r.status_code}: {r.text[:300]}")


def send_push(message: str):
    """Push in een eigen thread, zodat de handelslus nooit op het netwerk wacht."""
    if not os.environ.get('NTFY_TOPIC'):
        return

    def _run():
        try:
            _send_ntfy_now(message)
        except Exception as e:
            logger.warning(f"Pushmelding mislukt: {e}")

    threading.Thread(target=_run, daemon=True).start()


def mail_channel() -> str:
    """Welke verzendweg is ingesteld: 'resend', 'smtp' of '' (geen)."""
    if not os.environ.get('MAIL_TO'):
        return ""
    if os.environ.get('RESEND_API_KEY'):
        return "resend"
    if os.environ.get('SMTP_HOST'):
        return "smtp"
    return ""


def _send_via_resend(message: str):
    """
    Mail via de HTTPS-API van Resend.

    Nodig omdat Railway uitgaand SMTP blokkeert op Free, Trial en Hobby: een
    verbinding naar poort 587 komt daar niet eens tot stand ("Network is
    unreachable"). HTTPS gaat over 443 en wordt wél doorgelaten.
    """
    to      = os.environ['MAIL_TO']
    sender  = os.environ.get('MAIL_FROM') or 'onboarding@resend.dev'
    subject, plain, html = _mail_parts(message)

    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}"},
        json={"from": sender, "to": [to], "subject": subject, "html": html, "text": plain},
        timeout=10,
    )
    if r.status_code >= 400:
        # De tekst van Resend zegt meestal precies wat er mis is (ongeldige
        # sleutel, niet-geverifieerd afzenderdomein); die wil je dus zien.
        raise RuntimeError(f"Resend gaf {r.status_code}: {r.text[:300]}")


def _send_via_smtp(message: str):
    """
    Mail via SMTP. Werkt met elke provider, maar op Railway alleen op het
    Pro-plan — daaronder staat uitgaand SMTP uit.
    """
    import smtplib, ssl
    from email.message import EmailMessage

    host     = os.environ['SMTP_HOST']
    to       = os.environ['MAIL_TO']
    port     = int(os.environ.get('SMTP_PORT', '587'))
    user     = os.environ.get('SMTP_USER', '')
    password = os.environ.get('SMTP_PASSWORD', '')
    sender   = os.environ.get('MAIL_FROM', user or to)

    subject, plain, html = _mail_parts(message)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = to
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    # Versleuteling staat standaard aan: zonder TLS gaat het SMTP-wachtwoord
    # leesbaar over de lijn. SMTP_TLS=false is er alleen voor een vertrouwde
    # relay in hetzelfde netwerk.
    use_tls = os.environ.get('SMTP_TLS', 'true').strip().lower() not in ('false', '0', 'no', 'off')

    # Harde timeout: zonder dat kan een SMTP-server die niet antwoordt deze
    # thread voorgoed laten hangen (zie CLAUDE.md over netwerkclients).
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=15, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=15)

    with server:
        if port != 465 and use_tls:
            server.ehlo()
            if not server.has_extn("starttls"):
                raise RuntimeError(
                    f"{host}:{port} biedt geen STARTTLS. Gebruik poort 465, of zet "
                    f"SMTP_TLS=false als dit een vertrouwde relay is — maar stuur dan "
                    f"geen wachtwoord mee."
                )
            server.starttls(context=ssl.create_default_context())
        if user and password:
            if not use_tls and port != 465:
                raise RuntimeError("Weiger in te loggen zonder TLS: zet SMTP_TLS=true of gebruik poort 465.")
            server.login(user, password)
        server.send_message(msg)


def _send_email_now(message: str):
    """Verstuur de mail langs de ingestelde weg. Niets ingesteld = stil blijven."""
    channel = mail_channel()
    if channel == "resend":
        _send_via_resend(message)
    elif channel == "smtp":
        _send_via_smtp(message)


def send_email(message: str):
    """
    Mail in een eigen thread. Een SMTP-sessie of API-call kost al snel een paar
    seconden en de handelslus draait elke 10; die mag daar niet op wachten.
    """
    if not mail_channel():
        return

    def _run():
        try:
            _send_email_now(message)
        except Exception as e:
            logger.warning(f"E-mail melding mislukt: {e}")

    threading.Thread(target=_run, daemon=True).start()


def notify(message: str):
    """
    Eén ingang voor alle meldingen; stuurt naar elk kanaal dat is ingesteld.

    De kanalen staan los van elkaar: valt er één weg, dan blijven de meldingen
    via de andere gewoon doorkomen.
    """
    send_telegram(message)
    send_push(message)
    send_email(message)


def get_public_exchange():
    """
    OKX publieke API voor marktdata — geen auth nodig.

    timeout: zonder harde grens kan één call blijven hangen en daarmee de hele
    lus (of een backtest) stilleggen. enableRateLimit: een backtest doet tientallen
    requests achter elkaar; ccxt spreidt ze dan zelf i.p.v. throttling te riskeren.
    """
    return ccxt.okx({
        'options': {'defaultType': 'swap'},
        'timeout': 15000,
        'enableRateLimit': True,
    })

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
                             risk_pct: float) -> float:
    """
    Positiegrootte op basis van een vast risicobedrag.

    De SL staat waar de prijsactie hem dicteert — dus de afstand entry→SL verschilt
    per trade. De positiegrootte compenseert dat exact, zodat het verlies bij een SL
    altijd hetzelfde bedrag is: risk_pct van het saldo (1%, of 0.5% bij counter-trend).

    Er wordt bewust NIET meer geschaald met volatiliteit (vroeger vol_scale, ATR50/ATR14
    geclampt op [0.5, 2.0]). Dat maakte het werkelijke risico 0.5%–2% van het saldo,
    waardoor verliezen tot 8× uiteenliepen zonder dat dat uit de strategie volgde.
    """
    risk_amount = balance * risk_pct
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
        notify(
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
            # SL direct als échte order bij de beurs neerleggen, zodat hij ook
            # afgaat als de bot offline is of tussen twee prijs-checks in valt.
            sync_sl_order(exchange, trade)
            logger.info(
                f"[LIVE] [{signal.setup_type.upper()}] {signal.side.upper()} {qty} {symbol} @ {signal.entry:.0f} | "
                f"SL={signal.stop_loss:.0f} | TP1={signal.tp1:.0f} | TP2={signal.tp2:.0f} | TP3={signal.tp3:.0f}"
            )
            notify(
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

# ─── Beurs-zijdige stop-loss orders ───────────────────────────────────────────
# In live mode ligt de SL als échte order bij OKX, niet alleen in het geheugen van
# de bot. Daardoor wordt hij op het SL-niveau uitgevoerd — ook als de bot offline is
# of tussen twee prijs-checks in een snelle beweging plaatsvindt. Zonder deze order
# sloot de bot pas bij zijn eigen 60s-controle, vaak voorbij de SL, waardoor het
# verlies groter uitviel dan de bedoelde 1%.
#
# Order-id's worden bewust buiten de Trade dataclass gehouden: het is een vluchtig
# beurs-artefact dat niet in de database thuishoort.
_sl_orders: dict = {}


def _remaining_fraction(trade: Trade) -> float:
    """Welk deel van de originele positie staat nog open?"""
    is_ct = getattr(trade, 'counter_trend', False)
    frac  = 0.5 if is_ct else 0.25
    remaining = 1.0
    if trade.tp1_hit: remaining -= frac
    if trade.tp2_hit: remaining -= frac
    if not is_ct and trade.tp3_hit: remaining -= 0.25
    return max(0.0, round(remaining, 6))


def cancel_sl_order(exchange, trade: Trade):
    """Haal de openstaande stop-order bij de beurs weg (live only)."""
    order_id = _sl_orders.pop(trade.id, None)
    if not order_id or state.sim_mode:
        return
    try:
        exchange.cancel_order(order_id, trade.symbol)
    except Exception as e:
        logger.warning(f"Stop-order annuleren mislukt ({order_id}): {e}")


def sync_sl_order(exchange, trade: Trade):
    """
    Zet de stop-order bij de beurs gelijk aan de huidige SL en resterende omvang.
    Wordt aangeroepen na entry, na elke deelafsluiting en na elke SL-verschuiving.
    Mislukt dit, dan blijft de eigen bewaking van de bot als vangnet actief.
    """
    if state.sim_mode:
        return
    cancel_sl_order(exchange, trade)
    qty = round(trade.quantity * _remaining_fraction(trade), 6)
    if qty <= 0:
        return
    close_side = "sell" if trade.side == "buy" else "buy"
    try:
        order = exchange.create_order(
            trade.symbol, 'market', close_side, qty, None,
            {'stopLossPrice': trade.stop_loss, 'reduceOnly': True},
        )
        _sl_orders[trade.id] = order['id']
        logger.info(f"Stop-order bij beurs: {qty} @ {trade.stop_loss:.0f}")
    except Exception as e:
        logger.error(f"Stop-order plaatsen mislukt: {e} — bot bewaakt SL zelf")


def partial_close(exchange, trade: Trade, fraction: float, curr_price: float, label: str):
    """Sluit een deel van de positie — echt of gesimuleerd."""
    qty = round(trade.quantity * fraction, 6)

    if not state.sim_mode:
        try:
            close_side = "sell" if trade.side == "buy" else "buy"
            # reduceOnly: als de beurs de positie al via de stop-order sloot, wordt
            # dit een no-op in plaats van een nieuwe positie in tegengestelde richting.
            exchange.create_market_order(trade.symbol, close_side, qty,
                                         params={'reduceOnly': True})
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

def trail_sl_to_structure(trade: Trade, candles: list, phase: int) -> bool:
    """
    Verschuif SL naar relevante prijsactie na TP2 en TP3.
    phase 2 → laatste swing low/high
    phase 3 → nieuwste swing punt nog dichter bij prijs

    Geeft True terug als de SL daadwerkelijk verschoven is, zodat de aanroeper
    de stop-order bij de beurs alleen dán hoeft te vervangen.
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
                return True

    elif trade.side == "sell" and swing_highs:
        candidates = sorted(
            [p for _, p in swing_highs if p > candles[-1][4]]
        )
        if candidates:
            new_sl = candidates[0] * 1.001  # net erboven
            if new_sl < trade.stop_loss:    # alleen omlaag verschuiven
                logger.info(f"SL verschoven naar swing high: {trade.stop_loss:.0f} → {new_sl:.0f} (fase {phase})")
                trade.stop_loss = new_sl
                return True

    return False

def manage_open_trades(exchange, candles_15m, curr_price: float = None, symbol: str = None):
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
    symbol:     VERPLICHT zodra de bot meer dan één markt volgt. De prijs hoort bij
                één markt, dus zonder filter zou een ETH-positie worden afgerekend
                tegen de bitcoinprijs. None = alle trades (enkel-markt gedrag).
    """
    if curr_price is None:
        curr_price = candles_15m[-1][4]

    for trade in state.trades:
        if trade.status == "closed":
            continue
        if symbol is not None and trade.symbol != symbol:
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
            remaining = _remaining_fraction(trade)

            # Afrekenen op de SL-prijs, niet op de (doorgeschoten) controleprijs.
            # Live ligt de SL als echte order bij de beurs en wordt daar op dit
            # niveau uitgevoerd; de order hieronder is reduceOnly en dus een no-op
            # als de beurs al gesloten heeft. Zo is het verlies bij een SL altijd
            # het bedrag dat bij de positiegrootte hoort — 1% (0.5% counter-trend).
            sl_price = trade.stop_loss
            cancel_sl_order(exchange, trade)
            partial_close(exchange, trade, remaining, sl_price, "❌ SL")
            trade.status = "closed"
            trade.exit_price = sl_price
            update_trade(asdict(trade))
            from db import get_trade_candles, save_candle_snapshot
            snap = get_trade_candles(trade.id)
            entry_ts = snap.get("entry_ts") if isinstance(snap, dict) else None
            save_candle_snapshot(trade.id, candles_15m[-100:], entry_ts=entry_ts)
            _update_setup_health()

            _record_equity()
            state.consecutive_stops += 1
            # Cooldown geldt voor de markt waar de stop viel, niet voor alle markten:
            # een stop op BTC hoort ETH niet stil te leggen.
            state.sl_cooldown_candles = 1  # start cooldown (daytrade: 2 candles op 15m, scalp: 1 candle op 5m)
            if trade.symbol in state.markets:
                state.markets[trade.symbol]["cooldown"] = 1
            notify(
                f"❌ <b>SL HIT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} {trade.symbol}\n"
                f"Entry: {trade.entry_price:.0f} → Exit: {sl_price:.0f}\n"
                f"PnL: {trade.realized_pnl:+.2f} USDT | Stops op rij: {state.consecutive_stops}"
            )

            # Circuit breaker na 5 stops op rij
            if state.consecutive_stops >= 5:
                state.circuit_breaker_until = time.time() + 86400  # 24 uur
                resume = datetime.utcfromtimestamp(state.circuit_breaker_until).strftime('%Y-%m-%d %H:%M UTC')
                logger.warning(f"Circuit breaker actief tot {resume}")
                notify(
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
            sync_sl_order(exchange, trade)  # resterende omvang is gewijzigd
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            notify(
                f"✅ <b>TP1 GERAAKT</b> ({tp_fraction*100:.0f}%) — SL ongewijzigd\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT"
                + (" | counter-trend: max 1.5R" if is_ct else "")
            )

        elif hit_tp2 and is_ct:
            # Counter-trend: TP2 = max target (1.5R) → volledige exit, geen TP3/runner
            cancel_sl_order(exchange, trade)
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
            notify(
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
            sync_sl_order(exchange, trade)  # SL naar BE + kleinere omvang
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            logger.info(f"SL verschoven naar breakeven: {trade.entry_price:.0f}")
            notify(
                f"✅ <b>TP2 GERAAKT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT | SL → breakeven + swing PA"
            )

        elif hit_tp3:
            partial_close(exchange, trade, 0.25, curr_price, "✅ TP3")
            trade.tp3_hit = True
            trade.status = "partial_3"
            trail_sl_to_structure(trade, candles_15m, phase=3)
            sync_sl_order(exchange, trade)
            update_trade(asdict(trade))
            _record_equity()
            state.consecutive_stops = 0
            notify(
                f"✅ <b>TP3 GERAAKT</b>\n"
                f"{trade.setup_type.upper()} {trade.side.upper()} @ {curr_price:.0f}\n"
                f"PnL tot nu: {trade.realized_pnl:+.2f} USDT | Runner actief, SL trailend"
            )

        elif trade.tp3_hit and not is_ct:
            # Runner fase: SL continu trailen op elke nieuwe candle (alleen mét-trend trades).
            # Stop-order alleen vervangen als de SL echt verschoof — anders zou elke
            # candle onnodig een cancel+create bij de beurs veroorzaken.
            if trail_sl_to_structure(trade, candles_15m, phase=4):
                sync_sl_order(exchange, trade)
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
            notify(
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

    # Per markt de eigen prijs ophalen. Eerder werd altijd state.symbol gebruikt
    # en die prijs op álle open trades losgelaten — met meerdere markten zou een
    # ETH-stop dan tegen de bitcoinprijs worden bewaakt.
    for sym in {t.symbol for t in open_trades}:
        try:
            live_price = exchange.fetch_ticker(sym)['last']
            # Minimale fake candle met de live prijs voor de TP/SL-checks
            fake_candle = [0, live_price, live_price, live_price, live_price, 0]
            manage_open_trades(exchange, [fake_candle], symbol=sym)
        except Exception as e:
            logger.warning(f"Live price check mislukt voor {sym}: {e}")


def _market(symbol: str) -> dict:
    """Toestand van één markt; wordt aangemaakt zodra die markt voor het eerst langskomt."""
    return state.markets.setdefault(symbol, {
        "last_15m": "", "last_5m": "", "cooldown": 0, "analysis": {},
    })


def _process_market(exchange, symbol: str, allow_new: bool) -> bool:
    """
    Verwerk één markt: candles ophalen, open posities bijwerken en zo nodig een
    nieuwe setup zoeken. Geeft terug of er een positie is geopend.

    allow_new=False betekent dat er al ergens een positie openstaat. De bot houdt
    bewust ÉÉN positie tegelijk over alle markten samen, zodat het risico 1% van
    je kapitaal blijft en niet 1% per markt.
    """
    m = _market(symbol)

    candles_5m  = get_candles(exchange, symbol, '5m',  limit=60)
    candles_15m = get_candles(exchange, symbol, '15m', limit=150)
    candles_1h  = get_candles(exchange, symbol, '1h',  limit=50)
    candles_4h  = get_candles(exchange, symbol, '4h',  limit=30)
    # 30m voor daytrade secundair timeframe (15m + 30m)
    candles_30m = get_candles(exchange, symbol, '30m', limit=80) \
        if state.trade_mode in ('daytrade', 'both') else None
    # 1m voor scalp secundair timeframe (5m + 1m)
    candles_1m  = get_candles(exchange, symbol, '1m', limit=90) \
        if state.trade_mode in ('scalp', 'both') else None

    curr_5m_ts  = str(candles_5m[-1][0])
    curr_15m_ts = str(candles_15m[-1][0])

    # Nieuwe candle? Per markt bijgehouden — anders onderdrukt de ene markt de andere.
    new_15m = curr_15m_ts != m["last_15m"]
    new_5m  = curr_5m_ts  != m["last_5m"]

    # BELANGRIJK: de laatste candle in elke reeks is de NET GEOPENDE (forming)
    # candle — body ≈ 0, close ≈ open. Entry-condities (body ≥50%, gain the level)
    # moeten op de zojuist GESLOTEN candle worden beoordeeld, anders faalt vrijwel
    # elke setup. Daarom strippen we de forming candle van alle entry-timeframes.
    closed_15m = candles_15m[:-1]
    closed_5m  = candles_5m[:-1]
    closed_30m = candles_30m[:-1] if candles_30m else None
    closed_1m  = candles_1m[:-1]  if candles_1m  else None

    # Daytrade en 'both' werken identiek: 15m heeft prioriteit, 5m is fallback
    # wanneer 15m geen setup oplevert (DoopieCash punt 9 — meer trades genereren
    # door ook lagere timeframes te doorzoeken i.p.v. te wachten op 15m).
    multi_tf  = state.trade_mode in ('daytrade', 'both')
    triggered = new_5m if state.trade_mode == 'scalp' else (new_15m or new_5m)
    if not triggered:
        return False

    if state.trade_mode == 'scalp':
        m["last_5m"] = curr_5m_ts
    else:
        if new_5m:  m["last_5m"] = curr_5m_ts
        if new_15m: m["last_15m"] = curr_15m_ts

    # Scalp/multi-tf: gebruik de verse 5m slotkoers als TP/SL check prijs, zodat
    # trades die op een 5m close raken niet wachten op de volgende 15m close.
    # SL trailing blijft gebaseerd op 15m structuur (via candles_15m).
    mgmt_price = (
        candles_5m[-1][4]
        if (state.trade_mode == 'scalp' or multi_tf) and new_5m
        else None  # manage_open_trades valt terug op candles_15m[-1][4]
    )
    manage_open_trades(exchange, candles_15m, curr_price=mgmt_price, symbol=symbol)

    # Net nadat trades gesloten kunnen zijn: is er genoeg nieuwe ervaring voor een
    # leerronde? De functie bewaakt zelf hoe vaak dat gebeurt.
    _maybe_run_learning()

    # SL-cooldown van déze markt bijwerken
    cooldown_tick = new_5m if state.trade_mode == 'scalp' else new_15m
    if cooldown_tick and m["cooldown"] > 0:
        m["cooldown"] += 1
        if m["cooldown"] >= 3:
            m["cooldown"] = 0
            logger.info(f"[{symbol}] SL-cooldown voorbij — nieuwe setups worden weer gezocht")

    if not allow_new:
        return False

    def _try_place(sig, eff_mode):
        if not sig:
            return None
        # Richting-filter
        if state.trade_direction != 'both':
            allowed = 'buy' if state.trade_direction == 'long_only' else 'sell'
            if sig.side != allowed:
                logger.info(f"[{symbol}] Signal verworpen: TRADE_DIRECTION={state.trade_direction}")
                return None
        # Expiry: entry mag max 0.5% van de huidige prijs afwijken.
        ref_price = candles_5m[-1][4] if eff_mode == 'scalp' else candles_15m[-1][4]
        if abs(sig.entry - ref_price) / ref_price > 0.005:
            logger.info(f"[{symbol}] Signal vervallen: {sig.entry:.0f} vs {ref_price:.0f}")
            return None
        risk_pct = state.risk_per_trade * (0.5 if getattr(sig, 'is_counter_trend', False) else 1.0)
        qty = calculate_position_size(state.balance, sig.entry, sig.stop_loss, risk_pct)
        if qty <= 0:
            return None
        return place_order(exchange, symbol, sig, qty, candles_15m, trade_mode_override=eff_mode)

    common = dict(
        cooldown_candles=m["cooldown"],
        candles_4h=candles_4h,
        session_filter=False,
        # Alleen de munt, niet het hele paar: "[ETH][DAYTRADE] Structuur ..."
        market_label=symbol.split("/")[0],
        **_get_learned_analyze_kwargs(),
    )

    # 15m eerst, 5m als fallback. Eén positie tegelijk, dus zodra er iets geplaatst
    # is houdt het zoeken op.
    attempts = []
    if multi_tf:
        if new_15m:
            attempts.append(('daytrade', lambda: analyze(
                closed_15m, candles_1h, candles_5m=closed_5m,
                candles_30m=closed_30m, **common)))
        if new_5m:
            attempts.append(('scalp', lambda: analyze(
                closed_5m, closed_15m, min_cooldown_candles=1, candles_5m=None,
                candles_1m=closed_1m, scalp_mode=True, **common)))
    elif state.trade_mode == 'scalp':
        attempts.append(('scalp', lambda: analyze(
            closed_5m, closed_15m, min_cooldown_candles=1, candles_5m=None,
            candles_1m=closed_1m, scalp_mode=True, **common)))
    else:
        attempts.append(('daytrade', lambda: analyze(
            closed_15m, candles_1h, candles_5m=closed_5m, **common)))

    for eff, run_analyse in attempts:
        sig = run_analyse()
        m["analysis"][eff] = get_last_analysis()
        trade = _try_place(sig, eff)
        if trade:
            state.trades.append(trade)
            state.last_signal = sig.side
            state.last_setup  = sig.setup_type
            return True
    return False



def run_bot():
    state.sim_mode        = os.environ.get('SIM_MODE', 'true').lower() == 'true'
    state.trade_mode      = os.environ.get('TRADE_MODE', 'daytrade').lower()
    state.trade_direction = os.environ.get('TRADE_DIRECTION', 'both').lower()
    mode_label = "PAPER TRADING" if state.sim_mode else "LIVE TRADING"

    # SYMBOLS mag meerdere markten bevatten, komma-gescheiden. Een via /start
    # meegegeven lijst heeft voorrang; die staat dan al in state.symbols.
    env_symbols = os.environ.get('SYMBOLS', '').strip()
    if env_symbols and state.symbols == ["BTC/USDT:USDT"]:
        state.symbols = [s.strip() for s in env_symbols.split(',') if s.strip()]
    if not state.symbols:
        state.symbols = [state.symbol]
    state.symbol = state.symbols[0]

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

    # Geleerde en goedgekeurde richting/modus toepassen (overschrijft de env-default).
    try:
        _lp = get_learned_params()
        if _lp.get('trade_direction') in ('both', 'long_only', 'short_only'):
            state.trade_direction = _lp['trade_direction']
            logger.info(f"Geleerde richting toegepast: {state.trade_direction}")
        if _lp.get('trade_mode') in ('daytrade', 'scalp', 'both'):
            state.trade_mode = _lp['trade_mode']
            logger.info(f"Geleerde modus toegepast: {state.trade_mode}")
    except Exception as e:
        logger.warning(f"Kon geleerde richting/modus niet toepassen: {e}")

    exchange = get_public_exchange() if state.sim_mode else get_exchange()
    logger.info(f"DoopieCash Bot gestart | {', '.join(state.symbols)} | {mode_label} | "
                f"één positie tegelijk over alle markten")

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

            # ── Markten verwerken ─────────────────────────────────────────────
            # Eén positie tegelijk over ALLE markten samen. Zodra er ergens iets
            # openstaat worden de overige markten alleen nog beheerd, niet
            # geanalyseerd — zo blijft het risico 1% van je kapitaal in plaats
            # van 1% per markt.
            for sym in state.symbols:
                has_open = any(t.status != "closed" for t in state.trades)
                try:
                    _process_market(exchange, sym, allow_new=not has_open)
                except Exception as e:
                    # Eén markt die stukloopt mag de andere niet meeslepen.
                    logger.error(f"[{sym}] verwerken mislukt: {e}")

            if not any(t.status != "closed" for t in state.trades):
                state.last_signal = "none"
                state.last_setup  = "none"

            time.sleep(10)

        except Exception as e:
            logger.error(f"Bot loop fout: {e}")
            time.sleep(30)

    logger.info("Bot gestopt.")
