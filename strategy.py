"""
DoopieCash Strategy Engine
==========================
4 trade setups op 15m/1h timeframe:

1. LIQUIDITY SWEEP — wick door een key level, sluit terug aan de andere kant (fake-out)
2. ROTATION        — structuurbreuk (LL/HH) + bevestiging via afwijzing (lange wick of engulfing)
3. BREAKOUT        — candle sluit boven/onder key level + retest van dat level
4. CONTINUATION    — pullback naar vorige constructie (oud resistance = nieuwe support)

Risk management:
- Stop loss: net buiten het af te dekken prijsgebied (wick/level), met 0.1-0.2%
  buffer voorbij het swingpunt (geen liquidity pools), minimaal 1.5× ATR(14)
- Take profits op significante levels in de grafiek (mét-trend setups):
    TP1 (25%) → SL naar breakeven
    TP2 (25%) → SL naar laatste swing low/high (prijsactie)
    TP3 (25%) → SL naar nieuw swing punt dichter bij prijs
    25% blijft open als "runner" → SL blijft meeschuiven met marktstructuur
- Trend filter: higher highs & higher lows op 1h, 4h is leidende voorkeursrichting

Counter-trend regime (4h is voorkeur, geen harde blokkade):
- Setups tegen de 4h trend in zijn toegestaan onder strengere voorwaarden:
  halve positiegrootte, max 1.5R target (TP1=1R 50%, TP2=1.5R 50% — geen TP3/runner),
  context score ≥70 (i.p.v. ≥55 normaal), gelabeld als "counter_trend"

'Gain the level': een entry is alleen geldig als de bevestigingscandle voorbij
het relevante swingpunt SLUIT — een wick erdoorheen die terugkeert telt niet.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

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
    setup_type: str          # 'liquidity_sweep' | 'rotation' | 'breakout' | 'continuation'
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


def _refine_sl_with_5m(candles_5m: list, level_price: float, side: str) -> Optional[float]:
    """
    Zoek in de laatste 3 5m-candles de sweep-candle die het 15m-signaal veroorzaakte.
    Geeft de tightere 5m-SL terug als die gevonden wordt, anders None.
    Een 15m-candle = 3 5m-candles; dezelfde wick zit daarin maar compacter.
    """
    if not candles_5m or len(candles_5m) < 3:
        return None
    for candle in reversed(candles_5m[-3:]):
        open_, high, low, close = candle[1], candle[2], candle[3], candle[4]
        body = abs(close - open_)
        if body == 0:
            continue
        if side == 'buy':
            lower_wick = min(open_, close) - low
            if (low < level_price * 0.9998 and close > level_price and
                    lower_wick > body * 1.2 and close > open_):
                return low * 0.9990  # tighter dan 15m SL
        else:
            upper_wick = high - max(open_, close)
            if (high > level_price * 1.0002 and close < level_price and
                    upper_wick > body * 1.2 and close < open_):
                return high * 1.0010
    return None


def check_liquidity_sweep(candles, key_levels: list[Level], structure: str,
                           candles_5m: list = None) -> Optional[Signal]:
    """
    Liquidity Sweep setup:
    - Wick steekt voorbij een key level (jaagt stops na)
    - Candle sluit terug aan de andere kant van het level (fake-out bevestigd)
    - Wick is minimaal 1.5× de body
    - SL net voorbij de sweepwick, entry op close
    - candles_5m: optioneel — als opgegeven wordt SL verfijnd op 5m wick (tighter = betere R:R)

    Verschil met breakout: bij breakout verwacht je dat prijs doorloopt.
    Bij een sweep verwacht je dat prijs keert — de doorbraak was een val.
    """
    if len(candles) < 10:
        return None

    curr  = candles[-1]
    open_ = curr[1]
    high  = curr[2]
    low   = curr[3]
    close = curr[4]
    body  = abs(close - open_)
    total_range = high - low
    atr   = calc_atr(candles, 14)

    # Minimale wickgrootte: 0.5× ATR zodat kleine wicks worden genegeerd
    min_wick = atr * 0.5

    # DoopieCash-versterking: reversal candle moet een sterke body tonen (≥60%
    # van de totale candle range) — zwakke bodies wijzen op besluiteloosheid
    strong_body = total_range > 0 and body / total_range >= 0.6

    # Volume op de reversal candle moet duidelijk verhoogd zijn (>1.3× gemiddeld
    # van de laatste 20 candles) — bevestigt dat er echt liquiditeit gepakt is
    avg_vol = avg_volume(candles, 20)
    strong_volume = avg_vol == 0 or curr[5] > avg_vol * 1.3

    for level in sorted(key_levels, key=lambda l: -l.strength):
        # Level moet 'schoon' zijn — max 3 eerdere touches, anders is de
        # liquiditeit op dat niveau al opgedroogd en is een sweep minder betrouwbaar
        if level.strength > 3:
            continue
        lp = level.price

        # ── Bullish sweep: wick onder support, sluit terug erboven ────────────
        if structure in ('uptrend', 'ranging'):
            lower_wick = min(open_, close) - low
            swept_below = low < lp * 0.997     # wick gaat minimaal 0.3% door het level (duidelijke sweep)
            closed_above = close > lp          # maar sluit erboven — wint het niveau terug
            wick_significant = lower_wick > max(body * 1.5, min_wick)
            bullish_close = close > open_

            if (swept_below and closed_above and wick_significant and bullish_close
                    and strong_body and strong_volume):
                sl = low * 0.9985
                # Probeer tightere 5m SL — verbetert R:R zonder signal te verwerpen
                sl_5m = _refine_sl_with_5m(candles_5m, lp, 'buy')
                if sl_5m and sl_5m > sl:  # alleen gebruiken als het tighter is
                    sl = sl_5m
                if close - sl < atr * 0.3:
                    continue
                tp1, tp2, tp3 = find_tp_levels(close, 'buy', key_levels, candles)
                refined = "5m" if sl_5m else "15m"
                return Signal(
                    setup_type='liquidity_sweep', side='buy',
                    entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                    reason=f"Bullish sweep onder {lp:.0f} (wick {lower_wick:.0f}, body {body/total_range*100:.0f}%, SL={refined}, strength={level.strength})",
                    confidence=min(0.76 + level.strength * 0.04 + (0.05 if sl_5m else 0), 0.95),
                )

        # ── Bearish sweep: wick boven resistance, sluit terug eronder ─────────
        if structure in ('downtrend', 'ranging'):
            upper_wick = high - max(open_, close)
            swept_above  = high > lp * 1.003   # wick gaat minimaal 0.3% door het level (duidelijke sweep)
            closed_below = close < lp           # maar sluit eronder — wint het niveau terug
            wick_significant = upper_wick > max(body * 1.5, min_wick)
            bearish_close = close < open_

            if (swept_above and closed_below and wick_significant and bearish_close
                    and strong_body and strong_volume):
                sl = high * 1.0015
                sl_5m = _refine_sl_with_5m(candles_5m, lp, 'sell')
                if sl_5m and sl_5m < sl:  # alleen gebruiken als het tighter is
                    sl = sl_5m
                if sl - close < atr * 0.3:
                    continue
                tp1, tp2, tp3 = find_tp_levels(close, 'sell', key_levels, candles)
                refined = "5m" if sl_5m else "15m"
                return Signal(
                    setup_type='liquidity_sweep', side='sell',
                    entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                    reason=f"Bearish sweep boven {lp:.0f} (wick {upper_wick:.0f}, body {body/total_range*100:.0f}%, SL={refined}, strength={level.strength})",
                    confidence=min(0.76 + level.strength * 0.04 + (0.05 if sl_5m else 0), 0.95),
                )

    return None


def check_breakout(candles, key_levels: list[Level], structure: str,
                   structure_4h: str = None, candles_5m: list = None) -> Optional[Signal]:
    """
    Breakout setup (DoopieCash — gerepareerd):
    - NIET instappen op de breakout-candle zelf
    - Wacht op een retest van het gebroken level (binnen 5 candles, anders vervalt de setup)
    - Op de retest moet de candle het gebroken level opnieuw 'winnen': bullish
      retest sluit boven het level, bearish retest sluit eronder
    - Alleen in de richting van de 4h trend — geen counter-trend breakouts
    """
    if len(candles) < 15:
        return None

    curr  = candles[-1]
    open_ = curr[1]
    high  = curr[2]
    low   = curr[3]
    close = curr[4]

    # Hoeveel candles geleden was de breakout-candle? Max 5, en niet de huidige.
    def _recent_breakout_offset(level_price, side):
        for j in range(2, min(7, len(candles))):
            bc, bc_prev = candles[-j], candles[-j-1]
            if side == 'buy' and bc_prev[4] < level_price and bc[4] > level_price * 1.002:
                return j - 1   # aantal candles tussen breakout en nu
            if side == 'sell' and bc_prev[4] > level_price and bc[4] < level_price * 0.998:
                return j - 1
        return None

    for level in key_levels:
        lp = level.price

        # ── Bullish breakout + retest ─────────────────────────────────────────
        if structure in ('uptrend', 'ranging') and structure_4h != 'downtrend':
            offset = _recent_breakout_offset(lp, 'buy')
            if offset and offset <= 5:
                # Retest: prijs komt terug naar het level én wint het opnieuw (close erboven)
                if (near_level(low, lp, 0.008) and close > lp and close > open_ and
                        confirmation_candle(candles, 'bullish')):
                    sl = low - (close - low) * 0.3
                    sl_5m = _refine_sl_5m(candles_5m, 'buy')
                    if sl_5m and sl_5m > sl:
                        sl = sl_5m
                    tp1, tp2, tp3 = find_tp_levels(close, 'buy', key_levels, candles)
                    return Signal(
                        setup_type='breakout', side='buy',
                        entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                        reason=f"Bullish breakout van {lp:.0f}, retest na {offset} candle(s) wint het niveau terug",
                        confidence=0.75 + min(level.strength * 0.05, 0.2)
                    )

        # ── Bearish breakout + retest ─────────────────────────────────────────
        if structure in ('downtrend', 'ranging') and structure_4h != 'uptrend':
            offset = _recent_breakout_offset(lp, 'sell')
            if offset and offset <= 5:
                if (near_level(high, lp, 0.008) and close < lp and close < open_ and
                        confirmation_candle(candles, 'bearish')):
                    sl = high + (high - close) * 0.3
                    sl_5m = _refine_sl_5m(candles_5m, 'sell')
                    if sl_5m and sl_5m < sl:
                        sl = sl_5m
                    tp1, tp2, tp3 = find_tp_levels(close, 'sell', key_levels, candles)
                    return Signal(
                        setup_type='breakout', side='sell',
                        entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                        reason=f"Bearish breakout van {lp:.0f}, retest na {offset} candle(s) wint het niveau terug",
                        confidence=0.75 + min(level.strength * 0.05, 0.2)
                    )
    return None


def _pullback_consolidation(candles, level_price: float, min_candles: int = 3,
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
    Continuation setup (DoopieCash — gerepareerd):
    - Trend is duidelijk (uptrend of downtrend) op zowel 4h als 1h — beide
      moeten daadwerkelijk trenden, niet ranging zijn
    - Pullback stopt op een interessant level (oud resistance = nieuwe support
      of andersom) en consolideert daar minimaal 3 candles (inside bars / kleine bodies)
    - Entry-candle moet de trendrichting 'gainen' met zijn close
    """
    if structure not in ('uptrend', 'downtrend'):
        return None
    # Beide 4h én 1h moeten in dezelfde richting trenden — geen ranging hogere timeframe
    if structure_4h is not None and structure_4h != structure:
        return None

    close = candles[-1][4]
    open_ = candles[-1][1]
    low   = candles[-1][3]
    high  = candles[-1][2]

    for level in key_levels:
        lp = level.price

        if (structure == 'uptrend' and level.type == 'resistance' and
                near_level(close, lp, 0.006) and
                _pullback_consolidation(candles, lp) and
                close > open_ and close > lp and    # 'wint' het niveau: sluit erboven, niet alleen wick
                confirmation_candle(candles, 'bullish') and
                level.strength >= 1):
            sl = low - abs(close - lp) * 0.6
            sl_5m = _refine_sl_5m(candles_5m, 'buy')
            if sl_5m and sl_5m > sl:
                sl = sl_5m
            if close - sl <= 0 or close - sl < (close * 0.003):
                continue
            tp1, tp2, tp3 = find_tp_levels(close, 'buy', key_levels, candles)
            if tp2 - close < (close - sl) * 2:
                continue
            return Signal(
                setup_type='continuation', side='buy',
                entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                reason=f"Continuation long: pullback + consolidatie bij {lp:.0f} (oud resistance)",
                confidence=0.74
            )

        if (structure == 'downtrend' and level.type == 'support' and
                near_level(close, lp, 0.006) and
                _pullback_consolidation(candles, lp) and
                close < open_ and close < lp and    # 'wint' het niveau: sluit eronder, niet alleen wick
                confirmation_candle(candles, 'bearish') and
                level.strength >= 1):
            sl = high + abs(lp - close) * 0.6
            sl_5m = _refine_sl_5m(candles_5m, 'sell')
            if sl_5m and sl_5m < sl:
                sl = sl_5m
            if sl - close <= 0 or sl - close < (close * 0.003):
                continue
            tp1, tp2, tp3 = find_tp_levels(close, 'sell', key_levels, candles)
            if close - tp2 < (sl - close) * 2:
                continue
            return Signal(
                setup_type='continuation', side='sell',
                entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                reason=f"Continuation short: pullback + consolidatie bij {lp:.0f} (oud support)",
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
        return {'pullback_level': hl_price, 'construction_idx': hh_idx}

    else:  # bearish: H → L → LH → LL
        h_idx,  h_price  = swing_highs[-2]    # H  — origineel hoogste punt
        lh_idx, lh_price = swing_highs[-1]    # LH — lower high
        l_idx,  l_price  = swing_lows[-2]     # L  — bounce low
        ll_idx, ll_price = swing_lows[-1]     # LL — lower low
        if not (h_idx < l_idx < lh_idx < ll_idx):
            return None
        if not (lh_price < h_price and ll_price < l_price):
            return None
        return {'pullback_level': lh_price, 'construction_idx': ll_idx}


def check_rotation(candles, candles_5m: list = None) -> Optional[Signal]:
    """
    Rotation setup (DoopieCash — verder aangescherpt):
    - Pas geldig als de markt een VOLLEDIGE nieuwe constructie van 4 afwisselende
      swingpunten heeft gevormd: L→H→HL→HH (bullish) of H→L→LH→LL (bearish) —
      een kale structuurbreuk is niet meer genoeg
    - NIET instappen op de structuurbreuk-candle zelf: wachten op een pullback
      naar een interessant prijsgebied (het HL/LH-punt van de constructie)
    - Entry op de eerste candle die de nieuwe richting bevestigt na de pullback —
      de close moet het pullback-level 'winnen' (gainen, geen wick-only)
    - Volume filter: bevestigingscandle moet > 1.2× gemiddeld volume tonen
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

    # Tijdelijke levels voor TP berekening
    key_levels_temp = (
        [Level(price=p, strength=2, type='support')    for _, p in swing_lows[-4:]] +
        [Level(price=p, strength=2, type='resistance') for _, p in swing_highs[-4:]]
    )

    avg_vol = avg_volume(candles, 20)
    strong_volume = avg_vol == 0 or candles[-1][5] > avg_vol * 1.2

    last_idx = len(candles) - 1

    # ── Bullish rotation: L → H → HL → HH compleet, entry op pullback naar HL ──
    construction = _full_reversal_structure(swing_highs, swing_lows, 'bullish')
    if construction:
        pb = construction['pullback_level']
        # de constructie (HH) moet al gevormd zijn — niet instappen op die candle zelf
        on_break_candle = construction['construction_idx'] >= last_idx - 1
        if (not on_break_candle and strong_volume and
                near_level(close, pb, 0.01) and
                close > open_ and close > pb and
                confirmation_candle(candles, 'bullish')):
            sl = low - abs(close - pb) * 0.3
            sl_5m = _refine_sl_5m(candles_5m, 'buy')
            if sl_5m and sl_5m > sl:
                sl = sl_5m
            if close - sl > 0:
                tp1, tp2, tp3 = find_tp_levels(close, 'buy', key_levels_temp, candles)
                return Signal(
                    setup_type='rotation', side='buy',
                    entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                    reason=f"Rotation bullish: volledige omslag L→H→HL→HH bevestigd, entry op pullback naar {pb:.0f}",
                    confidence=0.78
                )

    # ── Bearish rotation: H → L → LH → LL compleet, entry op pullback naar LH ──
    construction = _full_reversal_structure(swing_highs, swing_lows, 'bearish')
    if construction:
        pb = construction['pullback_level']
        on_break_candle = construction['construction_idx'] >= last_idx - 1
        if (not on_break_candle and strong_volume and
                near_level(close, pb, 0.01) and
                close < open_ and close < pb and
                confirmation_candle(candles, 'bearish')):
            sl = high + abs(pb - close) * 0.3
            sl_5m = _refine_sl_5m(candles_5m, 'sell')
            if sl_5m and sl_5m < sl:
                sl = sl_5m
            if sl - close > 0:
                tp1, tp2, tp3 = find_tp_levels(close, 'sell', key_levels_temp, candles)
                return Signal(
                    setup_type='rotation', side='sell',
                    entry=close, stop_loss=sl, tp1=tp1, tp2=tp2, tp3=tp3,
                    reason=f"Rotation bearish: volledige omslag H→L→LH→LL bevestigd, entry op pullback naar {pb:.0f}",
                    confidence=0.78
                )
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
    Returns dict with 'score' (int), 'valid' (bool), 'breakdown' (dict of factor->points).

    Factoren (DoopieCash-gewogen — 4H-uitlijning en 'gain the level' wegen het zwaarst):
    - ATR SL geldigheid:                15 pts (verplicht)
    - 4H trend bevestigd + uitgelijnd:  25 pts
    - Bevestigingscandle wint niveau:   20 pts
    - 1H trend uitlijning:              15 pts
    - Volume bevestiging:               10 pts
    - Level cleanliness:                10 pts
    - Round number nabijheid:            5 pts
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
    breakdown['atr_sl'] = 15
    score += 15

    # ── 4H trend bevestigd EN setup is ermee uitgelijnd (DoopieCash kernregel) ─
    if candles_4h and len(candles_4h) >= 10:
        s4h = get_market_structure(candles_4h)
        if ((s4h == 'uptrend'   and signal.side == 'buy') or
            (s4h == 'downtrend' and signal.side == 'sell')):
            breakdown['trend_4h'] = 25; score += 25
        else:
            breakdown['trend_4h'] = 0
    else:
        breakdown['trend_4h'] = 0

    # ── Bevestigingscandle 'wint' het niveau (close voorbij swingpunt) ─────────
    if _entry_gains_level(candles_15m, signal, all_levels):
        breakdown['gains_level'] = 20; score += 20
    else:
        breakdown['gains_level'] = 0

    # ── 1H trend confirms signal direction ────────────────────────────────────
    s1h = get_market_structure(candles_1h)
    if ((s1h == 'uptrend'   and signal.side == 'buy') or
        (s1h == 'downtrend' and signal.side == 'sell')):
        breakdown['trend_1h'] = 15; score += 15
    else:
        breakdown['trend_1h'] = 0

    # ── Volume confirmation ───────────────────────────────────────────────────
    avg_vol = avg_volume(candles_15m, 20)
    curr_vol = candles_15m[-1][5] if len(candles_15m[-1]) > 5 else 0
    if avg_vol > 0 and curr_vol >= avg_vol * 1.2:
        breakdown['volume'] = 10; score += 10
    else:
        breakdown['volume'] = 0

    # ── Level cleanliness: max 2 prior touches ────────────────────────────────
    entry = signal.entry
    nearby = [l for l in all_levels if abs(l.price - entry) / entry < 0.005]
    if nearby and max(l.strength for l in nearby) <= 2:
        breakdown['level_clean'] = 10; score += 10
    elif not nearby:
        breakdown['level_clean'] = 10; score += 10  # no prior level = clean
    else:
        breakdown['level_clean'] = 0

    # ── Round number proximity (within 0.3% of x000 or x500) ─────────────────
    rounded_000 = round(entry / 1000) * 1000
    rounded_500 = round(entry / 500) * 500
    dist = min(abs(entry - rounded_000), abs(entry - rounded_500)) / entry
    if dist < 0.003:
        breakdown['round_number'] = 5; score += 5
    else:
        breakdown['round_number'] = 0

    # 'valid' geeft alleen aan dat de verplichte ATR-SL check is gepasseerd —
    # de daadwerkelijke score-drempel (55 normaal / 70 counter-trend) wordt
    # in analyze() toegepast, omdat die afhangt van het type setup.
    return {'score': min(score, 100), 'valid': True, 'breakdown': breakdown}


# ─── Main Analyzer ────────────────────────────────────────────────────────────

def _detect_signal(candles, structure_1h: str, structure_4h: Optional[str],
                   candles_5m: list, off: set, scalp_mode: bool):
    """
    Doorloopt de setup-checkers in DoopieCash-prioriteitsvolgorde voor één
    timeframe en past de overkoepelende regels toe:
    - 4h trend verplicht: bij ranging/onduidelijke 4h is ALLEEN Liquidity Sweep
      toegestaan — alle andere setups zijn geblokkeerd
    - Scalp modus: ALLEEN Liquidity Sweep en Breakout toegestaan
    Geeft (signal, key_levels van dit timeframe) terug.
    """
    levels = find_key_levels(candles)
    clear_4h_trend = structure_4h in ('uptrend', 'downtrend')

    sweep = check_liquidity_sweep(candles, levels, structure_1h, candles_5m) \
        if 'liquidity_sweep' not in off else None
    if sweep:
        return sweep, levels

    if not clear_4h_trend:
        # 4h ranging/onduidelijk → alleen Liquidity Sweep toegestaan
        return None, levels

    if scalp_mode:
        sig = check_breakout(candles, levels, structure_1h, structure_4h, candles_5m) \
            if 'breakout' not in off else None
        return sig, levels

    sig = (
        (check_rotation(candles, candles_5m) if 'rotation' not in off else None) or
        (check_breakout(candles, levels, structure_1h, structure_4h, candles_5m) if 'breakout' not in off else None) or
        (check_continuation(candles, levels, structure_1h, structure_4h, candles_5m) if 'continuation' not in off else None)
    )
    return sig, levels


def analyze(candles_15m: list, candles_1h: list, cooldown_candles: int = 0,
            candles_4h: list = None, candles_5m: list = None,
            candles_30m: list = None, candles_1m: list = None,
            session_filter: bool = False,
            disabled_setups: list = None,
            scalp_mode: bool = False) -> Optional[Signal]:
    """
    Analyseer de markt op alle 4 DoopieCash setups.
    Gebruikt 4h als leidende trendrichting, 1h voor trendbevestiging, 15m (of 5m
    in scalp modus) voor instap. Prioriteit: liquidity_sweep > rotation > breakout > continuation

    Overkoepelende DoopieCash-regels:
    - 4h trend is verplicht: bij ranging/onduidelijke 4h structuur is ALLEEN
      Liquidity Sweep toegestaan — dat is de enige setup die ook in een
      onduidelijke markt liquiditeit mag pakken.
    - 'Gain the level': bevestigingscandles moeten relevante levels met hun
      CLOSE winnen — een wick alleen telt niet (afgedwongen in elke checker).
    - Scalp modus: alleen Liquidity Sweep en Breakout toegestaan; TP1=0.8R,
      TP2=1.5R, SL=1.0× ATR op 5m.

    cooldown_candles: aantal candles sinds laatste SL — geen trades tijdens cooldown
        (2 candles = 30 min op 15m).
    candles_4h: bepaalt de leidende richting. Setups tegen de 4h trend in worden
        gemarkeerd als counter-trend en lopen via strengere regels: halve
        positiegrootte, max 1.5R (geen TP3/runner) en hogere score-eis (≥70).
    candles_30m / candles_1m: optionele extra timeframes — als opgegeven en het
        primaire timeframe levert geen signaal op, wordt hier ook op gezocht
        (DoopieCash frequentie-regel: daytrade kijkt op 15m én 30m, scalp op 5m én 1m).
    session_filter: niet meer gebruikt (altijd False), bewaard voor compatibiliteit.
    scalp_mode: als True, gebruik tightere SL minimum (1.0× ATR op 5m) en TP1/TP2
        op 0.8R/1.5R in plaats van chart-gebaseerde levels.
    """
    if len(candles_15m) < 30 or len(candles_1h) < 20:
        logger.warning("Niet genoeg candles voor analyse")
        return None

    # Cooldown na SL: geen nieuwe entry voor 2 candles (30 min op 15m)
    if cooldown_candles > 0 and cooldown_candles < 2:
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

    # Check setups in volgorde van prioriteit (sla uitgeschakelde setups over)
    off = set(disabled_setups or [])
    if off:
        logger.info(f"Uitgeschakelde setups: {', '.join(off)}")

    if structure_4h not in ('uptrend', 'downtrend'):
        logger.info(f"{mode_label} 4h is {structure_4h or 'onbekend'} — alleen Liquidity Sweep toegestaan")

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

            # R:R valideren op tp3 (na SL-correctie) — alleen voor trades mét de 4h trend
            reward = abs(signal.tp3 - signal.entry)
            rr = reward / risk if risk > 0 else 0
            if rr < 2.5:
                logger.info(f"Signal afgewezen: R:R te laag ({rr:.1f})")
                return None

        # Context score — counter-trend trades hebben een hogere drempel nodig.
        # Frequentie-regel: bij een duidelijke 4h trend (uptrend/downtrend, dus
        # met ≥2 bevestigde swingpunten in get_market_structure) mag de eis
        # omlaag van 55 naar 45 — de 4h-uitlijning compenseert het lagere aantal
        # overige factoren dat hoeft te kloppen.
        ctx = calculate_context_score(candles_15m, candles_1h, candles_4h or [], signal, all_levels,
                                      scalp_mode=scalp_mode, candles_5m=candles_5m)
        if is_counter_trend:
            min_score = 70
        elif structure_4h in ('uptrend', 'downtrend'):
            min_score = 45
        else:
            min_score = 55
        if ctx['score'] < min_score:
            logger.info(
                f"Signal afgewezen: context score te laag ({ctx['score']}/{min_score} vereist"
                f"{' — counter-trend' if is_counter_trend else ''})"
            )
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

    return signal
