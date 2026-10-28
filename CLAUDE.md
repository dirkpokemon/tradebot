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
| `AUTO_START` | `true` (default) = bot starts trading as soon as the service boots; `false` = wait for a manual Start. Without auto-start every redeploy silently halts trading until someone clicks Start. |
| `AUTO_LEARN` | `true` (default) = the bot applies proven improvements itself, one at a time, and reverts them if they do not hold up. `false` = proposals only, you approve them. |
| `RAILWAY_URL` | Public URL of the deployment (e.g. `https://xyz.up.railway.app`) — used to auto-register Telegram webhook on startup |

**Deployment:** `Procfile` runs `uvicorn api:app --host 0.0.0.0 --port $PORT` on Railway.

**Railway persistent volume:** Add a volume mounted at `/data` in Railway settings, then set `DB_PATH=/data/trades.db`. Without this, the SQLite DB resets on every redeploy.

**Public URL:** `get_public_url()` in `api.py` resolves the deployment's address — `RAILWAY_URL` first, then Railway's auto-injected `RAILWAY_PUBLIC_DOMAIN`, then `RAILWAY_STATIC_URL`; a bare domain gets `https://` prepended. It is returned by `/status` as `public_url`, logged at startup ("Dashboard bereikbaar op: …"), and used for the Telegram webhook. The dashboard is served by the API itself on `/`, so this URL *is* the dashboard address.

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

Market structure is determined on 1H. **Only two setups exist in the code** — `_detect_signal()` calls these and nothing else:

1. **Rotation** — `check_rotation()`: complete L→H→HL→HH (or H→L→LH→LL) construction, entry on the pullback
2. **Continuation** — `check_continuation()`: pullback to a flipped level (old resistance → new support) in a clear trend

**Liquidity Sweep and Breakout no longer exist.** The functions were removed; only the two above remain. Range was removed earlier. Re-adding a setup means writing and backtesting it from scratch — it is not a matter of re-wiring. This directly caps trade frequency at roughly 100/year.

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

### dashboard/ — React monitoring UI
Reads `VITE_API_URL` at build time (defaults to `http://localhost:8000`). Polls `/status` and `/stats` every 5 seconds.

**Files:**
| File | Contains |
|---|---|
| `src/theme.js` | `C` (the single colour palette — **dark**) and `MONO` (the numeric font). Nothing anywhere should hardcode a hex; add a token instead. |
| `src/metrics.js` | Derived figures: `tradeR`, `gradeOf`, `winRateOf`, `profitFactorOf`, `avgWinLossOf`, `byDay`, `cumulativeByDay`, `drawdownSeries`, `edgeScore`, `monthGrid`. All computed **client-side from `/status` + `/stats`** — deliberately no extra endpoints, so there is no second version of the truth. |
| `src/Widgets.jsx` | Hand-rolled SVG: `ArcGauge`, `DonutRing`, `Sparkline`, `WinLossBars`, `EdgeRadar`, `ScoreMeter`, `CalendarHeatmap`, plus `Card` and `Kpi`. |
| `src/App.jsx` | Layout, the five pages, and the panels. |
| `src/LiveCandleChart.jsx` | lightweight-charts; it draws on canvas and so takes its colours from `C` in JS, not from CSS. |

**Layout:** a left sidebar (`NAV`) with five pages, persisted in `localStorage` under `tradebot.page`:
- **Dashboard** — active trade, 5 KPI tiles (net P&L, trade win %, profit factor, day win %, avg win/loss), edge-score radar, cumulative P&L, net P&L per day, recent trades, calendar heatmap, balance, drawdown, setup stats, "what is the bot doing".
- **Journaal** — filters (side/setup/result/mode) over journal cards or a compact table, 25 at a time.
- **Analyse** — live chart, backtest, Monte Carlo. **Leren** — auto-learning, active settings, proposals. **Bot** — config and setup health.

**Edge score** (`edgeScore`) scores six properties on 0–100 and averages them. The scales are deliberately generous-topped ("100 = excellent", not "100 = achievable"), so a sound strategy lands around 60–80. The point is the *shape* of the hexagon — a dent names the weak spot — not the number.

**SETUP_META:** rotation and continuation — the only two setups the strategy implements.

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

**Candle pagination must always terminate.** `fetch_ohlcv_paginated` in `backtest.py` once hung the monthly autotune for six days. When OKX ignores `since` (it does for data far back on `/market/candles`) it returns the same most-recent window on every call, and the old exit test `newest >= now_ms` could never fire — the newest candle opens up to one interval *before* now, so `since` never advanced. The loop now has a page cap and aborts when `since` stops advancing. Keep both brakes in any change here.

**A stuck background job must not block itself forever.** After that hang, `autotune_state.running` stayed `True`, so the monthly loop and the dashboard button both refused to start ("draait al") until a redeploy — the feature was dead for six days without a single error in the logs. `autotune_stuck()` ignores the flag after `MAX_RUNTIME_S`. Use the same pattern for any new long-running job, and give network clients an explicit `timeout`.

**A backtest must not log per candle.** `analyze()` writes several INFO lines on every call, so a backtest over a few thousand candles emits thousands of lines per second. Railway caps a replica at 500 logs/sec and drops the rest — in one run ~12,000 messages vanished, including the backtest's own result lines, which made a perfectly healthy autotune look like it had hung. `run_backtest` now wraps the whole run in `_quiet_strategy_logging()`, which lifts the `strategy` logger to WARNING and restores it afterwards. Keep any new high-volume loop out of INFO.

**A redeploy must not silently stop trading.** `state.running` lives in memory and only `POST /start` used to set it, so every deploy left the bot idle until someone noticed and clicked Start — once for half a day. `auto_start_bot()` now launches the loop on boot (disable with `AUTO_START=false`). Both it and `/start` go through `_launch_bot_thread()`, which refuses to start a second loop.

**A trigger that is never called is not a feature.** `bot.py:_maybe_run_learning()` existed, looked finished, and was referenced nowhere — so `learn.py` only ever ran when someone clicked "Analyseer nu" in the dashboard. The monthly autotune was the *only* automatic learning path, and every proposal `learn.py` produced sat waiting for a human forever. It is now called from the main loop right after `manage_open_trades()`. Grep for call sites before trusting that a scheduled-looking function runs.

**Count down from a stored marker, not with a modulo.** The same function gated on `closed_count % 10 != 0`. It runs per candle, so at exactly 40 closed trades it would have re-analysed on *every* candle until the 41st trade closed. It now stores `last_learn_trades` in `learned_params` and compares the difference, which also survives a redeploy.

**Compare proposed values as numbers, not as JSON text.** A reverted change is blacklisted with the float the auto-learner used (`68.0`), while the original proposal carried an int (`68`). `json.dumps` made those two different signatures, so "de bot probeert dit niet opnieuw" quietly did not hold. `db.proposal_signature()` normalises numerically and is used by both `save_learning_proposals()` and the auto-learner.

**Whatever the bot applies by itself must be marked decided.** `maybe_auto_apply()` set the parameter and opened an experiment but left the proposal `pending`, so the dashboard kept asking for a decision about a change already live — and pressing "Negeren" would blacklist a running experiment. It now calls `decide_proposal(id, "accepted")`, and `_settle_duplicates()` does the same for repeat advice that matches the running experiment.

**The same ratio bug lived on in `learn.py` after autotune was fixed.** `_analyze_global_threshold` and `_analyze_setup_threshold` searched for the score threshold that maximised *average PnL per trade* — its own docstring said "maximaliseert de EXPECTANCY". Raising a score threshold is selection: you keep the best trades, so the per-trade figure almost always rises, even when you end up with less money. Within minutes of `_maybe_run_learning()` first being wired up, the bot raised rotation's threshold from 65 to 75 on that reasoning — tightening one of only two setups at a moment when the complaint was too few trades. Both now go through `_better_threshold()`, which judges **total** PnL, requires the filtered-out trades to actually lose money, and keeps at least `MIN_KEEP_RATIO` (75%) of the trade count. The margin is `base + max(|base| × 0.10, 1.0)`, not `base × 1.10`: with a negative total, a percentage bar moves the wrong way. When you fix an optimisation target, grep for every other place that scores the same thing.

**Anything the bot applies to itself needs an off switch.** A self-applied change could only be undone by sitting out the observation window — up to 60 days, and *longer* for a change that slows trading down, because the trades needed to judge it stop arriving. `cancel_active_experiment()` (button "⏹ Stop en zet terug" in `AutoLearnPanel`, `POST /learning/experiment/cancel`) reverts the parameter immediately and closes the experiment as `cancelled`. It deliberately does **not** blacklist: stopping something is not the same as proving it wrong.

**Optimise on total profit, never on per-trade ratios.** Autotune used to pick a variant on expectancy *per trade* plus profit factor. Both are ratios, so "fewer but better trades" always wins — even when it earns less money. Three tightenings stacked up that way (score 50→65, R:R 2.0→3.0, SL 1.5→1.75×ATR), each individually "proven", together throttling the bot: on one day 11 signals were rejected on score alone, three of them at 60 against a bar of 65. `_beats_baseline()` now judges on `total_pnl` over the test window, and refuses any variant that drops below 75% of the baseline's trade count, falls under profit factor 1.0, or deepens drawdown by more than half. Keep a frequency floor in any future objective.

**Verify imports after large changes.** After big refactors, run `python3 -m pip install -r requirements.txt` and then `python3 -c "from api import app; print('OK')"` to catch import errors before pushing. In this container plain `pip` and `python3` can resolve to different interpreters — use `python3 -m pip` to be sure they match.

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
- ✅ Autotune (`autotune.py`): maandelijkse (30d, checkloop in `api.py`) walk-forward backtest-sweep. `_neighbour_variants()` test per parameter de directe buren uit `PARAM_GRID` — één stap soepeler én één stap strenger, zodat versoepelen mogelijk is (de oude vaste lijst stond alleen bóven de default, waardoor de bot alleen kon aanscherpen). Dekt sl_atr_mult, min_rr én min_score_global; die laatste werd daarvóór nooit door een backtest getoetst. Beoordeling via `_beats_baseline()` op totaalwinst met frequentie-, profit-factor- en drawdown-remmen — één parameter per keer. Beste variant → `backtest_param` voorstel; factor-analyse over backtest-trades → `factor_insight` voorstel. Handmatig via `POST /learning/autotune` of dashboard-knop 🔬. Kernsetups (rotation/continuation) mogen NOOIT hard uitgeschakeld worden — dat legt de bot stil (deadlock: disabled setup kan win rate nooit herstellen).

- ✅ Automatisch leren (`autolearn.py`): na een autotune-sweep **én na elke leerronde** (`bot.py:_maybe_run_learning()`, om de 10 nieuwe gesloten trades vanaf 30) past de bot HOOGSTENS ÉÉN bewezen verbetering zelf toe, uit `AUTO_PARAMS` (sl_atr_mult, min_rr, min_score_global) plus de score per setup (`setup_min_scores.<setup>`, grenzen 40–75), en alleen binnen harde grenzen. Een advies dat eerder is afgewezen of teruggedraaid wordt NOOIT automatisch toegepast — `_candidate_from_proposals()` controleert de afwijs-lijst, net als `save_learning_proposals()`. Elke wijziging is een *experiment* in `learning_experiments`: de expectancy van de 20 trades ervóór wordt als nulmeting vastgelegd, daarna volgt een observatieperiode (20 trades of 60 dagen) waarin niets anders verandert. Daarna vergelijkt `evaluate_active_experiment()` — slechter dan de nulmeting → parameter terug naar de oude waarde én het advies op de afwijs-lijst, zodat het niet opnieuw wordt geprobeerd. Telegram meldt elke aanpassing, bevestiging en terugdraaiing; het dashboard toont het in `AutoLearnPanel`. Structurele ingrepen (setup uitschakelen, richtingfilter, handelsmodus) blijven BEWUST handmatig — die kunnen de handel in één keer stilleggen.

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
