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
};

const TF_OPTIONS = ["5m", "15m", "1h", "4h"];
const PRICE_AXIS_W = 68; // approximate right price-scale width in px

export default function SnapshotChart({ trade }) {
  const wrapperRef    = useRef(null);
  const containerRef  = useRef(null);
  const chartRef      = useRef(null);
  const seriesRef     = useRef(null);
  const markersRef    = useRef(null);
  const priceLinesRef = useRef([]);
  const entryTsRef    = useRef(null);

  // stable ref so the chart subscription always calls latest computeOverlay
  const computeOverlayRef = useRef(null);

  const [tf, setTf]         = useState("15m");
  const [error, setError]   = useState(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource]   = useState(null);
  const [overlay, setOverlay] = useState(null);

  const isLong = trade.side === "buy";

  // ── Compute zone overlay from chart internals ──────────────────────────────
  const computeOverlay = useCallback(() => {
    if (!chartRef.current || !seriesRef.current) return;
    if (!trade.entry_price || !trade.stop_loss)   return;

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

  // keep ref in sync
  useEffect(() => { computeOverlayRef.current = computeOverlay; }, [computeOverlay]);

  // ── Main draw function ─────────────────────────────────────────────────────
  const drawChart = useCallback(async () => {
    if (!seriesRef.current) return;
    setLoading(true);
    setError(null);
    setOverlay(null);

    try {
      let candles = null;
      let entryTs = trade.timestamp
        ? Math.floor(new Date(trade.timestamp).getTime() / 1000)
        : null;

      // Try stored snapshot for 15m (shows the actual market moment)
      if (tf === "15m" && trade.id) {
        try {
          const snapRes = await fetch(`${API_URL}/trades/${trade.id}/candles`);
          if (snapRes.ok) {
            const snap = await snapRes.json();
            if (snap.candles && snap.candles.length >= 20) {
              candles = snap.candles.map(c => ({
                time: c[0], open: c[1], high: c[2], low: c[3], close: c[4],
              }));
              if (snap.entry_ts) entryTs = snap.entry_ts;
              setSource("snapshot");
            }
          }
        } catch { /* fall through to live */ }
      }

      if (!candles) {
        const res = await fetch(`${API_URL}/candles?timeframe=${tf}&limit=300`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        candles = await res.json();
        setSource("live");
      }

      if (!candles.length) throw new Error("Geen candles beschikbaar");

      seriesRef.current.setData(candles);

      // Remove old price lines
      priceLinesRef.current.forEach(pl => {
        try { seriesRef.current.removePriceLine(pl); } catch { /* */ }
      });
      priceLinesRef.current = [];

      // ── Price lines ──────────────────────────────────────────────────────
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
        ? Math.abs(trade.entry_price - trade.stop_loss)
        : null;

      const rrText = (tp) => riskPts && tp
        ? `  ${(Math.abs(tp - trade.entry_price) / riskPts).toFixed(1)}R`
        : "";

      addLine(trade.entry_price, "#ffffff",
        `Entry  ${Math.round(trade.entry_price).toLocaleString()}`, 0, 2);
      addLine(trade.stop_loss,   THEME.red,
        `SL  ${Math.round(trade.stop_loss).toLocaleString()}`, 0, 1.5);

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

      // ── Markers ──────────────────────────────────────────────────────────
      const markers = [];

      if (entryTs) {
        entryTsRef.current = entryTs;

        const entryCandle = candles.reduce((best, c) =>
          Math.abs(c.time - entryTs) < Math.abs(best.time - entryTs) ? c : best
        , candles[0]);

        // Entry arrow
        markers.push({
          time:     entryCandle.time,
          position: isLong ? "belowBar" : "aboveBar",
          shape:    isLong ? "arrowUp"  : "arrowDown",
          color:    isLong ? THEME.green : THEME.red,
          size:     2,
          text:     isLong ? "▲ LONG" : "▼ SHORT",
        });

        const entryIdx  = candles.findIndex(c => c.time >= entryTs);
        const postEntry = entryIdx >= 0 ? candles.slice(entryIdx + 1) : [];

        // TP hit circles — exact candle where price crossed the level
        [[trade.tp1, trade.tp1_hit, 1], [trade.tp2, trade.tp2_hit, 2], [trade.tp3, trade.tp3_hit, 3]]
          .forEach(([tp, hit, n]) => {
            if (!tp || !hit) return;
            const c = postEntry.find(c => isLong ? c.high >= tp : c.low <= tp);
            if (c) markers.push({
              time:     c.time,
              position: isLong ? "aboveBar" : "belowBar",
              shape:    "circle",
              color:    "#00e5b5",
              size:     1,
              text:     `TP${n} ✓`,
            });
          });

        // SL hit circle (only for losing trades)
        if (trade.status === "closed" && trade.realized_pnl < 0 && trade.stop_loss) {
          const c = postEntry.find(c => isLong ? c.low <= trade.stop_loss : c.high >= trade.stop_loss);
          if (c) markers.push({
            time:     c.time,
            position: isLong ? "belowBar" : "aboveBar",
            shape:    "circle",
            color:    THEME.red,
            size:     1,
            text:     "SL ✗",
          });
        }

        // Zoom: 40 candles before entry, 60 after
        markers.sort((a, b) => a.time - b.time);

        const from = candles[Math.max(0, entryIdx - 40)].time;
        const to   = candles[Math.min(candles.length - 1, entryIdx + 60)].time;
        chartRef.current?.timeScale().setVisibleRange({ from, to });
      } else {
        chartRef.current?.timeScale().fitContent();
      }

      if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }
      if (markers.length) markersRef.current = createSeriesMarkers(seriesRef.current, markers);

    } catch (e) {
      setError(e?.message || "Fout bij laden");
    }
    setLoading(false);

    // Overlay coords need the chart to finish rendering first
    setTimeout(() => computeOverlayRef.current?.(), 120);
  }, [tf, trade, isLong]);

  // ── Create chart once ──────────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout:          { background: { color: THEME.bg }, textColor: THEME.text, fontSize: 10 },
      grid:            { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair:       { mode: 1 },
      rightPriceScale: { borderColor: THEME.border },
      timeScale:       { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
      width:           containerRef.current.clientWidth,
      height:          320,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: THEME.green, downColor: THEME.red,
      borderUpColor: THEME.green, borderDownColor: THEME.red,
      wickUpColor:   THEME.green, wickDownColor:   THEME.red,
    });

    chartRef.current  = chart;
    seriesRef.current = series;

    // Stable callback so the subscription always calls the latest computeOverlay
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
    };
  }, []);

  useEffect(() => { drawChart(); }, [drawChart]);

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div style={{ position: "relative" }}>
      {/* TF buttons + source indicator */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <span style={{ fontSize: 9, fontWeight: 600, color: source === "snapshot" ? "#26a69a" : "#8b92a5" }}>
          {source === "snapshot" ? "📸 Opgeslagen candles (trade-moment)" : source === "live" ? "⚡ Live candles" : ""}
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          {TF_OPTIONS.map(t => (
            <button key={t} className="tf-btn" onClick={() => setTf(t)} style={{
              padding: "3px 8px", borderRadius: 5, border: "1px solid",
              fontSize: 9, fontWeight: 700, cursor: "pointer", fontFamily: "inherit",
              background:  t === tf ? "#3b5bdb" : "#f0f2f7",
              color:       t === tf ? "#fff"    : "#8b92a5",
              borderColor: t === tf ? "#3b5bdb" : "#e2e5ef",
            }}>{t.toUpperCase()}</button>
          ))}
        </div>
      </div>

      {/* Chart + overlay in shared clipped container */}
      <div ref={wrapperRef} style={{ position: "relative", borderRadius: 8, overflow: "hidden" }}>
        <div ref={containerRef} />

        {/* ── Zone overlay ── */}
        {overlay && !loading && (() => {
          const { entryY, slY, tpY, entryX } = overlay;
          const showEntry = entryX != null && entryX > 10;

          // Risk zone (entry ↔ SL)
          const riskTop = Math.min(entryY, slY);
          const riskH   = Math.abs(entryY - slY);

          // Reward zone (entry ↔ bestTP)
          const rewTop = tpY != null ? Math.min(entryY, tpY) : null;
          const rewH   = tpY != null ? Math.abs(entryY - tpY) : 0;

          return (
            <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>

              {/* Reward zone — green */}
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

              {/* Risk zone — red */}
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

              {/* Entry vertical divider */}
              {showEntry && (
                <div style={{
                  position: "absolute",
                  left: entryX, top: 0, bottom: 0, width: 1,
                  background: "rgba(255,255,255,0.18)",
                }} />
              )}

              {/* "◀ Setup context" / "Uitkomst ▶" labels */}
              {entryX != null && entryX > 90 && (
                <>
                  <div style={{
                    position: "absolute", left: 6, top: 6,
                    fontSize: 8, fontWeight: 700, letterSpacing: 0.8,
                    textTransform: "uppercase", color: "rgba(139,146,165,0.5)",
                  }}>◀ Setup</div>
                  <div style={{
                    position: "absolute", left: entryX + 6, top: 6,
                    fontSize: 8, fontWeight: 700, letterSpacing: 0.8,
                    textTransform: "uppercase", color: "rgba(139,146,165,0.5)",
                  }}>Uitkomst ▶</div>
                </>
              )}
            </div>
          );
        })()}

        {/* Loading / error */}
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
