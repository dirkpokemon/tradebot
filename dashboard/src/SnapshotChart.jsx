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
};

const PRICE_AXIS_W = 68;

const SCENES = [
  { id: 1, emoji: "📊", label: "Context" },
  { id: 2, emoji: "🎯", label: "Entry" },
  { id: 3, emoji: "📈", label: "Verloop" },
];

// ── Helpers ───────────────────────────────────────────────────────────────────

function findSwingPoints(candles, lookback = 3) {
  const points = [];
  for (let i = lookback; i < candles.length - lookback; i++) {
    const c = candles[i];
    const isHH = candles.slice(i - lookback, i).every(x => x.high <= c.high)
              && candles.slice(i + 1, i + lookback + 1).every(x => x.high <= c.high);
    const isLL = candles.slice(i - lookback, i).every(x => x.low >= c.low)
              && candles.slice(i + 1, i + lookback + 1).every(x => x.low >= c.low);
    if (isHH) points.push({ type: "SH", time: c.time, price: c.high, idx: i });
    if (isLL) points.push({ type: "SL", time: c.time, price: c.low,  idx: i });
  }
  return points;
}

function extractKeyLevels(reason, entryPrice) {
  if (!reason || !entryPrice) return [];
  const matches = (reason.match(/\b\d{4,6}(?:\.\d+)?\b/g) || []).map(Number);
  return [...new Set(matches)].filter(
    p => p > 0 && Math.abs(p - entryPrice) / entryPrice < 0.06 && Math.round(p) !== Math.round(entryPrice)
  );
}

// ── Component ─────────────────────────────────────────────────────────────────

export default function SnapshotChart({ trade }) {
  const containerRef  = useRef(null);
  const chartRef      = useRef(null);
  const seriesRef     = useRef(null);
  const markersRef    = useRef(null);
  const priceLinesRef = useRef([]);
  const candlesRef    = useRef(null);
  const entryTsRef    = useRef(null);
  const sceneRef      = useRef(1);
  const computeOverlayRef = useRef(null);

  const [scene, setScene]     = useState(1);
  const [error, setError]     = useState(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource]   = useState(null);
  const [overlay, setOverlay] = useState(null);

  const isLong = trade.side === "buy";

  // ── Overlay computation ───────────────────────────────────────────────────
  const computeOverlay = useCallback(() => {
    if (!chartRef.current || !seriesRef.current) return;
    if (!trade.entry_price || !trade.stop_loss) return;
    if (sceneRef.current !== 2) { setOverlay(null); return; }
    try {
      const entryY = seriesRef.current.priceToCoordinate(trade.entry_price);
      const slY    = seriesRef.current.priceToCoordinate(trade.stop_loss);
      const bestTp = trade.tp3 || trade.tp2 || trade.tp1;
      const tpY    = bestTp ? seriesRef.current.priceToCoordinate(bestTp) : null;
      const entryX = entryTsRef.current
        ? chartRef.current.timeScale().timeToCoordinate(entryTsRef.current)
        : null;
      if (entryY == null || slY == null) { setOverlay(null); return; }
      setOverlay({ entryY, slY, tpY, entryX });
    } catch { setOverlay(null); }
  }, [trade]);

  useEffect(() => { computeOverlayRef.current = computeOverlay; }, [computeOverlay]);

  // ── Render a scene ────────────────────────────────────────────────────────
  const renderScene = useCallback((sceneId) => {
    if (!seriesRef.current || !chartRef.current || !candlesRef.current) return;
    const candles  = candlesRef.current;
    const entryTs  = entryTsRef.current;
    const entryIdx = entryTs != null
      ? candles.findIndex(c => c.time >= entryTs)
      : -1;

    priceLinesRef.current.forEach(pl => {
      try { seriesRef.current.removePriceLine(pl); } catch { /**/ }
    });
    priceLinesRef.current = [];

    if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }

    const addLine = (price, color, title, style = 2, width = 1) => {
      if (!price || price <= 0) return;
      priceLinesRef.current.push(
        seriesRef.current.createPriceLine({
          price, color, lineWidth: width, lineStyle: style,
          axisLabelVisible: true, title,
        })
      );
    };

    const riskPts = trade.entry_price && trade.stop_loss
      ? Math.abs(trade.entry_price - trade.stop_loss) : null;
    const rrText = (tp) => riskPts && tp
      ? `  ${(Math.abs(tp - trade.entry_price) / riskPts).toFixed(1)}R` : "";

    const markers = [];

    // ── Scene 1: Context ─────────────────────────────────────────────────────
    if (sceneId === 1) {
      const from = entryIdx >= 0 ? Math.max(0, entryIdx - 80) : 0;
      const to   = entryIdx >= 0 ? entryIdx + 5 : candles.length - 1;
      const contextCandles = candles.slice(from, to + 1);

      const keyLevels = extractKeyLevels(trade.reason, trade.entry_price);
      keyLevels.forEach(lvl => {
        addLine(lvl, THEME.yellow, `Key ${Math.round(lvl).toLocaleString()}`, 1, 1);
      });

      if (trade.entry_price) {
        addLine(trade.entry_price, "rgba(255,255,255,0.4)", `Entry ${Math.round(trade.entry_price).toLocaleString()}`, 2, 1);
      }

      const swings = findSwingPoints(contextCandles, 3);
      const dedupSwings = [];
      for (const s of swings) {
        const nearby = dedupSwings.find(x => x.type === s.type && Math.abs(x.idx - s.idx) < 5);
        if (!nearby) dedupSwings.push(s);
        else if (s.type === "SH" && s.price > nearby.price) {
          dedupSwings.splice(dedupSwings.indexOf(nearby), 1, s);
        } else if (s.type === "SL" && s.price < nearby.price) {
          dedupSwings.splice(dedupSwings.indexOf(nearby), 1, s);
        }
      }
      dedupSwings.slice(-6).forEach(s => {
        markers.push({
          time:     s.time,
          position: s.type === "SH" ? "aboveBar" : "belowBar",
          shape:    "circle",
          color:    s.type === "SH" ? "#9c64ff" : "#64b5f6",
          size:     0.8,
          text:     s.type === "SH" ? "SH" : "SL",
        });
      });

      if (entryTs && entryIdx >= 0) {
        const c = candles[entryIdx];
        markers.push({
          time:     c.time,
          position: isLong ? "belowBar" : "aboveBar",
          shape:    isLong ? "arrowUp" : "arrowDown",
          color:    isLong ? THEME.green : THEME.red,
          size:     1.5,
          text:     isLong ? "▲" : "▼",
        });
      }

      markers.sort((a, b) => a.time - b.time);
      if (markers.length) markersRef.current = createSeriesMarkers(seriesRef.current, markers);

      const fromTime = contextCandles[0]?.time;
      const toTime   = contextCandles[contextCandles.length - 1]?.time;
      if (fromTime && toTime) {
        chartRef.current.timeScale().setVisibleRange({ from: fromTime, to: toTime });
      }

    // ── Scene 2: Entry ───────────────────────────────────────────────────────
    } else if (sceneId === 2) {
      const from = entryIdx >= 0 ? Math.max(0, entryIdx - 12) : 0;
      const to   = entryIdx >= 0 ? Math.min(candles.length - 1, entryIdx + 16) : candles.length - 1;

      addLine(trade.entry_price, THEME.white,
        `Entry  ${Math.round(trade.entry_price).toLocaleString()}`, 0, 2);
      addLine(trade.stop_loss, THEME.red,
        `SL  ${Math.round(trade.stop_loss).toLocaleString()}`, 0, 1.5);
      addLine(trade.tp1, THEME.green, `TP1${rrText(trade.tp1)}`, 1, 1);
      if (trade.tp2) addLine(trade.tp2, THEME.green, `TP2${rrText(trade.tp2)}`, 1, 1);
      if (trade.tp3) addLine(trade.tp3, THEME.green, `TP3${rrText(trade.tp3)}`, 1, 1);

      const keyLevels = extractKeyLevels(trade.reason, trade.entry_price);
      keyLevels.forEach(lvl => {
        addLine(lvl, THEME.yellow, `Key ${Math.round(lvl).toLocaleString()}`, 1, 1);
      });

      if (entryTs && entryIdx >= 0) {
        const c = candles[entryIdx];
        markers.push({
          time:     c.time,
          position: isLong ? "belowBar" : "aboveBar",
          shape:    isLong ? "arrowUp" : "arrowDown",
          color:    isLong ? THEME.green : THEME.red,
          size:     2.5,
          text:     isLong ? "▲ LONG" : "▼ SHORT",
        });
      }

      markers.sort((a, b) => a.time - b.time);
      if (markers.length) markersRef.current = createSeriesMarkers(seriesRef.current, markers);

      const fromTime = candles[from]?.time;
      const toTime   = candles[to]?.time;
      if (fromTime && toTime) {
        chartRef.current.timeScale().setVisibleRange({ from: fromTime, to: toTime });
      }

    // ── Scene 3: Verloop ─────────────────────────────────────────────────────
    } else if (sceneId === 3) {
      addLine(trade.entry_price, THEME.white,
        `Entry  ${Math.round(trade.entry_price).toLocaleString()}`, 0, 1.5);
      addLine(trade.stop_loss, THEME.red,
        `SL  ${Math.round(trade.stop_loss).toLocaleString()}`, 0, 1);

      const tpLine = (tp, n, hit) => {
        if (!tp) return;
        const label = hit ? `TP${n} ✓${rrText(tp)}` : `TP${n}${rrText(tp)}`;
        addLine(tp, hit ? "#00e5b5" : THEME.green, label, hit ? 0 : 1, hit ? 1.5 : 1);
      };
      tpLine(trade.tp1, 1, trade.tp1_hit);
      tpLine(trade.tp2, 2, trade.tp2_hit);
      tpLine(trade.tp3, 3, trade.tp3_hit);

      if (trade.exit_price && trade.exit_price !== trade.entry_price) {
        addLine(trade.exit_price, THEME.yellow,
          `Exit  ${Math.round(trade.exit_price).toLocaleString()}`, 0, 1.5);
      }

      if (entryTs && entryIdx >= 0) {
        const postEntry = candles.slice(entryIdx + 1);
        const c = candles[entryIdx];

        markers.push({
          time:     c.time,
          position: isLong ? "belowBar" : "aboveBar",
          shape:    isLong ? "arrowUp" : "arrowDown",
          color:    isLong ? THEME.green : THEME.red,
          size:     2,
          text:     isLong ? "▲ LONG" : "▼ SHORT",
        });

        [[trade.tp1, trade.tp1_hit, 1], [trade.tp2, trade.tp2_hit, 2], [trade.tp3, trade.tp3_hit, 3]]
          .forEach(([tp, hit, n]) => {
            if (!tp || !hit) return;
            const found = postEntry.find(x => isLong ? x.high >= tp : x.low <= tp);
            if (found) markers.push({
              time:     found.time,
              position: isLong ? "aboveBar" : "belowBar",
              shape:    "circle",
              color:    "#00e5b5",
              size:     1.2,
              text:     `TP${n} ✓`,
            });
          });

        if (trade.status === "closed" && trade.realized_pnl < 0 && trade.stop_loss) {
          const found = postEntry.find(x => isLong ? x.low <= trade.stop_loss : x.high >= trade.stop_loss);
          if (found) markers.push({
            time:     found.time,
            position: isLong ? "belowBar" : "aboveBar",
            shape:    "circle",
            color:    THEME.red,
            size:     1.2,
            text:     "SL ✗",
          });
        }

        markers.sort((a, b) => a.time - b.time);
        if (markers.length) markersRef.current = createSeriesMarkers(seriesRef.current, markers);

        const lastMarkerIdx = candles.findIndex(x => x.time === markers[markers.length - 1]?.time);
        const toIdx = lastMarkerIdx >= 0
          ? Math.min(candles.length - 1, lastMarkerIdx + 10)
          : Math.min(candles.length - 1, entryIdx + 80);
        const fromTime = candles[Math.max(0, entryIdx - 5)]?.time;
        const toTime   = candles[toIdx]?.time;
        if (fromTime && toTime) {
          chartRef.current.timeScale().setVisibleRange({ from: fromTime, to: toTime });
        }
      } else {
        chartRef.current.timeScale().fitContent();
      }
    }

    setTimeout(() => computeOverlayRef.current?.(), 80);
  }, [trade, isLong]);

  // ── Load candles once ─────────────────────────────────────────────────────
  const loadAndRender = useCallback(async () => {
    if (!seriesRef.current) return;
    setLoading(true);
    setError(null);
    setOverlay(null);

    try {
      let candles = null;
      let entryTs = trade.timestamp
        ? Math.floor(new Date(trade.timestamp).getTime() / 1000)
        : null;

      if (trade.id) {
        try {
          const res = await fetch(`${API_URL}/trades/${trade.id}/candles`);
          if (res.ok) {
            const snap = await res.json();
            if (snap.candles && snap.candles.length >= 20) {
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

      seriesRef.current.setData(candles);
      candlesRef.current = candles;
      entryTsRef.current = entryTs;

      renderScene(sceneRef.current);
    } catch (e) {
      setError(e?.message || "Fout bij laden");
    }
    setLoading(false);
  }, [trade, renderScene]);

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
      height:          300,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: THEME.green, downColor: THEME.red,
      borderUpColor: THEME.green, borderDownColor: THEME.red,
      wickUpColor:   THEME.green, wickDownColor:   THEME.red,
    });

    chartRef.current  = chart;
    seriesRef.current = series;

    const onRangeChange = () => computeOverlayRef.current?.();
    chart.timeScale().subscribeVisibleTimeRangeChange(onRangeChange);

    const ro = new ResizeObserver(e => {
      chart.applyOptions({ width: e[0].contentRect.width });
      setTimeout(() => computeOverlayRef.current?.(), 50);
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.timeScale().unsubscribeVisibleTimeRangeChange(onRangeChange);
      if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }
      chart.remove();
      chartRef.current = null; seriesRef.current = null;
      candlesRef.current = null;
    };
  }, []);

  useEffect(() => { loadAndRender(); }, [loadAndRender]);

  // ── Scene switch ──────────────────────────────────────────────────────────
  const switchScene = useCallback((id) => {
    sceneRef.current = id;
    setScene(id);
    setOverlay(null);
    if (candlesRef.current) renderScene(id);
  }, [renderScene]);

  const sceneLabel = {
    1: "Marktcontext vóór de trade — swing punten (SH/SL), key niveaus",
    2: "Entrymoment — SL, TP doelwitten en risk/reward zones",
    3: "Uitkomst — welke TPs werden geraakt, hoe liep de trade af",
  }[scene];

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div style={{ position: "relative" }}>
      {/* Scene tabs */}
      <div style={{ display: "flex", gap: 4, marginBottom: 6, alignItems: "center", justifyContent: "space-between" }}>
        <span style={{ fontSize: 9, color: source === "snapshot" ? "#26a69a" : "#8b92a5", fontWeight: 600 }}>
          {source === "snapshot" ? "📸 Trade-moment" : source === "live" ? "⚡ Live" : ""}
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          {SCENES.map(s => (
            <button
              key={s.id}
              onClick={() => switchScene(s.id)}
              style={{
                padding: "4px 10px", borderRadius: 6, border: "1px solid",
                fontSize: 10, fontWeight: 700, cursor: "pointer", fontFamily: "inherit",
                background:  s.id === scene ? "#3b5bdb" : "#1a1d2e",
                color:       s.id === scene ? "#fff"    : "#8b92a5",
                borderColor: s.id === scene ? "#3b5bdb" : "#2a2d3e",
                transition:  "all 0.15s",
              }}
            >
              {s.emoji} {s.label}
            </button>
          ))}
        </div>
      </div>

      {/* Scene description */}
      <div style={{
        fontSize: 9, color: "#8b92a5", marginBottom: 5,
        padding: "3px 8px", background: "#1a1d2e",
        borderRadius: 4, borderLeft: "2px solid #3b5bdb",
      }}>
        {sceneLabel}
      </div>

      {/* Chart + overlay wrapper */}
      <div style={{ position: "relative", borderRadius: 8, overflow: "hidden" }}>
        <div ref={containerRef} />

        {/* Zone overlay — only scene 2 */}
        {overlay && !loading && scene === 2 && (() => {
          const { entryY, slY, tpY, entryX } = overlay;
          const showEntry = entryX != null && entryX > 10;
          const riskTop = Math.min(entryY, slY);
          const riskH   = Math.abs(entryY - slY);
          const rewTop  = tpY != null ? Math.min(entryY, tpY) : null;
          const rewH    = tpY != null ? Math.abs(entryY - tpY) : 0;

          return (
            <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
              {showEntry && rewTop != null && (
                <div style={{
                  position: "absolute",
                  left: entryX, top: rewTop,
                  width: `calc(100% - ${entryX}px - ${PRICE_AXIS_W}px)`,
                  height: rewH,
                  background: "rgba(38,166,154,0.10)",
                  borderTop:    isLong ? "none" : "1px solid rgba(38,166,154,0.35)",
                  borderBottom: isLong ? "1px solid rgba(38,166,154,0.35)" : "none",
                }} />
              )}
              {showEntry && (
                <div style={{
                  position: "absolute",
                  left: entryX, top: riskTop,
                  width: `calc(100% - ${entryX}px - ${PRICE_AXIS_W}px)`,
                  height: riskH,
                  background: "rgba(239,83,80,0.11)",
                  borderTop:    isLong ? "1px solid rgba(239,83,80,0.35)" : "none",
                  borderBottom: isLong ? "none" : "1px solid rgba(239,83,80,0.35)",
                }} />
              )}
              {showEntry && (
                <div style={{
                  position: "absolute",
                  left: entryX, top: 0, bottom: 0, width: 1,
                  background: "rgba(255,255,255,0.18)",
                }} />
              )}
            </div>
          );
        })()}

        {/* Scene 1 legend */}
        {!loading && scene === 1 && (
          <div style={{
            position: "absolute", bottom: 8, left: 8, pointerEvents: "none",
            display: "flex", gap: 10, fontSize: 8, fontWeight: 700,
          }}>
            <span style={{ color: "#9c64ff" }}>● SH = Swing High</span>
            <span style={{ color: "#64b5f6" }}>● SL = Swing Low</span>
            <span style={{ color: THEME.yellow }}>— Key niveau</span>
          </div>
        )}

        {/* Scene 3 outcome badge */}
        {!loading && scene === 3 && trade.status === "closed" && (
          <div style={{
            position: "absolute", top: 8, right: PRICE_AXIS_W + 4, pointerEvents: "none",
            background: trade.realized_pnl > 0 ? "rgba(38,166,154,0.25)" : "rgba(239,83,80,0.25)",
            border: `1px solid ${trade.realized_pnl > 0 ? "#26a69a" : "#ef5350"}`,
            borderRadius: 4, padding: "2px 7px",
            fontSize: 10, fontWeight: 700,
            color: trade.realized_pnl > 0 ? "#26a69a" : "#ef5350",
          }}>
            {trade.realized_pnl > 0 ? "✓ WIN" : "✗ LOSS"}&nbsp;
            {trade.realized_pnl != null ? `$${trade.realized_pnl > 0 ? "+" : ""}${trade.realized_pnl.toFixed(0)}` : ""}
          </div>
        )}

        {loading && (
          <div style={{
            position: "absolute", inset: 0,
            display: "flex", alignItems: "center", justifyContent: "center",
            background: "rgba(19,23,34,0.75)", fontSize: 11, color: "#8b92a5",
          }}>Candles laden…</div>
        )}
        {error && !loading && (
          <div style={{
            position: "absolute", inset: 0,
            display: "flex", alignItems: "center", justifyContent: "center",
            background: "rgba(19,23,34,0.9)",
            fontSize: 11, color: THEME.red, fontWeight: 600,
          }}>⚠ {error}</div>
        )}
      </div>
    </div>
  );
}
