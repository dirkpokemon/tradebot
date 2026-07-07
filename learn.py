"""
Self-learning module — analyzes closed trades and proposes parameter adjustments.
Triggered automatically after every 10 new closed trades (minimum 30 total).
"""
import uuid, logging, math
from datetime import datetime

logger = logging.getLogger(__name__)

SETUP_NL = {
    "liquidity_sweep": "Liquiditeitssweep",
    "rotation":        "Rotatie",
    "breakout":        "Breakout",
    "continuation":    "Continuatie",
}
SETUP_TYPES = list(SETUP_NL.keys())


def analyze_for_proposals(min_trades: int = 30) -> list[dict]:
    """
    Load closed trades from DB, run analysis, return list of proposal dicts.
    Returns [] if not enough data.
    """
    from db import _conn
    import sqlite3
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT setup_type, side, realized_pnl, context_score, trade_mode, session "
            "FROM trades WHERE status='closed' AND realized_pnl IS NOT NULL "
            "ORDER BY timestamp ASC"
        ).fetchall()
    trades = [dict(r) for r in rows]

    if len(trades) < min_trades:
        logger.info(f"Learning: {len(trades)} closed trades — minimaal {min_trades} nodig")
        return []

    proposals = []

    # Analyses die alleen realized_pnl + side/mode nodig hebben (altijd beschikbaar)
    for fn in (_analyze_direction, _analyze_trade_mode):
        p = fn(trades)
        if p:
            proposals.append(p)

    # Analyses op basis van context_score (alleen zinvol als scores gevuld zijn)
    p = _analyze_global_threshold(trades)
    if p:
        proposals.append(p)

    for setup in SETUP_TYPES:
        p = _analyze_setup_disable(trades, setup)
        if p:
            proposals.append(p)

    for setup in SETUP_TYPES:
        p = _analyze_setup_threshold(trades, setup)
        if p:
            proposals.append(p)

    logger.info(f"Learning: {len(proposals)} voorstellen gegenereerd uit {len(trades)} trades")
    return proposals


def _win_rate(trades):
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if (t.get("realized_pnl") or 0) > 0)
    return wins / len(trades)


def _avg_pnl(trades):
    if not trades:
        return 0.0
    return sum((t.get("realized_pnl") or 0) for t in trades) / len(trades)


def _profit_factor(trades):
    """Gross winst / gross verlies. >1 = winstgevend. inf = alleen winst, 0 = alleen verlies."""
    gross_profit = sum((t.get("realized_pnl") or 0) for t in trades if (t.get("realized_pnl") or 0) > 0)
    gross_loss   = abs(sum((t.get("realized_pnl") or 0) for t in trades if (t.get("realized_pnl") or 0) < 0))
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def _pf_str(pf):
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def _analyze_direction(trades, min_side=10):
    """
    Stel een richtingfilter voor ALLEEN als één kant daadwerkelijk geld verliest
    (negatieve gem. PnL én profit factor < 1) terwijl de andere kant winstgevend is.
    Oordeel op rendement, niet op win rate — een lage win rate met grote winnaars is prima.
    """
    longs  = [t for t in trades if t.get("side") == "buy"]
    shorts = [t for t in trades if t.get("side") == "sell"]
    if len(longs) < min_side or len(shorts) < min_side:
        return None

    for weak, strong, weak_nl, proposed, keep_nl in (
        (shorts, longs, "short", "long_only",  "alleen long"),
        (longs, shorts, "long",  "short_only", "alleen short"),
    ):
        if _avg_pnl(weak) < 0 and _profit_factor(weak) < 1.0 and _avg_pnl(strong) > 0:
            return {
                "id": str(uuid.uuid4()),
                "type": "trade_direction",
                "setup_type": None,
                "description": f"{keep_nl.capitalize()} traden",
                "current_value": "both",
                "proposed_value": proposed,
                "reasoning": (
                    f"{weak_nl.capitalize()}-trades verliezen geld: profit factor "
                    f"{_pf_str(_profit_factor(weak))}, gemiddeld ${_avg_pnl(weak):.0f} per trade "
                    f"over {len(weak)} trades. De andere kant is winstgevend "
                    f"(PF {_pf_str(_profit_factor(strong))}, ${_avg_pnl(strong):+.0f}/trade). "
                    f"Voorstel: {keep_nl} — het verlieslatende deel eruit."
                ),
                "win_rate_before": round(_win_rate(trades), 3),
                "win_rate_after": round(_win_rate(strong), 3),
                "sample_size": len(trades),
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
                "decided_at": None,
            }
    return None


def _analyze_trade_mode(trades, min_mode=10):
    """Stel een modusfilter voor alleen als één modus geld verliest en de andere winstgevend is."""
    scalp = [t for t in trades if t.get("trade_mode") == "scalp"]
    day   = [t for t in trades if t.get("trade_mode") == "daytrade"]
    if len(scalp) < min_mode or len(day) < min_mode:
        return None

    for weak, strong, weak_nl, proposed in (
        (scalp, day, "scalp",    "daytrade"),
        (day, scalp, "daytrade", "scalp"),
    ):
        if _avg_pnl(weak) < 0 and _profit_factor(weak) < 1.0 and _avg_pnl(strong) > 0:
            return {
                "id": str(uuid.uuid4()),
                "type": "trade_mode",
                "setup_type": None,
                "description": f"Alleen {proposed}-modus traden",
                "current_value": "both",
                "proposed_value": proposed,
                "reasoning": (
                    f"{weak_nl.capitalize()}-trades verliezen geld: profit factor "
                    f"{_pf_str(_profit_factor(weak))}, gemiddeld ${_avg_pnl(weak):.0f} per trade "
                    f"over {len(weak)} trades. De {proposed}-modus is winstgevend "
                    f"(PF {_pf_str(_profit_factor(strong))}, ${_avg_pnl(strong):+.0f}/trade). "
                    f"Voorstel: alleen {proposed}."
                ),
                "win_rate_before": round(_win_rate(trades), 3),
                "win_rate_after": round(_win_rate(strong), 3),
                "sample_size": len(trades),
                "status": "pending",
                "created_at": datetime.utcnow().isoformat(),
                "decided_at": None,
            }
    return None


def _analyze_global_threshold(trades, min_sample=10):
    """
    Zoek de min_score-drempel die de EXPECTANCY (gem. PnL per trade) maximaliseert.
    Alleen voorstellen als de eruit gefilterde trades samen geld verliezen — dan snijdt
    het echt verlies weg. Win rate is bewust niet de maatstaf.
    """
    overall_avg = _avg_pnl(trades)
    current_threshold = 50

    best_threshold = current_threshold
    best_avg = overall_avg

    for threshold in range(55, 86, 5):
        above = [t for t in trades if (t.get("context_score") or 0) >= threshold]
        below = [t for t in trades if (t.get("context_score") or 0) < threshold]
        if len(above) < min_sample or len(below) < 5:
            continue
        # Verbetering alleen tellen als het weggefilterde deel netto verliest
        if _avg_pnl(above) > best_avg and _avg_pnl(below) < 0:
            best_avg = _avg_pnl(above)
            best_threshold = threshold

    if best_threshold <= current_threshold:
        return None

    below = [t for t in trades if (t.get("context_score") or 0) < best_threshold]
    above = [t for t in trades if (t.get("context_score") or 0) >= best_threshold]

    return {
        "id": str(uuid.uuid4()),
        "type": "min_score_global",
        "setup_type": None,
        "description": f"Minimum context score verhogen van {current_threshold} naar {best_threshold}",
        "current_value": current_threshold,
        "proposed_value": best_threshold,
        "reasoning": (
            f"Trades met score < {best_threshold} verliezen geld: gemiddeld ${_avg_pnl(below):.0f} "
            f"per trade (PF {_pf_str(_profit_factor(below))}, {len(below)} trades). "
            f"Trades met score ≥ {best_threshold} verdienen ${_avg_pnl(above):+.0f}/trade "
            f"(PF {_pf_str(_profit_factor(above))}, {len(above)} trades). "
            f"De drempel verhogen tilt de expectancy van ${overall_avg:+.0f} naar ${_avg_pnl(above):+.0f} per trade."
        ),
        "win_rate_before": round(_win_rate(trades), 3),
        "win_rate_after": round(_win_rate(above), 3),
        "sample_size": len(trades),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "decided_at": None,
    }


# Kernsetups die nooit hard uitgeschakeld mogen worden: het zijn de enige twee
# actieve setups, dus uitschakelen legt de bot stil. Onderprestatie wordt opgevangen
# door de score-drempel te verhogen (_analyze_setup_threshold), niet door te disablen.
_CORE_SETUPS = {"rotation", "continuation"}


def _analyze_setup_disable(trades, setup_type, min_trades=12):
    """Stel uitschakelen voor alleen als een (niet-kern)setup daadwerkelijk geld verliest."""
    if setup_type in _CORE_SETUPS:
        return None
    setup_trades = [t for t in trades if t.get("setup_type") == setup_type]
    if len(setup_trades) < min_trades:
        return None

    pf  = _profit_factor(setup_trades)
    avg = _avg_pnl(setup_trades)
    if pf >= 1.0 or avg >= 0:  # verdient geld → niet uitschakelen, ook bij lage win rate
        return None

    name = SETUP_NL.get(setup_type, setup_type)
    return {
        "id": str(uuid.uuid4()),
        "type": "disable_setup",
        "setup_type": setup_type,
        "description": f"{name} setup uitschakelen (verliesgevend)",
        "current_value": "enabled",
        "proposed_value": "disabled",
        "reasoning": (
            f"{name} verliest geld: profit factor {_pf_str(pf)}, gemiddeld ${avg:.0f} per trade "
            f"over {len(setup_trades)} trades. Winnende setups halen dit niet in — uitschakelen "
            f"verhoogt het totaalrendement."
        ),
        "win_rate_before": round(_win_rate(setup_trades), 3),
        "win_rate_after": None,
        "sample_size": len(setup_trades),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "decided_at": None,
    }


def _analyze_setup_threshold(trades, setup_type, min_sample=8):
    """Zoek een hogere min_score voor één setup als laag-scorende trades geld verliezen."""
    setup_trades = [t for t in trades if t.get("setup_type") == setup_type]
    if len(setup_trades) < min_sample * 2:
        return None

    overall_avg = _avg_pnl(setup_trades)
    best_threshold = 50
    best_avg = overall_avg

    for threshold in range(55, 86, 5):
        above = [t for t in setup_trades if (t.get("context_score") or 0) >= threshold]
        below = [t for t in setup_trades if (t.get("context_score") or 0) < threshold]
        if len(above) < min_sample or len(below) < 4:
            continue
        if _avg_pnl(above) > best_avg and _avg_pnl(below) < 0:
            best_avg = _avg_pnl(above)
            best_threshold = threshold

    if best_threshold <= 50:
        return None

    above = [t for t in setup_trades if (t.get("context_score") or 0) >= best_threshold]
    name  = SETUP_NL.get(setup_type, setup_type)

    return {
        "id": str(uuid.uuid4()),
        "type": "min_score_setup",
        "setup_type": setup_type,
        "description": f"Min. score voor {name} verhogen naar {best_threshold}",
        "current_value": 50,
        "proposed_value": best_threshold,
        "reasoning": (
            f"{name} verdient significant meer bij score ≥ {best_threshold}: "
            f"${_avg_pnl(above):+.0f}/trade (PF {_pf_str(_profit_factor(above))}, {len(above)} trades) "
            f"vs ${overall_avg:+.0f}/trade overall ({len(setup_trades)} trades)."
        ),
        "win_rate_before": round(_win_rate(setup_trades), 3),
        "win_rate_after": round(_win_rate(above), 3),
        "sample_size": len(setup_trades),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "decided_at": None,
    }
