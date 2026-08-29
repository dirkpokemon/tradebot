# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Install Python dependencies
pip install -r requirements.txt

# Copy and fill in API keys
cp .env.example .env

# Start the FastAPI backend (bot API on http://localhost:8000)
uvicorn api:app --reload

# Start the React dashboard (separate terminal)
cd dashboard
npm install
VITE_API_URL=http://localhost:8000 npm run dev

# After changing dashboard/src/App.jsx, rebuild and force-add dist/:
cd dashboard && npm run build
git add -f dashboard/dist/
git commit -m "rebuild dashboard"
```

**Environment variables** (`.env` or Railway config):
| Variable | Description |
|---|---|
| `OKX_API_KEY` | OKX API key |
| `OKX_SECRET` | OKX secret |
| `OKX_PASSPHRASE` | OKX passphrase |
| `OKX_SANDBOX` | `true` for paper mode via OKX sandbox header |
| `SIM_MODE` | `true` (default) = internal paper trading, no OKX orders placed |
| `SIM_BALANCE` | Starting capital for sim mode (default `10000`) |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for trade alerts + approval keyboard |
| `TELEGRAM_CHAT_ID` | Telegram chat ID for trade alerts |
| `HUMAN_APPROVAL` | `true` = wait for Telegram approve/skip before placing order |
| `TRADE_MODE` | `daytrade` (default, triggers on 15m close) or `scalp` (triggers on 5m close, tighter TPs) |
| `DB_PATH` | Path to SQLite DB, e.g. `/data/trades.db` for Railway volume persistence |
| `RAILWAY_URL` | Public URL of the deployment (e.g. `https://xyz.up.railway.app`) — used to auto-register Telegram webhook on startup |

**Deployment:** `Procfile` runs `uvicorn api:app --host 0.0.0.0 --port $PORT` on Railway.

**Railway persistent volume:** Add a volume mounted at `/data` in Railway settings, then set `DB_PATH=/data/trades.db`. Without this, the SQLite DB resets on every redeploy.

**Telegram webhook:** Set `RAILWAY_URL` and `TELEGRAM_BOT_TOKEN`. On startup, `api.py` auto-registers `{RAILWAY_URL}/telegram/webhook`. After first Railway deploy, get the URL from Railway dashboard and add it as env var, then redeploy once more.

## Architecture

The system is three layers communicating at runtime:

```
strategy.py  ←  pure analysis, no side effects
    ↓
bot.py       ←  exchange connection, trade state, order execution
    ↓
api.py       ←  FastAPI HTTP layer, starts/stops bot in a daemon thread
    ↓
dashboard/   ←  React SPA, polls /status every 5s
```

### strategy.py — signal generation
The `analyze()` function is the single entry point. Full signature:
```python
analyze(candles_15m, candles_1h, cooldown_candles=0, candles_4h=None, candles_5m=None, session_filter=True, disabled_setups=None, scalp_mode=False)
```

Market structure is determined on 1H. Four setups are checked in priority order:

1. **Liquidity Sweep** — price wicks through a key level, immediately reverses. 5m SL refinement via `_refine_sl_with_5m()`.
2. **Rotation** — structural break (uptrend makes LL / downtrend makes HH) + rejection candle or engulfing
3. **Breakout** — candle closes beyond a key level, retest of that level with confirmation
4. **Continuation** — pullback to a flipped level (old resistance → new support) in a clear trend

**Range setup has been removed** from both code and UI.

Each checker returns a `Signal` dataclass or `None`. The first non-`None` result wins. After detection, `analyze()` validates minimum R:R of 2.5 (on TP3) and runs `calculate_context_score()`. Signals scoring below 50 are rejected.

All 4 setup checkers accept `candles_5m=None`. When provided, `_refine_sl_5m()` tightens the SL using 5m swing points, improving R:R without changing signal quality.

**Context score (0–100):** 7 factors scored in `calculate_context_score()`:
- ATR SL validity: 15 pts (mandatory — if SL < 0.5×ATR, signal is rejected regardless of total score)
- 4H trend alignment: 20 pts
- 1H trend alignment: 15 pts
- Volume confirmation: 15 pts
- Level cleanliness: 15 pts
- Round number proximity: 10 pts
- No inside/doji candle at entry: 10 pts

**Scalp mode:** When `scalp_mode=True`, SL minimum is 1.0×ATR (vs 1.5× normally), and TPs are overridden to 1R/2R/3R instead of chart-based levels.

**Session filter:** The `session_filter` parameter exists but the guard was removed — sessions are only used for logging. Trades can trigger at any time.

TP levels are picked from real key levels in the chart, not fixed R:R multiples. ATR-based fallbacks fill in if fewer than 3 chart levels exist ahead.

### bot.py — execution and state
`BotState` is a module-level singleton (`state`). The main loop (`run_bot`) polls on a 10-second interval and only processes a new candle when `last_candle_time` changes.

Per new candle:
1. `manage_open_trades()` checks every open trade for SL/TP hits at the current close price
2. If no open trades, `analyze()` is called; if a signal is returned and `human_approval=False`, `place_order()` is called immediately
3. If `human_approval=True`, signal is stored in `pending_signals` dict and sent to Telegram for manual approve/skip. 10-minute expiry is enforced in the main loop.

**Trade lifecycle — 4-tranche exit:**
- TP1 hit → close 25%, SL moves to breakeven
- TP2 hit → close 25%, SL trails to last swing low/high (`trail_sl_to_structure`)
- TP3 hit → close 25%, SL trails to newer swing point
- Remaining 25% ("runner") → SL keeps trailing on every candle until hit

`sim_mode=True` (default) creates `SIM-XXXX` trades and tracks PnL without placing real orders. Live mode calls `ccxt.myokx` (with EEA fallback to `ccxt.okx`).

**Human approval flow:**
- `send_approval_request()` sends a Telegram message with context score breakdown and inline keyboard (✅ GOEDKEUREN / ❌ OVERSLAAN)
- `pending_signals` dict is protected by `_pending_lock` (threading.Lock)
- Signal IDs are formatted as `S{int(time.time()) % 99999:05d}`
- After skip, bot asks for a rejection reason via follow-up Telegram message

**Trade dataclass fields:** `id, symbol, side, setup_type, entry_price, quantity, stop_loss, tp1, tp2, tp3, timestamp, reason, status, tp1_hit, tp2_hit, tp3_hit, exit_price, realized_pnl, session, valid_until, review_label, review_note, context_score`

### api.py — HTTP interface
Thin FastAPI wrapper around `state` and `run_bot`. The bot runs in a `daemon=True` thread. Key endpoints:
- `GET /status` — full state snapshot including `trade_mode`, `human_approval`, `pending_count`
- `POST /start` — accepts `BotConfig` (symbol, timeframe, risk_per_trade), starts thread
- `POST /stop` — sets `state.running = False`
- `GET /trades` / `DELETE /trades` — trade history
- `GET /stats` — per-setup win rate / profit factor, daily PnL list, equity history for charts
- `GET /pending` — pending signals waiting for human approval
- `POST /telegram/webhook` — handles Telegram callback_query: approve (places order), skip (asks reason), reason (saves to DB)
- `GET /reviews/summary` — closed trades with review labels
- `GET /trades/{id}/candles` — candle snapshot for a trade
- `POST /trades/{id}/review` — save review label + note
- `GET /learning_stats` — factor averages for approved vs skipped signals (useful after 5+ reviews)

**Telegram webhook registration:** On startup, if `TELEGRAM_BOT_TOKEN` and `RAILWAY_URL` are both set, the app automatically calls the Telegram API to register the webhook URL.

### dashboard/src/App.jsx — React monitoring UI
Single-file React SPA. Reads `VITE_API_URL` at build time (defaults to `http://localhost:8000`). Polls `/status` every 5 seconds.

**Key UI components:**
- `ActiveTradeCard` — shows open trade with context score bar
- `PendingApprovalPanel` — shows pending signals with context score bar and 10-min countdown
- `TradeReviewModal` — label a closed trade (✅ Good / ⚠️ Marginal / ❌ Bad) with optional note
- `TradeReviewPanel` — shows reviewed trades with labels
- `CandleChart` — SVG candlestick chart using stored candle snapshot
- Header badges: DAYTRADE/SCALP mode tag, HUMAN APPROVAL tag (yellow, only shown when active)

**SETUP_META:** Only 4 entries — liquidity_sweep, rotation, breakout, continuation. Range has been removed.

### dashboard/dist/ — pre-built frontend
`dist/` is in `.gitignore`. After every rebuild, force-add with:
```bash
git add -f dashboard/dist/
```
The JS bundle filename changes on every build (Vite content hash). If you forget to commit the new dist/, the site will load but show a blank page or 404 on the JS file.

## Key design constraints

- **One trade at a time.** `run_bot` only calls `analyze()` when `open_count == 0`.
- **Candle-level resolution.** TP checks use the candle close price (plus a 60s live-price poll), not tick-level prices. A trade can skip a TP level if price jumps. SL is the exception — see the exchange-order point below.
- **SL only moves in the favorable direction.** `trail_sl_to_structure` guards against moving SL against the trade.
- **EEA / Netherlands users** must use `ccxt.myokx` (OKX's European entity). The code tries `myokx` first and falls back to `okx`.
- **Fixed-risk position sizing.** `calculate_position_size(balance, entry, stop, risk_pct)` sizes so that an SL hit always costs exactly `risk_pct` of balance — 1%, or 0.5% for counter-trend trades (deliberate: they cap at 1.5R and have no runner). The SL itself is price-action based, so its distance varies per trade; the quantity compensates. **Do not reintroduce `vol_scale`** (the old ATR50/ATR14 factor clamped to [0.5, 2.0]): it made real risk swing between 0.5% and 2% of balance, so losses varied up to 8× for no strategy reason.
- **SL is a real exchange order in live mode.** `sync_sl_order()` places a reduce-only stop order at OKX on entry and replaces it after every partial close and every SL move (`trail_sl_to_structure` returns `True` when the SL actually moved, so the runner phase doesn't spam cancel/create). `cancel_sl_order()` runs before any full exit. All closing market orders use `reduceOnly` so they become no-ops if the exchange already closed the position — without this, a double close would open an opposite position. SL exits are booked at `trade.stop_loss`, not at the (overshot) polling price, in both `bot.py` and `backtest.py` — keep these two in sync or autotune tunes on wrong assumptions.
- **SL cooldown.** `analyze()` skips signal detection for 5 candles (75 min on 15m) after a stop loss hit, via the `cooldown_candles` parameter.
- **Circuit breaker.** After 5 consecutive stop losses, `state.circuit_breaker_until` is set to `now + 86400s` and the bot loop sleeps until that timestamp.
- **Daily loss limit.** If equity drops more than 3% from the day's starting equity, no new trades are taken for the rest of that UTC day.
- **Sim mode uses OKX public API.** `get_public_exchange()` calls `ccxt.okx` without auth (market data only). `get_exchange()` requires credentials and is only called in live mode.
- **No test suite.** There are no automated tests in this repo.

## Lessons learned (important — don't repeat these mistakes)

**Never loosen multiple strategy filters at once.** An earlier experiment removed the 4H filter, lowered min_wick, and lowered R:R simultaneously. Result: profit factor dropped from 1.12 to 0.61 (−$3590). All three changes were reverted. Rule: change ONE parameter at a time and measure the effect.

**Low signal frequency is a real constraint.** The strategy generates ~70 trades/year. This makes statistical validation slow — Monte Carlo analysis needs 100+ trades to be meaningful. Don't optimize prematurely. The 5m SL refinement was added specifically to improve R:R so more signals pass the 2.5× minimum, increasing frequency without degrading quality.

**Dashboard dist/ must be force-committed after every rebuild.** The `dist/` directory is in `.gitignore`. After `npm run build`, always run `git add -f dashboard/dist/` before committing. If only `index.html` is committed but the new JS bundle file is not, the deployed site loads a blank page.

**Verify imports after large changes.** After big refactors, run `python -c "from api import app; print('OK')"` to catch import errors before pushing.

## Roadmap

### Completed
- ✅ SQLite persistence (`db.py` with trades, pending_signals, signal_reviews tables)
- ✅ 5m SL refinement for all 4 setups
- ✅ Context score (0–100) with 7 factors — mandatory ATR SL check
- ✅ Human-in-the-loop Telegram approval with 10-min expiry
- ✅ TRADE_MODE=daytrade/scalp
- ✅ Session filter removed (was causing too many missed trades)
- ✅ H4 macro bias in `analyze()`
- ✅ Trade review system (candle snapshot, label modal, learning stats)
- ✅ Dashboard badges for mode and approval status
- ✅ Telegram webhook auto-registration via RAILWAY_URL
- ✅ Liquidity Sweep setup
- ✅ Range setup removed
- ✅ Afgewezen voorstellen komen niet terug: `save_learning_proposals()` slaat adviezen over waarvan de handtekening (type + setup_type + proposed_value) al eens is afgewezen, en geeft terug hoeveel er écht zijn bewaard. Een advies met een *andere* waarde komt wél door. `POST /learning/proposals/allow-rejected` wist de afwijs-geschiedenis (knop "Weer toestaan" in het dashboard).
- ✅ Self-learning (`learn.py`): analyse van gesloten trades op **expectancy/profit factor** (nooit win rate) → voorstellen in `learning_proposals`, gebruiker keurt goed/af via dashboard. Toegepaste params in `learned_params` tabel; `bot.py:_get_learned_analyze_kwargs()` geeft ze door aan elke `analyze()`-call.
- ✅ Autotune (`autotune.py`): maandelijkse (30d, checkloop in `api.py`) walk-forward backtest-sweep over SL-multiplier (1.5/1.75/2.0×ATR) en min R:R (2.0/2.5/3.0) — één parameter per keer. Beste variant → `backtest_param` voorstel; factor-analyse over backtest-trades → `factor_insight` voorstel. Handmatig via `POST /learning/autotune` of dashboard-knop 🔬. Kernsetups (rotation/continuation) mogen NOOIT hard uitgeschakeld worden — dat legt de bot stil (deadlock: disabled setup kan win rate nooit herstellen).

### Fase 3 — Strategie gezondheid
- **Auto-disable per setup**: als een setup de afgelopen 20 trades onder 40% win rate zakt, tijdelijk uitschakelen. Automatisch herstellen als historische performance terugkomt.
- **Sharpe ratio + max drawdown** toevoegen aan `/stats` endpoint.
- **Outcome polling elke 60s**: TP/SL checken op live prijs elke minuut, niet alleen op candle-close.

### Fase 4 — Backtester
- `backtest.py`: walk-forward backtest op historische OKX candles. Train op periode A, test op periode B. Geeft per setup: win rate, profit factor, Sharpe, max drawdown.
- Nodig als basis voor alle verdere optimalisatie — zonder dit weet je niet of aanpassingen helpen of schaden.

### Fase 5 — Fair Value Gaps
- FVG (imbalance between 3 candles) als extra confirmation filter of als zelfstandig entry-punt.
- Combineren met Liquidity Sweep: sweep + FVG fill = sterk signaal.

### Fase 6 — Monte Carlo validatie (later)
- Pas zinvol als Fase 4 (backtester) draait en er voldoende historische trades zijn (100+).
- Shuffle trades 1000×, vergelijk met random strategie.
