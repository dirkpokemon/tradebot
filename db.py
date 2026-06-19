"""
Lichtgewicht SQLite persistence voor trades.
Werkt met plain dicts (via dataclasses.asdict) zodat er geen circulaire imports zijn.

Op Railway: zet DB_PATH=/data/trades.db en mount een volume op /data voor echte
persistentie tussen deploys. Standaard schrijft het naar trades.db in de werkmap.
"""

import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get('DB_PATH', 'trades.db')

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id            TEXT PRIMARY KEY,
    symbol        TEXT,
    side          TEXT,
    setup_type    TEXT,
    entry_price   REAL,
    quantity      REAL,
    stop_loss     REAL,
    tp1           REAL,
    tp2           REAL,
    tp3           REAL,
    timestamp     TEXT,
    reason        TEXT,
    status        TEXT DEFAULT 'open',
    tp1_hit       INTEGER DEFAULT 0,
    tp2_hit       INTEGER DEFAULT 0,
    tp3_hit       INTEGER DEFAULT 0,
    exit_price    REAL,
    realized_pnl  REAL DEFAULT 0.0,
    session       TEXT DEFAULT 'unknown',
    valid_until   TEXT DEFAULT ''
);
"""

_CREATE_PENDING_SIGNALS = """
CREATE TABLE IF NOT EXISTS pending_signals (
    id           TEXT PRIMARY KEY,
    setup_type   TEXT,
    side         TEXT,
    entry        REAL,
    stop_loss    REAL,
    tp1          REAL,
    tp2          REAL,
    tp3          REAL,
    reason       TEXT,
    context_score INTEGER DEFAULT 0,
    context_breakdown TEXT,
    symbol       TEXT,
    quantity     REAL,
    created_at   REAL,
    expires_at   REAL,
    status       TEXT DEFAULT 'pending',
    rejection_reason TEXT DEFAULT NULL,
    timestamp    TEXT
);
"""

_CREATE_LEARNING_PROPOSALS = """
CREATE TABLE IF NOT EXISTS learning_proposals (
    id           TEXT PRIMARY KEY,
    type         TEXT,
    setup_type   TEXT,
    description  TEXT,
    current_value TEXT,
    proposed_value TEXT,
    reasoning    TEXT,
    win_rate_before REAL,
    win_rate_after  REAL,
    sample_size  INTEGER,
    status       TEXT DEFAULT 'pending',
    created_at   TEXT,
    decided_at   TEXT
);
"""

_CREATE_LEARNED_PARAMS = """
CREATE TABLE IF NOT EXISTS learned_params (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);
"""

_CREATE_SIGNAL_REVIEWS = """
CREATE TABLE IF NOT EXISTS signal_reviews (
    id           TEXT PRIMARY KEY,
    signal_id    TEXT,
    setup_type   TEXT,
    side         TEXT,
    approved     INTEGER,
    rejection_reason TEXT,
    score        INTEGER,
    score_atr_sl     INTEGER DEFAULT 0,
    score_trend_4h   INTEGER DEFAULT 0,
    score_trend_1h   INTEGER DEFAULT 0,
    score_volume     INTEGER DEFAULT 0,
    score_level_clean INTEGER DEFAULT 0,
    score_round_number INTEGER DEFAULT 0,
    score_inside_doji INTEGER DEFAULT 0,
    outcome      TEXT DEFAULT NULL,
    timestamp    TEXT
);
"""

_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN candle_snapshot TEXT",
    "ALTER TABLE trades ADD COLUMN review_label    TEXT DEFAULT NULL",
    "ALTER TABLE trades ADD COLUMN review_note     TEXT DEFAULT NULL",
    "ALTER TABLE trades ADD COLUMN context_score   INTEGER DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN trade_mode      TEXT DEFAULT 'daytrade'",
    "ALTER TABLE trades ADD COLUMN counter_trend   INTEGER DEFAULT 0",
]

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with _conn() as c:
        c.execute(_CREATE_TABLE)
        c.execute(_CREATE_PENDING_SIGNALS)
        c.execute(_CREATE_SIGNAL_REVIEWS)
        c.execute(_CREATE_LEARNING_PROPOSALS)
        c.execute(_CREATE_LEARNED_PARAMS)
        for sql in _MIGRATIONS:
            try:
                c.execute(sql)
            except Exception:
                pass  # column already exists
    logger.info(f"Database geïnitialiseerd: {DB_PATH}")

def save_trade(t: dict):
    sql = """
    INSERT OR REPLACE INTO trades
        (id, symbol, side, setup_type, entry_price, quantity, stop_loss,
         tp1, tp2, tp3, timestamp, reason, status,
         tp1_hit, tp2_hit, tp3_hit, exit_price, realized_pnl, session, valid_until,
         review_label, review_note, context_score, trade_mode, counter_trend)
    VALUES
        (:id, :symbol, :side, :setup_type, :entry_price, :quantity, :stop_loss,
         :tp1, :tp2, :tp3, :timestamp, :reason, :status,
         :tp1_hit, :tp2_hit, :tp3_hit, :exit_price, :realized_pnl, :session, :valid_until,
         :review_label, :review_note, :context_score, :trade_mode, :counter_trend)
    """
    with _conn() as c:
        c.execute(sql, {
            **t,
            'tp1_hit': int(t.get('tp1_hit', False)),
            'tp2_hit': int(t.get('tp2_hit', False)),
            'tp3_hit': int(t.get('tp3_hit', False)),
            'session': t.get('session', 'unknown'),
            'valid_until': t.get('valid_until', ''),
            'review_label': t.get('review_label', None),
            'review_note': t.get('review_note', None),
            'context_score': t.get('context_score', 0),
            'trade_mode': t.get('trade_mode', 'daytrade'),
            'counter_trend': int(t.get('counter_trend', False)),
        })

def update_trade(t: dict):
    sql = """
    UPDATE trades SET
        stop_loss    = :stop_loss,
        status       = :status,
        tp1_hit      = :tp1_hit,
        tp2_hit      = :tp2_hit,
        tp3_hit      = :tp3_hit,
        exit_price   = :exit_price,
        realized_pnl = :realized_pnl
    WHERE id = :id
    """
    with _conn() as c:
        c.execute(sql, {
            **t,
            'tp1_hit': int(t.get('tp1_hit', False)),
            'tp2_hit': int(t.get('tp2_hit', False)),
            'tp3_hit': int(t.get('tp3_hit', False)),
        })

def load_trades() -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, symbol, side, setup_type, entry_price, quantity, stop_loss, "
            "tp1, tp2, tp3, timestamp, reason, status, tp1_hit, tp2_hit, tp3_hit, "
            "exit_price, realized_pnl, session, valid_until, review_label, review_note, "
            "COALESCE(context_score, 0) as context_score, "
            "COALESCE(trade_mode, 'daytrade') as trade_mode, "
            "COALESCE(counter_trend, 0) as counter_trend "
            "FROM trades ORDER BY timestamp ASC"
        ).fetchall()
    trades = []
    for row in rows:
        d = dict(row)
        d['tp1_hit'] = bool(d['tp1_hit'])
        d['tp2_hit'] = bool(d['tp2_hit'])
        d['tp3_hit'] = bool(d['tp3_hit'])
        d['counter_trend'] = bool(d['counter_trend'])
        trades.append(d)
    logger.info(f"{len(trades)} trades geladen uit database")
    return trades

def clear_trades():
    with _conn() as c:
        c.execute("DELETE FROM trades")

def save_candle_snapshot(trade_id: str, candles: list, entry_ts=None):
    import json
    data = {"candles": candles, "entry_ts": entry_ts}
    with _conn() as c:
        c.execute("UPDATE trades SET candle_snapshot = ? WHERE id = ?",
                  (json.dumps(data), trade_id))

def get_trade_candles(trade_id: str):
    import json
    with _conn() as c:
        row = c.execute("SELECT candle_snapshot FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row or not row[0]:
        return None, None
    data = json.loads(row[0])
    if isinstance(data, list):  # backward compat met oude snapshots
        return data, None
    return data.get("candles"), data.get("entry_ts")

def save_review(trade_id: str, label: str, note: str = ""):
    with _conn() as c:
        c.execute("UPDATE trades SET review_label = ?, review_note = ? WHERE id = ?",
                  (label, note, trade_id))

def load_reviews_summary() -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT setup_type, side, realized_pnl, review_label, review_note "
            "FROM trades WHERE status = 'closed' AND review_label IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def save_pending_signal(ps: dict):
    import json
    with _conn() as c:
        c.execute("""
        INSERT OR REPLACE INTO pending_signals
        (id, setup_type, side, entry, stop_loss, tp1, tp2, tp3, reason,
         context_score, context_breakdown, symbol, quantity, created_at, expires_at, status, timestamp)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?)
        """, (
            ps['id'], ps['setup_type'], ps['side'], ps['entry'], ps['stop_loss'],
            ps['tp1'], ps['tp2'], ps['tp3'], ps['reason'],
            ps.get('context_score', 0), json.dumps(ps.get('context_breakdown', {})),
            ps['symbol'], ps['quantity'], ps['created_at'], ps['expires_at'],
            ps['timestamp']
        ))


def update_pending_signal_status(signal_id: str, status: str, rejection_reason: str = None):
    with _conn() as c:
        c.execute("UPDATE pending_signals SET status=?, rejection_reason=? WHERE id=?",
                  (status, rejection_reason, signal_id))


def load_pending_signals(status='pending') -> list[dict]:
    with _conn() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM pending_signals WHERE status=? ORDER BY created_at DESC", (status,)
        ).fetchall()
    return [dict(r) for r in rows]


def save_signal_review(signal_id: str, setup_type: str, side: str, approved: bool,
                        rejection_reason: str, score_dict: dict):
    import json
    import uuid
    from datetime import datetime
    breakdown = score_dict.get('breakdown', {})
    with _conn() as c:
        c.execute("""
        INSERT OR REPLACE INTO signal_reviews
        (id, signal_id, setup_type, side, approved, rejection_reason, score,
         score_atr_sl, score_trend_4h, score_trend_1h, score_volume,
         score_level_clean, score_round_number, score_inside_doji, timestamp)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            str(uuid.uuid4()), signal_id, setup_type, side, int(approved),
            rejection_reason, score_dict.get('score', 0),
            breakdown.get('atr_sl', 0), breakdown.get('trend_4h', 0),
            breakdown.get('trend_1h', 0), breakdown.get('volume', 0),
            breakdown.get('level_clean', 0), breakdown.get('round_number', 0),
            breakdown.get('inside_doji', 0),
            datetime.utcnow().isoformat()
        ))


def save_learning_proposals(proposals: list[dict]):
    """Save new proposals, replacing any existing pending proposal of the same type+setup."""
    import json
    with _conn() as c:
        for p in proposals:
            # Remove old pending proposal of same type+setup
            c.execute(
                "DELETE FROM learning_proposals WHERE type=? AND (setup_type=? OR (setup_type IS NULL AND ? IS NULL)) AND status='pending'",
                (p["type"], p.get("setup_type"), p.get("setup_type"))
            )
            c.execute("""
                INSERT INTO learning_proposals
                (id, type, setup_type, description, current_value, proposed_value,
                 reasoning, win_rate_before, win_rate_after, sample_size, status, created_at, decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                p["id"], p["type"], p.get("setup_type"),
                p["description"],
                json.dumps(p["current_value"]), json.dumps(p["proposed_value"]),
                p["reasoning"],
                p.get("win_rate_before"), p.get("win_rate_after"),
                p.get("sample_size"), p.get("status", "pending"),
                p.get("created_at"), p.get("decided_at"),
            ))


def get_learning_proposals(status: str = None) -> list[dict]:
    import json
    with _conn() as c:
        c.row_factory = sqlite3.Row
        if status:
            rows = c.execute(
                "SELECT * FROM learning_proposals WHERE status=? ORDER BY created_at DESC",
                (status,)
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM learning_proposals ORDER BY created_at DESC"
            ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try: d["current_value"] = json.loads(d["current_value"])
        except: pass
        try: d["proposed_value"] = json.loads(d["proposed_value"])
        except: pass
        result.append(d)
    return result


def decide_proposal(proposal_id: str, decision: str):
    """Accept or reject a proposal. decision: 'accepted' | 'rejected'"""
    from datetime import datetime
    with _conn() as c:
        c.execute(
            "UPDATE learning_proposals SET status=?, decided_at=? WHERE id=?",
            (decision, datetime.utcnow().isoformat(), proposal_id)
        )


def get_learned_params() -> dict:
    """Return all currently active learned parameters as {key: value}."""
    import json
    try:
        with _conn() as c:
            rows = c.execute("SELECT key, value FROM learned_params").fetchall()
        return {r[0]: json.loads(r[1]) for r in rows}
    except Exception:
        return {}


def set_learned_param(key: str, value):
    import json
    from datetime import datetime
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO learned_params (key, value, updated_at) VALUES (?,?,?)",
            (key, json.dumps(value), datetime.utcnow().isoformat())
        )


def delete_learned_param(key: str):
    with _conn() as c:
        c.execute("DELETE FROM learned_params WHERE key=?", (key,))
