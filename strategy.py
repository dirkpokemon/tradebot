"""
DoopieCash Strategy Engine
==========================
2 trade setups: Rotation en Continuation

Kernprincipes:
- Prijsconstructie is leidend — de setup bewijst zichzelf via HH+HL / LL+LH
- 4H en 1H geven alleen een score-bonus (met-trend = hogere score), nooit een blokkade
- Beide richtingen altijd tradeable: in uptrend mag je shorten, in downtrend longen
- 'Gain the level': close moet minimaal 0.15% voorbij het swing punt/level sluiten
- Body filter: body van entry candle minimaal 50% van totale candle range
- SL altijd prijsconstructie-gebaseerd: 0.15% buffer buiten het swing punt

Risk management:
- Max 1× open daytrade + 1× open scalp tegelijk (totaal max 2 posities)
- TP1 (25%) — SL niet automatisch naar breakeven, pas bij TP2
- TP2 (25%) → SL naar breakeven + laatste swing prijsactie
- TP3 (25%) → SL naar nieuwer swing punt
- Runner (25%) → SL trailend op marktstructuur
- Counter-trend (setup tegen 4H bias): halve positie, max 1R–1.5R, geen runner
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

# ─── Analyse-diagnostiek ──────────────────────────────────────────────────────
# Houdt bij waarom de laatste analyze() wel/geen signaal opleverde. De bot kopieert
# dit naar state.last_analysis zodat dashboard en /status kunnen tonen waar de
# bot op wacht — in plaats van dagenlang stil te lijken.
_analysis_notes: list = []
_last_analysis: dict = {}

def _note(msg: str):
    _analysis_notes.append(msg)

def get_last_analysis() -> dict:
    return dict(_last_analysis)

# ─── Volume helpers ───────────────────────────────────────────────────────────

def avg_volume(candles: list, n: int = 20) -> float:
    """Gemiddeld volume over de laatste n candles (exclusief huidige)."""
    vols = [c[5] for c in candles[-(n + 1):-1] if c[5] > 0]
    return sum(vols) / len(vols) if vols else 0.0

def vol_confirm(candles: list, factor: float = 1.1, n: int = 20) -> bool:
    """True als het volume van de laatste candle >= factor × gemiddelde."""
    avg = avg_volume(candles, n)
    return avg == 0 or candles[-1][5] >= avg * factor

def vol_weak(candles: list, factor: float = 1.2, n: int = 20) -> bool:
    """True als het volume van de laatste candle < factor × gemiddelde (zwakke pullback)."""
    avg = avg_volume(candles, n)
    return avg == 0 or candles[-1][5] < avg * factor


# ─── Session Filter ────────────────────────────────────────────────────────────

SESSIONS = {
    'london': (8, 12),   # 08:00–12:00 UTC
    'new_york': (13, 17), # 13:00–17:00 UTC
}

def in_active_session(dt: datetime = None) -> tuple[bool, str]:
    """
    Geeft (True, sessienaam) als de huidige UTC tijd binnen London of NY sessie valt.
    Crypto heeft ook buiten deze tijden volume, maar de scherpste price action
    en minste fake-outs vallen binnen deze windows.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    hour = dt.hour
    for name, (start, end) in SESSIONS.items():
        if start <= hour < end:
            return True, name
    return False, 'off-hours'

# ─── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class Level:
    price: float
    strength: int  # hoe vaak getest
    type: str      # 'support' | 'resistance' | 'range_low' | 'range_high'

@dataclass
class Signal:
    setup_type: str          # 'rotation' | 'continuation'
    side: str                # 'buy' | 'sell'
    entry: float
    stop_loss: float
    tp1: float               # 25% uitstap → SL naar breakeven
    tp2: float               # 25% uitstap → SL naar swing prijsactie
    tp3: float               # 25% uitstap → SL naar nieuw swing punt
    # 25% blijft open als runner, SL trailend op marktstructuur
    reason: str
    confidence: float        # 0.0 - 1.0
    session: str = 'unknown' # 'london' | 'new_york' | 'off-hours'
    valid_until: str = ''    # ISO timestamp; na dit tijdstip is het signaal stale
    context_score: int = 0
    context_breakdown: dict = field(default_factory=dict)
    is_counter_trend: bool = False  # True als setup tegen de 4h voorkeursrichting ingaat

# ─── Market Structure ──────────────────────────────────────────────────────────

def get_swing_points(candles, lookback: int = 3):
    """Detecteer swing highs en lows voor marktstructuur analyse."""
    highs = [c[2] for c in candles]
    lows  = [c[3] for c in candles]
    swing_highs, swing_lows = [], []

    for i in range(lookback, len(candles) - lookback):
        if highs[i] == max(highs[i-lookback:i+lookback+1]):
            swing_highs.append((i, highs[i]))
        if lows[i] == min(lows[i-lookback:i+lookback+1]):
            swing_lows.append((i, lows[i]))

    return swing_highs, swing_lows

def get_market_structure(candles) -> str:
    """
    Bepaal trend op basis van HH/HL (uptrend) of LL/LH (downtrend).
    Returnt: 'uptrend' | 'downtrend' | 'ranging'
    """
    swing_highs, swing_lows = get_swing_points(candles, lookback=3)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return 'ranging'

    last_highs = [p for _, p in swing_highs[-3:]]
    last_lows  = [p for _, p in swing_lows[-3:]]

    hh = all(last_highs[i] > last_highs[i-1] for i in range(1, len(last_highs)))
    hl = all(last_lows[i]  > last_lows[i-1]  for i in range(1, len(last_lows)))
    ll = all(last_lows[i]  < last_lows[i-1]  for i in range(1, len(last_lows)))
    lh = all(last_highs[i] < last_highs[i-1] for i in range(1, len(last_highs)))

    if hh and hl:
        return 'uptrend'
    if ll and lh:
        return 'downtrend'
    return 'ranging'

def find_key_levels(candles, tolerance: float = 0.002) -> list[Level]:
    """
    Identificeer sterke support/resistance levels op basis van
    hoe vaak de prijs een zone heeft getest of gerespecteerd.
    """
    swing_highs, swing_lows = get_swing_points(candles, lookback=3)
    levels = {}

    def add_level(price, level_type):
        # Cluster levels die dicht bij elkaar liggen
        for existing in list(levels.keys()):
            if abs(existing - price) / price < tolerance:
                levels[existing]['strength'] += 1
                return
        levels[price] = {'strength': 1, 'type': level_type}

    for _, p in swing_highs:
        add_level(p, 'resistance')
    for _, p in swing_lows:
        add_level(p, 'support')

    return [
        Level(price=p, strength=v['strength'], type=v['type'])
        for p, v in levels.items()
        if v['strength'] >= 1  # alle swing levels meenemen; sterkere krijgen hogere confidence
    ]

# ─── Candlestick Helpers ───────────────────────────────────────────────────────

def is_rejection_candle(candle, direction='bullish') -> bool:
    """Lange wick = afwijzing van een level (pin bar stijl)."""
    o, h, l, c = candle[1], candle[2], candle[3], candle[4]
    body  = abs(c - o)
    total = h - l
    if total == 0:
        return False
    if direction == 'bullish':
        lower_wick = min(o, c) - l
        return lower_wick >= body * 2 and lower_wick / total >= 0.5
    else:
        upper_wick = h - max(o, c)
        return upper_wick >= body * 2 and upper_wick / total >= 0.5

def is_engulfing(candles, direction='bullish') -> bool:
    if len(candles) < 2:
        return False
    prev, curr = candles[-2], candles[-1]
    if direction == 'bullish':
        return (prev[4] < prev[1] and curr[4] > curr[1] and
                curr[1] <= prev[4] and curr[4] >= prev[1])
    else:
        return (prev[4] > prev[1] and curr[4] < curr[1] and
                curr[1] >= prev[4] and curr[4] <= prev[1])

def confirmation_candle(candles, direction='bullish') -> bool:
    """Bevestiging = rejection candle OF engulfing."""
    return (is_rejection_candle(candles[-1], direction) or
            is_engulfing(candles, direction))

def near_level(price, level, tolerance=0.003) -> bool:
    return abs(price - level) / level < tolerance

def gains_level(close: float, level_price: float, side: str, tolerance: float = 0.0015) -> bool:
    """
    DoopieCash 'gain the level': close moet minimaal 0.15% voorbij het level sluiten.
    Een wick erdoorheen telt niet — alleen de slotkoers telt.
    """
    if side == 'buy':
        return close > level_price * (1 - tolerance)
    else:
        return close < level_price * (1 + tolerance)

def has_strong_body(candle, min_ratio: float = 0.5) -> bool:
    """Body minimaal 50% van de totale candle range (filter voor zwakke/doji candles)."""
    o, h, l, c = candle[1], candle[2], candle[3], candle[4]
    total = h - l
    return total > 0 and abs(c - o) / total >= min_ratio

def find_tp_levels(entry: float, side: str, key_levels: list[Level], candles) -> tuple[float, float, float]:
    """
    Zoek de 3 eerstvolgende significante levels voorbij de entry als TP-levels.
    Gebruikt key levels uit de grafiek — geen vaste R:R.
    Valt terug op ATR-gebaseerde afstanden als er onvoldoende levels zijn.
    """
    atr = sum(abs(c[2] - c[3]) for c in candles[-14:]) / 14

    if side == 'buy':
        candidates = sorted(
            [l.price for l in key_levels if l.price > entry * 1.002],
        )
        fallbacks = [entry + atr * 2, entry + atr * 3.5, entry + atr * 5.5]
    else:
        candidates = sorted(
            [l.price for l in key_levels if l.price < entry * 0.998],
            reverse=True
        )
        fallbacks = [entry - atr * 2, entry - atr * 3.5, entry - atr * 5.5]

    # Vul aan met fallbacks als er te weinig levels zijn
    while len(candidates) < 3:
        candidates.append(fallbacks[len(candidates)])

    return candidates[0], candidates[1], candidates[2]

# ─── 5 Setup Detectors ────────────────────────────────────────────────────────

def _refine_sl_5m(candles_5m: list, side: str) -> Optional[float]:
    """
    Algemene 5m SL-verfijning voor alle setups.
    Zoekt de laatste bevestigingscandle in de afgelopen 3 5m-candles
    en geeft de low (buy) of high (sell) terug als tightere SL.
    """
    if not candles_5m or len(candles_5m) < 3:
        return None
    for candle in reversed(candles_5m[-3:]):
        open_, high, low, close = candle[1], candle[2], candle[3], candle[4]
        if side == 'buy' and close > open_:
            return low * 0.9992
        elif side == 'sell' and close < open_:
            return high * 1.0008
    return None




def _pullback_consolidation(candles, level_price: float, min_candles: int = 2,
                            tolerance: float = 0.012) -> bool:
    """
    DoopieCash: vereist minimaal `min_candles` candles consolidatie (inside bars
    of kleine-body candles) vlak bij het pullback-level, direct voorafgaand aan
    de entry-candle — toont dat de markt daadwerkelijk even 'rust' op het level
    in plaats van er zonder pauze doorheen te schieten.
    """
    if len(candles) < min_candles + 2:
        return False
    window = candles[-(min_candles + 1):-1]
    for k in range(1, len(window)):
        c, prev = window[k], window[k-1]
        o, h, l, cl = c[1], c[2], c[3], c[4]
        if not near_level(cl, level_price, tolerance):
            return False
        body, total = abs(cl - o), h - l
        inside_bar = h <= prev[2] and l >= prev[3]
        small_body = total > 0 and body <= total * 0.4
        if not (inside_bar or small_body):
            return False
    return True


def check_continuation(candles, key_levels: list[Level], structure: str,
                       structure_4h: str = None, candles_5m: list = None) -> Optional[Signal]:
    """
    Continuation setup:
    - Prijs in duidelijke trend, pullback naar oud resistance/support level
    - Minimaal 2 candles consolidatie op het level (inside bars / kleine bodies)
    - Body filter: entry candle body ≥50% van totale range
    - Gain filter: close ≥0.15% voorbij het level
    - Met 4H trend → geen extra confirmatie candle nodig
    - Tegen 4H trend → confirmatie candle verplicht
    - SL: 0.15% buiten het meest recente swing punt (low/high van de entry candle)
    """
    if structure not in ('uptrend', 'downtrend'):
        _note("continuation: entry-timeframe is ranging — continuation vereist een duidelijke trend")
        return None

    close = candles[-1][4]
    open_ = candles[-1][1]
    low   = candles[-1][3]
    high  = candles[-1][2]

    for level in key_levels:
        lp = level.price

        # ── Bullish continuation: uptrend, pullback naar oud resistance = nieuw support ──
        if (structure == 'uptrend' and level.type == 'resistance' and
                near_level(close, lp, 0.006) and
                _pullback_consolidation(candles, lp) and
                close > open_ and
                has_strong_body(candles[-1]) and
                gains_level(close, lp, 'buy') and
                level.strength >= 1):

            # Confirmatie alleen verplicht bij echte counter-trend (4H trendt tegen)
            counter_trend = (structure_4h == 'downtrend')
            if counter_trend and not confirmation_candle(candles, 'bullish'):
                _note(f"continuation long bij {lp:.0f}: counter-trend, wacht op confirmatie-candle")
                continue

            sl = low * 0.9985
            sl_5m = _refine_sl_5m(candles_5m, 'buy')
            if sl_5m and sl_5m > sl:
                sl = sl_5m
            if close - sl <= 0 or close - sl < (close * 0.003):
                continue
            tp1, tp2, tp3 = find_tp_levels(close, 'buy', key_levels, candles)
            if tp2 - close < (close - sl) * 1.5:   # TP2 minimaal 1.5R (was 2R — te streng)
                _note(f"continuation long bij {lp:.0f}: alle entry-condities OK maar TP2 < 1.5R")
                continue
            trend_tag = "COUNTER-TREND" if counter_trend else ("met 4H trend" if structure_4h == 'uptrend' else "4H neutraal")
            return Signal(
                setup_type='continuation', side='buy',
                entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                reason=f"Continuation long ({trend_tag}): pullback + consolidatie bij {lp:.0f}",
                confidence=0.74
            )

        # ── Bearish continuation: downtrend, pullback naar oud support = nieuw resistance ──
        if (structure == 'downtrend' and level.type == 'support' and
                near_level(close, lp, 0.006) and
                _pullback_consolidation(candles, lp) and
                close < open_ and
                has_strong_body(candles[-1]) and
                gains_level(close, lp, 'sell') and
                level.strength >= 1):

            counter_trend = (structure_4h == 'uptrend')
            if counter_trend and not confirmation_candle(candles, 'bearish'):
                _note(f"continuation short bij {lp:.0f}: counter-trend, wacht op confirmatie-candle")
                continue

            sl = high * 1.0015
            sl_5m = _refine_sl_5m(candles_5m, 'sell')
            if sl_5m and sl_5m < sl:
                sl = sl_5m
            if sl - close <= 0 or sl - close < (close * 0.003):
                continue
            tp1, tp2, tp3 = find_tp_levels(close, 'sell', key_levels, candles)
            if close - tp2 < (sl - close) * 1.5:   # TP2 minimaal 1.5R (was 2R — te streng)
                _note(f"continuation short bij {lp:.0f}: alle entry-condities OK maar TP2 < 1.5R")
                continue
            trend_tag = "COUNTER-TREND" if counter_trend else ("met 4H trend" if structure_4h == 'downtrend' else "4H neutraal")
            return Signal(
                setup_type='continuation', side='sell',
                entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                reason=f"Continuation short ({trend_tag}): pullback + consolidatie bij {lp:.0f}",
                confidence=0.74
            )
    return None


def _full_reversal_structure(swing_highs, swing_lows, direction: str) -> Optional[dict]:
    """
    DoopieCash rotation-bevestiging: een rotation is pas geldig als de markt een
    volledige nieuwe constructie van 4 afwisselende swingpunten heeft gevormd:
      Bullish: L → H → HL → HH   (lower low, bounce, higher low, higher high)
      Bearish: H → L → LH → LL   (spiegelbeeld)
    Geeft een dict terug met het 'pullback level' (de HL/LH) waar de entry op
    moet wachten, of None als de constructie nog niet compleet/geldig is.
    """
    if len(swing_lows) < 2 or len(swing_highs) < 2:
        return None

    if direction == 'bullish':
        l_idx,  l_price  = swing_lows[-2]     # L  — origineel laagste punt
        hl_idx, hl_price = swing_lows[-1]     # HL — higher low
        h_idx,  h_price  = swing_highs[-2]    # H  — bounce high
        hh_idx, hh_price = swing_highs[-1]    # HH — higher high
        if not (l_idx < h_idx < hl_idx < hh_idx):
            return None
        if not (hl_price > l_price and hh_price > h_price):
            return None
        return {'pullback_level': hl_price}

    else:  # bearish: H → L → LH → LL
        h_idx,  h_price  = swing_highs[-2]    # H  — origineel hoogste punt
        lh_idx, lh_price = swing_highs[-1]    # LH — lower high
        l_idx,  l_price  = swing_lows[-2]     # L  — bounce low
        ll_idx, ll_price = swing_lows[-1]     # LL — lower low
        if not (h_idx < l_idx < lh_idx < ll_idx):
            return None
        if not (lh_price < h_price and ll_price < l_price):
            return None
        return {'pullback_level': lh_price}


def check_rotation(candles, structure_4h: str = None, candles_5m: list = None) -> Optional[Signal]:
    """
    Rotation setup:
    - Volledige constructie vereist: L→H→HL→HH (bullish) of H→L→LH→LL (bearish)
    - Na HH/LL: wacht op pullback naar het HL/LH-punt (vervalt na 15 candles)
    - Body filter: entry candle body ≥50% van totale range
    - Gain filter: close ≥0.15% voorbij het pullback-level
    - Met 4H trend → geen extra confirmatie candle nodig (limit-stijl entry op het level)
    - Tegen 4H trend → confirmatie candle verplicht (rejection of engulfing)
    - SL: 0.15% buiten het meest recente swing punt
    - Volume: geen harde poort (telt mee in context score)
    """
    if len(candles) < 25:
        return None

    swing_highs, swing_lows = get_swing_points(candles[:-1], lookback=3)
    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    close = candles[-1][4]
    open_ = candles[-1][1]
    low   = candles[-1][3]
    high  = candles[-1][2]
    n_prev = len(candles[:-1]) - 1  # index van de laatste confirmed candle in candles[:-1]

    key_levels_temp = (
        [Level(price=p, strength=2, type='support')    for _, p in swing_lows[-4:]] +
        [Level(price=p, strength=2, type='resistance') for _, p in swing_highs[-4:]]
    )

    # NB: geen harde volume-poort meer — volume telt al voor 15 punten mee in de
    # context score; dubbel eisen (poort én score) drukte de frequentie onnodig.

    # ── Bullish rotation: L → H → HL → HH compleet, entry op pullback naar HL ──
    construction = _full_reversal_structure(swing_highs, swing_lows, 'bullish')
    if construction:
        hh_age = n_prev - swing_highs[-1][0]  # candles geleden dat HH bevestigd werd
        pb = construction['pullback_level']
        if hh_age > 15:
            _note(f"rotation bullish: constructie compleet maar HH is {hh_age} candles oud (max 15) — venster verlopen")
        else:
            # Confirmatie alleen verplicht bij ECHTE counter-trend (4H trendt tegen).
            # 4H ranging/onbekend is neutraal — geen extra eis.
            counter_trend = (structure_4h == 'downtrend')
            checks = {
                'pullback-zone (1.2% van HL)': near_level(close, pb, 0.012),
                'bullish close': close > open_,
                'body ≥50%': has_strong_body(candles[-1]),
                'gain level (0.15%)': gains_level(close, pb, 'buy'),
            }
            if counter_trend:
                checks['confirmatie-candle (counter-trend)'] = confirmation_candle(candles, 'bullish')
            if all(checks.values()):
                sl = low * 0.9985
                sl_5m = _refine_sl_5m(candles_5m, 'buy')
                if sl_5m and sl_5m > sl:
                    sl = sl_5m
                if close - sl > 0:
                    tp1, tp2, tp3 = find_tp_levels(close, 'buy', key_levels_temp, candles)
                    trend_tag = "COUNTER-TREND" if counter_trend else ("met 4H trend" if structure_4h == 'uptrend' else "4H neutraal")
                    return Signal(
                        setup_type='rotation', side='buy',
                        entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                        reason=f"Rotation bullish ({trend_tag}): L→H→HL→HH, pullback {pb:.0f}, HH {hh_age}c geleden",
                        confidence=0.78
                    )
            else:
                failed = [k for k, v in checks.items() if not v]
                _note(f"rotation bullish: constructie OK (pullback {pb:.0f}), wacht op: {', '.join(failed)}")

    # ── Bearish rotation: H → L → LH → LL compleet, entry op pullback naar LH ──
    construction = _full_reversal_structure(swing_highs, swing_lows, 'bearish')
    if construction:
        ll_age = n_prev - swing_lows[-1][0]
        pb = construction['pullback_level']
        if ll_age > 15:
            _note(f"rotation bearish: constructie compleet maar LL is {ll_age} candles oud (max 15) — venster verlopen")
        else:
            counter_trend = (structure_4h == 'uptrend')
            checks = {
                'pullback-zone (1.2% van LH)': near_level(close, pb, 0.012),
                'bearish close': close < open_,
                'body ≥50%': has_strong_body(candles[-1]),
                'gain level (0.15%)': gains_level(close, pb, 'sell'),
            }
            if counter_trend:
                checks['confirmatie-candle (counter-trend)'] = confirmation_candle(candles, 'bearish')
            if all(checks.values()):
                sl = high * 1.0015
                sl_5m = _refine_sl_5m(candles_5m, 'sell')
                if sl_5m and sl_5m < sl:
                    sl = sl_5m
                if sl - close > 0:
                    tp1, tp2, tp3 = find_tp_levels(close, 'sell', key_levels_temp, candles)
                    trend_tag = "COUNTER-TREND" if counter_trend else ("met 4H trend" if structure_4h == 'downtrend' else "4H neutraal")
                    return Signal(
                        setup_type='rotation', side='sell',
                        entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                        reason=f"Rotation bearish ({trend_tag}): H→L→LH→LL, pullback {pb:.0f}, LL {ll_age}c geleden",
                        confidence=0.78
                    )
            else:
                failed = [k for k, v in checks.items() if not v]
                _note(f"rotation bearish: constructie OK (pullback {pb:.0f}), wacht op: {', '.join(failed)}")
    return None

# ─── ATR Helper ───────────────────────────────────────────────────────────────

def calc_atr(candles: list, period: int = 14) -> float:
    """Gemiddeld high−low bereik over de laatste `period` candles."""
    n = min(period, len(candles))
    return sum(abs(c[2] - c[3]) for c in candles[-n:]) / n if n > 0 else 0.0

# ─── Context Score ────────────────────────────────────────────────────────────

def _entry_gains_level(candles: list, signal, all_levels: list) -> bool:
    """
    DoopieCash 'gain the level': True als de bevestigingscandle voorbij het
    dichtstbijzijnde relevante level sluit — een wick erdoorheen telt niet,
    de candle moet het gebied voorbij het niveau daadwerkelijk 'winnen'.
    """
    close = candles[-1][4]
    entry = signal.entry
    nearby = [l for l in all_levels if abs(l.price - entry) / entry < 0.01]
    if not nearby:
        return True  # geen nabijgelegen level = geen wick-afwijzing mogelijk
    nearest = min(nearby, key=lambda l: abs(l.price - entry))
    if signal.side == 'buy':
        return close > nearest.price
    else:
        return close < nearest.price


def calculate_context_score(candles_15m: list, candles_1h: list, candles_4h: list,
                             signal, all_levels: list, scalp_mode: bool = False,
                             candles_5m: list = None) -> dict:
    """
    Score 0-100. ATR SL check is mandatory — if it fails, score=0 and setup is invalid.

    Gewichten (totaal = 100):
    - ATR SL minimum gehaald:          5 pts  (verplicht — als NIET gehaald: score=0)
    - 4H trend bevestigd + uitgelijnd: 25 pts
    - Gain filter geslaagd:            20 pts  (close 0.15%+ voorbij level, body 50%+)
    - Context TF (1H/15m) uitlijning:  15 pts
    - Volume >1.2× gemiddeld:          15 pts
    - Level schoon (≤4 touches):       10 pts
    - Round number proximity (0.3%):   10 pts
    """
    breakdown = {}
    score = 0

    # ── Mandatory: SL >= ATR minimum (1.0× scalp op 5m, 1.5× daytrade op 15m) ─
    atr_multiplier = 1.0 if scalp_mode else 1.5
    atr_source = candles_5m if (scalp_mode and candles_5m and len(candles_5m) >= 14) else candles_15m
    atr14 = calc_atr(atr_source, 14)
    sl_dist = abs(signal.entry - signal.stop_loss)
    if atr14 > 0 and sl_dist < atr14 * atr_multiplier:
        return {'score': 0, 'valid': False, 'breakdown': {'atr_sl': 0}}
    breakdown['atr_sl'] = 5
    score += 5

    # ── 4H trend bevestigd EN setup is ermee uitgelijnd ───────────────────────
    if candles_4h and len(candles_4h) >= 10:
        s4h = get_market_structure(candles_4h)
        if ((s4h == 'uptrend'   and signal.side == 'buy') or
            (s4h == 'downtrend' and signal.side == 'sell')):
            breakdown['trend_4h'] = 25; score += 25
        else:
            breakdown['trend_4h'] = 0
    else:
        breakdown['trend_4h'] = 0

    # ── Gain filter: close voorbij level én sterke body ──────────────────────
    if _entry_gains_level(candles_15m, signal, all_levels) and has_strong_body(candles_15m[-1]):
        breakdown['gains_level'] = 20; score += 20
    else:
        breakdown['gains_level'] = 0

    # ── Context TF trend uitlijning (1H voor daytrade, 15m voor scalp) ───────
    s1h = get_market_structure(candles_1h)
    if ((s1h == 'uptrend'   and signal.side == 'buy') or
        (s1h == 'downtrend' and signal.side == 'sell')):
        breakdown['trend_ctx'] = 15; score += 15
    else:
        breakdown['trend_ctx'] = 0

    # ── Volume confirmation: >1.2× gemiddeld van laatste 20 candles ─────────
    avg_vol = avg_volume(candles_15m, 20)
    curr_vol = candles_15m[-1][5] if len(candles_15m[-1]) > 5 else 0
    if avg_vol > 0 and curr_vol >= avg_vol * 1.2:
        breakdown['volume'] = 15; score += 15
    else:
        breakdown['volume'] = 0

    # ── Level schoon: maximaal 4 eerdere touches ──────────────────────────────
    entry = signal.entry
    nearby = [l for l in all_levels if abs(l.price - entry) / entry < 0.005]
    if not nearby or max(l.strength for l in nearby) <= 4:
        breakdown['level_clean'] = 10; score += 10
    else:
        breakdown['level_clean'] = 0

    # ── Round number proximity (binnen 0.3% van x000 of x500) ────────────────
    rounded_000 = round(entry / 1000) * 1000
    rounded_500 = round(entry / 500) * 500
    dist = min(abs(entry - rounded_000), abs(entry - rounded_500)) / entry
    if dist < 0.003:
        breakdown['round_number'] = 10; score += 10
    else:
        breakdown['round_number'] = 0

    return {'score': min(score, 100), 'valid': True, 'breakdown': breakdown}


# ─── Main Analyzer ────────────────────────────────────────────────────────────

def _detect_signal(candles, structure_1h: str, structure_4h: Optional[str],
                   candles_5m: list, off: set, scalp_mode: bool):
    """
    Doorloopt Rotation en Continuation in prioriteitsvolgorde voor één timeframe.
    4H-richting blokkeert nooit — het beïnvloedt alleen de contextscore.
    Geeft (signal, key_levels van dit timeframe) terug.
    """
    levels = find_key_levels(candles)

    sig = (
        (check_rotation(candles, structure_4h, candles_5m) if 'rotation' not in off else None) or
        (check_continuation(candles, levels, structure_1h, structure_4h, candles_5m) if 'continuation' not in off else None)
    )
    return sig, levels


def analyze(candles_15m: list, candles_1h: list, cooldown_candles: int = 0,
            candles_4h: list = None, candles_5m: list = None,
            candles_30m: list = None, candles_1m: list = None,
            session_filter: bool = False,
            disabled_setups: list = None,
            scalp_mode: bool = False,
            min_cooldown_candles: int = 2) -> Optional[Signal]:
    """
    Analyseer de markt op Rotation en Continuation.
    4H en 1H geven score-bonus maar blokkeren nooit — beide richtingen zijn altijd tradeable.
    Prioriteit: rotation > continuation.

    cooldown_candles: candles sinds laatste SL.
    min_cooldown_candles: cooldown-drempel (default 3 op 15m, scalp gebruikt 1).
    candles_4h: bias-richting voor score-bonus en counter-trend risicobeheer.
    candles_30m / candles_1m: secundaire timeframes (daytrade: 15m+30m, scalp: 5m+1m).
    scalp_mode: tightere SL (1.0×ATR op 5m), TP1=0.8R, TP2=1.5R.
    """
    global _analysis_notes, _last_analysis
    _analysis_notes = []
    mode_key = 'scalp' if scalp_mode else 'daytrade'

    if len(candles_15m) < 30 or len(candles_1h) < 20:
        logger.warning("Niet genoeg candles voor analyse")
        _last_analysis = {'mode': mode_key, 'result': 'te weinig candles', 'notes': []}
        return None

    # Cooldown na SL: geen nieuwe entry tot min_cooldown_candles bereikt is
    if cooldown_candles > 0 and cooldown_candles < min_cooldown_candles:
        _last_analysis = {'mode': mode_key,
                          'result': f'SL-cooldown actief ({cooldown_candles}/{min_cooldown_candles} candles)',
                          'notes': []}
        return None

    # Session info (voor logging en signaal metadata — filter niet meer actief)
    now = datetime.now(timezone.utc)
    _, session_name = in_active_session(now)

    # Macro-bias op 4h: trade alleen mee met de 4h trend
    structure_4h = None
    if candles_4h and len(candles_4h) >= 10:
        structure_4h = get_market_structure(candles_4h)

    # Trend bepalen op 1h (hogere context)
    structure_1h  = get_market_structure(candles_1h)
    structure_15m = get_market_structure(candles_15m)

    mode_label = "[SCALP]" if scalp_mode else "[DAYTRADE]"
    logger.info(
        f"{mode_label} Structuur 4h: {structure_4h or '—'} | 1h: {structure_1h} | 15m: {structure_15m}"
        f" | Sessie: {session_name}"
    )

    # Diagnostiek-object: wordt gevuld bij elk eindpunt van deze functie
    info = {
        'mode': mode_key,
        'ts': now.strftime('%Y-%m-%dT%H:%M:%SZ'),
        'structuur': {'4h': structure_4h or 'onbekend', 'context': structure_1h, 'entry': structure_15m},
        'result': None,
        'notes': [],
    }
    _last_analysis = info

    # Check setups in volgorde van prioriteit (sla uitgeschakelde setups over)
    off = set(disabled_setups or [])
    if off:
        logger.info(f"Uitgeschakelde setups: {', '.join(off)}")

    if structure_4h not in ('uptrend', 'downtrend'):
        logger.info(f"{mode_label} 4h is {structure_4h or 'onbekend'} — geen score-bonus (traden wel mogelijk)")

    signal, levels_primary = _detect_signal(candles_15m, structure_1h, structure_4h, candles_5m, off, scalp_mode)
    timeframe_used = '15m' if not scalp_mode else '5m'

    # Frequentie-regel: kijk ook op het secundaire timeframe als het primaire niets oplevert
    # (daytrade: 15m én 30m | scalp: 5m én 1m)
    secondary = candles_30m if not scalp_mode else candles_1m
    levels_secondary = []
    if not signal and secondary and len(secondary) >= 30:
        signal, levels_secondary = _detect_signal(secondary, structure_1h, structure_4h, candles_5m, off, scalp_mode)
        if signal:
            timeframe_used = '30m' if not scalp_mode else '1m'
            logger.info(f"{mode_label} Signal gevonden op secundair timeframe ({timeframe_used})")

    # Key levels op alle beschikbare timeframes (voor context score / TP-validatie)
    levels_1h  = find_key_levels(candles_1h)
    levels_4h  = find_key_levels(candles_4h) if candles_4h and len(candles_4h) >= 10 else []
    all_levels = levels_4h + levels_1h + levels_primary + levels_secondary

    if signal:
        # SL-bron: 5m ATR in scalp modus (DoopieCash: SL = 1.0× ATR op 5m), anders 15m
        atr_source = candles_5m if (scalp_mode and candles_5m and len(candles_5m) >= 14) else candles_15m
        atr = calc_atr(atr_source, 14)

        # SL minimaal 1.5× ATR van entry (1.0× in scalp mode)
        min_sl_multiplier = 1.0 if scalp_mode else 1.5
        min_sl_dist = min_sl_multiplier * atr
        sl_dist = abs(signal.entry - signal.stop_loss)
        if sl_dist < min_sl_dist:
            logger.info(f"SL vergroot: {sl_dist:.0f} → {min_sl_dist:.0f} ({min_sl_multiplier}× ATR={atr:.0f})")
            signal.stop_loss = (
                signal.entry - min_sl_dist if signal.side == 'buy'
                else signal.entry + min_sl_dist
            )

        # 4h is de leidende voorkeursrichting (niet een harde blokkade): setups
        # tégen de 4h trend zijn toegestaan maar lopen via een strenger
        # counter-trend regime — halve positiegrootte, max 1.5R (geen TP3/runner)
        # en een hogere context-score eis (≥70 i.p.v. ≥55).
        is_counter_trend = False
        if structure_4h and structure_4h != 'ranging':
            if ((structure_4h == 'uptrend'   and signal.side == 'sell') or
                (structure_4h == 'downtrend' and signal.side == 'buy')):
                is_counter_trend = True
                logger.info(
                    f"{mode_label} {signal.setup_type} {signal.side.upper()} is COUNTER-TREND "
                    f"(4h={structure_4h}) — strengere regels van toepassing"
                )
        signal.is_counter_trend = is_counter_trend

        risk = abs(signal.entry - signal.stop_loss)

        if is_counter_trend:
            # Counter-trend: max 1.5R, geen TP3/runner — sneller winst nemen tegen de hoofdtrend in
            if signal.side == 'buy':
                signal.tp1 = signal.entry + risk * 1.0
                signal.tp2 = signal.entry + risk * 1.5
            else:
                signal.tp1 = signal.entry - risk * 1.0
                signal.tp2 = signal.entry - risk * 1.5
            signal.tp3 = signal.tp2
            rr = 1.5
        elif scalp_mode:
            # Scalp modus: snellere uitstap — TP1 op 0.8R, TP2 op 1.5R, TP3 als runner-extensie
            if signal.side == 'buy':
                signal.tp1 = signal.entry + risk * 0.8
                signal.tp2 = signal.entry + risk * 1.5
                signal.tp3 = signal.entry + risk * 2.2
            else:
                signal.tp1 = signal.entry - risk * 0.8
                signal.tp2 = signal.entry - risk * 1.5
                signal.tp3 = signal.entry - risk * 2.2
            rr = 2.2
        else:
            # TP volgorde afdwingen: long → oplopend, short → aflopend
            tps = sorted([signal.tp1, signal.tp2, signal.tp3])
            if signal.side == 'buy':
                signal.tp1, signal.tp2, signal.tp3 = tps[0], tps[1], tps[2]
            else:
                signal.tp1, signal.tp2, signal.tp3 = tps[2], tps[1], tps[0]

            # R:R valideren op tp3 (na SL-correctie). Minimaal 2.0 — was 2.5,
            # maar in combinatie met de SL-vergroting naar 1.5× ATR sneuvelden
            # daar te veel geldige setups op.
            reward = abs(signal.tp3 - signal.entry)
            rr = reward / risk if risk > 0 else 0
            if rr < 2.0:
                logger.info(f"Signal afgewezen: R:R te laag ({rr:.1f})")
                info['result'] = f"setup gevonden ({signal.setup_type} {signal.side}) maar afgewezen: R:R {rr:.1f} < 2.0"
                info['notes'] = list(_analysis_notes)
                return None

        # Context score — counter-trend trades hebben een hogere drempel nodig.
        # Frequentie-regel: bij een duidelijke 4h trend (uptrend/downtrend, dus
        # met ≥2 bevestigde swingpunten in get_market_structure) mag de eis
        # omlaag van 55 naar 45 — de 4h-uitlijning compenseert het lagere aantal
        # overige factoren dat hoeft te kloppen.
        ctx = calculate_context_score(candles_15m, candles_1h, candles_4h or [], signal, all_levels,
                                      scalp_mode=scalp_mode, candles_5m=candles_5m)
        # Score-drempel: 4H en 1H geven alleen bonus, blokkeren nooit.
        # 4H+context TF beide aligned → laagste drempel (40)
        # Eén TF aligned → 45
        # Geen alignment (ranging of counter-trend) → normaal (55)
        double_trend_aligned = (
            structure_4h in ('uptrend', 'downtrend')
            and structure_1h == structure_4h
            and not is_counter_trend
        )
        single_trend_aligned = (
            structure_4h in ('uptrend', 'downtrend')
            and not is_counter_trend
        )
        if double_trend_aligned:
            min_score = 40
        elif single_trend_aligned:
            min_score = 45
        else:
            min_score = 55  # ranging of counter-trend: hogere lat (maar niet geblokkeerd)
        if ctx['score'] < min_score:
            logger.info(
                f"Signal afgewezen: context score te laag ({ctx['score']}/{min_score} vereist"
                f"{' — counter-trend' if is_counter_trend else ''})"
            )
            info['result'] = (f"setup gevonden ({signal.setup_type} {signal.side}) maar afgewezen: "
                              f"score {ctx['score']} < {min_score}")
            info['score_breakdown'] = ctx['breakdown']
            info['notes'] = list(_analysis_notes)
            return None
        signal.context_score = ctx['score']
        signal.context_breakdown = ctx['breakdown']

        # Sessie en expiry invullen op het signaal
        signal.session = session_name
        # Signaal is geldig voor 2 candles (30 min op 15m)
        from datetime import timedelta
        signal.valid_until = (now + timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ')

        ct_label = " [COUNTER-TREND]" if is_counter_trend else ""
        logger.info(
            f"{mode_label}{ct_label} Signal ({timeframe_used}): {signal.setup_type.upper()} {signal.side.upper()} | "
            f"{signal.reason} | R:R={rr:.1f} | sessie={session_name} | "
            f"score={ctx['score']}/{min_score} | geldig tot {signal.valid_until}"
        )
        info['result'] = (f"SIGNAAL: {signal.setup_type} {signal.side.upper()} ({timeframe_used}) | "
                          f"score {ctx['score']}/{min_score} | R:R {rr:.1f}")
        info['notes'] = list(_analysis_notes)
    else:
        info['result'] = 'geen setup gevonden'
        info['notes'] = list(_analysis_notes)

    return signal
