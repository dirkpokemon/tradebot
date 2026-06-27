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
            "SELECT setup_type, realized_pnl, context_score, trade_mode, session "
            "FROM trades WHERE status='closed' AND realized_pnl IS NOT NULL "
            "ORDER BY timestamp ASC"
        ).fetchall()
    trades = [dict(r) for r in rows]

    if len(trades) < min_trades:
        logger.info(f"Learning: {len(trades)} closed trades — minimaal {min_trades} nodig")
        return []

    proposals = []

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


def _analyze_global_threshold(trades, min_sample=10):
    """Find the min_score threshold that maximizes win rate."""
    overall_wr = _win_rate(trades)
    current_threshold = 50  # effective baseline

    best_threshold = current_threshold
    best_wr = overall_wr

    for threshold in range(45, 86, 5):
        above = [t for t in trades if (t.get("context_score") or 0) >= threshold]
        if len(above) < min_sample:
            continue
        wr = _win_rate(above)
        if wr > best_wr + 0.08:  # minimaal 8% verbetering
            best_wr = wr
            best_threshold = threshold

    if best_threshold <= current_threshold:
        return None

    below  = [t for t in trades if (t.get("context_score") or 0) < best_threshold]
    above  = [t for t in trades if (t.get("context_score") or 0) >= best_threshold]
    wr_below = _win_rate(below)
    wr_above = _win_rate(above)

    reasoning = (
        f"Trades met score < {best_threshold}: {round(wr_below*100)}% win rate ({len(below)} trades, "
        f"gem. ${_avg_pnl(below):.0f}). "
        f"Trades met score >= {best_threshold}: {round(wr_above*100)}% win rate ({len(above)} trades, "
        f"gem. ${_avg_pnl(above):.0f}). "
        f"Door de drempel te verhogen worden structureel verliezende trades uitgefilterd."
    )

    return {
        "id": str(uuid.uuid4()),
        "type": "min_score_global",
        "setup_type": None,
        "description": f"Minimum context score verhogen van {current_threshold} naar {best_threshold}",
        "current_value": current_threshold,
        "proposed_value": best_threshold,
        "reasoning": reasoning,
        "win_rate_before": round(overall_wr, 3),
        "win_rate_after": round(wr_above, 3),
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
    """Propose disabling a setup if win rate is structurally bad."""
    if setup_type in _CORE_SETUPS:
        return None
    setup_trades = [t for t in trades if t.get("setup_type") == setup_type]
    if len(setup_trades) < min_trades:
        return None

    wr = _win_rate(setup_trades)
    avg = _avg_pnl(setup_trades)
    if wr >= 0.38:  # 38% or better -> no action
        return None

    name = SETUP_NL.get(setup_type, setup_type)
    reasoning = (
        f"{name} heeft {len(setup_trades)} trades gehad met {round(wr*100)}% win rate "
        f"(gemiddeld ${avg:.0f} per trade). "
        f"Dit is structureel te laag voor een winstgevende strategie."
    )

    return {
        "id": str(uuid.uuid4()),
        "type": "disable_setup",
        "setup_type": setup_type,
        "description": f"{name} setup uitschakelen (win rate {round(wr*100)}%)",
        "current_value": "enabled",
        "proposed_value": "disabled",
        "reasoning": reasoning,
        "win_rate_before": round(wr, 3),
        "win_rate_after": None,
        "sample_size": len(setup_trades),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "decided_at": None,
    }


def _analyze_setup_threshold(trades, setup_type, min_sample=8):
    """Find a higher min_score for a specific setup if low-scoring trades lose."""
    setup_trades = [t for t in trades if t.get("setup_type") == setup_type]
    if len(setup_trades) < min_sample * 2:
        return None

    overall_wr = _win_rate(setup_trades)
    best_threshold = 50
    best_wr = overall_wr

    for threshold in range(55, 86, 5):
        above = [t for t in setup_trades if (t.get("context_score") or 0) >= threshold]
        if len(above) < min_sample:
            continue
        wr = _win_rate(above)
        if wr > best_wr + 0.10:
            best_wr = wr
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
            f"{name} presteert significant beter bij score >= {best_threshold}: "
            f"{round(best_wr*100)}% win rate ({len(above)} trades) vs "
            f"{round(overall_wr*100)}% overall ({len(setup_trades)} trades)."
        ),
        "win_rate_before": round(overall_wr, 3),
        "win_rate_after": round(best_wr, 3),
        "sample_size": len(setup_trades),
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
        "decided_at": None,
    }
