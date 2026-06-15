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

export default function SnapshotChart({ trade }) {
  const containerRef  = useRef(null);
  const chartRef      = useRef(null);
  const seriesRef     = useRef(null);
  const markersRef    = useRef(null);
  const priceLinesRef = useRef([]);

  const [tf, setTf]         = useState("15m");
  const [error, setError]   = useState(null);
  const [loading, setLoading] = useState(true);
  const [source, setSource] = useState(null); // "snapshot" | "live"

  const drawChart = useCallback(async () => {
    if (!seriesRef.current) return;
    setLoading(true);
    setError(null);

    try {
      let candles = null;
      let entryTs = trade.timestamp
        ? Math.floor(new Date(trade.timestamp).getTime() / 1000)
        : null;

      // For 15m: try stored snapshot first (shows actual market context at trade time)
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

      // Fall back to live candles
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

      const addLine = (price, color, title, style = 2, width = 1) => {
        if (!price || price <= 0) return;
        priceLinesRef.current.push(
          seriesRef.current.createPriceLine({
            price, color, lineWidth: width, lineStyle: style,
            axisLabelVisible: true, title,
          })
        );
      };

      // Entry line (solid white, thicker)
      addLine(trade.entry_price, "#ffffff", "Entry", 0, 2);
      // SL
      addLine(trade.stop_loss, THEME.red, "SL");
      // TPs — brighter + checkmark when hit
      addLine(trade.tp1, trade.tp1_hit ? "#00e5b5" : THEME.green, trade.tp1_hit ? "TP1 ✓" : "TP1");
      addLine(trade.tp2, trade.tp2_hit ? "#00e5b5" : THEME.green, trade.tp2_hit ? "TP2 ✓" : "TP2");
      addLine(trade.tp3, trade.tp3_hit ? "#00e5b5" : THEME.green, trade.tp3_hit ? "TP3 ✓" : "TP3");
      // Exit price (orange, solid)
      if (trade.exit_price && trade.exit_price !== trade.entry_price) {
        addLine(trade.exit_price, THEME.yellow, "Exit", 0, 1.5);
      }

      // Entry marker arrow
      if (entryTs) {
        const closest = candles.reduce((best, c) =>
          Math.abs(c.time - entryTs) < Math.abs(best.time - entryTs) ? c : best
        , candles[0]);

        if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }
        markersRef.current = createSeriesMarkers(seriesRef.current, [{
          time:     closest.time,
          position: trade.side === "buy" ? "belowBar" : "aboveBar",
          shape:    trade.side === "buy" ? "arrowUp"  : "arrowDown",
          color:    trade.side === "buy" ? THEME.green : THEME.red,
          size:     2,
          text:     trade.side === "buy" ? "▲ LONG" : "▼ SHORT",
        }]);

        // Show 40 candles before entry + 60 after = full setup context visible
        const idx = candles.findIndex(c => c.time >= entryTs);
        if (idx >= 0) {
          const from = candles[Math.max(0, idx - 40)].time;
          const to   = candles[Math.min(candles.length - 1, idx + 60)].time;
          chartRef.current?.timeScale().setVisibleRange({ from, to });
        } else {
          chartRef.current?.timeScale().fitContent();
        }
      } else {
        chartRef.current?.timeScale().fitContent();
      }
    } catch (e) {
      setError(e?.message || "Fout bij laden");
    }
    setLoading(false);
  }, [tf, trade]);

  // Create chart once
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout:  { background: { color: THEME.bg }, textColor: THEME.text, fontSize: 10 },
      grid:    { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
      width:  containerRef.current.clientWidth,
      height: 300,
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor:         THEME.green, downColor:       THEME.red,
      borderUpColor:   THEME.green, borderDownColor: THEME.red,
      wickUpColor:     THEME.green, wickDownColor:   THEME.red,
    });
    chartRef.current  = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(e => chart.applyOptions({ width: e[0].contentRect.width }));
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }
      chart.remove();
      chartRef.current = null; seriesRef.current = null;
    };
  }, []);

  useEffect(() => {
    drawChart();
  }, [drawChart]);

  return (
    <div style={{ position: "relative" }}>
      {/* Timeframe buttons + source label */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
        <span style={{ fontSize: 9, fontWeight: 600, color: source === "snapshot" ? "#26a69a" : "#8b92a5" }}>
          {source === "snapshot" ? "📸 Opgeslagen candles (trade-moment)" : source === "live" ? "⚡ Live candles" : ""}
        </span>
        <div style={{ display: "flex", gap: 4 }}>
          {TF_OPTIONS.map(t => (
            <button key={t} onClick={() => setTf(t)} style={{
              padding: "3px 8px", borderRadius: 5, border: "1px solid",
              fontSize: 9, fontWeight: 700, cursor: "pointer", fontFamily: "inherit",
              background:  t === tf ? "#3b5bdb" : "#f0f2f7",
              color:       t === tf ? "#fff"    : "#8b92a5",
              borderColor: t === tf ? "#3b5bdb" : "#e2e5ef",
            }}>{t.toUpperCase()}</button>
          ))}
        </div>
      </div>

      <div ref={containerRef} style={{ borderRadius: 8, overflow: "hidden" }} />

      {loading && (
        <div style={{
          position: "absolute", inset: "30px 0 0 0",
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "rgba(19,23,34,0.7)", borderRadius: 8, fontSize: 11, color: "#8b92a5",
        }}>Candles laden…</div>
      )}
      {error && (
        <div style={{
          position: "absolute", inset: "30px 0 0 0",
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "rgba(19,23,34,0.9)", borderRadius: 8,
          fontSize: 11, color: THEME.red, fontWeight: 600,
        }}>⚠ {error}</div>
      )}
    </div>
  );
}
