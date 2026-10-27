import { useEffect, useRef, useState, useCallback } from "react";
import { createChart, CandlestickSeries, createSeriesMarkers } from "lightweight-charts";
import { C } from "./theme";

const API_URL = import.meta.env.VITE_API_URL || "";

const TIMEFRAMES = ["5m", "15m", "1h", "4h"];

// De grafiek deelt het palet met de rest van het dashboard; lightweight-charts
// tekent op canvas en kan daardoor niets uit de CSS overnemen.
const THEME = {
  bg:      C.card,
  text:    C.muted,
  grid:    C.border,
  border:  C.border,
  green:   C.green,
  red:     C.red,
};

export default function LiveCandleChart({ trades = [] }) {
  const containerRef  = useRef(null);
  const chartRef      = useRef(null);
  const seriesRef     = useRef(null);
  const markersRef    = useRef(null);
  const priceLinesRef = useRef([]);

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
      if (!res.ok) {
        // Toon het echte serverfout-detail zodat we kunnen zien wat er mis is
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body?.detail) detail = String(body.detail).slice(0, 160);
        } catch { /* geen JSON body */ }
        throw new Error(detail);
      }
      const candles = await res.json();
      if (!candles.length) return;

      seriesRef.current.setData(candles);
      chartRef.current?.timeScale().fitContent();
      setLastUpdate(new Date().toLocaleTimeString("nl-NL"));
      setError(null);
    } catch (e) {
      setError(e?.message || "Kan candles niet ophalen");
      setLoading(false);
      return;
    }

    // Markers + entry/SL/TP-lijnen in een eigen try: een fout hier mag de
    // candle-weergave zelf niet blokkeren.
    try {
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
        .filter(m => Number.isFinite(m.time))
        .sort((a, b) => a.time - b.time);

      if (markers.length) {
        markersRef.current = createSeriesMarkers(seriesRef.current, markers);
      }

      // Entry/SL/TP-prijslijnen voor OPEN trades
      priceLinesRef.current.forEach(pl => {
        try { seriesRef.current.removePriceLine(pl); } catch { /* al verwijderd */ }
      });
      priceLinesRef.current = [];
      trades
        .filter(t => t.status !== "closed" && t.entry_price)
        .forEach(t => {
          const addLine = (price, color, title, style = 2) => {
            if (!price) return;
            priceLinesRef.current.push(seriesRef.current.createPriceLine({
              price, color, lineWidth: 1, lineStyle: style,
              axisLabelVisible: true, title,
            }));
          };
          addLine(t.entry_price, C.blue, "entry", 0);
          addLine(t.stop_loss,   THEME.red,  "SL");
          addLine(t.tp1, THEME.green, "TP1");
          addLine(t.tp2, THEME.green, "TP2");
          addLine(t.tp3, THEME.green, "TP3");
        });
    } catch { /* markers/lijnen zijn nice-to-have */ }

    setLoading(false);
  }, [timeframe, trades]);

  useEffect(() => {
    setLoading(true);
    fetchAndDraw();
    const id = setInterval(fetchAndDraw, 15000);
    return () => clearInterval(id);
  }, [fetchAndDraw]);

  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, padding: 18 }}>
      {/* Header */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 10, letterSpacing: 1.5, color: C.muted, textTransform: "uppercase", fontWeight: 700 }}>
            Live Grafiek
          </div>
          {lastUpdate && (
            <div style={{ fontSize: 9, color: C.dim, marginTop: 2 }}>
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
                background:   tf === timeframe ? C.blue   : C.card2,
                color:        tf === timeframe ? "#ffffff" : C.muted,
                borderColor:  tf === timeframe ? C.blue    : C.border,
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
            background: "rgba(18,19,25,0.75)", borderRadius: 8,
            fontSize: 11, color: C.muted,
          }}>
            Candles laden…
          </div>
        )}
        {error && (
          <div style={{
            position: "absolute", inset: 0, display: "flex",
            alignItems: "center", justifyContent: "center",
            background: "rgba(18,19,25,0.9)", borderRadius: 8,
            fontSize: 11, color: C.red, fontWeight: 600,
          }}>
            ⚠ {error}
          </div>
        )}
      </div>

      {/* Legend */}
      {trades.some(t => t.entry_price) && (
        <div style={{ display: "flex", gap: 16, marginTop: 10, fontSize: 10, color: C.muted }}>
          <span style={{ color: C.green, fontWeight: 600 }}>▲ Long entry</span>
          <span style={{ color: C.red, fontWeight: 600 }}>▼ Short entry</span>
        </div>
      )}
    </div>
  );
}
