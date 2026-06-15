import { useEffect, useRef } from "react";
import { createChart, CandlestickSeries } from "lightweight-charts";

/**
 * Toont een candle snapshot voor de trade review modal.
 * candles: [[timestamp_ms, open, high, low, close, volume], ...]
 */
export default function SnapshotChart({ candles, entry, sl, tp1, tp2, tp3, side, entryTs }) {
  const containerRef = useRef(null);

  useEffect(() => {
    if (!containerRef.current || !candles?.length) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { color: "#131722" },
        textColor:  "#8b92a5",
        fontSize:   10,
      },
      grid: {
        vertLines: { color: "#1e2130" },
        horzLines: { color: "#1e2130" },
      },
      crosshair: { mode: 1 },
      rightPriceScale: { borderColor: "#2a2d3e" },
      timeScale: {
        borderColor:    "#2a2d3e",
        timeVisible:    true,
        secondsVisible: false,
      },
      width:  containerRef.current.clientWidth,
      height: 240,
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor:         "#26a69a",
      downColor:       "#ef5350",
      borderUpColor:   "#26a69a",
      borderDownColor: "#ef5350",
      wickUpColor:     "#26a69a",
      wickDownColor:   "#ef5350",
    });

    // Converteer [ts_ms, o, h, l, c] → lightweight-charts formaat
    const data = candles
      .map(c => ({
        time:  Math.floor(c[0] / 1000),
        open:  c[1],
        high:  c[2],
        low:   c[3],
        close: c[4],
      }))
      .filter(c => c.open > 0 && c.high > 0)
      .sort((a, b) => a.time - b.time);

    if (!data.length) { chart.remove(); return; }
    series.setData(data);

    // Prijslijnen
    const addLine = (price, color, title, style = 2) => {
      if (!price || price <= 0) return;
      series.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title });
    };
    addLine(entry, "#ffffff", "entry", 0);
    addLine(sl,   "#ef5350", "SL");
    addLine(tp1,  "#26a69a", "TP1");
    addLine(tp2,  "#26a69a", "TP2");
    addLine(tp3,  "#26a69a", "TP3");

    // Zoom naar de trade zone: 10 candles voor entry tot einde
    if (entryTs) {
      const entryUnix = Math.floor(entryTs / 1000);
      const entryIdx  = data.findIndex(c => c.time >= entryUnix);
      if (entryIdx > 0) {
        const from = data[Math.max(0, entryIdx - 10)].time;
        const to   = data[data.length - 1].time;
        chart.timeScale().setVisibleRange({ from, to });
      } else {
        chart.timeScale().fitContent();
      }
    } else {
      chart.timeScale().fitContent();
    }

    const ro = new ResizeObserver(e => chart.applyOptions({ width: e[0].contentRect.width }));
    ro.observe(containerRef.current);

    return () => { ro.disconnect(); chart.remove(); };
  }, [candles, entry, sl, tp1, tp2, tp3, entryTs]);

  if (!candles?.length) {
    return (
      <div style={{
        height: 120, display: "flex", alignItems: "center", justifyContent: "center",
        background: "#131722", borderRadius: 8, color: "#8b92a5", fontSize: 11,
      }}>
        Geen candle snapshot opgeslagen voor deze trade
      </div>
    );
  }

  return <div ref={containerRef} style={{ borderRadius: 8, overflow: "hidden" }} />;
}
