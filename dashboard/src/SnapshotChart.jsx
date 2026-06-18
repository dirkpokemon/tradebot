import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, CandlestickSeries, createSeriesMarkers } from "lightweight-charts";

const API_URL = import.meta.env.VITE_API_URL || "";

const THEME = {
  bg:     "#131722",
  text:   "#8b92a5",
  grid:   "#1e2130",
  border: "#2a2d3e",
  green:  "#26a69a",
  red:    "#ef5350",
  yellow: "#f59f00",
  white:  "#d1d4dc",
  blue:   "#3b5bdb",
  purple: "#9c64ff",
};

const SETUP_NL = {
  liquidity_sweep: "Liquiditeitssweep",
  rotation:        "Rotatie (HL/LH)",
  breakout:        "Breakout",
  continuation:    "Continuatie",
};

const SPEEDS = [1, 5, 20];

// ── Helpers ───────────────────────────────────────────────────────────────────

function findSwingPoints(candles, lookback = 3) {
  const out = [];
  for (let i = lookback; i < candles.length - lookback; i++) {
    const c = candles[i];
    if (candles.slice(i - lookback, i).every(x => x.high <= c.high) &&
        candles.slice(i + 1, i + lookback + 1).every(x => x.high <= c.high))
      out.push({ type: "SH", time: c.time, price: c.high, idx: i });
    if (candles.slice(i - lookback, i).every(x => x.low >= c.low) &&
        candles.slice(i + 1, i + lookback + 1).every(x => x.low >= c.low))
      out.push({ type: "SL", time: c.time, price: c.low, idx: i });
  }
  return out;
}

function extractKeyLevels(reason, entryPrice) {
  if (!reason || !entryPrice) return [];
  const matches = (reason.match(/\b\d{4,6}(?:\.\d+)?\b/g) || []).map(Number);
  return [...new Set(matches)].filter(
    p => p > 0 && Math.abs(p - entryPrice) / entryPrice < 0.06 && Math.round(p) !== Math.round(entryPrice)
  );
}

function buildKeyFrames(candles, trade, entryIdx, isLong) {
  const frames = [];
  const riskPts = trade.entry_price && trade.stop_loss
    ? Math.abs(trade.entry_price - trade.stop_loss) : null;

  // Context moment (a bit before entry)
  if (entryIdx >= 10) {
    frames.push({
      idx:   Math.max(0, entryIdx - 8),
      type:  "context",
      title: `📊 ${SETUP_NL[trade.setup_type] || trade.setup_type} setup herkend`,
      body:  trade.reason || "Zie de structuur die de bot heeft gedetecteerd.",
      color: THEME.purple,
    });
  }

  // Entry
  if (entryIdx >= 0) {
    frames.push({
      idx:   entryIdx,
      type:  "entry",
      title: `${isLong ? "▲ LONG entry" : "▼ SHORT entry"} — score ${trade.context_score ?? "?"}`,
      body:  `Entry @ ${trade.entry_price?.toLocaleString()} · SL @ ${trade.stop_loss?.toLocaleString()} · Risico: ${riskPts ? Math.round(riskPts) : "?"}pt`,
      color: isLong ? THEME.green : THEME.red,
    });
  }

  // TP hits
  [[trade.tp1, trade.tp1_hit, 1], [trade.tp2, trade.tp2_hit, 2], [trade.tp3, trade.tp3_hit, 3]]
    .forEach(([tp, hit, n]) => {
      if (!tp || !hit || entryIdx < 0) return;
      const idx = candles.findIndex((c, i) => i > entryIdx && (isLong ? c.high >= tp : c.low <= tp));
      if (idx < 0) return;
      const rr = riskPts ? (Math.abs(tp - trade.entry_price) / riskPts).toFixed(1) : "?";
      frames.push({
        idx,
        type:  "tp",
        title: `✅ TP${n} bereikt — +${rr}R`,
        body:  `Prijs ${isLong ? "steeg" : "daalde"} naar ${Math.round(tp).toLocaleString()}. 25% van de positie gesloten.`,
        color: "#00e5b5",
      });
    });

  // SL hit
  if (trade.status === "closed" && trade.realized_pnl != null && trade.realized_pnl < 0 && trade.stop_loss && entryIdx >= 0) {
    const idx = candles.findIndex((c, i) => i > entryIdx && (isLong ? c.low <= trade.stop_loss : c.high >= trade.stop_loss));
    if (idx >= 0) frames.push({
      idx,
      type:  "sl",
      title: `❌ Stop Loss geraakt`,
      body:  `Prijs raakte SL @ ${Math.round(trade.stop_loss).toLocaleString()}. PnL: $${trade.realized_pnl.toFixed(0)}`,
      color: THEME.red,
    });
  }

  // End card
  const lastEventIdx = frames.length ? Math.max(...frames.map(f => f.idx)) + 12 : candles.length - 1;
  const endIdx = Math.min(candles.length - 1, lastEventIdx);
  if (trade.status === "closed") {
    const won = trade.realized_pnl != null && trade.realized_pnl > 0;
    frames.push({
      idx:   endIdx,
      type:  "end",
      title: won ? "🏁 Trade gesloten — WIN" : "🏁 Trade gesloten — LOSS",
      body:  `PnL: $${trade.realized_pnl != null ? (trade.realized_pnl > 0 ? "+" : "") + trade.realized_pnl.toFixed(2) : "?"}`,
      color: won ? THEME.green : THEME.red,
    });
  }

  return frames.sort((a, b) => a.idx - b.idx);
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function SnapshotChart({ trade }) {
  const containerRef   = useRef(null);
  const chartRef       = useRef(null);
  const seriesRef      = useRef(null);
  const markersRef     = useRef(null);
  const priceLinesRef  = useRef([]);
  const allCandlesRef  = useRef([]);
  const keyFramesRef   = useRef([]);
  const entryIdxRef    = useRef(-1);
  const frameRef       = useRef(0);
  const intervalRef    = useRef(null);
  const speedRef       = useRef(5);

  const [loaded, setLoaded]       = useState(false);
  const [error, setError]         = useState(null);
  const [playing, setPlaying]     = useState(false);
  const [frame, setFrame]         = useState(0);
  const [totalFrames, setTotalFrames] = useState(0);
  const [speedIdx, setSpeedIdx]   = useState(1); // index into SPEEDS
  const [annotation, setAnnotation] = useState(null);
  const [source, setSource]       = useState(null);

  const isLong = trade.side === "buy";

  // ── Apply a frame to the chart ────────────────────────────────────────────
  const applyFrame = useCallback((idx) => {
    if (!seriesRef.current || !allCandlesRef.current.length) return;
    const candles = allCandlesRef.current;
    const slice   = candles.slice(0, idx + 1);
    seriesRef.current.setData(slice);

    // Clear price lines
    priceLinesRef.current.forEach(pl => {
      try { seriesRef.current.removePriceLine(pl); } catch { /**/ }
    });
    priceLinesRef.current = [];
    if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }

    const addLine = (price, color, title, style = 2, width = 1) => {
      if (!price || price <= 0) return;
      priceLinesRef.current.push(seriesRef.current.createPriceLine({
        price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title,
      }));
    };

    const riskPts = trade.entry_price && trade.stop_loss
      ? Math.abs(trade.entry_price - trade.stop_loss) : null;
    const rrText = (tp) => riskPts && tp
      ? `  ${(Math.abs(tp - trade.entry_price) / riskPts).toFixed(1)}R` : "";

    const entryIdx = entryIdxRef.current;

    // Show lines only after entry
    if (idx >= entryIdx && entryIdx >= 0) {
      addLine(trade.entry_price, THEME.white,
        `Entry  ${Math.round(trade.entry_price).toLocaleString()}`, 0, 2);
      addLine(trade.stop_loss, THEME.red,
        `SL  ${Math.round(trade.stop_loss).toLocaleString()}`, 0, 1.5);

      const tpLine = (tp, n, hit) => {
        if (!tp) return;
        const reached = hit && candles.slice(entryIdx, idx + 1).some(c => isLong ? c.high >= tp : c.low <= tp);
        addLine(tp, reached ? "#00e5b5" : THEME.green,
          reached ? `TP${n} ✓${rrText(tp)}` : `TP${n}${rrText(tp)}`,
          reached ? 0 : 1, reached ? 1.5 : 1);
      };
      tpLine(trade.tp1, 1, trade.tp1_hit);
      tpLine(trade.tp2, 2, trade.tp2_hit);
      tpLine(trade.tp3, 3, trade.tp3_hit);

      if (trade.exit_price && trade.exit_price !== trade.entry_price &&
          idx >= candles.length - 5) {
        addLine(trade.exit_price, THEME.yellow,
          `Exit  ${Math.round(trade.exit_price).toLocaleString()}`, 0, 1.5);
      }
    } else {
      // Pre-entry: show swing points and key level as context
      const swings = findSwingPoints(slice, 3);
      const dedupSwings = [];
      for (const s of swings) {
        const nearby = dedupSwings.find(x => x.type === s.type && Math.abs(x.idx - s.idx) < 5);
        if (!nearby) dedupSwings.push(s);
        else if (s.type === "SH" && s.price > nearby.price)
          dedupSwings.splice(dedupSwings.indexOf(nearby), 1, s);
        else if (s.type === "SL" && s.price < nearby.price)
          dedupSwings.splice(dedupSwings.indexOf(nearby), 1, s);
      }

      const keyLevels = extractKeyLevels(trade.reason, trade.entry_price);
      keyLevels.forEach(lvl => {
        addLine(lvl, `rgba(245,159,0,0.6)`, `Key ${Math.round(lvl).toLocaleString()}`, 1, 1);
      });

      const markers = dedupSwings.slice(-8).map(s => ({
        time:     s.time,
        position: s.type === "SH" ? "aboveBar" : "belowBar",
        shape:    "circle",
        color:    s.type === "SH" ? THEME.purple : "#64b5f6",
        size:     0.7,
        text:     s.type,
      }));

      if (markers.length)
        markersRef.current = createSeriesMarkers(seriesRef.current, markers);
    }

    // After entry: build up accumulated markers
    if (idx >= entryIdx && entryIdx >= 0) {
      const markers = [];
      const postEntry = candles.slice(entryIdx + 1, idx + 1);

      markers.push({
        time:     candles[entryIdx].time,
        position: isLong ? "belowBar" : "aboveBar",
        shape:    isLong ? "arrowUp" : "arrowDown",
        color:    isLong ? THEME.green : THEME.red,
        size:     2,
        text:     isLong ? "▲ LONG" : "▼ SHORT",
      });

      [[trade.tp1, trade.tp1_hit, 1], [trade.tp2, trade.tp2_hit, 2], [trade.tp3, trade.tp3_hit, 3]]
        .forEach(([tp, hit, n]) => {
          if (!tp || !hit) return;
          const c = postEntry.find(x => isLong ? x.high >= tp : x.low <= tp);
          if (c) markers.push({
            time:     c.time,
            position: isLong ? "aboveBar" : "belowBar",
            shape:    "circle", color: "#00e5b5", size: 1.2, text: `TP${n} ✓`,
          });
        });

      if (trade.status === "closed" && trade.realized_pnl != null && trade.realized_pnl < 0 && trade.stop_loss) {
        const c = postEntry.find(x => isLong ? x.low <= trade.stop_loss : x.high >= trade.stop_loss);
        if (c) markers.push({
          time:     c.time,
          position: isLong ? "belowBar" : "aboveBar",
          shape:    "circle", color: THEME.red, size: 1.2, text: "SL ✗",
        });
      }

      markers.sort((a, b) => a.time - b.time);
      if (markers.length)
        markersRef.current = createSeriesMarkers(seriesRef.current, markers);
    }

    // Auto-scroll to keep latest candle visible
    if (slice.length > 0) {
      const last = slice[slice.length - 1].time;
      const visibleCount = Math.min(60, slice.length);
      const first = slice[Math.max(0, slice.length - visibleCount)].time;
      chartRef.current?.timeScale().setVisibleRange({ from: first, to: last });
    }
  }, [trade, isLong]);

  // ── Tick ──────────────────────────────────────────────────────────────────
  const tick = useCallback(() => {
    const candles = allCandlesRef.current;
    const next = frameRef.current + 1;
    if (next >= candles.length) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
      setPlaying(false);
      return;
    }

    const kf = keyFramesRef.current.find(k => k.idx === next);
    frameRef.current = next;
    setFrame(next);
    applyFrame(next);

    if (kf) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
      setPlaying(false);
      setAnnotation(kf);
    }
  }, [applyFrame]);

  // ── Play / Pause ──────────────────────────────────────────────────────────
  const play = useCallback(() => {
    if (intervalRef.current) return;
    setAnnotation(null);
    const ms = Math.max(30, Math.round(1000 / speedRef.current));
    intervalRef.current = setInterval(tick, ms);
    setPlaying(true);
  }, [tick]);

  const pause = useCallback(() => {
    clearInterval(intervalRef.current);
    intervalRef.current = null;
    setPlaying(false);
  }, []);

  const togglePlay = useCallback(() => {
    if (playing) pause();
    else play();
  }, [playing, play, pause]);

  const seekTo = useCallback((idx) => {
    pause();
    frameRef.current = idx;
    setFrame(idx);
    setAnnotation(null);
    applyFrame(idx);
  }, [pause, applyFrame]);

  const restart = useCallback(() => {
    pause();
    setAnnotation(null);
    seekTo(0);
  }, [pause, seekTo]);

  const changeSpeed = useCallback(() => {
    const next = (speedIdx + 1) % SPEEDS.length;
    setSpeedIdx(next);
    speedRef.current = SPEEDS[next];
    if (playing) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
      const ms = Math.max(30, Math.round(1000 / SPEEDS[next]));
      intervalRef.current = setInterval(tick, ms);
    }
  }, [speedIdx, playing, tick]);

  // ── Load data ─────────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoaded(false);
      setError(null);
      setAnnotation(null);
      pause();

      try {
        let candles = null;
        let entryTs = trade.timestamp
          ? Math.floor(new Date(trade.timestamp).getTime() / 1000) : null;

        if (trade.id) {
          try {
            const res = await fetch(`${API_URL}/trades/${trade.id}/candles`);
            if (res.ok) {
              const snap = await res.json();
              if (snap.candles?.length >= 20) {
                candles = snap.candles.map(c => ({
                  time: c[0], open: c[1], high: c[2], low: c[3], close: c[4],
                }));
                if (snap.entry_ts) entryTs = snap.entry_ts;
                setSource("snapshot");
              }
            }
          } catch { /**/ }
        }

        if (!candles) {
          const res = await fetch(`${API_URL}/candles?timeframe=15m&limit=500`);
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          candles = await res.json();
          setSource("live");
        }

        if (!candles.length) throw new Error("Geen candles beschikbaar");
        if (cancelled) return;

        const entryIdx = entryTs != null
          ? candles.findIndex(c => c.time >= entryTs) : -1;

        allCandlesRef.current = candles;
        entryIdxRef.current   = entryIdx;
        keyFramesRef.current  = buildKeyFrames(candles, trade, entryIdx, isLong);
        frameRef.current      = 0;

        setTotalFrames(candles.length);
        setFrame(0);
        seriesRef.current?.setData([]);
        applyFrame(0);
        setLoaded(true);

        // Start the replay automatically after a short delay
        setTimeout(() => { if (!cancelled) play(); }, 600);
      } catch (e) {
        if (!cancelled) setError(e?.message || "Fout bij laden");
      }
    }
    load();
    return () => { cancelled = true; clearInterval(intervalRef.current); intervalRef.current = null; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [trade.id]);

  // ── Create chart once ─────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout:          { background: { color: THEME.bg }, textColor: THEME.text, fontSize: 10 },
      grid:            { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair:       { mode: 1 },
      rightPriceScale: { borderColor: THEME.border },
      timeScale:       { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
      width:           containerRef.current.clientWidth,
      height:          280,
      handleScroll:    false,
      handleScale:     false,
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: THEME.green, downColor: THEME.red,
      borderUpColor: THEME.green, borderDownColor: THEME.red,
      wickUpColor: THEME.green, wickDownColor: THEME.red,
    });
    chartRef.current  = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(e => chart.applyOptions({ width: e[0].contentRect.width }));
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      clearInterval(intervalRef.current);
      if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }
      chart.remove();
      chartRef.current = null; seriesRef.current = null;
    };
  }, []);

  // ── Derived ───────────────────────────────────────────────────────────────
  const progress = totalFrames > 1 ? frame / (totalFrames - 1) : 0;
  const entryPassed = frame >= entryIdxRef.current && entryIdxRef.current >= 0;
  const wonTrade = trade.realized_pnl != null && trade.realized_pnl > 0;

  const ANNOTATION_ICONS = { context: "📊", entry: isLong ? "▲" : "▼", tp: "✅", sl: "❌", end: "🏁" };

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={{ fontFamily: "inherit" }}>

      {/* ── Header bar ── */}
      <div style={{
        display: "flex", alignItems: "center", gap: 8, marginBottom: 6,
        padding: "6px 10px", background: "#1a1d2e", borderRadius: 6,
        flexWrap: "wrap",
      }}>
        <span style={{
          fontSize: 10, fontWeight: 800, letterSpacing: 0.5, textTransform: "uppercase",
          color: isLong ? THEME.green : THEME.red,
        }}>
          {isLong ? "▲ LONG" : "▼ SHORT"}
        </span>
        <span style={{ fontSize: 9, color: THEME.text }}>·</span>
        <span style={{ fontSize: 10, fontWeight: 700, color: THEME.white }}>
          {SETUP_NL[trade.setup_type] || trade.setup_type || "?"}
        </span>
        {trade.context_score != null && (
          <>
            <span style={{ fontSize: 9, color: THEME.text }}>·</span>
            <span style={{
              fontSize: 9, fontWeight: 700,
              color: trade.context_score >= 70 ? THEME.green : trade.context_score >= 50 ? THEME.yellow : THEME.red,
            }}>
              Score {trade.context_score}/100
            </span>
          </>
        )}
        <div style={{ flex: 1 }} />
        {source && (
          <span style={{ fontSize: 8, color: source === "snapshot" ? THEME.green : THEME.text }}>
            {source === "snapshot" ? "📸 Trade-moment" : "⚡ Live candles"}
          </span>
        )}
        {trade.status === "closed" && trade.realized_pnl != null && (
          <span style={{
            fontSize: 9, fontWeight: 800,
            color: wonTrade ? THEME.green : THEME.red,
            background: wonTrade ? "rgba(38,166,154,0.15)" : "rgba(239,83,80,0.15)",
            border: `1px solid ${wonTrade ? THEME.green : THEME.red}`,
            borderRadius: 4, padding: "1px 6px",
          }}>
            {wonTrade ? "WIN" : "LOSS"} ${trade.realized_pnl > 0 ? "+" : ""}{trade.realized_pnl.toFixed(0)}
          </span>
        )}
      </div>

      {/* ── Chart + annotation wrapper ── */}
      <div style={{ position: "relative", borderRadius: 8, overflow: "hidden" }}>
        <div ref={containerRef} />

        {/* Annotation card */}
        {annotation && (
          <div style={{
            position: "absolute", top: 8, left: 8, right: 72,
            background: "rgba(19,23,34,0.94)",
            border: `1px solid ${annotation.color}`,
            borderLeft: `3px solid ${annotation.color}`,
            borderRadius: 6, padding: "8px 12px",
            pointerEvents: "none",
            animation: "fadeIn 0.2s ease",
          }}>
            <div style={{ fontSize: 11, fontWeight: 800, color: annotation.color, marginBottom: 2 }}>
              {annotation.title}
            </div>
            <div style={{ fontSize: 9, color: THEME.text, lineHeight: 1.5 }}>
              {annotation.body}
            </div>
            <div style={{ fontSize: 8, color: "rgba(139,146,165,0.6)", marginTop: 4 }}>
              Druk ▶ om verder te gaan
            </div>
          </div>
        )}

        {/* Loading overlay */}
        {!loaded && !error && (
          <div style={{
            position: "absolute", inset: 0, display: "flex",
            alignItems: "center", justifyContent: "center",
            background: "rgba(19,23,34,0.85)", fontSize: 11, color: THEME.text,
          }}>
            Candles laden…
          </div>
        )}
        {error && (
          <div style={{
            position: "absolute", inset: 0, display: "flex",
            alignItems: "center", justifyContent: "center",
            background: "rgba(19,23,34,0.9)", fontSize: 11,
            color: THEME.red, fontWeight: 600,
          }}>⚠ {error}</div>
        )}
      </div>

      {/* ── Controls ── */}
      <div style={{ marginTop: 8, display: "flex", flexDirection: "column", gap: 6 }}>
        {/* Progress bar (scrubber) */}
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 8, color: THEME.text, minWidth: 28, textAlign: "right" }}>
            {frame}
          </span>
          <div
            style={{ flex: 1, height: 4, background: "#2a2d3e", borderRadius: 2, cursor: "pointer", position: "relative" }}
            onClick={e => {
              const rect = e.currentTarget.getBoundingClientRect();
              const ratio = (e.clientX - rect.left) / rect.width;
              seekTo(Math.round(ratio * (totalFrames - 1)));
            }}
          >
            <div style={{
              height: "100%", borderRadius: 2,
              width: `${progress * 100}%`,
              background: entryPassed ? (wonTrade ? THEME.green : THEME.red) : THEME.blue,
              transition: "width 0.05s linear",
            }} />
            {/* Key frame markers on scrubber */}
            {keyFramesRef.current.map((kf, i) => (
              <div key={i} style={{
                position: "absolute", top: -2, bottom: -2, width: 2,
                left: `${(kf.idx / (totalFrames - 1)) * 100}%`,
                background: kf.color || THEME.yellow,
                borderRadius: 1, opacity: 0.8,
              }} />
            ))}
          </div>
          <span style={{ fontSize: 8, color: THEME.text, minWidth: 28 }}>
            {totalFrames}
          </span>
        </div>

        {/* Buttons row */}
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          {/* Restart */}
          <button onClick={restart} style={btnStyle("#2a2d3e", THEME.text)}>
            ⏮
          </button>
          {/* Play / Pause */}
          <button onClick={togglePlay} disabled={!loaded} style={btnStyle(THEME.blue, "#fff", !loaded)}>
            {playing ? "⏸ Pauze" : "▶ Speel"}
          </button>
          {/* Speed */}
          <button onClick={changeSpeed} style={btnStyle("#2a2d3e", THEME.yellow)}>
            {SPEEDS[speedIdx]}×
          </button>

          <div style={{ flex: 1 }} />

          {/* Skip to entry */}
          {entryIdxRef.current >= 0 && (
            <button onClick={() => seekTo(entryIdxRef.current)} style={btnStyle("#2a2d3e", THEME.green)}>
              🎯 Entry
            </button>
          )}
          {/* Skip to end */}
          <button onClick={() => seekTo(totalFrames - 1)} style={btnStyle("#2a2d3e", THEME.text)}>
            ⏭ Einde
          </button>
        </div>
      </div>

      {/* ── Legend ── */}
      <div style={{
        marginTop: 8, padding: "6px 10px", background: "#1a1d2e",
        borderRadius: 6, display: "flex", gap: 14, flexWrap: "wrap",
      }}>
        <LegendItem color={THEME.green}  symbol="▲" label="LONG entry" />
        <LegendItem color={THEME.red}    symbol="▼" label="SHORT entry" />
        <LegendItem color="#00e5b5"      symbol="●" label="TP geraakt" />
        <LegendItem color={THEME.red}    symbol="●" label="SL geraakt" />
        <LegendItem color={THEME.purple} symbol="●" label="Swing High" />
        <LegendItem color="#64b5f6"      symbol="●" label="Swing Low" />
        <LegendItem color={THEME.yellow} symbol="—" label="Key niveau" />
      </div>

      <style>{`
        @keyframes fadeIn { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: translateY(0); } }
      `}</style>
    </div>
  );
}

function LegendItem({ color, symbol, label }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
      <span style={{ color, fontSize: 10, fontWeight: 800, lineHeight: 1 }}>{symbol}</span>
      <span style={{ fontSize: 8, color: "#8b92a5" }}>{label}</span>
    </div>
  );
}

function btnStyle(bg, color, disabled = false) {
  return {
    background: bg, color, border: "none", borderRadius: 5,
    padding: "4px 10px", fontSize: 10, fontWeight: 700,
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.4 : 1, fontFamily: "inherit",
  };
}
