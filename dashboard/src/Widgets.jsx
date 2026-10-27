// Kleine visuele bouwstenen voor het dashboard: meters, ringen, de edge-radar en
// de kalender. Allemaal handgeschreven SVG — ze zijn klein genoeg dat een
// grafiekbibliotheek er alleen maar gewicht en styling-gevechten bij oplevert.

import { C, MONO } from "./theme";

const fmtMoney = (n, d = 2) =>
  n == null ? "—" : `${n < 0 ? "-" : ""}$${Math.abs(Number(n)).toLocaleString("nl-NL", {
    minimumFractionDigits: d, maximumFractionDigits: d,
  })}`;

export const money = fmtMoney;

// ── Kaart ─────────────────────────────────────────────────────────────────────

export function Card({ title, right, children, pad = 16, style }) {
  return (
    <div style={{
      background: C.card, border: `1px solid ${C.border}`, borderRadius: 12,
      padding: pad, minWidth: 0, ...style,
    }}>
      {(title || right) && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 10, marginBottom: 12,
        }}>
          <div style={{
            fontSize: 10, letterSpacing: 1.2, textTransform: "uppercase",
            color: C.muted, fontWeight: 700,
          }}>{title}</div>
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

// ── Boogmeter ─────────────────────────────────────────────────────────────────
// Een halve ring die van links naar rechts volloopt. Gebruikt voor percentages:
// de boog zegt "hoe ver van vol", het getal zegt hoeveel precies.

export function ArcGauge({ value, color, size = 56, track = C.border }) {
  const pct = Math.max(0, Math.min(100, value ?? 0));
  const r = size / 2 - 5;
  const cx = size / 2, cy = size / 2;
  // Halve cirkel van 180° (links) tot 0° (rechts), met de bolle kant omhoog.
  const polar = (deg) => {
    const rad = (deg * Math.PI) / 180;
    return [cx + r * Math.cos(rad), cy - r * Math.sin(rad)];
  };
  const [sx, sy] = polar(180);
  const [ex, ey] = polar(180 - (pct / 100) * 180);
  const [fx, fy] = polar(0);
  const big = pct > 50 ? 1 : 0;

  return (
    <svg width={size} height={size / 2 + 4} viewBox={`0 0 ${size} ${size / 2 + 4}`}>
      <path d={`M ${sx} ${sy} A ${r} ${r} 0 1 1 ${fx} ${fy}`}
        fill="none" stroke={track} strokeWidth={5} strokeLinecap="round" />
      {pct > 0 && (
        <path d={`M ${sx} ${sy} A ${r} ${r} 0 ${big} 1 ${ex} ${ey}`}
          fill="none" stroke={color} strokeWidth={5} strokeLinecap="round" />
      )}
    </svg>
  );
}

// ── Donutring ─────────────────────────────────────────────────────────────────

export function DonutRing({ value, color, size = 54, stroke = 6 }) {
  const pct = Math.max(0, Math.min(100, value ?? 0));
  const r = (size - stroke) / 2;
  const circ = 2 * Math.PI * r;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={C.border} strokeWidth={stroke} />
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke={color} strokeWidth={stroke}
        strokeLinecap="round" strokeDasharray={`${circ * pct / 100} ${circ}`}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: "stroke-dasharray 0.5s ease" }} />
    </svg>
  );
}

// ── Sparkline ─────────────────────────────────────────────────────────────────

export function Sparkline({ values, color, width = 82, height = 30 }) {
  if (!values || values.length < 2) return <div style={{ width, height }} />;
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const x = (i) => (i / (values.length - 1)) * width;
  const y = (v) => height - ((v - min) / span) * (height - 4) - 2;
  const line = values.map((v, i) => `${i ? "L" : "M"} ${x(i).toFixed(1)} ${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L ${width} ${height} L 0 ${height} Z`;
  const id = `spark-${color.replace(/[^a-z0-9]/gi, "")}`;
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.3} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${id})`} />
      <path d={line} fill="none" stroke={color} strokeWidth={1.6} strokeLinejoin="round" />
    </svg>
  );
}

// ── Winst/verlies-balk ────────────────────────────────────────────────────────
// Twee staafjes tegenover elkaar: gemiddelde winst boven, gemiddeld verlies
// onder. De lengteverhouding ís de uitkomst — je ziet meteen of winsten de
// verliezen dragen.

export function WinLossBars({ avgWin, avgLoss, width = 84 }) {
  const max = Math.max(avgWin, avgLoss, 1);
  return (
    <div style={{ width, display: "flex", flexDirection: "column", gap: 5 }}>
      <div style={{ height: 8, background: C.border, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${avgWin / max * 100}%`, height: "100%", background: C.green, borderRadius: 2 }} />
      </div>
      <div style={{ height: 8, background: C.border, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${avgLoss / max * 100}%`, height: "100%", background: C.red, borderRadius: 2 }} />
      </div>
    </div>
  );
}

// ── KPI-tegel ─────────────────────────────────────────────────────────────────

export function Kpi({ label, value, valueColor, sub, visual, hint }) {
  return (
    <div title={hint} style={{
      background: C.card, border: `1px solid ${C.border}`, borderRadius: 12,
      padding: "14px 16px", display: "flex", alignItems: "center",
      justifyContent: "space-between", gap: 12, minWidth: 0,
    }}>
      <div style={{ minWidth: 0 }}>
        <div style={{
          fontSize: 9.5, letterSpacing: 1.1, textTransform: "uppercase",
          color: C.muted, fontWeight: 700, marginBottom: 7, whiteSpace: "nowrap",
        }}>{label}</div>
        <div style={{
          fontFamily: MONO, fontSize: 21, fontWeight: 700, lineHeight: 1,
          color: valueColor || C.text, whiteSpace: "nowrap",
        }}>{value}</div>
        {sub && (
          <div style={{ fontSize: 10, color: C.dim, marginTop: 6, whiteSpace: "nowrap" }}>{sub}</div>
        )}
      </div>
      {visual && <div style={{ flexShrink: 0 }}>{visual}</div>}
    </div>
  );
}

// ── Edge-radar ────────────────────────────────────────────────────────────────
// Zes assen, elk 0-100. De gevulde vorm toont het profiel van de strategie; een
// inkeping wijst het zwakke punt aan zonder dat je zes getallen hoeft te lezen.

export function EdgeRadar({ axes, size = 190 }) {
  const cx = size / 2, cy = size / 2 + 4;
  const R = size / 2 - 34;
  const n = axes.length;
  const pt = (i, frac) => {
    const ang = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [cx + R * frac * Math.cos(ang), cy + R * frac * Math.sin(ang)];
  };
  const rings = [0.25, 0.5, 0.75, 1];
  const shape = axes.map((a, i) => pt(i, Math.max(0.04, a.value / 100))).map(p => p.join(",")).join(" ");

  // De viewBox is bewust ruimer dan de zeshoek zelf: de aslabels staan búiten
  // de buitenste ring en zouden anders tegen de rand worden afgekapt.
  return (
    <svg width="100%" viewBox={`-32 -6 ${size + 64} ${size + 20}`}
      style={{ display: "block", maxWidth: size + 64 }}>
      {rings.map(f => (
        <polygon key={f}
          points={axes.map((_, i) => pt(i, f).join(",")).join(" ")}
          fill="none" stroke={C.border} strokeWidth={1} />
      ))}
      {axes.map((_, i) => {
        const [x, y] = pt(i, 1);
        return <line key={i} x1={cx} y1={cy} x2={x} y2={y} stroke={C.border} strokeWidth={1} />;
      })}
      <polygon points={shape} fill="rgba(230,233,242,0.16)" stroke={C.text} strokeWidth={1.4} strokeLinejoin="round" />
      {axes.map((a, i) => {
        const [x, y] = pt(i, 1.3);
        return (
          <text key={a.key} x={x} y={y} fill={C.muted} fontSize={9}
            textAnchor={Math.abs(x - cx) < 6 ? "middle" : x > cx ? "start" : "end"}
            dominantBaseline="middle">
            {a.label}
          </text>
        );
      })}
    </svg>
  );
}

/** Kleurverloop rood→groen met een naald: waar staat de score op de schaal. */
export function ScoreMeter({ score }) {
  const pct = Math.max(0, Math.min(100, score ?? 0));
  return (
    <div>
      <div style={{
        position: "relative", height: 7, borderRadius: 99,
        background: "linear-gradient(90deg, #f05469 0%, #f0b429 45%, #24c98a 100%)",
      }}>
        <div style={{
          position: "absolute", left: `${pct}%`, top: -3, transform: "translateX(-50%)",
          width: 4, height: 13, borderRadius: 2, background: C.text,
          boxShadow: `0 0 0 2px ${C.card}`,
        }} />
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 5 }}>
        {[0, 20, 40, 60, 80, 100].map(n => (
          <span key={n} style={{ fontSize: 8, color: C.dim, fontFamily: MONO }}>{n}</span>
        ))}
      </div>
    </div>
  );
}

// ── Kalender-heatmap ──────────────────────────────────────────────────────────
// Een maandrooster waarin elke handelsdag zijn resultaat draagt. Dit maakt
// patronen zichtbaar die een lijst niet toont: clusters van verliesdagen,
// weken zonder enkele trade.

const DOW = ["ma", "di", "wo", "do", "vr", "za", "zo"];

export function CalendarHeatmap({ cells, maxAbs, monthLabel, onPrev, onNext, weekSums }) {
  const tone = (p) => {
    if (p == null) return null;
    const f = Math.min(1, Math.abs(p) / (maxAbs || 1));
    const a = 0.14 + f * 0.42;
    return p >= 0 ? `rgba(36,201,138,${a})` : `rgba(240,84,105,${a})`;
  };

  return (
    <div>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10 }}>
        <button onClick={onPrev} className="icon-btn" aria-label="Vorige maand">‹</button>
        <div style={{ fontSize: 12, fontWeight: 700, color: C.text }}>{monthLabel}</div>
        <button onClick={onNext} className="icon-btn" aria-label="Volgende maand">›</button>
      </div>

      {/* minmax(0,…) op elke kolom: anders dwingt de inhoud van een dagvakje een
          minimumbreedte af en wordt het rooster op een telefoon breder dan het scherm. */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(7, minmax(0,1fr)) minmax(38px, 52px)", gap: 4 }}>
        {DOW.map(d => (
          <div key={d} style={{
            fontSize: 8.5, color: C.dim, textAlign: "center", textTransform: "uppercase",
            letterSpacing: 0.6, paddingBottom: 2,
          }}>{d}</div>
        ))}
        <div style={{ fontSize: 8.5, color: C.dim, textAlign: "center", letterSpacing: 0.6 }}>WK</div>

        {cells.map((c, i) => {
          const isWeekEnd = i % 7 === 6;
          const cell = (
            <div key={`c${i}`} style={{
              aspectRatio: "1 / 1", borderRadius: 6, padding: 4,
              background: c?.pnl != null ? tone(c.pnl) : (c ? C.card2 : "transparent"),
              border: `1px solid ${c?.pnl != null ? "transparent" : (c ? C.border : "transparent")}`,
              display: "flex", flexDirection: "column", justifyContent: "space-between",
              minHeight: 42,
            }}>
              <div style={{ fontSize: 8.5, color: c ? C.muted : "transparent", fontFamily: MONO }}>
                {c?.day ?? ""}
              </div>
              {c?.pnl != null && (
                <div>
                  <div style={{
                    fontSize: 9.5, fontWeight: 700, fontFamily: MONO, lineHeight: 1.1,
                    color: c.pnl >= 0 ? C.green : C.red,
                  }}>
                    {c.pnl >= 0 ? "+" : "−"}${Math.abs(c.pnl).toFixed(0)}
                  </div>
                  <div style={{ fontSize: 8, color: C.muted }}>{c.trades}t</div>
                </div>
              )}
            </div>
          );
          if (!isWeekEnd) return cell;
          const wk = weekSums[Math.floor(i / 7)];
          return [
            cell,
            <div key={`w${i}`} style={{
              borderRadius: 6, background: C.card2, border: `1px solid ${C.border}`,
              display: "flex", flexDirection: "column", alignItems: "center",
              justifyContent: "center", minHeight: 42,
            }}>
              <div style={{
                fontSize: 9.5, fontWeight: 700, fontFamily: MONO,
                color: !wk?.trades ? C.dim : wk.pnl >= 0 ? C.green : C.red,
              }}>
                {wk?.trades ? `${wk.pnl >= 0 ? "+" : "−"}$${Math.abs(wk.pnl).toFixed(0)}` : "—"}
              </div>
              {wk?.trades > 0 && <div style={{ fontSize: 8, color: C.muted }}>{wk.trades}t</div>}
            </div>,
          ];
        })}
      </div>
    </div>
  );
}
