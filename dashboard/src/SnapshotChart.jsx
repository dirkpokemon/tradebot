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
};

const TF_OPTIONS = ["5m", "15m", "1h", "4h"];

/**
 * Grafiek in het trade-review modal.
 * Laadt LIVE candles (betrouwbaar), markeert de entry-prijs en toont SL/TP-lijnen.
 * Snapshot-data (entryTs) wordt gebruikt om de juiste positie in de chart te vinden.
 */
export default function SnapshotChart({ trade }) {
  const containerRef  = useRef(null);
  const chartRef      = useRef(null);
  const seriesRef     = useRef(null);
  const markersRef    = useRef(null);
  const priceLinesRef = useRef([]);

  const [tf, setTf]         = useState("15m");
  const [error, setError]   = useState(null);
  const [loading, setLoading] = useState(true);

  const drawChart = useCallback(async () => {
    if (!seriesRef.current) return;
    try {
      const res = await fetch(`${API_URL}/candles?timeframe=${tf}&limit=200`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const candles = await res.json();
      if (!candles.length) return;

      seriesRef.current.setData(candles);
      setError(null);

      // Verwijder oude price lines
      priceLinesRef.current.forEach(pl => {
        try { seriesRef.current.removePriceLine(pl); } catch { /* */ }
      });
      priceLinesRef.current = [];

      // Teken entry/SL/TP-lijnen
      const addLine = (price, color, title, style = 2) => {
        if (!price || price <= 0) return;
        priceLinesRef.current.push(
          seriesRef.current.createPriceLine({
            price, color, lineWidth: 1, lineStyle: style,
            axisLabelVisible: true, title,
          })
        );
      };
      addLine(trade.entry_price, "#ffffff",    "entry",  0);
      addLine(trade.stop_loss,   THEME.red,    "SL");
      addLine(trade.tp1,         THEME.green,  "TP1");
      addLine(trade.tp2,         THEME.green,  "TP2");
      addLine(trade.tp3,         THEME.green,  "TP3");
      if (trade.exit_price) addLine(trade.exit_price, "#f59f00", "exit");

      // Entry-marker: zoek de candle die het dichtst bij de entry-tijd ligt
      if (trade.timestamp) {
        const entryUnix = Math.floor(new Date(trade.timestamp).getTime() / 1000);
        // Zoek de candle die het dichtst bij de entry timestamp ligt
        const closest = candles.reduce((best, c) =>
          Math.abs(c.time - entryUnix) < Math.abs(best.time - entryUnix) ? c : best
        , candles[0]);

        if (markersRef.current) { markersRef.current.detach(); markersRef.current = null; }
        markersRef.current = createSeriesMarkers(seriesRef.current, [{
          time:     closest.time,
          position: trade.side === "buy" ? "belowBar" : "aboveBar",
          shape:    trade.side === "buy" ? "arrowUp"  : "arrowDown",
          color:    trade.side === "buy" ? THEME.green : THEME.red,
          text:     `Entry ${Math.round(trade.entry_price)}`,
        }]);

        // Zoom: toon 20 candles voor de entry tot 10 erna
        const idx = candles.findIndex(c => c.time >= entryUnix - 30);
        if (idx >= 0) {
          const from = candles[Math.max(0, idx - 5)].time;
          const to   = candles[Math.min(candles.length - 1, idx + 30)].time;
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

  // Chart aanmaken (eenmalig)
  useEffect(() => {
    if (!containerRef.current) return;
    const chart = createChart(containerRef.current, {
      layout:  { background: { color: THEME.bg }, textColor: THEME.text, fontSize: 10 },
      grid:    { vertLines: { color: THEME.grid }, horzLines: { color: THEME.grid } },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: { borderColor: THEME.border, timeVisible: true, secondsVisible: false },
      width:  containerRef.current.clientWidth,
      height: 260,
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
    setLoading(true);
    drawChart();
  }, [drawChart]);

  return (
    <div style={{ position: "relative" }}>
      {/* Timeframe knoppen */}
      <div style={{ display: "flex", gap: 4, marginBottom: 8, justifyContent: "flex-end" }}>
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

      <div ref={containerRef} style={{ borderRadius: 8, overflow: "hidden" }} />

      {loading && (
        <div style={{
          position: "absolute", inset: "32px 0 0 0",
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "rgba(19,23,34,0.7)", borderRadius: 8, fontSize: 11, color: "#8b92a5",
        }}>Candles laden…</div>
      )}
      {error && (
        <div style={{
          position: "absolute", inset: "32px 0 0 0",
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "rgba(19,23,34,0.9)", borderRadius: 8,
          fontSize: 11, color: THEME.red, fontWeight: 600,
        }}>⚠ {error}</div>
      )}
    </div>
  );
}
