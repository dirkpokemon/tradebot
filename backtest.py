"""
Walk-forward backtester voor de DoopieCash strategie.

Aanpak:
  - Haalt historische 15m candles op (configurable aantal dagen)
  - Resamplet intern naar 1h en 4h (geen extra API calls)
  - Test window = laatste test_pct van alle candles
  - Train window = de rest (geeft context aan analyze())
  - Voor elke candle in de test window: analyze() aanroepen, trade simuleren
  - Geen look-ahead bias: analyze() ziet alleen candles t/m huidige candle

Vereenvoudiging t.o.v. live bot:
  - Trailing SL na TP2/TP3 is gebaseerd op recente swing lows/highs uit de candles
  - Position sizing gebruikt ATR maar geen vol_scale (te weinig effect op stats)
"""

import math
import random
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime, timezone

from strategy import analyze, calc_atr, get_swing_points

logger = logging.getLogger(__name__)


# ─── Config & Dataclasses ──────────────────────────────────────────────────────

@dataclass
class BacktestConfig:
    symbol: str          = "BTC/USDT:USDT"
    days: int            = 90      # totale periode
    test_pct: float      = 0.30    # laatste 30% = test window
    risk_per_trade: float = 0.01
    starting_balance: float = 10000.0
    session_filter: bool = True    # London/NY filter aan/uit
    trade_mode: str = 'daytrade'   # 'daytrade' | 'scalp' | 'both' — welke timeframe-modus simuleren

@dataclass
class BtTrade:
    id: int
    setup_type: str
    side: str
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    tp3: float
    entry_idx: int
    quantity: float
    exit_idx: int    = -1
    exit_price: float = 0.0
    realized_pnl: float = 0.0
    status: str      = "open"   # open | closed
    tp1_hit: bool    = False
    tp2_hit: bool    = False
    tp3_hit: bool    = False
    counter_trend: bool = False  # True = setup ging tegen de 4h voorkeursrichting in (max 1.5R, geen TP3/runner)
    trade_mode: str = 'daytrade'  # 'daytrade' (15m trigger) of 'scalp' (5m trigger)

@dataclass
class BacktestResult:
    config: dict             = field(default_factory=dict)
    total_trades: int        = 0
    wins: int                = 0
    losses: int              = 0
    win_rate: float          = 0.0
    profit_factor: float     = 0.0
    sharpe: Optional[float]  = None
    max_drawdown_pct: float  = 0.0
    total_pnl: float         = 0.0
    expectancy: float        = 0.0
    equity_curve: list       = field(default_factory=list)
    trades: list             = field(default_factory=list)
    setup_stats: dict        = field(default_factory=dict)
    counter_trend_stats: dict = field(default_factory=dict)
    mode_stats: dict         = field(default_factory=dict)
    train_period: str        = ""
    test_period: str         = ""
    duration_s: float        = 0.0

@dataclass
class BacktestState:
    running: bool            = False
    progress: float          = 0.0   # 0.0–1.0
    result: Optional[dict]   = None
    error: str               = ""

backtest_state = BacktestState()


# ─── Monte Carlo dataclasses ───────────────────────────────────────────────────

@dataclass
class MonteCarloResult:
    n_simulations: int           = 0
    n_trades: int                = 0
    actual_pnl: float            = 0.0
    actual_max_dd: float         = 0.0
    actual_sharpe: Optional[float] = None

    # Bootstrap CI (resample with replacement)
    bootstrap_pnl_p5:   float   = 0.0
    bootstrap_pnl_p50:  float   = 0.0
    bootstrap_pnl_p95:  float   = 0.0
    bootstrap_dd_p50:   float   = 0.0
    bootstrap_dd_p95:   float   = 0.0
    bootstrap_positive_pct: float = 0.0  # % of bootstrap samples with PnL > 0

    # Random-strategy baseline (coin-flip with same avg win/loss)
    random_pnl_p5:    float     = 0.0
    random_pnl_p50:   float     = 0.0
    random_pnl_p95:   float     = 0.0
    percentile_vs_random: float = 0.0   # actual beats X% of random

    # Verdict
    has_edge: bool  = False
    verdict: str    = ""

    # Histogram: random-baseline distribution with actual marked
    pnl_histogram: list = field(default_factory=list)

    duration_s: float = 0.0

@dataclass
class MonteCarloState:
    running:  bool          = False
    progress: float         = 0.0
    result:   Optional[dict] = None
    error:    str           = ""

monte_carlo_state = MonteCarloState()


# ─── Helpers ───────────────────────────────────────────────────────────────────

def fetch_ohlcv_paginated(exchange, symbol: str, timeframe: str, days: int, per_page: int = 300) -> list:
    """Haal historische candles op in meerdere requests (OKX cap = 300 per request)."""
    tf_minutes = {'1m': 1, '5m': 5, '15m': 15, '30m': 30, '1h': 60, '4h': 240, '1d': 1440}
    tf_ms      = tf_minutes.get(timeframe, 15) * 60 * 1000

    now_ms    = int(time.time() * 1000)
    since_ms  = now_ms - days * 24 * 60 * 60 * 1000

    all_candles: list = []
    current_since = since_ms

    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=per_page)
        if not batch:
            break
        all_candles.extend(batch)
        if len(batch) < per_page or batch[-1][0] >= now_ms:
            break
        current_since = batch[-1][0] + tf_ms

    # Dedupliceer en sorteer op timestamp
    seen, unique = set(), []
    for c in all_candles:
        if c[0] not in seen:
            seen.add(c[0])
            unique.append(c)
    return sorted(unique, key=lambda c: c[0])


def resample_candles(candles_15m: list, factor: int) -> list:
    """Resample 15m → hogere timeframe. factor=4 → 1h, factor=16 → 4h."""
    result = []
    for i in range(0, len(candles_15m) - factor + 1, factor):
        grp   = candles_15m[i:i + factor]
        ts    = grp[0][0]
        open_ = grp[0][1]
        high  = max(c[2] for c in grp)
        low   = min(c[3] for c in grp)
        close = grp[-1][4]
        vol   = sum(c[5] for c in grp)
        result.append([ts, open_, high, low, close, vol])
    return result


def _position_size(balance: float, entry: float, stop: float,
                   risk_pct: float, candles: list) -> float:
    risk_amount   = balance * risk_pct
    risk_per_unit = abs(entry - stop)
    if risk_per_unit == 0:
        return 0.0
    return round(risk_amount / risk_per_unit, 6)


def _trail_sl(trade: BtTrade, candles: list) -> None:
    """Schuif SL naar recente swing punt (alleen in gunstige richting)."""
    swing_highs, swing_lows = get_swing_points(candles[:-1], lookback=3)

    if trade.side == "buy" and swing_lows:
        candidates = sorted(
            [p for _, p in swing_lows if p < candles[-1][4]],
            reverse=True,
        )
        if candidates:
            new_sl = candidates[0] * 0.999
            if new_sl > trade.stop_loss:
                trade.stop_loss = new_sl

    elif trade.side == "sell" and swing_highs:
        candidates = sorted(
            [p for _, p in swing_highs if p > candles[-1][4]]
        )
        if candidates:
            new_sl = candidates[0] * 1.001
            if new_sl < trade.stop_loss:
                trade.stop_loss = new_sl


def _manage_trade(trade: BtTrade, candle_close: float,
                  candles_window: list, balance: float) -> float:
    """
    Simuleert één candle van trade management. Geeft PnL-delta terug.
    Past trade in-place aan (status, tp*_hit, stop_loss, exit_price, realized_pnl).
    """
    pnl_delta = 0.0
    is_ct = trade.counter_trend
    tp_fraction = 0.5 if is_ct else 0.25

    hit_sl = (
        (trade.side == "buy"  and candle_close <= trade.stop_loss) or
        (trade.side == "sell" and candle_close >= trade.stop_loss)
    )
    hit_tp1 = not trade.tp1_hit and (
        (trade.side == "buy"  and candle_close >= trade.tp1) or
        (trade.side == "sell" and candle_close <= trade.tp1)
    )
    hit_tp2 = trade.tp1_hit and not trade.tp2_hit and (
        (trade.side == "buy"  and candle_close >= trade.tp2) or
        (trade.side == "sell" and candle_close <= trade.tp2)
    )
    # Counter-trend trades hebben geen TP3/runner-fase (max 1.5R, volledige exit op TP2)
    hit_tp3 = (not is_ct) and trade.tp2_hit and not trade.tp3_hit and (
        (trade.side == "buy"  and candle_close >= trade.tp3) or
        (trade.side == "sell" and candle_close <= trade.tp3)
    )

    if hit_sl:
        remaining = 1.0
        if trade.tp1_hit: remaining -= tp_fraction
        if trade.tp2_hit: remaining -= tp_fraction
        if not is_ct and trade.tp3_hit: remaining -= 0.25
        qty = trade.quantity * remaining
        pnl = (candle_close - trade.entry_price) * qty if trade.side == "buy" \
              else (trade.entry_price - candle_close) * qty
        trade.realized_pnl += pnl
        pnl_delta = pnl
        trade.exit_price = candle_close
        trade.status = "closed"

    elif hit_tp1:
        qty = trade.quantity * tp_fraction
        pnl = (candle_close - trade.entry_price) * qty if trade.side == "buy" \
              else (trade.entry_price - candle_close) * qty
        trade.realized_pnl += pnl
        pnl_delta = pnl
        trade.tp1_hit = True
        trade.stop_loss = trade.entry_price  # breakeven

    elif hit_tp2 and is_ct:
        # Counter-trend: TP2 = max target (1.5R) → volledige exit
        qty = trade.quantity * tp_fraction
        pnl = (candle_close - trade.entry_price) * qty if trade.side == "buy" \
              else (trade.entry_price - candle_close) * qty
        trade.realized_pnl += pnl
        pnl_delta = pnl
        trade.tp2_hit = True
        trade.tp3_hit = True
        trade.exit_price = candle_close
        trade.status = "closed"

    elif hit_tp2:
        qty = trade.quantity * tp_fraction
        pnl = (candle_close - trade.entry_price) * qty if trade.side == "buy" \
              else (trade.entry_price - candle_close) * qty
        trade.realized_pnl += pnl
        pnl_delta = pnl
        trade.tp2_hit = True
        _trail_sl(trade, candles_window)

    elif hit_tp3:
        qty = trade.quantity * 0.25
        pnl = (candle_close - trade.entry_price) * qty if trade.side == "buy" \
              else (trade.entry_price - candle_close) * qty
        trade.realized_pnl += pnl
        pnl_delta = pnl
        trade.tp3_hit = True
        _trail_sl(trade, candles_window)

    elif trade.tp3_hit and not is_ct:
        _trail_sl(trade, candles_window)

    return pnl_delta


# ─── Monte Carlo helpers ──────────────────────────────────────────────────────

def _mc_max_drawdown(pnls: list, start: float) -> float:
    eq, peak, max_dd = start, start, 0.0
    for pnl in pnls:
        eq += pnl
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd * 100.0


def _mc_sharpe(pnls: list) -> Optional[float]:
    if len(pnls) < 2:
        return None
    n = len(pnls)
    mean_r = sum(pnls) / n
    var = sum((r - mean_r) ** 2 for r in pnls) / (n - 1)
    std_r = math.sqrt(var) if var > 0 else 0.0
    return round(mean_r / std_r * math.sqrt(n), 2) if std_r > 0 else None


def _percentile(sorted_arr: list, p: float) -> float:
    if not sorted_arr:
        return 0.0
    idx = max(0, min(int(len(sorted_arr) * p / 100), len(sorted_arr) - 1))
    return sorted_arr[idx]


def run_monte_carlo(
    trade_pnls: list,
    starting_balance: float = 10000.0,
    n_simulations: int = 1000,
) -> MonteCarloResult:
    """
    Two-pronged Monte Carlo validation:
    1. Bootstrap (resample with replacement) → CI around actual metrics.
    2. Random-strategy baseline (coin-flip with same avg win/loss amounts)
       → percentile rank of actual vs. luck.
    """
    t_start = time.time()
    result  = MonteCarloResult(n_simulations=n_simulations, n_trades=len(trade_pnls))

    if len(trade_pnls) < 5:
        raise ValueError("Minimaal 5 trades nodig voor Monte Carlo analyse")

    result.actual_pnl    = round(sum(trade_pnls), 2)
    result.actual_max_dd = round(_mc_max_drawdown(trade_pnls, starting_balance), 2)
    result.actual_sharpe = _mc_sharpe(trade_pnls)

    rng  = random.Random(42)
    n    = len(trade_pnls)

    wins   = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p <= 0]
    avg_win  = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    # ── 1. Bootstrap (resample with replacement) ──────────────────────────────
    boot_pnls: list[float] = []
    boot_dds:  list[float] = []

    for i in range(n_simulations):
        monte_carlo_state.progress = 0.1 + 0.45 * (i / n_simulations)
        sample = [rng.choice(trade_pnls) for _ in range(n)]
        boot_pnls.append(round(sum(sample), 2))
        boot_dds.append(round(_mc_max_drawdown(sample, starting_balance), 2))

    sorted_bpnl = sorted(boot_pnls)
    sorted_bdd  = sorted(boot_dds)

    result.bootstrap_pnl_p5   = _percentile(sorted_bpnl,  5)
    result.bootstrap_pnl_p50  = _percentile(sorted_bpnl, 50)
    result.bootstrap_pnl_p95  = _percentile(sorted_bpnl, 95)
    result.bootstrap_dd_p50   = _percentile(sorted_bdd,  50)
    result.bootstrap_dd_p95   = _percentile(sorted_bdd,  95)
    result.bootstrap_positive_pct = round(
        sum(1 for p in boot_pnls if p > 0) / n_simulations * 100, 1
    )

    # ── 2. Random strategy baseline (coin-flip) ───────────────────────────────
    rand_pnls: list[float] = []

    for i in range(n_simulations):
        monte_carlo_state.progress = 0.55 + 0.40 * (i / n_simulations)
        sim_pnl = sum(
            avg_win if rng.random() < 0.5 else avg_loss
            for _ in range(n)
        )
        rand_pnls.append(round(sim_pnl, 2))

    sorted_rpnl = sorted(rand_pnls)
    result.random_pnl_p5  = _percentile(sorted_rpnl,  5)
    result.random_pnl_p50 = _percentile(sorted_rpnl, 50)
    result.random_pnl_p95 = _percentile(sorted_rpnl, 95)

    beats_random = sum(1 for p in rand_pnls if p < result.actual_pnl)
    result.percentile_vs_random = round(beats_random / n_simulations * 100, 1)

    # ── Verdict ───────────────────────────────────────────────────────────────
    strong_edge  = result.percentile_vs_random >= 95 and result.bootstrap_positive_pct >= 70
    weak_edge    = result.percentile_vs_random >= 80 or  result.bootstrap_positive_pct >= 60
    result.has_edge = strong_edge
    if strong_edge:
        result.verdict = "Statistisch voordeel aangetoond"
    elif weak_edge:
        result.verdict = "Twijfelachtig — meer trades nodig"
    else:
        result.verdict = "Geen statistisch voordeel — parameters herzien"

    # ── Histogram of random baseline ─────────────────────────────────────────
    all_values = sorted_rpnl + [result.actual_pnl]
    min_v = min(all_values)
    max_v = max(all_values)
    span  = max_v - min_v or 1.0
    n_buckets   = 20
    bucket_size = span / n_buckets

    buckets = [0] * n_buckets
    for p in rand_pnls:
        idx = min(int((p - min_v) / bucket_size), n_buckets - 1)
        buckets[idx] += 1

    actual_bucket = min(int((result.actual_pnl - min_v) / bucket_size), n_buckets - 1)

    result.pnl_histogram = [
        {
            "bucket_center": round(min_v + (i + 0.5) * bucket_size, 0),
            "count": buckets[i],
            "is_actual": i == actual_bucket,
        }
        for i in range(n_buckets)
    ]

    result.duration_s = round(time.time() - t_start, 2)

    logger.info(
        f"Monte Carlo klaar in {result.duration_s}s | "
        f"{n_simulations} sims | rank={result.percentile_vs_random}% | "
        f"edge={result.has_edge}"
    )
    return result


# ─── Main Backtester ───────────────────────────────────────────────────────────

def run_backtest(config: BacktestConfig, exchange) -> BacktestResult:
    t_start = time.time()
    result  = BacktestResult(config=asdict(config))

    # ── Data ophalen ──────────────────────────────────────────────────────────
    trade_mode = config.trade_mode if config.trade_mode in ('daytrade', 'scalp', 'both') else 'daytrade'
    use_5m     = trade_mode in ('scalp', 'both')
    backtest_state.progress = 0.05

    if use_5m:
        # Eén fetch op 5m — 15m/1h/4h worden hieruit geresampled zodat alle
        # timeframes perfect uitgelijnd zijn (geen aparte 15m-fetch nodig).
        logger.info(f"Backtest: {config.symbol} | {config.days}d | trade_mode={trade_mode} | 5m candles ophalen...")
        candles_5m  = fetch_ohlcv_paginated(exchange, config.symbol, '5m', config.days)
        if len(candles_5m) < 600:
            raise ValueError(f"Te weinig 5m candles ontvangen: {len(candles_5m)}")
        candles_15m  = resample_candles(candles_5m, 3)
        candles_1h   = resample_candles(candles_5m, 12)
        candles_4h   = resample_candles(candles_5m, 48)
        exec_candles = candles_5m
        tf_factor    = {'5m': 1, '15m': 3, '1h': 12, '4h': 48}
        exec_label   = '5m'
    else:
        logger.info(f"Backtest: {config.symbol} | {config.days}d | trade_mode={trade_mode} | 15m candles ophalen...")
        candles_15m  = fetch_ohlcv_paginated(exchange, config.symbol, '15m', config.days)
        if len(candles_15m) < 200:
            raise ValueError(f"Te weinig candles ontvangen: {len(candles_15m)}")
        candles_5m   = None
        candles_1h   = resample_candles(candles_15m, 4)
        candles_4h   = resample_candles(candles_15m, 16)
        exec_candles = candles_15m
        tf_factor    = {'15m': 1, '1h': 4, '4h': 16}
        exec_label   = '15m'

    # ── Train / test split (op de uitvoerings-timeframe) ──────────────────────
    n_total      = len(exec_candles)
    n_test       = max(100, int(n_total * config.test_pct))
    n_train      = n_total - n_test
    test_candles = exec_candles[n_train:]

    def ts_to_str(ts_ms):
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')

    result.train_period = f"{ts_to_str(exec_candles[0][0])} → {ts_to_str(exec_candles[n_train-1][0])}"
    result.test_period  = f"{ts_to_str(test_candles[0][0])} → {ts_to_str(test_candles[-1][0])}"
    logger.info(f"Train: {result.train_period} | Test: {result.test_period} ({n_test} {exec_label}-candles)")

    # ── Simulatie ─────────────────────────────────────────────────────────────
    balance     = config.starting_balance
    equity_curve = [{'idx': 0, 'equity': balance, 'ts': ts_to_str(test_candles[0][0])}]
    trades: list[BtTrade] = []
    open_trade: Optional[BtTrade] = None
    trade_counter = 0
    cooldown = 0

    CTX_5M  = 150
    CTX_15M = 100
    CTX_1H  = 50
    CTX_4H  = 30

    for i, candle in enumerate(test_candles):
        backtest_state.progress = 0.1 + 0.85 * (i / n_test)
        global_i = n_train + i
        close    = candle[4]

        # Context vensters per timeframe (alleen verleden zichtbaar, uitgelijnd op exec-index)
        def _ctx(tf_candles, factor, window):
            if tf_candles is None:
                return []
            idx = global_i // factor
            return tf_candles[max(0, idx - window): idx]

        ctx_5m  = _ctx(candles_5m,  tf_factor.get('5m', 1), CTX_5M)
        ctx_15m = _ctx(candles_15m, tf_factor['15m'],       CTX_15M)
        ctx_1h  = _ctx(candles_1h,  tf_factor['1h'],        CTX_1H)
        ctx_4h  = _ctx(candles_4h,  tf_factor['4h'],        CTX_4H)

        # Sluit deze candle ook een 15m candle af? (relevant voor 'both': daytrade
        # heeft op zo'n moment voorrang, net als in run_bot's multi_tf-logica)
        is_new_15m = (not use_5m) or (global_i % tf_factor['15m'] == tf_factor['15m'] - 1)

        # Trade management — gebruik altijd 15m-structuur voor SL-trailing,
        # maar de close van de uitvoerings-timeframe voor TP/SL-detectie
        # (zelfde principe als `mgmt_price` in bot.py: scalp reageert sneller).
        if open_trade and open_trade.status == "open":
            pnl_delta = _manage_trade(open_trade, close, ctx_15m, balance)
            balance  += pnl_delta

            if open_trade.status == "closed":
                open_trade.exit_idx = i
                trades.append(open_trade)
                equity_curve.append({
                    'idx': i,
                    'equity': round(balance, 2),
                    'ts': ts_to_str(candle[0]),
                })
                if open_trade.realized_pnl < 0:
                    cooldown = 1
                open_trade = None
            continue  # één trade tegelijk

        # Cooldown tellen
        if cooldown > 0:
            cooldown += 1
            if cooldown >= 5:
                cooldown = 0

        # Geen open trade: genoeg context?
        if len(ctx_15m) < 30 or len(ctx_1h) < 20 or (use_5m and len(ctx_5m) < 30):
            continue

        signal = None
        effective_mode = 'daytrade'

        if trade_mode == 'daytrade':
            signal = analyze(
                ctx_15m, ctx_1h,
                cooldown_candles=cooldown,
                candles_4h=ctx_4h if len(ctx_4h) >= 10 else None,
                session_filter=config.session_filter,
            )
        elif trade_mode == 'scalp':
            # Pure scalp: 5m is de instap-timeframe, 15m geeft hogere context.
            # Rotation/continuation zijn (net als in run_bot) uitgeschakeld voor scalp.
            signal = analyze(
                ctx_5m, ctx_15m,
                cooldown_candles=cooldown,
                candles_4h=ctx_4h if len(ctx_4h) >= 10 else None,
                candles_5m=None,
                disabled_setups=['rotation', 'continuation'],
                session_filter=config.session_filter,
                scalp_mode=True,
            )
            effective_mode = 'scalp'
        else:
            # 'both': 15m heeft voorrang op een nieuwe 15m-candle, anders 5m-fallback
            # (zelfde prioriteit als multi_tf in run_bot).
            if is_new_15m:
                signal = analyze(
                    ctx_15m, ctx_1h,
                    cooldown_candles=cooldown,
                    candles_4h=ctx_4h if len(ctx_4h) >= 10 else None,
                    candles_5m=ctx_5m if ctx_5m else None,
                    session_filter=config.session_filter,
                )
                if signal:
                    effective_mode = 'daytrade'
            if not signal:
                signal = analyze(
                    ctx_5m, ctx_15m,
                    cooldown_candles=cooldown,
                    candles_4h=ctx_4h if len(ctx_4h) >= 10 else None,
                    candles_5m=None,
                    disabled_setups=['rotation', 'continuation'],
                    session_filter=config.session_filter,
                    scalp_mode=True,
                )
                if signal:
                    effective_mode = 'scalp'

        if signal:
            # Entry drift check (>0.5% stale)
            drift = abs(signal.entry - close) / close
            if drift > 0.005:
                continue

            # Counter-trend setups: halve risico per trade (DoopieCash punt 4 — strenger regime)
            risk_pct = config.risk_per_trade * (0.5 if signal.is_counter_trend else 1.0)
            qty = _position_size(balance, signal.entry, signal.stop_loss,
                                 risk_pct, ctx_15m)
            if qty <= 0:
                continue

            trade_counter += 1
            open_trade = BtTrade(
                id=trade_counter,
                setup_type=signal.setup_type,
                side=signal.side,
                entry_price=signal.entry,
                stop_loss=signal.stop_loss,
                tp1=signal.tp1,
                tp2=signal.tp2,
                tp3=signal.tp3,
                entry_idx=i,
                quantity=qty,
                counter_trend=signal.is_counter_trend,
                trade_mode=effective_mode,
            )

    # Openstaande trade bij einde forceren sluiten op laatste close
    if open_trade and open_trade.status == "open":
        last_close = test_candles[-1][4]
        tp_frac = 0.5 if open_trade.counter_trend else 0.25
        remaining = 1.0
        if open_trade.tp1_hit: remaining -= tp_frac
        if open_trade.tp2_hit: remaining -= tp_frac
        if not open_trade.counter_trend and open_trade.tp3_hit: remaining -= 0.25
        qty = open_trade.quantity * remaining
        pnl = (last_close - open_trade.entry_price) * qty if open_trade.side == "buy" \
              else (open_trade.entry_price - last_close) * qty
        open_trade.realized_pnl += pnl
        open_trade.exit_price = last_close
        open_trade.status = "closed"
        open_trade.exit_idx = len(test_candles) - 1
        balance += pnl
        trades.append(open_trade)

    # ── Statistieken berekenen ────────────────────────────────────────────────
    closed = [t for t in trades if t.status == "closed"]
    wins   = [t for t in closed if t.realized_pnl > 0]
    losses = [t for t in closed if t.realized_pnl <= 0]

    gross_profit = sum(t.realized_pnl for t in wins)
    gross_loss   = abs(sum(t.realized_pnl for t in losses))

    result.total_trades    = len(closed)
    result.wins            = len(wins)
    result.losses          = len(losses)
    result.win_rate        = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
    result.profit_factor   = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None
    result.total_pnl       = round(balance - config.starting_balance, 2)
    result.expectancy      = round(sum(t.realized_pnl for t in closed) / len(closed), 2) if closed else 0.0
    result.equity_curve    = equity_curve

    # Max drawdown
    if equity_curve:
        equities = [e['equity'] for e in equity_curve]
        peak, max_dd = equities[0], 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown_pct = round(max_dd * 100, 2)

    # Sharpe op dagelijkse PnL
    daily: dict = {}
    for t in closed:
        day = ts_to_str(test_candles[t.exit_idx][0])
        daily[day] = daily.get(day, 0.0) + t.realized_pnl
    if len(daily) >= 2:
        returns = list(daily.values())
        n = len(returns)
        mean_r = sum(returns) / n
        var = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
        std_r = math.sqrt(var) if var > 0 else 0
        if std_r > 0:
            result.sharpe = round(mean_r / std_r * math.sqrt(252), 2)

    # Per-setup breakdown
    for setup in ['liquidity_sweep', 'rotation', 'breakout', 'continuation', 'range']:
        ts = [t for t in closed if t.setup_type == setup]
        if not ts:
            result.setup_stats[setup] = {'trades': 0}
            continue
        sw = [t for t in ts if t.realized_pnl > 0]
        sl = [t for t in ts if t.realized_pnl <= 0]
        gp = sum(t.realized_pnl for t in sw)
        gl = abs(sum(t.realized_pnl for t in sl))
        result.setup_stats[setup] = {
            'trades':        len(ts),
            'wins':          len(sw),
            'win_rate':      round(len(sw) / len(ts) * 100, 1),
            'profit_factor': round(gp / gl, 2) if gl > 0 else None,
            'avg_pnl':       round(sum(t.realized_pnl for t in ts) / len(ts), 2),
        }

    # Counter-trend vs. mét-trend opsplitsing
    for label, group in (('counter_trend', [t for t in closed if t.counter_trend]),
                         ('with_trend',    [t for t in closed if not t.counter_trend])):
        if not group:
            result.counter_trend_stats[label] = {'trades': 0}
            continue
        gw = [t for t in group if t.realized_pnl > 0]
        gl = [t for t in group if t.realized_pnl <= 0]
        gp = sum(t.realized_pnl for t in gw)
        gloss = abs(sum(t.realized_pnl for t in gl))
        result.counter_trend_stats[label] = {
            'trades':        len(group),
            'wins':          len(gw),
            'win_rate':      round(len(gw) / len(group) * 100, 1),
            'profit_factor': round(gp / gloss, 2) if gloss > 0 else None,
            'avg_pnl':       round(sum(t.realized_pnl for t in group) / len(group), 2),
        }

    # Daytrade vs. scalp opsplitsing (alleen relevant bij trade_mode='both',
    # maar wordt altijd berekend zodat het overzicht consistent blijft)
    for label, group in (('daytrade', [t for t in closed if t.trade_mode == 'daytrade']),
                         ('scalp',    [t for t in closed if t.trade_mode == 'scalp'])):
        if not group:
            result.mode_stats[label] = {'trades': 0}
            continue
        mw = [t for t in group if t.realized_pnl > 0]
        ml = [t for t in group if t.realized_pnl <= 0]
        mp = sum(t.realized_pnl for t in mw)
        mloss = abs(sum(t.realized_pnl for t in ml))
        result.mode_stats[label] = {
            'trades':        len(group),
            'wins':          len(mw),
            'win_rate':      round(len(mw) / len(group) * 100, 1),
            'profit_factor': round(mp / mloss, 2) if mloss > 0 else None,
            'avg_pnl':       round(sum(t.realized_pnl for t in group) / len(group), 2),
        }

    result.trades    = [asdict(t) for t in closed[-200:]]  # max 200 trades teruggeven
    result.duration_s = round(time.time() - t_start, 1)

    logger.info(
        f"Backtest klaar in {result.duration_s}s | "
        f"{result.total_trades} trades | WR={result.win_rate}% | "
        f"PF={result.profit_factor} | PnL={result.total_pnl:+.0f}"
    )
    backtest_state.progress = 1.0
    return result
