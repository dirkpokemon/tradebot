"""
Backtest-gedreven parameter-tuning ("autotune").

Draait maandelijks (of handmatig via de dashboard-knop) een walk-forward backtest
over meerdere parameter-varianten en zet de beste verbetering als voorstel in het
leerrapport. De gebruiker keurt elk voorstel goed of af — er wordt nooit iets
automatisch toegepast.

Ontwerpprincipes (zie CLAUDE.md "Lessons learned"):
  - Eén parameter per keer variëren, nooit meerdere filters tegelijk versoepelen.
  - Oordelen op expectancy (gem. $ per trade) en profit factor, niet op win rate.
  - Alleen voorstellen bij voldoende trades in het testvenster (min. 10) en een
    duidelijke marge boven de baseline — anders is het ruis.
  - Factor-analyse over de baseline-trades laat zien welke context-scorefactoren
    winnaars daadwerkelijk onderscheidden (informatief voorstel).
"""

import uuid
import time
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from backtest import BacktestConfig, run_backtest

logger = logging.getLogger(__name__)

# Sweep: baseline + één-parameter-per-keer varianten
BASELINE = {"sl_atr_mult": 1.5, "min_rr": 2.0}
VARIANTS = [
    {"param": "sl_atr_mult", "value": 1.75},
    {"param": "sl_atr_mult", "value": 2.0},
    {"param": "min_rr",      "value": 2.5},
    {"param": "min_rr",      "value": 3.0},
]

PARAM_NL = {
    "sl_atr_mult": "SL-afstand (×ATR)",
    "min_rr":      "Minimale R:R (TP3)",
}

FACTOR_NL = {
    "atr_sl":       "ATR/SL-validiteit",
    "trend_4h":     "4H-trend uitlijning",
    "gains_level":  "Level gewonnen",
    "trend_ctx":    "1H-trend context",
    "volume":       "Volume-bevestiging",
    "level_clean":  "Schoon niveau",
    "round_number": "Rond getal nabij",
}

MIN_TRADES = 10          # minimaal aantal trades in het testvenster per variant
MIN_EXPECTANCY_GAIN = 1.15  # variant moet ≥15% hogere expectancy hebben dan baseline


@dataclass
class AutotuneState:
    running:  bool = False
    progress: float = 0.0        # 0.0–1.0
    step:     str = ""           # bijv. "variant 2/5: min_rr=2.5"
    error:    str = ""
    last_run: Optional[str] = None
    summary:  Optional[dict] = None  # compacte vergelijkingstabel van de laatste run


autotune_state = AutotuneState()


def _fmt_pf(pf):
    return "∞" if pf is None else f"{pf:.2f}"


def _bt_config(days: int, test_pct: float, overrides: dict) -> BacktestConfig:
    return BacktestConfig(
        symbol="BTC/USDT:USDT",
        days=days,
        test_pct=test_pct,
        session_filter=False,   # live bot draait ook zonder sessiefilter
        trade_mode="daytrade",
        sl_atr_mult=overrides.get("sl_atr_mult"),
        min_rr=overrides.get("min_rr"),
    )


def _run_one(exchange, days, test_pct, overrides, label) -> Optional[dict]:
    try:
        result = run_backtest(_bt_config(days, test_pct, overrides), exchange)
        from dataclasses import asdict
        r = asdict(result)
        logger.info(
            f"Autotune [{label}]: {r['total_trades']} trades | WR {r['win_rate']}% | "
            f"PF {r['profit_factor']} | exp ${r['expectancy']}"
        )
        return r
    except Exception as e:
        logger.warning(f"Autotune [{label}] mislukt: {e}")
        return None


def _analyze_factors(baseline_trades: list) -> Optional[dict]:
    """
    Vergelijk de gemiddelde factor-punten van winnaars vs verliezers.
    Geeft een informatief voorstel terug als één of meer factoren duidelijk
    onderscheidend zijn (verschil ≥ 30% van de max factor-score).
    """
    with_bd = [t for t in baseline_trades if t.get("context_breakdown")]
    if len(with_bd) < 15:
        return None

    winners = [t for t in with_bd if t["realized_pnl"] > 0]
    losers  = [t for t in with_bd if t["realized_pnl"] <= 0]
    if len(winners) < 5 or len(losers) < 5:
        return None

    def avg_factor(group, key):
        vals = [t["context_breakdown"].get(key, 0) for t in group]
        return sum(vals) / len(vals) if vals else 0.0

    factors = set()
    for t in with_bd:
        factors.update(t["context_breakdown"].keys())

    diffs = []
    for f in sorted(factors):
        w, l = avg_factor(winners, f), avg_factor(losers, f)
        max_seen = max((t["context_breakdown"].get(f, 0) for t in with_bd), default=0)
        if max_seen > 0 and (w - l) / max_seen >= 0.30:
            diffs.append((f, w, l))

    if not diffs:
        return None

    diffs.sort(key=lambda x: -(x[1] - x[2]))
    lines = [
        f"{FACTOR_NL.get(f, f)}: winnaars gem. {w:.0f} pt vs verliezers {l:.0f} pt"
        for f, w, l in diffs[:3]
    ]
    return {
        "id": str(uuid.uuid4()),
        "type": "factor_insight",
        "setup_type": None,
        "description": f"Factoren die winnaars voorspellen: {', '.join(FACTOR_NL.get(f, f) for f, _, _ in diffs[:3])}",
        "current_value": None,
        "proposed_value": None,
        "reasoning": (
            f"Analyse over {len(with_bd)} backtest-trades ({len(winners)} winnaars, {len(losers)} verliezers). "
            + " · ".join(lines)
            + ". Trades waar deze factoren scoren winnen structureel vaker — weeg ze mee in je "
              "beoordeling van signalen. (Informatief: accepteren wijzigt geen parameters.)"
        ),
        "win_rate_before": None,
        "win_rate_after": None,
        "sample_size": len(with_bd),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "decided_at": None,
    }


def run_autotune(exchange, days: int = 150, test_pct: float = 0.35) -> list[dict]:
    """
    Draai de volledige sweep. Geeft de gegenereerde voorstellen terug (kan leeg zijn).
    Werkt de module-state bij voor de status-endpoint.
    """
    from db import get_learned_params

    autotune_state.running = True
    autotune_state.error = ""
    autotune_state.progress = 0.0
    autotune_state.summary = None
    t0 = time.time()

    try:
        # Huidige (geleerde of default) instellingen vormen de baseline
        learned = get_learned_params()
        current = {
            "sl_atr_mult": learned.get("sl_atr_mult", BASELINE["sl_atr_mult"]),
            "min_rr":      learned.get("min_rr",      BASELINE["min_rr"]),
        }

        total_steps = 1 + len(VARIANTS)
        autotune_state.step = f"baseline: SL {current['sl_atr_mult']}×ATR, R:R {current['min_rr']}"
        base = _run_one(exchange, days, test_pct, current, "baseline")
        autotune_state.progress = 1 / total_steps
        if not base or base["total_trades"] < MIN_TRADES:
            n = base["total_trades"] if base else 0
            raise ValueError(
                f"Baseline-backtest leverde te weinig trades ({n} < {MIN_TRADES}) — "
                f"periode te kort of markt zonder setups."
            )

        rows = [{**current, "label": "baseline (huidig)",
                 "trades": base["total_trades"], "win_rate": base["win_rate"],
                 "profit_factor": base["profit_factor"], "expectancy": base["expectancy"]}]

        best_variant, best_result = None, None
        for i, v in enumerate(VARIANTS):
            if v["value"] == current.get(v["param"]):
                autotune_state.progress = (i + 2) / total_steps
                continue  # variant is al de huidige instelling
            overrides = {**current, v["param"]: v["value"]}
            autotune_state.step = f"variant {i+1}/{len(VARIANTS)}: {v['param']}={v['value']}"
            r = _run_one(exchange, days, test_pct, overrides, autotune_state.step)
            autotune_state.progress = (i + 2) / total_steps
            if not r:
                continue

            rows.append({**overrides, "label": f"{v['param']}={v['value']}",
                         "trades": r["total_trades"], "win_rate": r["win_rate"],
                         "profit_factor": r["profit_factor"], "expectancy": r["expectancy"]})

            if r["total_trades"] < MIN_TRADES:
                continue
            base_exp = base["expectancy"]
            improves_exp = (base_exp <= 0 and r["expectancy"] > 0) or \
                           (base_exp > 0 and r["expectancy"] >= base_exp * MIN_EXPECTANCY_GAIN)
            base_pf = base["profit_factor"] or 0
            var_pf  = r["profit_factor"] or 0
            if improves_exp and var_pf > base_pf:
                if not best_result or r["expectancy"] > best_result["expectancy"]:
                    best_variant, best_result = v, r

        autotune_state.summary = {
            "rows": rows,
            "test_period": base.get("test_period", ""),
            "days": days,
        }

        proposals = []

        if best_variant and best_result:
            param, value = best_variant["param"], best_variant["value"]
            proposals.append({
                "id": str(uuid.uuid4()),
                "type": "backtest_param",
                "setup_type": param,  # draagt de parameternaam (accept gebruikt dit)
                "description": f"{PARAM_NL.get(param, param)} wijzigen van {current[param]} naar {value}",
                "current_value": current[param],
                "proposed_value": value,
                "reasoning": (
                    f"Walk-forward backtest over {days} dagen (testvenster {base.get('test_period','')}): "
                    f"met {PARAM_NL.get(param, param)} = {value} steeg de expectancy van "
                    f"${base['expectancy']:.0f} naar ${best_result['expectancy']:.0f} per trade en de "
                    f"profit factor van {_fmt_pf(base['profit_factor'])} naar {_fmt_pf(best_result['profit_factor'])} "
                    f"({best_result['total_trades']} vs {base['total_trades']} trades, win rate "
                    f"{base['win_rate']}% → {best_result['win_rate']}%). "
                    f"Let op: één testvenster — resultaat kan aan de periode liggen."
                ),
                "win_rate_before": round(base["win_rate"] / 100, 3),
                "win_rate_after": round(best_result["win_rate"] / 100, 3),
                "sample_size": best_result["total_trades"],
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
                "decided_at": None,
            })

        factor_p = _analyze_factors(base.get("trades", []))
        if factor_p:
            proposals.append(factor_p)

        autotune_state.last_run = datetime.utcnow().isoformat()
        logger.info(f"Autotune klaar in {time.time()-t0:.0f}s: {len(proposals)} voorstel(len)")
        return proposals

    except Exception as e:
        autotune_state.error = str(e)
        logger.error(f"Autotune mislukt: {e}")
        return []
    finally:
        autotune_state.running = False
        autotune_state.progress = 1.0
        autotune_state.step = ""
