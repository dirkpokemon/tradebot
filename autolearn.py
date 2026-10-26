"""
Automatisch leren: de bot past verbeteringen zelf toe en controleert achteraf of
ze geholpen hebben.

Werkwijze per cyclus:
  1. De autotune-sweep levert voorstellen op die zich out-of-sample hebben bewezen.
  2. Hiervan wordt er HOOGUIT ÉÉN automatisch toegepast, en alleen uit een veilige
     set numerieke parameters binnen harde grenzen.
  3. De prestatie van vlak vóór de wijziging wordt vastgelegd als nulmeting.
  4. Er volgt een observatieperiode waarin niets anders verandert — anders weet je
     achteraf nooit welke wijziging het verschil maakte (zie CLAUDE.md).
  5. Daarna wordt de werkelijke prestatie vergeleken. Slechter? Dan draait de bot
     de wijziging zelf terug en zet hem op de afwijs-lijst, zodat hij niet opnieuw
     wordt geprobeerd.

Bewust NIET automatisch: setups uitschakelen, richtingfilter en handelsmodus. Die
halveren of stoppen de handel in één klap; daarvoor blijft jouw goedkeuring nodig.
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Parameters die zichzelf mogen bijstellen, met de grenzen waarbinnen dat mag.
# De grenzen voorkomen dat een reeks kleine stappen de bot stilletjes dichtknijpt.
AUTO_PARAMS = {
    "sl_atr_mult":      (1.0, 2.5),
    "min_rr":           (1.5, 3.5),
    "min_score_global": (40, 70),
}

PARAM_NL = {
    "sl_atr_mult":      "SL-afstand (×ATR)",
    "min_rr":           "Minimale R:R",
    "min_score_global": "Min. context score",
}

OBSERVE_TRADES   = 20   # zoveel trades meekijken voor we oordelen
OBSERVE_MAX_DAYS = 60   # ...of zoveel dagen, wat er eerder is
MIN_TRADES_JUDGE = 8    # minder dan dit is te weinig om het backtest-bewijs te overrulen
BASELINE_TRADES  = 20   # nulmeting over de laatste zoveel trades vóór de wijziging


def auto_learning_enabled() -> bool:
    return os.environ.get("AUTO_LEARN", "true").strip().lower() not in ("false", "0", "no", "off")


def _expectancy(trades: list) -> float:
    """Gemiddelde PnL per trade — waar het uiteindelijk om gaat."""
    if not trades:
        return 0.0
    return sum((t.get("realized_pnl") or 0) for t in trades) / len(trades)


def _notify(message: str):
    """Telegram-bericht; de bot beslist zelfstandig, dus je hoort wél wat hij doet."""
    try:
        from bot import send_telegram
        send_telegram(message)
    except Exception as e:
        logger.warning(f"Melding versturen mislukt: {e}")


def _candidate_from_proposals(proposals: list) -> Optional[tuple]:
    """
    Kies hoogstens één voorstel dat automatisch toegepast mag worden.
    Geeft (param, nieuwe_waarde, beschrijving) terug, of None.
    """
    for p in proposals or []:
        ptype = p.get("type")
        if ptype == "backtest_param":
            param = p.get("setup_type")          # draagt de parameternaam
        elif ptype == "min_score_global":
            param = "min_score_global"
        else:
            continue                              # structureel → blijft handmatig

        if param not in AUTO_PARAMS:
            continue
        try:
            value = float(p["proposed_value"])
        except (TypeError, ValueError, KeyError):
            continue

        low, high = AUTO_PARAMS[param]
        if not (low <= value <= high):
            logger.info(f"Auto-leren slaat {param}={value} over: buiten de grenzen {low}–{high}")
            continue
        return param, value, p.get("description", f"{param} → {value}")
    return None


def maybe_auto_apply(proposals: list) -> Optional[dict]:
    """
    Pas hoogstens één bewezen verbetering toe. Geeft het experiment terug, of None
    wanneer er niets toepasbaars was of er al een experiment loopt.
    """
    from db import (get_active_experiment, get_learned_params, set_learned_param,
                    save_experiment, closed_trades_for_learning)

    if not auto_learning_enabled():
        return None

    running = get_active_experiment()
    if running:
        logger.info(
            f"Auto-leren wacht: experiment op {running['param']} loopt nog "
            f"(sinds {running['applied_at'][:16]})"
        )
        return None

    candidate = _candidate_from_proposals(proposals)
    if not candidate:
        return None
    param, new_value, description = candidate

    params = get_learned_params()
    defaults = {"sl_atr_mult": 1.5, "min_rr": 2.0, "min_score_global": 50}
    old_value = params.get(param, defaults.get(param))
    if old_value is not None and float(old_value) == new_value:
        return None

    baseline = closed_trades_for_learning(limit=BASELINE_TRADES)
    experiment = {
        "id": str(uuid.uuid4()),
        "param": param,
        "old_value": old_value,
        "new_value": new_value,
        "applied_at": datetime.utcnow().isoformat(),
        "baseline_n": len(baseline),
        "baseline_expectancy": round(_expectancy(baseline), 2),
        "status": "active",
    }

    set_learned_param(param, new_value)
    save_experiment(experiment)

    logger.info(f"Auto-leren toegepast: {param} {old_value} → {new_value}")
    _notify(
        f"🤖 <b>BOT HEEFT ZICHZELF AANGEPAST</b>\n"
        f"{PARAM_NL.get(param, param)}: <b>{old_value} → {new_value}</b>\n"
        f"{description}\n\n"
        f"Nulmeting: ${experiment['baseline_expectancy']:+.2f} per trade over "
        f"{experiment['baseline_n']} trades.\n"
        f"Na {OBSERVE_TRADES} trades controleert hij of dit beter werkt en draait "
        f"hij het anders zelf terug."
    )
    return experiment


def evaluate_active_experiment() -> Optional[dict]:
    """
    Beoordeel het lopende experiment zodra de observatieperiode voorbij is.
    Slechter dan de nulmeting → terugdraaien en op de afwijs-lijst zetten.
    """
    from db import (get_active_experiment, set_learned_param, close_experiment,
                    closed_trades_for_learning, blacklist_proposal)

    exp = get_active_experiment()
    if not exp:
        return None

    since = exp["applied_at"]
    trades = closed_trades_for_learning(since=since)
    n = len(trades)

    try:
        age_days = (datetime.utcnow() - datetime.fromisoformat(since)).days
    except ValueError:
        age_days = 0

    if n < OBSERVE_TRADES and age_days < OBSERVE_MAX_DAYS:
        return None  # nog in observatie

    param     = exp["param"]
    old_value = exp["old_value"]
    new_value = exp["new_value"]
    baseline  = exp.get("baseline_expectancy") or 0.0
    result    = round(_expectancy(trades), 2)

    # Te weinig trades om het backtest-bewijs te overrulen: laten staan.
    if n < MIN_TRADES_JUDGE:
        close_experiment(exp["id"], "kept", n, result,
                         f"te weinig trades ({n}) om te oordelen — wijziging blijft staan")
        logger.info(f"Experiment {param}: slechts {n} trades in {age_days} dagen — behouden")
        return {"status": "kept", "param": param, "n": n}

    if result < baseline:
        set_learned_param(param, old_value)
        close_experiment(exp["id"], "reverted", n, result,
                         f"expectancy ${result:+.2f} < nulmeting ${baseline:+.2f}")
        blacklist_proposal(
            "backtest_param" if param != "min_score_global" else "min_score_global",
            param if param != "min_score_global" else None,
            new_value,
            f"{PARAM_NL.get(param, param)} → {new_value}",
            f"Automatisch teruggedraaid: ${result:+.2f} vs ${baseline:+.2f} per trade over {n} trades",
        )
        logger.warning(f"Experiment {param} teruggedraaid: {result} < {baseline}")
        _notify(
            f"↩️ <b>WIJZIGING TERUGGEDRAAID</b>\n"
            f"{PARAM_NL.get(param, param)}: <b>{new_value} → {old_value}</b>\n\n"
            f"Resultaat was ${result:+.2f} per trade over {n} trades, tegenover "
            f"${baseline:+.2f} ervoor. De bot probeert dit niet opnieuw."
        )
        return {"status": "reverted", "param": param, "n": n,
                "baseline": baseline, "result": result}

    close_experiment(exp["id"], "kept", n, result,
                     f"expectancy ${result:+.2f} >= nulmeting ${baseline:+.2f}")
    logger.info(f"Experiment {param} behouden: {result} >= {baseline}")
    _notify(
        f"✅ <b>WIJZIGING BEVESTIGD</b>\n"
        f"{PARAM_NL.get(param, param)} blijft op <b>{new_value}</b>.\n\n"
        f"${result:+.2f} per trade over {n} trades, tegenover ${baseline:+.2f} ervoor."
    )
    return {"status": "kept", "param": param, "n": n,
            "baseline": baseline, "result": result}


def learning_status() -> dict:
    """Compacte status voor het dashboard."""
    from db import get_active_experiment, get_experiments

    active = get_active_experiment()
    observing = None
    if active:
        from db import closed_trades_for_learning
        n = len(closed_trades_for_learning(since=active["applied_at"]))
        observing = {
            "param": active["param"],
            "param_nl": PARAM_NL.get(active["param"], active["param"]),
            "old_value": active["old_value"],
            "new_value": active["new_value"],
            "applied_at": active["applied_at"],
            "trades_done": n,
            "trades_needed": OBSERVE_TRADES,
            "baseline_expectancy": active.get("baseline_expectancy"),
        }
    return {
        "enabled": auto_learning_enabled(),
        "observing": observing,
        "history": get_experiments(limit=10),
    }
