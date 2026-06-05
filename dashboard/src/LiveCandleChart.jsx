import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, CandlestickSeries, createSeriesMarkers } from "lightweight-charts";

const API_URL = import.meta.env.VITE_API_URL || "";

const TIMEFRAMES = ["5m", "15m", "1h", "4h"];

const THEME = {
  bg:      "#ffffff",
  text:    "#8b92a5",
  grid:    "#f0f2f7",
  border:  "#e2e5ef",
  green:   "#00b37e",
  red:     "#e63946",
};

export default function LiveCandleChart({ trades = [] }) {
  const containerRef = useRef(null);
  const chartRef     = useRef(null);
  const seriesRef    = useRef(null);
  const markersRef   = useRef(null);

  const [timeframe,   setTimeframe]   = useState("15m");
  const [lastUpdate,  setLastUpdate]  = useState(null);
  const [error,       setError]       = useState(null);
  const [loading,     setLoading]     = useState(true);

  // Create chart once on mount
  useEffect(() => {
    if (!containerRef.current) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: THEME.bg },
        textColor:  THEME.text,
        fontSize:   11,
      },
      grid: {
        vertLines: { color: THEME.grid },
        horzLines: { color: THEME.grid },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: THEME.border },
      timeScale: {
        borderColor:    THEME.border,
        timeVisible:    true,
        secondsVisible: false,
        fixLeftEdge:    true,
      },
      width:  containerRef.current.clientWidth,
      height: 380,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor:        THEME.green,
      downColor:      THEME.red,
      borderUpColor:  THEME.green,
      borderDownColor:THEME.red,
      wickUpColor:    THEME.green,
      wickDownColor:  THEME.red,
    });

    chartRef.current  = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver(entries => {
      chart.applyOptions({ width: entries[0].contentRect.width });
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      if (markersRef.current) {
        markersRef.current.detach();
        markersRef.current = null;
      }
      chart.remove();
      chartRef.current  = null;
      seriesRef.current = null;
    };
  }, []);

  const fetchAndDraw = useCallback(async () => {
    if (!seriesRef.current) return;
    try {
      const res = await fetch(`${API_URL}/candles?timeframe=${timeframe}&limit=150`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const candles = await res.json();
      if (!candles.length) return;

      seriesRef.current.setData(candles);

      // Rebuild markers
      if (markersRef.current) {
        markersRef.current.detach();
        markersRef.current = null;
      }

      const markers = trades
        .filter(t => t.entry_price && t.timestamp)
        .map(t => {
          const ts = Math.floor(new Date(t.timestamp).getTime() / 1000);
          const label = (t.setup_type || "")
            .split("_")
            .map(w => w[0]?.toUpperCase() + w.slice(1))
            .join(" ");
          return {
            time:     ts,
            position: t.side === "buy" ? "belowBar" : "aboveBar",
            color:    t.side === "buy" ? THEME.green : THEME.red,
            shape:    t.side === "buy" ? "arrowUp"   : "arrowDown",
            text:     label,
          };
        })
        .sort((a, b) => a.time - b.time);

      if (markers.length) {
        markersRef.current = createSeriesMarkers(seriesRef.current, markers);
      }

      chartRef.current?.timeScale().fitContent();
      setLastUpdate(new Date().toLocaleTimeString("nl-NL"));
      setError(null);
    } catch (e) {
      setError("Kan candles niet ophalen");
    } finally {
      setLoading(false);
    }
  }, [timeframe, trades]);

  useEffect(() => {
    setLoading(true);
    fetchAndDraw();
    const id = setInterval(fetchAndDraw, 15000);
    return () => clearInterval(id);
  }, [fetchAndDraw]);

  return (
    <div style={{ background: "#fff", borderRadius: 14, padding: 20, boxShadow: "0 1px 4px rgba(30,40,80,0.07)" }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 10, letterSpacing: 1.5, color: "#8b92a5", textTransform: "uppercase", fontWeight: 600 }}>
            Live Grafiek
          </div>
          {lastUpdate && (
            <div style={{ fontSize: 9, color: "#c5c9d6", marginTop: 2 }}>
              Bijgewerkt {lastUpdate} · vernieuwt elke 15s
            </div>
          )}
        </div>

        {/* Timeframe selector */}
        <div style={{ display: "flex", gap: 4 }}>
          {TIMEFRAMES.map(tf => (
            <button
              key={tf}
              onClick={() => setTimeframe(tf)}
              style={{
                padding: "4px 10px", borderRadius: 6, border: "1px solid",
                fontSize: 10, fontWeight: 700, cursor: "pointer",
                fontFamily: "inherit", letterSpacing: 0.5,
                background:   tf === timeframe ? "#3b5bdb" : "#f0f2f7",
                color:        tf === timeframe ? "#ffffff"  : "#8b92a5",
                borderColor:  tf === timeframe ? "#3b5bdb"  : "#e2e5ef",
              }}
            >
              {tf.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* Chart container */}
      <div style={{ position: "relative" }}>
        <div ref={containerRef} />
        {loading && (
          <div style={{
            position: "absolute", inset: 0, display: "flex",
            alignItems: "center", justifyContent: "center",
            background: "rgba(255,255,255,0.7)", borderRadius: 8,
            fontSize: 11, color: "#8b92a5",
          }}>
            Candles laden…
          </div>
        )}
        {error && (
          <div style={{
            position: "absolute", inset: 0, display: "flex",
            alignItems: "center", justifyContent: "center",
            background: "rgba(255,255,255,0.9)", borderRadius: 8,
            fontSize: 11, color: "#e63946", fontWeight: 600,
          }}>
            ⚠ {error}
          </div>
        )}
      </div>

      {/* Legend */}
      {trades.some(t => t.entry_price) && (
        <div style={{ display: "flex", gap: 16, marginTop: 10, fontSize: 10, color: "#8b92a5" }}>
          <span style={{ color: "#00b37e", fontWeight: 600 }}>▲ Long entry</span>
          <span style={{ color: "#e63946", fontWeight: 600 }}>▼ Short entry</span>
        </div>
      )}
    </div>
  );
}
