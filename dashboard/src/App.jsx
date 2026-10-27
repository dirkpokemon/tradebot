import { useState, useEffect, useCallback, useMemo } from "react";
import LiveCandleChart from "./LiveCandleChart";
import {
  AreaChart, Area,
  BarChart, Bar,
  XAxis, YAxis, CartesianGrid,
  ResponsiveContainer, Cell, ReferenceLine, Tooltip,
} from "recharts";
import { C, MONO } from "./theme";
import {
  Card, Kpi, ArcGauge, DonutRing, Sparkline, WinLossBars,
  EdgeRadar, ScoreMeter, CalendarHeatmap, money,
} from "./Widgets";
import {
  tradeR, gradeOf, winRateOf, profitFactorOf, avgWinLossOf,
  byDay, cumulativeByDay, drawdownSeries, edgeScore, monthGrid,
} from "./metrics";

const API_URL = import.meta.env.VITE_API_URL || "";
// Perpetual futures symbolen (long én short) — spot verwijderd
const SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"];

const PHASES = {
  open:      { label: "OPEN",    color: C.green,  bg: C.greenBg,  pct: 100 },
  partial_1: { label: "TP1 ✓",  color: C.yellow, bg: C.yellowBg, pct: 75 },
  partial_2: { label: "TP2 ✓",  color: C.orange, bg: C.orangeBg, pct: 50 },
  partial_3: { label: "RUNNER", color: C.red,    bg: C.redBg,    pct: 25 },
  closed:    { label: "CLOSED", color: C.muted,  bg: C.card2,    pct: 0  },
};

const HEALTH = {
  healthy:   { color: C.green,  bg: C.greenBg,  label: "Actief"    },
  degrading: { color: C.yellow, bg: C.yellowBg, label: "Degrading" },
  disabled:  { color: C.red,    bg: C.redBg,    label: "Disabled"  },
};

const SETUP_META = [
  { key: "rotation",     label: "Rotation",     desc: "L→H→HL→HH / H→L→LH→LL" },
  { key: "continuation", label: "Continuation", desc: "Pullback naar oud niveau" },
];

// ─── Utilities ────────────────────────────────────────────────────────────────

const fmt     = (n, d = 2) => n == null ? "—" : Number(n).toFixed(d);
const fmtP    = (n) => n == null ? "—" : Number(n).toLocaleString("nl-NL", { maximumFractionDigits: 0 });
const fmtSign = (n, d = 2) => n == null ? "—" : `${n >= 0 ? "+" : "−"}$${Math.abs(Number(n)).toFixed(d)}`;
// ISO-timestamp (UTC, zoals de bot hem opslaat) → "05/08". Geen tijdzone-omrekening:
// de rest van de UI toont ook UTC, dus omrekenen zou de tijden onderling laten verschillen.
const fmtDate = (iso) => {
  if (!iso || iso.length < 10) return "—";
  const [, m, d] = iso.slice(0, 10).split("-");
  return `${d}/${m}`;
};
const getPhase = (t) => PHASES[t.status] || PHASES.open;

const MONTHS_NL = ["januari", "februari", "maart", "april", "mei", "juni",
  "juli", "augustus", "september", "oktober", "november", "december"];
const DAYS_NL = ["zondag", "maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag"];

// Gedeelde as-instellingen; donkere grafieken vragen om zwakkere gridlijnen dan
// de standaard van recharts, anders overschreeuwt het raster de data.
const AXIS = { stroke: "transparent", tick: { fill: C.dim, fontSize: 9 }, tickLine: false, axisLine: false };
const GRID = { strokeDasharray: "3 3", stroke: C.border, vertical: false };

// ─── Atoms ────────────────────────────────────────────────────────────────────

function PnlBadge({ value, size = 13 }) {
  if (value == null) return <span style={{ color: C.muted }}>—</span>;
  const pos = value >= 0;
  return (
    <span style={{ color: pos ? C.green : C.red, fontWeight: 700, fontSize: size, fontFamily: MONO }}>
      {pos ? "+" : "−"}${Math.abs(value).toFixed(2)}
    </span>
  );
}

function Tag({ children, color, bg }) {
  return (
    <span style={{
      fontSize: 9, padding: "2px 8px", borderRadius: 5, fontWeight: 700,
      letterSpacing: 0.5, textTransform: "uppercase",
      color: color || C.muted, background: bg || C.card2,
    }}>
      {children}
    </span>
  );
}

function SectionLabel({ children, badge }) {
  return (
    <div style={{
      fontSize: 10, letterSpacing: 1.2, color: C.muted, marginBottom: 10,
      textTransform: "uppercase", fontWeight: 700,
      display: "flex", alignItems: "center", gap: 8,
    }}>
      {children}
      {badge > 0 && (
        <span style={{
          background: C.blue, color: "#fff", fontSize: 9, fontWeight: 700,
          borderRadius: 99, padding: "1px 7px",
        }}>{badge}</span>
      )}
    </div>
  );
}

function EmptyState({ icon, text, sub, height = 120 }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center",
      justifyContent: "center", height, gap: 6,
    }}>
      <div style={{ fontSize: 22, opacity: 0.3 }}>{icon}</div>
      <div style={{ fontSize: 12, color: C.muted }}>{text}</div>
      {sub && <div style={{ fontSize: 10, color: C.dim }}>{sub}</div>}
    </div>
  );
}

// ─── Circuit Breaker Banner ───────────────────────────────────────────────────

function CircuitBreakerBanner({ status }) {
  if (!status?.circuit_breaker_active) return null;
  const until = status.circuit_breaker_until
    ? new Date(status.circuit_breaker_until * 1000).toLocaleString("nl-NL")
    : "—";
  return (
    <div style={{
      background: C.redBg, border: `1px solid ${C.red}55`,
      borderRadius: 10, padding: "12px 16px", marginBottom: 16,
      display: "flex", alignItems: "center", gap: 12,
    }}>
      <span style={{ fontSize: 18 }}>🚨</span>
      <div style={{ flex: 1 }}>
        <div style={{ fontSize: 12, color: C.red, fontWeight: 700 }}>
          Circuit Breaker actief — {status.consecutive_stops} stops op rij
        </div>
        <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>
          Bot gepauzeerd tot {until}
        </div>
      </div>
    </div>
  );
}

// ─── Win Rate Bar ─────────────────────────────────────────────────────────────

function WinRateBar({ value, small }) {
  if (value == null) return <div style={{ fontSize: 10, color: C.dim, fontStyle: "italic" }}>geen data</div>;
  const color = value >= 50 ? C.green : value >= 40 ? C.yellow : C.red;
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 4 }}>
        <span style={{ fontSize: small ? 9 : 10, color: C.muted }}>Win rate</span>
        <span style={{ fontSize: small ? 10 : 12, fontWeight: 700, color, fontFamily: MONO }}>{value}%</span>
      </div>
      <div style={{ height: 5, background: C.border, borderRadius: 99, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${value}%`, background: color, borderRadius: 99, transition: "width 0.4s ease" }} />
      </div>
    </div>
  );
}

// ─── Price Ladder ─────────────────────────────────────────────────────────────

function PriceLadder({ trade }) {
  const isLong = trade.side === "buy";
  const prices = [trade.stop_loss, trade.entry_price, trade.tp1, trade.tp2, trade.tp3];
  const minP = Math.min(...prices) * 0.9985;
  const maxP = Math.max(...prices) * 1.0015;
  const span = maxP - minP;
  const pct  = (p) => (p - minP) / span * 100;

  const zones = isLong ? [
    { from: trade.stop_loss,   to: trade.entry_price, color: C.red + "33"   },
    { from: trade.entry_price, to: trade.tp1,          color: C.green + "33" },
    { from: trade.tp1,         to: trade.tp2,          color: C.green + "55" },
    { from: trade.tp2,         to: trade.tp3,          color: C.green + "77" },
  ] : [
    { from: trade.entry_price, to: trade.stop_loss,   color: C.red + "33"   },
    { from: trade.tp1,         to: trade.entry_price, color: C.green + "33" },
    { from: trade.tp2,         to: trade.tp1,         color: C.green + "55" },
    { from: trade.tp3,         to: trade.tp2,         color: C.green + "77" },
  ];

  const markers = [
    { price: trade.stop_loss,   label: "SL",    color: C.red,   hit: true },
    { price: trade.entry_price, label: "ENTRY", color: C.text,  hit: true },
    { price: trade.tp1,         label: "TP1",   color: C.green, hit: trade.tp1_hit },
    { price: trade.tp2,         label: "TP2",   color: C.green, hit: trade.tp2_hit },
    { price: trade.tp3,         label: "TP3",   color: C.green, hit: trade.tp3_hit },
  ];

  return (
    <div style={{ position: "relative", height: 56, marginTop: 12, userSelect: "none" }}>
      <div style={{ position: "absolute", top: 22, left: 0, right: 0, height: 3, background: C.border, borderRadius: 2 }} />
      {zones.map((z, i) => {
        const l = Math.min(pct(z.from), pct(z.to));
        const w = Math.abs(pct(z.to) - pct(z.from));
        return <div key={i} style={{ position: "absolute", top: 22, height: 3, left: `${l}%`, width: `${w}%`, background: z.color }} />;
      })}
      {markers.map((m) => (
        <div key={m.label} style={{
          position: "absolute", left: `${pct(m.price)}%`,
          transform: "translateX(-50%)",
          display: "flex", flexDirection: "column", alignItems: "center", top: 0,
        }}>
          <div style={{ fontSize: 8, color: m.hit ? m.color : C.dim, whiteSpace: "nowrap", marginBottom: 1, fontWeight: 700 }}>
            {m.label}
          </div>
          <div style={{ width: 1, height: 9, background: m.hit ? m.color : C.border }} />
          <div style={{
            width: 7, height: 7, borderRadius: "50%",
            background: m.hit ? m.color : C.card2,
            border: `2px solid ${m.hit ? m.color : C.borderLt}`,
          }} />
          <div style={{ fontSize: 8, color: C.muted, whiteSpace: "nowrap", marginTop: 3, fontFamily: MONO }}>
            {fmtP(m.price)}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── TP Progress ──────────────────────────────────────────────────────────────

function TpProgress({ trade }) {
  return (
    <div style={{ display: "flex", gap: 4 }}>
      {[
        { key: "tp1_hit", label: "TP1" },
        { key: "tp2_hit", label: "TP2" },
        { key: "tp3_hit", label: "TP3" },
      ].map(tp => (
        <span key={tp.key} style={{
          fontSize: 9, padding: "2px 8px", borderRadius: 5, fontWeight: 700,
          background: trade[tp.key] ? C.greenBg : C.card2,
          color: trade[tp.key] ? C.green : C.muted,
        }}>
          {trade[tp.key] ? "✓ " : ""}{tp.label}
        </span>
      ))}
      <span style={{
        fontSize: 9, padding: "2px 8px", borderRadius: 5, fontWeight: 700,
        background: trade.status === "partial_3" ? C.redBg : C.card2,
        color: trade.status === "partial_3" ? C.red : C.muted,
      }}>
        {trade.status === "partial_3" ? "⟳ " : ""}RUN
      </span>
    </div>
  );
}

// ─── Active Trade Card ────────────────────────────────────────────────────────

function ActiveTradeCard({ trade }) {
  const phase  = getPhase(trade);
  const isLong = trade.side === "buy";
  const slLabel = trade.tp1_hit ? (trade.tp2_hit ? "SL (swing)" : "SL (BE)") : "SL";

  return (
    <div style={{
      background: C.card, borderRadius: 12, padding: "16px 18px", marginBottom: 10,
      border: `1px solid ${C.border}`,
      borderLeft: `3px solid ${isLong ? C.green : C.red}`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14, flexWrap: "wrap", gap: 8 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <span style={{ fontSize: 13, fontWeight: 800, color: isLong ? C.green : C.red }}>
            {isLong ? "▲ LONG" : "▼ SHORT"}
          </span>
          <span style={{ fontSize: 11, color: C.muted, fontFamily: MONO }}>{trade.symbol}</span>
          <Tag color={C.blue} bg={C.blueBg}>{trade.setup_type?.replace("_", " ")}</Tag>
          {trade.trade_mode && (
            <Tag color={trade.trade_mode === "scalp" ? C.orange : C.muted} bg={trade.trade_mode === "scalp" ? C.orangeBg : C.card2}>
              {trade.trade_mode.toUpperCase()}
            </Tag>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Tag color={phase.color} bg={phase.bg}>{phase.label}</Tag>
          <span style={{ fontSize: 10, color: C.muted }}>{phase.pct}% open</span>
        </div>
      </div>

      <div className="price-grid-6" style={{ display: "grid", gridTemplateColumns: "repeat(6,1fr)", gap: 8 }}>
        {[
          { label: "ENTRY",  value: fmtP(trade.entry_price), color: C.text },
          { label: slLabel,  value: fmtP(trade.stop_loss),   color: C.red  },
          { label: "TP1",    value: fmtP(trade.tp1), color: trade.tp1_hit ? C.green : C.muted },
          { label: "TP2",    value: fmtP(trade.tp2), color: trade.tp2_hit ? C.green : C.muted },
          { label: "TP3",    value: fmtP(trade.tp3), color: trade.tp3_hit ? C.green : C.muted },
          { label: "RUNNER", value: trade.tp3_hit ? "OPEN" : "—", color: trade.tp3_hit ? C.red : C.dim },
        ].map(item => (
          <div key={item.label} style={{ textAlign: "center" }}>
            <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1, marginBottom: 3, textTransform: "uppercase" }}>{item.label}</div>
            <div style={{ fontSize: 12, color: item.color, fontWeight: 700, fontFamily: MONO }}>{item.value}</div>
          </div>
        ))}
      </div>

      <PriceLadder trade={trade} />

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 10, flexWrap: "wrap", gap: 8 }}>
        <TpProgress trade={trade} />
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, marginBottom: 2 }}>GEREALISEERD</div>
          <PnlBadge value={trade.realized_pnl} size={14} />
        </div>
      </div>

      {trade.reason && (
        <div style={{ marginTop: 8, fontSize: 10.5, color: C.muted, lineHeight: 1.5 }}>{trade.reason}</div>
      )}

      {trade.context_score > 0 && (
        <div style={{ marginTop: 10 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
            <span style={{ fontSize: 9, color: C.muted, letterSpacing: 1 }}>CONTEXT SCORE</span>
            <span style={{ fontSize: 10, fontWeight: 700, fontFamily: MONO, color: trade.context_score >= 70 ? C.green : C.yellow }}>
              {trade.context_score}/100
            </span>
          </div>
          <div style={{ height: 4, background: C.border, borderRadius: 99, overflow: "hidden" }}>
            <div style={{
              height: "100%", borderRadius: 99, width: `${trade.context_score}%`,
              background: trade.context_score >= 70 ? C.green : C.yellow,
            }} />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Chart Tooltips ───────────────────────────────────────────────────────────

function ChartTip({ label, rows }) {
  return (
    <div style={{
      background: C.card2, border: `1px solid ${C.borderLt}`, borderRadius: 8,
      padding: "8px 11px", boxShadow: C.shadowMd, fontSize: 11,
    }}>
      <div style={{ color: C.muted, fontSize: 9, marginBottom: 4 }}>{label}</div>
      {rows.map((r, i) => (
        <div key={i} style={{ color: r.color, fontWeight: 700, fontFamily: MONO }}>{r.value}</div>
      ))}
    </div>
  );
}

const moneyTip = (fmtLabel = (l) => l) => ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  const v = payload[0].value;
  return <ChartTip label={fmtLabel(label)} rows={[{ value: fmtSign(v), color: v >= 0 ? C.green : C.red }]} />;
};

const absMoneyTip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return <ChartTip label={label} rows={[{ value: money(payload[0].value, 0), color: C.text }]} />;
};

// ─── Dashboard-grafieken ──────────────────────────────────────────────────────

/**
 * Vloeiende vlakgrafiek; gebruikt voor cumulatieve P&L en voor het saldo.
 *
 * `domain` staat standaard op automatisch in plaats van op nul: een saldo dat
 * rond $12.000 schommelt werd met een as vanaf $0 een kaarsrechte lijn, waarin
 * juist de beweging die je wilt zien verdween.
 */
function AreaSeries({ data, xKey, yKey, color, height = 150, zeroRef, tip, fmtY, domain = ["auto", "auto"] }) {
  const id = `grad-${yKey}-${color.replace(/[^a-z0-9]/gi, "")}`;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={data} margin={{ top: 6, right: 8, left: -22, bottom: 0 }}>
        <defs>
          <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"  stopColor={color} stopOpacity={0.28} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid {...GRID} />
        <XAxis dataKey={xKey} {...AXIS} interval="preserveStartEnd" minTickGap={44} tickFormatter={fmtDate} />
        <YAxis {...AXIS} tickFormatter={fmtY} width={54} domain={domain} />
        <Tooltip content={tip} cursor={{ stroke: C.borderLt }} />
        {zeroRef != null && <ReferenceLine y={zeroRef} stroke={C.borderLt} strokeDasharray="4 4" />}
        <Area type="monotone" dataKey={yKey} stroke={color} strokeWidth={2}
          fill={`url(#${id})`} dot={false} animationDuration={400} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

function DailyNetBars({ days, height = 150 }) {
  if (!days.length) return <EmptyState icon="▦" text="Nog geen handelsdagen" height={height} />;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart data={days} margin={{ top: 6, right: 8, left: -22, bottom: 0 }}>
        <CartesianGrid {...GRID} />
        <XAxis dataKey="date" {...AXIS} tickFormatter={fmtDate} interval="preserveStartEnd" />
        <YAxis {...AXIS} tickFormatter={v => `$${v}`} width={54} />
        <Tooltip content={moneyTip(l => fmtDate(l))} cursor={{ fill: C.card2 }} />
        <ReferenceLine y={0} stroke={C.borderLt} />
        <Bar dataKey="pnl" radius={[3, 3, 0, 0]} maxBarSize={26} animationDuration={400}>
          {days.map((d, i) => <Cell key={i} fill={d.pnl >= 0 ? C.green : C.red} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function DrawdownChart({ series, height = 150 }) {
  if (series.length < 2) return <EmptyState icon="◣" text="Nog geen drawdown-historie" height={height} />;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart data={series} margin={{ top: 6, right: 8, left: -22, bottom: 0 }}>
        <defs>
          <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%"  stopColor={C.red} stopOpacity={0.05} />
            <stop offset="100%" stopColor={C.red} stopOpacity={0.35} />
          </linearGradient>
        </defs>
        <CartesianGrid {...GRID} />
        <XAxis dataKey="ts" {...AXIS} interval="preserveStartEnd" minTickGap={44} tickFormatter={fmtDate} />
        <YAxis {...AXIS} tickFormatter={v => `$${v}`} width={54} />
        <Tooltip content={moneyTip(l => fmtDate(l))} cursor={{ stroke: C.borderLt }} />
        <ReferenceLine y={0} stroke={C.borderLt} />
        <Area type="monotone" dataKey="dd" stroke={C.red} strokeWidth={1.6}
          fill="url(#ddGrad)" dot={false} animationDuration={400} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ─── KPI-rij ──────────────────────────────────────────────────────────────────

function HeroKpis({ closed, days, status }) {
  const net = closed.reduce((s, t) => s + (t.realized_pnl || 0), 0);
  const wr  = winRateOf(closed);
  const pf  = profitFactorOf(closed);
  const { avgWin, avgLoss, ratio } = avgWinLossOf(closed);
  const dayWr = days.length ? days.filter(d => d.pnl > 0).length / days.length * 100 : null;
  const cum = cumulativeByDay(days).map(d => d.cum);

  const pfDisplay = pf == null ? "—" : pf === Infinity ? "∞" : pf.toFixed(2);
  const pfColor   = pf == null ? C.muted : (pf === Infinity || pf >= 1) ? C.green : C.red;

  return (
    <div className="kpi-row">
      <Kpi
        label="Netto P&L"
        value={closed.length ? fmtSign(net) : "—"}
        valueColor={net >= 0 ? C.green : C.red}
        sub={`${closed.length} gesloten trades`}
        visual={<Sparkline values={cum.length > 1 ? cum : null} color={net >= 0 ? C.green : C.red} />}
        hint="Som van alle gerealiseerde winsten en verliezen."
      />
      <Kpi
        label="Trade win %"
        value={wr == null ? "—" : `${wr.toFixed(1)}%`}
        valueColor={wr == null ? C.muted : wr >= 50 ? C.green : C.yellow}
        sub={`${closed.filter(t => (t.realized_pnl || 0) > 0).length} van ${closed.length}`}
        visual={<ArcGauge value={wr} color={wr >= 50 ? C.green : C.yellow} />}
        hint="Aandeel trades dat met winst sloot."
      />
      <Kpi
        label="Profit factor"
        value={pfDisplay}
        valueColor={pfColor}
        sub="winst ÷ verlies"
        visual={<DonutRing value={pf == null ? 0 : Math.min(100, (pf === Infinity ? 3 : pf) / 3 * 100)} color={pfColor} />}
        hint="Boven 1,0 verdient de strategie geld; dit telt zwaarder dan win rate."
      />
      <Kpi
        label="Dag win %"
        value={dayWr == null ? "—" : `${dayWr.toFixed(1)}%`}
        valueColor={dayWr == null ? C.muted : dayWr >= 50 ? C.green : C.yellow}
        sub={`${days.filter(d => d.pnl > 0).length} van ${days.length} dagen`}
        visual={<ArcGauge value={dayWr} color={dayWr >= 50 ? C.green : C.yellow} />}
        hint="Aandeel handelsdagen dat in de plus eindigde."
      />
      <Kpi
        label="Gem. winst / verlies"
        value={ratio == null ? "—" : ratio.toFixed(2)}
        valueColor={ratio == null ? C.muted : ratio >= 1 ? C.green : C.yellow}
        sub={`${money(avgWin, 0)} tegen ${money(avgLoss, 0)}`}
        visual={<WinLossBars avgWin={avgWin} avgLoss={avgLoss} />}
        hint="Hoeveel een gemiddelde winnaar opbrengt ten opzichte van een gemiddelde verliezer."
      />
    </div>
  );
}

// ─── Edge score ───────────────────────────────────────────────────────────────

function EdgeScoreCard({ closed, maxDd }) {
  const edge = useMemo(() => edgeScore(closed, maxDd), [closed, maxDd]);

  if (!edge) {
    return (
      <Card title="Edge score">
        <EmptyState icon="◇" text="Nog geen gesloten trades" sub="Verschijnt na de eerste trade" height={210} />
      </Card>
    );
  }

  const color = edge.score >= 70 ? C.green : edge.score >= 45 ? C.yellow : C.red;
  const weakest = [...edge.axes].sort((a, b) => a.value - b.value)[0];

  return (
    <Card title="Edge score">
      <div style={{ display: "flex", justifyContent: "center" }}>
        <EdgeRadar axes={edge.axes} />
      </div>
      <div style={{ marginTop: 10 }}>
        <ScoreMeter score={edge.score} />
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 8, marginTop: 10 }}>
        <span style={{ fontSize: 9.5, color: C.muted, letterSpacing: 1, textTransform: "uppercase", fontWeight: 700 }}>
          Jouw score
        </span>
        <span style={{ fontFamily: MONO, fontSize: 24, fontWeight: 700, color, lineHeight: 1 }}>
          {edge.score.toFixed(1)}
        </span>
      </div>
      <div style={{ fontSize: 10.5, color: C.muted, marginTop: 6, lineHeight: 1.5 }}>
        Zwakste punt: <span style={{ color: C.text, fontWeight: 600 }}>{weakest.label}</span> ({weakest.value}/100)
      </div>
    </Card>
  );
}

// ─── Kalender ─────────────────────────────────────────────────────────────────

function CalendarCard({ days }) {
  const dayMap = useMemo(() => new Map(days.map(d => [d.date, d])), [days]);
  const latest = days.length ? days[days.length - 1].date : new Date().toISOString().slice(0, 10);
  const [cursor, setCursor] = useState(() => {
    const [y, m] = latest.split("-").map(Number);
    return { y, m: m - 1 };
  });

  const cells = useMemo(() => monthGrid(cursor.y, cursor.m, dayMap), [cursor, dayMap]);
  const maxAbs = useMemo(() => Math.max(1, ...days.map(d => Math.abs(d.pnl))), [days]);
  const monthTotal = cells.reduce((s, c) => s + (c?.pnl || 0), 0);
  const monthTrades = cells.reduce((s, c) => s + (c?.trades || 0), 0);

  const weekSums = useMemo(() => {
    const out = [];
    for (let i = 0; i < cells.length; i += 7) {
      const week = cells.slice(i, i + 7);
      out.push({
        pnl: week.reduce((s, c) => s + (c?.pnl || 0), 0),
        trades: week.reduce((s, c) => s + (c?.trades || 0), 0),
      });
    }
    return out;
  }, [cells]);

  const shift = (delta) => setCursor(({ y, m }) => {
    const d = new Date(Date.UTC(y, m + delta, 1));
    return { y: d.getUTCFullYear(), m: d.getUTCMonth() };
  });

  return (
    <Card
      title="Kalender"
      right={
        <span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 700, color: monthTrades ? (monthTotal >= 0 ? C.green : C.red) : C.dim }}>
          {monthTrades ? `${fmtSign(monthTotal)} · ${monthTrades}t` : "geen trades"}
        </span>
      }
    >
      <CalendarHeatmap
        cells={cells}
        weekSums={weekSums}
        maxAbs={maxAbs}
        monthLabel={`${MONTHS_NL[cursor.m]} ${cursor.y}`}
        onPrev={() => shift(-1)}
        onNext={() => shift(1)}
      />
    </Card>
  );
}

// ─── Journaal ─────────────────────────────────────────────────────────────────

/**
 * Eén trade als journaalkaart: bedrag en R-multiple groot, daaronder het
 * verhaal. De gekleurde rand links maakt een reeks kaarten in één blik
 * leesbaar — je ziet de verliesclusters zonder een getal te lezen.
 */
function JournalCard({ t }) {
  const win = (t.realized_pnl || 0) >= 0;
  const r = tradeR(t);
  const grade = gradeOf(t.context_score);
  const isLong = t.side === "buy";

  return (
    <div style={{
      background: C.card, border: `1px solid ${C.border}`,
      borderLeft: `3px solid ${win ? C.green : C.red}`,
      borderRadius: 10, padding: "12px 14px", marginBottom: 8,
    }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
        <span style={{ fontFamily: MONO, fontSize: 17, fontWeight: 700, color: win ? C.green : C.red }}>
          {fmtSign(t.realized_pnl)}
        </span>
        {r != null && (
          <span style={{ fontFamily: MONO, fontSize: 11, fontWeight: 700, color: C.muted }}>
            {r >= 0 ? "+" : "−"}{Math.abs(r).toFixed(2)}R
          </span>
        )}
        <span style={{ fontFamily: MONO, fontSize: 10.5, color: C.dim }}>
          {(t.timestamp || "").slice(0, 10)} {(t.timestamp || "").slice(11, 16)}
        </span>
        <span style={{ fontFamily: MONO, fontSize: 10.5, color: C.muted }}>
          {(t.symbol || "").split("/")[0]}
        </span>
        <Tag color={isLong ? C.green : C.red} bg={isLong ? C.greenBg : C.redBg}>
          {isLong ? "long" : "short"}
        </Tag>
        <Tag color={C.blue} bg={C.blueBg}>{t.setup_type?.replace("_", " ")}</Tag>
        {t.trade_mode === "scalp" && <Tag color={C.orange} bg={C.orangeBg}>scalp</Tag>}
        {grade && (
          <span style={{
            fontSize: 10, fontWeight: 800, fontFamily: MONO,
            color: grade.color, border: `1px solid ${grade.color}66`,
            borderRadius: 99, padding: "1px 8px",
          }} title={`Context score ${t.context_score}/100`}>
            {grade.label}
          </span>
        )}
        <span style={{ marginLeft: "auto", display: "flex", gap: 3 }}>
          {["tp1_hit", "tp2_hit", "tp3_hit"].map((k, i) => (
            <span key={k} style={{
              fontSize: 8.5, padding: "1px 6px", borderRadius: 4, fontWeight: 700, fontFamily: MONO,
              background: t[k] ? C.greenBg : C.card2,
              color:      t[k] ? C.green   : C.dim,
            }}>TP{i + 1}</span>
          ))}
        </span>
      </div>

      {t.reason && (
        <div style={{ fontSize: 11, color: C.muted, marginTop: 8, lineHeight: 1.55 }}>
          <span style={{ color: C.dim, fontWeight: 700, fontSize: 9, letterSpacing: 1 }}>WAAROM · </span>
          {t.reason}
        </div>
      )}

      <div style={{ display: "flex", gap: 16, marginTop: 8, fontSize: 10, color: C.dim, fontFamily: MONO, flexWrap: "wrap" }}>
        <span>entry {fmtP(t.entry_price)}</span>
        <span>exit {fmtP(t.exit_price)}</span>
        <span>sl {fmtP(t.stop_loss)}</span>
        {t.context_score > 0 && <span>score {t.context_score}</span>}
      </div>
    </div>
  );
}

function ClosedTradesTable({ trades }) {
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 12, overflow: "hidden" }}>
      <div className="closed-table-scroll">
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11, minWidth: 800, tableLayout: "fixed" }}>
          <colgroup>
            {[104, 116, 78, 78, 78, 62, 56, null, 104].map((w, i) => (
              <col key={i} style={w ? { width: w } : undefined} />
            ))}
          </colgroup>
          <thead>
            <tr style={{ borderBottom: `1px solid ${C.border}`, background: C.card2 }}>
              {[
                ["Datum (UTC)", "left"], ["Setup", "left"], ["Side", "left"],
                ["Entry", "right"], ["Exit", "right"], ["R", "right"], ["Score", "right"],
                ["Reden", "left"], ["PnL", "right"],
              ].map(([h, align]) => (
                <th key={h} style={{
                  padding: "8px 12px", textAlign: align, color: C.muted, fontWeight: 700,
                  fontSize: 9, letterSpacing: 1, textTransform: "uppercase",
                }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {trades.map((t, i) => {
              const r = tradeR(t);
              return (
                <tr key={t.id || i} className="closed-row" style={{ borderBottom: `1px solid ${C.border}` }}>
                  <td style={{ padding: "7px 12px", whiteSpace: "nowrap", fontFamily: MONO }}>
                    <span style={{ color: C.text, fontWeight: 600 }}>{fmtDate(t.timestamp)}</span>
                    <span style={{ color: C.muted, marginLeft: 6 }}>{t.timestamp?.slice(11, 16)}</span>
                  </td>
                  <td style={{ padding: "7px 12px" }}>
                    <Tag color={C.blue} bg={C.blueBg}>{t.setup_type?.replace("_", " ")}</Tag>
                  </td>
                  <td style={{ padding: "7px 12px" }}>
                    <span style={{ color: t.side === "buy" ? C.green : C.red, fontWeight: 700, textTransform: "uppercase" }}>
                      {t.side === "buy" ? "▲" : "▼"} {t.side}
                    </span>
                  </td>
                  <td style={{ padding: "7px 12px", fontWeight: 600, textAlign: "right", fontFamily: MONO }}>{fmtP(t.entry_price)}</td>
                  <td style={{ padding: "7px 12px", color: C.muted, textAlign: "right", fontFamily: MONO }}>{fmtP(t.exit_price)}</td>
                  <td style={{ padding: "7px 12px", textAlign: "right", fontFamily: MONO, color: r == null ? C.dim : r >= 0 ? C.green : C.red }}>
                    {r == null ? "—" : `${r >= 0 ? "+" : "−"}${Math.abs(r).toFixed(1)}`}
                  </td>
                  <td style={{ padding: "7px 12px", textAlign: "right", color: C.muted, fontWeight: 600, fontFamily: MONO }}>
                    {t.context_score || "—"}
                  </td>
                  <td style={{ padding: "7px 12px", color: C.muted }} title={t.reason || ""}>
                    <div style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {t.reason || "—"}
                    </div>
                  </td>
                  <td style={{ padding: "7px 12px", textAlign: "right" }}><PnlBadge value={t.realized_pnl} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Select({ value, onChange, options, label }) {
  return (
    <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
      <span style={{ fontSize: 9.5, color: C.dim, textTransform: "uppercase", letterSpacing: 0.8, fontWeight: 700 }}>{label}</span>
      <select value={value} onChange={e => onChange(e.target.value)} style={{ width: "auto", padding: "5px 9px", fontSize: 11 }}>
        {options.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
      </select>
    </label>
  );
}

/**
 * Journaalpagina: eerst filteren, dan lezen. De filters staan bovenaan omdat de
 * eerste vraag bij een journaal bijna altijd "laat alleen X zien" is.
 */
function JournalPage({ closed }) {
  const [side,   setSide]   = useState("all");
  const [setup,  setSetup]  = useState("all");
  const [result, setResult] = useState("all");
  const [mode,   setMode]   = useState("all");
  const [view,   setView]   = useState(() => {
    try { return localStorage.getItem("tradebot.journalView") || "cards"; } catch { return "cards"; }
  });
  // Het journaal groeit onbeperkt; zonder deze grens wordt de pagina na een paar
  // maanden handelen metershoog en zoek je je eigen trades kwijt.
  const PAGE = 25;
  const [shown, setShown] = useState(PAGE);

  const pickView = (v) => {
    setView(v);
    try { localStorage.setItem("tradebot.journalView", v); } catch { /* private mode */ }
  };

  const filtered = useMemo(() => {
    return [...closed].reverse().filter(t => {
      if (side !== "all"   && t.side !== side) return false;
      if (setup !== "all"  && t.setup_type !== setup) return false;
      if (mode !== "all"   && (t.trade_mode || "daytrade") !== mode) return false;
      if (result === "win"  && (t.realized_pnl || 0) < 0) return false;
      if (result === "loss" && (t.realized_pnl || 0) >= 0) return false;
      return true;
    });
  }, [closed, side, setup, result, mode]);

  // Een nieuwe filtercombinatie is een nieuwe lijst: weer vanaf de eerste pagina.
  useEffect(() => { setShown(PAGE); }, [side, setup, result, mode]);

  const net = filtered.reduce((s, t) => s + (t.realized_pnl || 0), 0);
  const wr  = winRateOf(filtered);
  const visible = filtered.slice(0, shown);

  return (
    <>
      <div style={{
        display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap",
        background: C.card, border: `1px solid ${C.border}`, borderRadius: 12,
        padding: "11px 14px", marginBottom: 12,
      }}>
        <Select label="Side"   value={side}   onChange={setSide}
          options={[["all", "alle"], ["buy", "long"], ["sell", "short"]]} />
        <Select label="Setup"  value={setup}  onChange={setSetup}
          options={[["all", "alle"], ...SETUP_META.map(s => [s.key, s.label])]} />
        <Select label="Result" value={result} onChange={setResult}
          options={[["all", "alle"], ["win", "winst"], ["loss", "verlies"]]} />
        <Select label="Mode"   value={mode}   onChange={setMode}
          options={[["all", "alle"], ["daytrade", "daytrade"], ["scalp", "scalp"]]} />

        <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 14 }}>
          <span style={{ fontSize: 11, color: C.muted }}>
            {filtered.length} trades ·{" "}
            <span style={{ fontFamily: MONO, fontWeight: 700, color: net >= 0 ? C.green : C.red }}>{fmtSign(net)}</span>
            {wr != null && <span style={{ fontFamily: MONO }}> · {wr.toFixed(0)}% WR</span>}
          </span>
          <div className="seg">
            {[["cards", "Kaarten"], ["table", "Compact"]].map(([v, l]) => (
              <button key={v} onClick={() => pickView(v)} className={view === v ? "seg-on" : ""}>{l}</button>
            ))}
          </div>
        </div>
      </div>

      {filtered.length === 0 ? (
        <Card><EmptyState icon="▤" text="Geen trades met deze filters" height={120} /></Card>
      ) : view === "cards" ? (
        visible.map((t, i) => <JournalCard key={t.id || i} t={t} />)
      ) : (
        <ClosedTradesTable trades={visible} />
      )}

      {filtered.length > shown && (
        <button onClick={() => setShown(s => s + PAGE)} style={{
          width: "100%", marginTop: 8, padding: "11px 0",
          background: C.card, border: `1px solid ${C.border}`, borderRadius: 10,
          color: C.muted, fontFamily: "inherit", fontSize: 11.5, fontWeight: 700, cursor: "pointer",
        }}>
          Toon {Math.min(PAGE, filtered.length - shown)} meer — nog {filtered.length - shown} te gaan
        </button>
      )}
    </>
  );
}

// ─── Recente trades (compacte lijst op het dashboard) ────────────────────────

function RecentTradesCard({ closed }) {
  const rows = [...closed].reverse().slice(0, 10);
  return (
    <Card title="Recente trades" right={<span style={{ fontSize: 9.5, color: C.dim, letterSpacing: 1, textTransform: "uppercase", fontWeight: 700 }}>Netto P&L</span>}>
      {rows.length === 0 ? (
        <EmptyState icon="▤" text="Nog geen gesloten trades" height={120} />
      ) : (
        <div>
          {rows.map((t, i) => (
            <div key={t.id || i} style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "7px 0", borderBottom: i === rows.length - 1 ? "none" : `1px solid ${C.border}`,
            }}>
              <span style={{ fontFamily: MONO, fontSize: 11, color: C.muted, width: 86, flexShrink: 0 }}>
                {(t.timestamp || "").slice(0, 10)}
              </span>
              <span style={{ fontFamily: MONO, fontSize: 11, color: C.text, width: 44, flexShrink: 0 }}>
                {(t.symbol || "").split("/")[0]}
              </span>
              <span style={{ fontSize: 10, color: t.side === "buy" ? C.green : C.red, fontWeight: 700, width: 40, flexShrink: 0 }}>
                {t.side === "buy" ? "LONG" : "SHORT"}
              </span>
              <span style={{ fontSize: 10, color: C.dim, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {t.setup_type?.replace("_", " ")}
              </span>
              <PnlBadge value={t.realized_pnl} size={12} />
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// ─── Setup Health Panel ───────────────────────────────────────────────────────

function SetupHealthPanel({ setupHealth }) {
  return (
    <Card title="Setups">
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {SETUP_META.map(s => {
          const h = setupHealth?.[s.key];
          const health = HEALTH[h?.status || "healthy"];
          const isDisabled = h?.status === "disabled";
          return (
            <div key={s.key} style={{ opacity: isDisabled ? 0.55 : 1 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6, gap: 8 }}>
                <div style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 11, fontWeight: 700, color: isDisabled ? C.red : C.text }}>{s.label}</span>
                  <span style={{ fontSize: 9, color: C.dim, marginLeft: 6 }}>{s.desc}</span>
                </div>
                <Tag color={health.color} bg={health.bg}>{health.label}</Tag>
              </div>
              <WinRateBar value={h?.win_rate != null ? Math.round(h.win_rate * 100) : null} small />
              {h?.trades > 0 && (
                <div style={{ fontSize: 9, color: C.dim, marginTop: 3 }}>{h.trades} recente trades</div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function SetupStatsGrid({ stats }) {
  return (
    <div className="setup-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
      {SETUP_META.map(s => {
        const d = stats?.[s.key];
        const health = HEALTH[d?.health || "healthy"];
        const isDisabled = d?.health === "disabled";
        return (
          <div key={s.key} style={{
            background: C.card2, border: `1px solid ${C.border}`,
            borderLeft: `3px solid ${health.color}`,
            borderRadius: 10, padding: "12px 14px",
            opacity: isDisabled ? 0.6 : 1,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10, gap: 8 }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 10, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5 }}>{s.label}</div>
                <div style={{ fontSize: 9, color: C.dim, marginTop: 2 }}>{s.desc}</div>
              </div>
              <Tag color={health.color} bg={health.bg}>{health.label}</Tag>
            </div>
            <WinRateBar value={d?.win_rate ?? null} />
            {d?.count > 0 ? (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 10 }}>
                {[
                  ["TRADES", d.count, C.text],
                  ["GEM. PNL", fmtSign(d.avg_pnl), d.avg_pnl >= 0 ? C.green : C.red],
                  ["WINST", `${d.wins}/${d.count}`, C.text],
                  ["PROF. FACTOR", d.profit_factor ?? "—", d.profit_factor > 1 ? C.green : d.profit_factor != null ? C.red : C.muted],
                ].map(([l, v, col]) => (
                  <div key={l}>
                    <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1 }}>{l}</div>
                    <div style={{ fontSize: 11, fontWeight: 700, color: col, marginTop: 2, fontFamily: MONO }}>{v}</div>
                  </div>
                ))}
              </div>
            ) : (
              <div style={{ fontSize: 10, color: C.dim, fontStyle: "italic", marginTop: 6 }}>geen trades</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Analyse Panel: wat ziet de bot ───────────────────────────────────────────

function TrendBadge({ value }) {
  if (!value) return <span style={{ color: C.muted, fontSize: 11 }}>—</span>;
  const map = {
    uptrend:   { label: "↑ Stijgend",  color: C.green },
    downtrend: { label: "↓ Dalend",    color: C.red   },
    ranging:   { label: "↔ Zijwaarts", color: C.muted },
    onbekend:  { label: "?",            color: C.dim   },
  };
  const t = map[value] || { label: value, color: C.muted };
  return (
    <span style={{ fontSize: 11, fontWeight: 700, color: t.color, background: t.color + "22", borderRadius: 5, padding: "1px 8px" }}>
      {t.label}
    </span>
  );
}

function AnalysisMode({ a, mode }) {
  if (!a) return null;
  const isSignal   = (a.result || "").startsWith("SIGNAAL");
  const isCooldown = (a.result || "").includes("cooldown");
  const ts = a.ts ? a.ts.replace("T", " ").replace("Z", " UTC") : null;

  let resultColor = C.text, resultIcon = "○";
  if (isSignal)   { resultColor = C.green;  resultIcon = "●"; }
  if (isCooldown) { resultColor = C.yellow; resultIcon = "⏸"; }

  const bd = a.score_breakdown;

  return (
    <div style={{
      border: `1px solid ${isSignal ? C.green + "55" : C.border}`,
      borderRadius: 10, padding: "12px 14px",
      background: isSignal ? C.greenBg : C.card2,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: 1.2, textTransform: "uppercase", color: C.muted }}>
          {mode}
        </span>
        {ts && <span style={{ fontSize: 9, color: C.dim, fontFamily: MONO }}>{ts}</span>}
      </div>

      {a.structuur && (
        <div style={{ display: "grid", gridTemplateColumns: "40px 1fr", gap: "4px 8px", marginBottom: 10, alignItems: "center" }}>
          {[["4H", a.structuur["4h"]], ["1H", a.structuur.context], ["15M", a.structuur.entry]].map(([l, v]) => (
            <Fragment2 key={l}>
              <span style={{ fontSize: 9, color: C.muted, letterSpacing: 0.8 }}>{l}</span>
              <TrendBadge value={v} />
            </Fragment2>
          ))}
        </div>
      )}

      <div style={{
        display: "flex", alignItems: "flex-start", gap: 6,
        padding: "8px 10px", borderRadius: 8,
        background: isSignal ? C.green + "22" : C.card,
        marginBottom: (a.notes?.length || bd) ? 8 : 0,
      }}>
        <span style={{ fontSize: 14, lineHeight: 1, color: resultColor, flexShrink: 0 }}>{resultIcon}</span>
        <span style={{ fontSize: 11, color: resultColor, fontWeight: isSignal ? 700 : 400, lineHeight: 1.45 }}>
          {a.result || "—"}
        </span>
      </div>

      {bd && !isSignal && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 4 }}>
            Score breakdown
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "3px 8px" }}>
            {Object.entries(bd).map(([k, v]) => (
              <span key={k} style={{ fontSize: 9, color: v > 0 ? C.green : C.dim, fontFamily: MONO }}>
                {k}: {v > 0 ? "+" : ""}{v}
              </span>
            ))}
          </div>
        </div>
      )}

      {(a.notes || []).length > 0 && (
        <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 6 }}>
          <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 4 }}>
            Wat de bot zag
          </div>
          {(a.notes || []).map((n, i) => (
            <div key={i} style={{
              fontSize: 10, color: C.muted, marginBottom: 3, lineHeight: 1.45,
              paddingLeft: 8, borderLeft: `2px solid ${C.borderLt}`,
            }}>{n}</div>
          ))}
        </div>
      )}
    </div>
  );
}

// Kleine helper omdat <></> geen key accepteert
function Fragment2({ children }) { return <>{children}</>; }

function AnalysisPanel({ lastAnalysis }) {
  const modes = ["daytrade", "scalp"];
  const hasData = modes.some(m => lastAnalysis?.[m]);
  return (
    <Card title="Wat doet de bot?">
      {!hasData ? (
        <div style={{ fontSize: 11, color: C.muted, padding: "12px 0" }}>
          Wacht op de eerste candle — start de bot om analyse te zien.
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {modes.map(m => <AnalysisMode key={m} a={lastAnalysis?.[m]} mode={m} />)}
        </div>
      )}
    </Card>
  );
}

// ─── Config Panel ─────────────────────────────────────────────────────────────

function ConfigPanel({ config, setConfig, isRunning, loading, onStart, onStop }) {
  return (
    <Card title="Configuratie">
      <label style={{ display: "block", marginBottom: 14 }}>
        <div style={{ fontSize: 9, color: C.muted, marginBottom: 5, letterSpacing: 1, textTransform: "uppercase" }}>Symbol</div>
        <select value={config.symbol} onChange={e => setConfig({ ...config, symbol: e.target.value })} disabled={isRunning}>
          {SYMBOLS.map(s => <option key={s}>{s}</option>)}
        </select>
      </label>
      <label style={{ display: "block", marginBottom: 18 }}>
        <div style={{ fontSize: 9, color: C.muted, marginBottom: 5, letterSpacing: 1, textTransform: "uppercase" }}>Risico per trade</div>
        <input type="number" min="0.001" max="0.05" step="0.001"
          value={config.risk_per_trade}
          onChange={e => setConfig({ ...config, risk_per_trade: parseFloat(e.target.value) })}
          disabled={isRunning} />
        <div style={{ fontSize: 10, color: C.muted, marginTop: 4 }}>
          {(config.risk_per_trade * 100).toFixed(1)}% per trade
        </div>
      </label>
      {!isRunning
        ? <button className="btn-primary" onClick={onStart} disabled={loading}>{loading ? "Starten…" : "▶  Start Bot"}</button>
        : <button className="btn-danger"  onClick={onStop}  disabled={loading}>{loading ? "Stoppen…" : "■  Stop Bot"}</button>
      }
    </Card>
  );
}

// ─── Backtest Panel ───────────────────────────────────────────────────────────

function MetricTile({ label, value, color, sub }) {
  return (
    <div style={{
      background: C.card2, border: `1px solid ${C.border}`, borderRadius: 10,
      padding: "10px 14px", flex: 1, minWidth: 92,
    }}>
      <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 14, fontWeight: 700, color: color || C.text, fontFamily: MONO }}>{value}</div>
      {sub && <div style={{ fontSize: 9, color: C.dim, marginTop: 3 }}>{sub}</div>}
    </div>
  );
}

function BacktestPanel() {
  const [btConfig, setBtConfig] = useState({ symbol: "BTC/USDT:USDT", days: 90, test_pct: 0.30, trade_mode: "daytrade" });
  const [bt, setBt] = useState(null);
  const [loading, setLoading] = useState(false);

  const pollBacktest = useCallback(async () => {
    const res  = await fetch(`${API_URL}/backtest`);
    const data = await res.json();
    setBt(data);
    if (data.running) setTimeout(pollBacktest, 2000);
  }, []);

  async function startBt() {
    setLoading(true);
    await fetch(`${API_URL}/backtest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...btConfig, days: Math.min(365, Math.max(30, Number(btConfig.days) || 90)) }),
    });
    setLoading(false);
    pollBacktest();
  }

  const r = bt?.result;
  const isPos = r?.total_pnl >= 0;

  return (
    <Card title="Backtest" style={{ marginBottom: 14 }} pad={18}>
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 14, alignItems: "flex-end" }}>
        <label style={{ flex: 1, minWidth: 90 }}>
          <div style={{ fontSize: 9, color: C.muted, marginBottom: 4, letterSpacing: 1, textTransform: "uppercase" }}>Dagen (30-365)</div>
          <input type="number" min="30" max="365" value={btConfig.days}
            onChange={e => setBtConfig({ ...btConfig, days: e.target.value === "" ? "" : +e.target.value })}
            onBlur={e => setBtConfig({ ...btConfig, days: Math.min(365, Math.max(30, Number(e.target.value) || 90)) })}
            disabled={bt?.running} />
        </label>
        <label style={{ flex: 1, minWidth: 90 }}>
          <div style={{ fontSize: 9, color: C.muted, marginBottom: 4, letterSpacing: 1, textTransform: "uppercase" }}>Test %</div>
          <input type="number" min="10" max="50" step="5" value={btConfig.test_pct * 100}
            onChange={e => setBtConfig({ ...btConfig, test_pct: e.target.value === "" ? "" : +e.target.value / 100 })}
            onBlur={e => setBtConfig({ ...btConfig, test_pct: Math.min(0.5, Math.max(0.1, (Number(e.target.value) || 30) / 100)) })}
            disabled={bt?.running} />
        </label>
        <label style={{ flex: 1, minWidth: 120 }}>
          <div style={{ fontSize: 9, color: C.muted, marginBottom: 4, letterSpacing: 1, textTransform: "uppercase" }}>Modus</div>
          <select value={btConfig.trade_mode} onChange={e => setBtConfig({ ...btConfig, trade_mode: e.target.value })} disabled={bt?.running}>
            <option value="daytrade">Daytrade (15m)</option>
            <option value="scalp">Scalp (5m)</option>
            <option value="both">Beide (multi-tf)</option>
          </select>
        </label>
        <button className="btn-primary" onClick={startBt} disabled={bt?.running || loading} style={{ flex: 1, minWidth: 130 }}>
          {bt?.running ? `${bt.progress}% bezig…` : "▶  Run Backtest"}
        </button>
      </div>

      {bt?.running && (
        <div style={{ height: 6, background: C.border, borderRadius: 99, overflow: "hidden", marginBottom: 16 }}>
          <div style={{ height: "100%", width: `${bt.progress}%`, background: C.blue, borderRadius: 99, transition: "width 0.3s" }} />
        </div>
      )}

      {bt?.error && <div style={{ color: C.red, fontSize: 11, marginBottom: 10 }}>⚠ {bt.error}</div>}

      {r && (
        <>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
            <MetricTile label="Trades"       value={r.total_trades} />
            <MetricTile label="Win Rate"     value={`${r.win_rate}%`} color={r.win_rate >= 50 ? C.green : C.red} />
            <MetricTile label="Prof. Factor" value={r.profit_factor ?? "—"} color={r.profit_factor > 1 ? C.green : C.red} />
            <MetricTile label="Sharpe"       value={r.sharpe ?? "—"} color={r.sharpe > 1 ? C.green : C.yellow} />
            <MetricTile label="Max DD"       value={r.max_drawdown_pct != null ? `-${r.max_drawdown_pct}%` : "—"} color={r.max_drawdown_pct > 15 ? C.red : C.muted} />
            <MetricTile label="PnL"          value={fmtSign(r.total_pnl)} color={isPos ? C.green : C.red} />
            <MetricTile label="Expectancy"   value={fmtSign(r.expectancy)} color={r.expectancy >= 0 ? C.green : C.red} />
          </div>

          <div style={{ fontSize: 9, color: C.dim, marginBottom: 12 }}>
            Train: {r.train_period} · Test: {r.test_period} · {r.duration_s}s
          </div>

          {r.equity_curve?.length > 1 && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 8 }}>
                Equity Curve — Test Window
              </div>
              <AreaSeries data={r.equity_curve} xKey="ts" yKey="equity" height={140}
                color={isPos ? C.greenLine : C.red} zeroRef={r.equity_curve[0]?.equity}
                tip={absMoneyTip} fmtY={v => `$${(v / 1000).toFixed(1)}k`} />
            </div>
          )}

          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(130px,1fr))", gap: 8 }}>
            {Object.entries(r.setup_stats || {}).map(([setup, d]) => (
              <div key={setup} style={{ background: C.card2, border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 12px" }}>
                <div style={{ fontSize: 9, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
                  {setup.replace("_", " ")}
                </div>
                {d.trades === 0
                  ? <div style={{ fontSize: 9, color: C.dim }}>geen trades</div>
                  : <>
                      <div style={{ fontSize: 13, fontWeight: 800, fontFamily: MONO, color: d.win_rate >= 50 ? C.green : C.red }}>{d.win_rate}%</div>
                      <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>{d.trades} trades · PF {d.profit_factor ?? "—"}</div>
                    </>
                }
              </div>
            ))}
          </div>

          {r.counter_trend_stats && Object.keys(r.counter_trend_stats).length > 0 && (
            <>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", margin: "16px 0 8px" }}>
                Met-trend vs. tegen-trend
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 8 }}>
                {[["with_trend", "Mét trend"], ["counter_trend", "⚖️ Tegen-trend"]].map(([key, label]) => {
                  const d = r.counter_trend_stats[key];
                  return (
                    <div key={key} style={{ background: C.card2, border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 12px" }}>
                      <div style={{ fontSize: 9, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>{label}</div>
                      {!d || d.trades === 0
                        ? <div style={{ fontSize: 9, color: C.dim }}>geen trades</div>
                        : <>
                            <div style={{ fontSize: 13, fontWeight: 800, fontFamily: MONO, color: d.win_rate >= 50 ? C.green : C.red }}>{d.win_rate}%</div>
                            <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>{d.trades} trades · PF {d.profit_factor ?? "—"} · gem. {fmtSign(d.avg_pnl)}</div>
                          </>
                      }
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {r.config?.trade_mode === "both" && r.mode_stats && Object.keys(r.mode_stats).length > 0 && (
            <>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", margin: "16px 0 8px" }}>
                Daytrade vs. scalp (multi-tf)
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 8 }}>
                {[["daytrade", "Daytrade (15m)"], ["scalp", "Scalp (5m)"]].map(([key, label]) => {
                  const d = r.mode_stats[key];
                  return (
                    <div key={key} style={{ background: C.card2, border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 12px" }}>
                      <div style={{ fontSize: 9, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>{label}</div>
                      {!d || d.trades === 0
                        ? <div style={{ fontSize: 9, color: C.dim }}>geen trades</div>
                        : <>
                            <div style={{ fontSize: 13, fontWeight: 800, fontFamily: MONO, color: d.win_rate >= 50 ? C.green : C.red }}>{d.win_rate}%</div>
                            <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>{d.trades} trades · PF {d.profit_factor ?? "—"} · gem. {fmtSign(d.avg_pnl)}</div>
                          </>
                      }
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </>
      )}
    </Card>
  );
}

// ─── Monte Carlo Panel ────────────────────────────────────────────────────────

function MonteCarloPanel() {
  const [mc, setMc] = useState(null);
  const [loading, setLoading] = useState(false);
  const [sims, setSims] = useState(1000);

  const pollMc = useCallback(async () => {
    const res  = await fetch(`${API_URL}/monte-carlo`);
    const data = await res.json();
    setMc(data);
    if (data.running) setTimeout(pollMc, 1500);
  }, []);

  async function startMc() {
    setLoading(true);
    const res = await fetch(`${API_URL}/monte-carlo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ n_simulations: sims }),
    });
    if (!res.ok) {
      const err = await res.json();
      setMc({ error: err.detail, running: false, progress: 0, result: null });
      setLoading(false);
      return;
    }
    setLoading(false);
    pollMc();
  }

  useEffect(() => { pollMc(); }, [pollMc]);

  const r = mc?.result;
  const verdictStyle = r
    ? (r.has_edge ? { color: C.green, bg: C.greenBg }
      : r.percentile_vs_random >= 80 ? { color: C.yellow, bg: C.yellowBg }
      : { color: C.red, bg: C.redBg })
    : null;

  return (
    <Card title="Monte Carlo validatie" style={{ marginBottom: 14 }} pad={18}>
      <div style={{ display: "flex", gap: 10, alignItems: "flex-end", marginBottom: 14, flexWrap: "wrap" }}>
        <label style={{ flex: 1, minWidth: 90 }}>
          <div style={{ fontSize: 9, color: C.muted, marginBottom: 4, letterSpacing: 1, textTransform: "uppercase" }}>Simulaties</div>
          <select value={sims} onChange={e => setSims(+e.target.value)} disabled={mc?.running}>
            {[500, 1000, 2000, 5000].map(n => <option key={n} value={n}>{n.toLocaleString()}</option>)}
          </select>
        </label>
        <button className="btn-primary" onClick={startMc} disabled={mc?.running || loading} style={{ flex: 2, minWidth: 180 }}>
          {mc?.running ? `${mc.progress}% bezig…` : "▶  Run Monte Carlo"}
        </button>
      </div>

      {!r && !mc?.running && (
        <div style={{ fontSize: 10.5, color: C.muted }}>
          Voer eerst een backtest uit, daarna Monte Carlo voor statistische validatie.
        </div>
      )}

      {mc?.running && (
        <div style={{ height: 5, background: C.border, borderRadius: 99, overflow: "hidden", marginBottom: 12 }}>
          <div style={{ height: "100%", width: `${mc.progress}%`, background: C.blue, borderRadius: 99, transition: "width 0.3s" }} />
        </div>
      )}

      {mc?.error && <div style={{ color: C.red, fontSize: 11, marginBottom: 10 }}>⚠ {mc.error}</div>}

      {r && (
        <>
          <div style={{
            display: "flex", alignItems: "center", gap: 12, marginBottom: 18,
            background: verdictStyle.bg, borderRadius: 10, padding: "12px 16px",
            border: `1px solid ${verdictStyle.color}44`,
          }}>
            <div style={{ fontSize: 20 }}>{r.has_edge ? "✅" : r.percentile_vs_random >= 80 ? "⚠️" : "❌"}</div>
            <div>
              <div style={{ fontSize: 12, fontWeight: 700, color: verdictStyle.color }}>{r.verdict}</div>
              <div style={{ fontSize: 10, color: C.muted, marginTop: 2 }}>
                {r.n_trades} trades · {r.n_simulations.toLocaleString()} simulaties · {r.duration_s}s
              </div>
            </div>
          </div>

          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
            <MetricTile label="Actuele PnL" value={fmtSign(r.actual_pnl)} color={r.actual_pnl >= 0 ? C.green : C.red} sub="backtest resultaat" />
            <MetricTile label="Bootstrap CI (90%)" value={`${fmtSign(r.bootstrap_pnl_p5)} – ${fmtSign(r.bootstrap_pnl_p95)}`} color={r.bootstrap_pnl_p5 >= 0 ? C.green : C.yellow} sub="bandbreedte bij hersampling" />
            <MetricTile label="Positieve kans" value={`${r.bootstrap_positive_pct}%`} color={r.bootstrap_positive_pct >= 70 ? C.green : r.bootstrap_positive_pct >= 50 ? C.yellow : C.red} sub="bootstrap samples > $0" />
            <MetricTile label="Rang vs. willekeurig" value={`${r.percentile_vs_random}e`} color={r.percentile_vs_random >= 95 ? C.green : r.percentile_vs_random >= 80 ? C.yellow : C.red} sub="percentiel vs. coin-flip" />
            <MetricTile label="Max DD (actueel)" value={`-${r.actual_max_dd}%`} color={r.actual_max_dd > 20 ? C.red : C.muted} sub={`mediaan random: -${r.bootstrap_dd_p50}%`} />
          </div>

          {r.pnl_histogram?.length > 0 && (
            <div>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 8 }}>
                PnL-verdeling willekeurige strategie — blauwe balk = actueel resultaat
              </div>
              <ResponsiveContainer width="100%" height={120}>
                <BarChart data={r.pnl_histogram} margin={{ top: 2, right: 4, left: -24, bottom: 0 }}>
                  <CartesianGrid {...GRID} />
                  <XAxis dataKey="bucket_center" {...AXIS} tickFormatter={v => `$${Math.round(v / 100) * 100}`} interval={3} />
                  <YAxis {...AXIS} />
                  <Tooltip
                    cursor={{ fill: C.card2 }}
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null;
                      const d = payload[0].payload;
                      return (
                        <ChartTip label={`~$${Math.round(d.bucket_center)}`}
                          rows={[{ value: `${d.count} simulaties${d.is_actual ? " ← actueel" : ""}`, color: d.is_actual ? C.blue : C.text }]} />
                      );
                    }}
                  />
                  <Bar dataKey="count" radius={[3, 3, 0, 0]} maxBarSize={40} animationDuration={400}>
                    {r.pnl_histogram.map((entry, i) => (
                      <Cell key={i} fill={entry.is_actual ? C.blue : C.borderLt} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
              <div style={{ fontSize: 9, color: C.dim, marginTop: 6, textAlign: "center" }}>
                Willekeurige mediaan: {fmtSign(r.random_pnl_p50)} · 95e percentiel: {fmtSign(r.random_pnl_p95)}
              </div>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// ─── Leren ────────────────────────────────────────────────────────────────────

const PARAM_META = {
  sl_atr_mult:      { label: "SL-afstand",        fmt: v => `${v}× ATR` },
  min_rr:           { label: "Minimale R:R",      fmt: v => `${v}:1` },
  min_score_global: { label: "Min. context score", fmt: v => `${v}/100` },
  setup_min_scores: { label: "Score per setup",   fmt: v => Object.entries(v || {}).map(([k, s]) => `${k}: ${s}`).join(", ") || "—" },
  disabled_setups:  { label: "Uitgeschakelde setups", fmt: v => (v && v.length) ? v.join(", ") : "geen" },
  trade_direction:  { label: "Handelsrichting",   fmt: v => ({ both: "long + short", long_only: "alleen long", short_only: "alleen short" }[v] || v) },
  trade_mode:       { label: "Handelsmodus",      fmt: v => v },
  risk_per_trade:   { label: "Risico per trade",  fmt: v => `${(v * 100).toFixed(2)}%` },
};

// Interne bookkeeping-sleutels: geen strategie-instelling, dus niet tonen.
const HIDDEN_PARAMS = new Set(["last_autotune"]);

function AutoLearnPanel() {
  const [data, setData] = useState(null);

  useEffect(() => {
    const load = () => fetch(`${API_URL}/learning/auto`).then(r => r.json()).then(setData).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  if (!data) return null;

  const obs = data.observing;
  const pct = obs ? Math.min(100, Math.round(obs.trades_done / obs.trades_needed * 100)) : 0;
  const fmtVal = v => (v == null ? "—" : String(v));

  const STATUS = {
    kept:     { label: "Behouden",      color: C.green, bg: C.greenBg },
    reverted: { label: "Teruggedraaid", color: C.red,   bg: C.redBg   },
    active:   { label: "Loopt",         color: C.blue,  bg: C.blueBg  },
  };

  return (
    <Card
      title="🤖 Automatisch leren"
      pad={18}
      style={{ marginBottom: 14 }}
      right={
        <span style={{
          fontSize: 9, fontWeight: 800, textTransform: "uppercase", letterSpacing: 0.8,
          color: data.enabled ? C.green : C.muted,
          background: data.enabled ? C.greenBg : C.card2,
          border: `1px solid ${data.enabled ? C.green + "66" : C.border}`,
          borderRadius: 99, padding: "2px 9px",
        }}>{data.enabled ? "Actief" : "Uit"}</span>
      }
    >
      <div style={{ fontSize: 11, color: C.muted, marginBottom: 14, lineHeight: 1.5 }}>
        De bot past bewezen verbeteringen zelf toe — één tegelijk — en draait ze terug als ze niet helpen.
      </div>

      {obs ? (
        <div style={{
          background: C.blueBg, border: `1px solid ${C.blue}44`, borderLeft: `3px solid ${C.blue}`,
          borderRadius: 10, padding: "12px 14px", marginBottom: 12,
        }}>
          <div style={{ fontSize: 12, fontWeight: 700, color: C.text, marginBottom: 2 }}>
            {obs.param_nl}: {fmtVal(obs.old_value)} → <span style={{ color: C.blue }}>{fmtVal(obs.new_value)}</span>
          </div>
          <div style={{ fontSize: 10, color: C.muted, marginBottom: 8 }}>
            Toegepast op {(obs.applied_at || "").slice(0, 16).replace("T", " ")} UTC ·
            nulmeting ${Number(obs.baseline_expectancy ?? 0).toFixed(2)} per trade
          </div>
          <div style={{ background: C.border, borderRadius: 99, height: 6, overflow: "hidden" }}>
            <div style={{ width: `${pct}%`, height: "100%", background: C.blue, borderRadius: 99, transition: "width 0.4s" }} />
          </div>
          <div style={{ fontSize: 10, color: C.muted, marginTop: 5 }}>
            {obs.trades_done} van {obs.trades_needed} trades geobserveerd — daarna oordeelt hij zelf
          </div>
        </div>
      ) : (
        <div style={{
          fontSize: 11, color: C.muted, background: C.card2, borderRadius: 8,
          padding: "10px 12px", marginBottom: 12, border: `1px dashed ${C.borderLt}`,
        }}>
          Geen lopend experiment — de bot wacht op de volgende backtest-tuning.
        </div>
      )}

      {data.history?.length > 0 && (
        <>
          <div style={{ fontSize: 9, fontWeight: 700, color: C.dim, textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 6 }}>
            Eerdere aanpassingen
          </div>
          {data.history.filter(h => h.status !== "active").map(h => {
            const s = STATUS[h.status] || STATUS.active;
            return (
              <div key={h.id} style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                gap: 10, flexWrap: "wrap",
                background: C.card2, border: `1px solid ${C.border}`, borderRadius: 8,
                padding: "8px 12px", marginBottom: 5,
              }}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: 11, fontWeight: 600, color: C.text }}>
                    {h.param}: {fmtVal(h.old_value)} → {fmtVal(h.new_value)}
                  </div>
                  <div style={{ fontSize: 9, color: C.muted, marginTop: 1 }}>{h.note}</div>
                </div>
                <span style={{
                  fontSize: 9, fontWeight: 800, color: s.color, background: s.bg,
                  borderRadius: 4, padding: "2px 7px", whiteSpace: "nowrap",
                }}>{s.label}</span>
              </div>
            );
          })}
        </>
      )}
    </Card>
  );
}

function ActiveSettingsPanel() {
  const [data, setData] = useState(null);

  useEffect(() => {
    const load = () => fetch(`${API_URL}/learning/params`).then(r => r.json()).then(setData).catch(() => {});
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, []);

  if (!data) return null;

  const history = (data.history || []).filter(h => !HIDDEN_PARAMS.has(h.key));
  const defaults = data.defaults || {};
  const learnedKeys = new Set(history.map(h => h.key));
  const untouched = Object.entries(defaults).filter(([k]) => !learnedKeys.has(k));

  const fmtV = (key, value) => {
    const meta = PARAM_META[key];
    try { return meta ? meta.fmt(value) : JSON.stringify(value); }
    catch { return String(value); }
  };
  const label = key => PARAM_META[key]?.label || key;

  return (
    <Card title="⚙️ Actieve instellingen" pad={18} style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 11, color: C.muted, marginBottom: 14, lineHeight: 1.5 }}>
        Wat de bot nu daadwerkelijk gebruikt. Aangepaste waarden staan bovenaan, met de datum waarop ze ingingen.
      </div>

      {history.length === 0 ? (
        <div style={{
          fontSize: 11, color: C.muted, background: C.card2, borderRadius: 8,
          padding: "10px 12px", marginBottom: 12, border: `1px dashed ${C.borderLt}`,
        }}>
          Nog geen enkele wijziging toegepast — de bot draait volledig op de standaardwaarden hieronder.
        </div>
      ) : (
        <div style={{ marginBottom: untouched.length ? 16 : 0 }}>
          {history.map((h, i) => (
            <div key={h.key} style={{
              display: "flex", alignItems: "center", justifyContent: "space-between",
              gap: 10, flexWrap: "wrap",
              background: i === 0 ? C.greenBg : C.card2,
              border: `1px solid ${i === 0 ? C.green + "55" : C.border}`,
              borderRadius: 8, padding: "10px 12px", marginBottom: 6,
            }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12, fontWeight: 700, color: C.text }}>
                  {label(h.key)}
                  {i === 0 && (
                    <span style={{ marginLeft: 8, fontSize: 8, fontWeight: 800, color: C.green, textTransform: "uppercase", letterSpacing: 0.8 }}>
                      ← laatst toegepast
                    </span>
                  )}
                </div>
                <div style={{ fontSize: 10, color: C.muted, marginTop: 2 }}>
                  Sinds {(h.updated_at || "").slice(0, 16).replace("T", " ")} UTC
                </div>
              </div>
              <div style={{ fontSize: 13, fontWeight: 800, color: C.blue, whiteSpace: "nowrap", fontFamily: MONO }}>
                {fmtV(h.key, h.value)}
              </div>
            </div>
          ))}
        </div>
      )}

      {untouched.length > 0 && (
        <>
          <div style={{ fontSize: 9, fontWeight: 700, color: C.dim, textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 6 }}>
            Standaard (nooit gewijzigd)
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {untouched.map(([k, v]) => (
              <div key={k} style={{
                background: C.card2, border: `1px solid ${C.border}`, borderRadius: 6,
                padding: "6px 10px", fontSize: 11, color: C.muted,
              }}>
                {label(k)}: <span style={{ fontWeight: 700, color: C.text, fontFamily: MONO }}>{fmtV(k, v)}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </Card>
  );
}

function LearningPanel() {
  const [data, setData] = useState(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [msg, setMsg] = useState(null);
  const [showHistory, setShowHistory] = useState(false);
  const [tune, setTune] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/learning/proposals`);
      if (res.ok) setData(await res.json());
    } catch { /**/ }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  const loadTune = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/learning/autotune`);
      if (res.ok) setTune(await res.json());
    } catch { /**/ }
  }, []);

  useEffect(() => { loadTune(); }, [loadTune]);

  useEffect(() => {
    if (!tune?.running) return;
    const t = setInterval(() => { loadTune(); load(); }, 4000);
    return () => clearInterval(t);
  }, [tune?.running, loadTune, load]);

  const runAnalysis = async () => {
    setAnalyzing(true); setMsg(null);
    try {
      const res  = await fetch(`${API_URL}/learning/analyze`, { method: "POST" });
      const json = await res.json();
      setMsg(json.message);
      load();
    } finally { setAnalyzing(false); }
  };

  const runAutotune = async () => {
    setMsg(null);
    try {
      const res  = await fetch(`${API_URL}/learning/autotune`, { method: "POST" });
      const json = await res.json();
      setMsg(json.message || json.detail);
      loadTune();
    } catch { /**/ }
  };

  const decide = async (id, action) => {
    setMsg(null);
    const res  = await fetch(`${API_URL}/learning/proposals/${id}/${action}`, { method: "POST" });
    const json = await res.json();
    setMsg(json.message);
    load();
  };

  const allowRejected = async () => {
    setMsg(null);
    const res  = await fetch(`${API_URL}/learning/proposals/allow-rejected`, { method: "POST" });
    const json = await res.json();
    setMsg(json.message);
    load();
  };

  const pending    = data?.proposals?.filter(p => p.status === "pending") ?? [];
  const decided    = data?.proposals?.filter(p => p.status !== "pending") ?? [];
  const suppressed = data?.suppressed ?? 0;

  const typeLabel = {
    min_score_global: "Min. score",
    disable_setup:    "Setup uitschakelen",
    min_score_setup:  "Setup-score",
    trade_direction:  "Richting",
    trade_mode:       "Handelsmodus",
    backtest_param:   "Backtest-tuning",
    factor_insight:   "Factor-inzicht",
  };

  const ProposalCard = ({ p }) => {
    const isPending  = p.status === "pending";
    const isAccepted = p.status === "accepted";
    const borderCol  = isPending ? C.yellow : isAccepted ? C.green : C.dim;

    return (
      <div style={{
        background: C.card2, borderRadius: 10, padding: "14px 16px", marginBottom: 10,
        border: `1px solid ${C.border}`, borderLeft: `3px solid ${borderCol}`,
      }}>
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 10, marginBottom: 6 }}>
          <div>
            <span style={{
              display: "inline-block", fontSize: 9, fontWeight: 700, textTransform: "uppercase",
              background: C.blueBg, color: C.blue, borderRadius: 4, padding: "1px 6px", marginBottom: 4,
            }}>{typeLabel[p.type] || p.type}</span>
            <div style={{ fontSize: 13, fontWeight: 700, color: C.text }}>{p.description}</div>
          </div>
          {!isPending && (
            <span style={{
              fontSize: 9, fontWeight: 800, textTransform: "uppercase",
              color: isAccepted ? C.green : C.muted,
              background: isAccepted ? C.greenBg : C.card,
              borderRadius: 4, padding: "2px 7px", whiteSpace: "nowrap",
            }}>{isAccepted ? "✓ Toegepast" : "✗ Afgewezen"}</span>
          )}
        </div>

        <div style={{ fontSize: 11, color: C.muted, lineHeight: 1.55, marginBottom: 10 }}>{p.reasoning}</div>

        <div style={{ display: "flex", gap: 16, marginBottom: isPending ? 12 : 0, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: 8, color: C.dim, textTransform: "uppercase", letterSpacing: 0.8 }}>Samples</div>
            <div style={{ fontSize: 13, fontWeight: 700, color: C.text, fontFamily: MONO }}>{p.sample_size}</div>
          </div>
          <div>
            <div style={{ fontSize: 8, color: C.dim, textTransform: "uppercase", letterSpacing: 0.8 }}>Win rate nu</div>
            <div style={{ fontSize: 13, fontWeight: 700, color: C.red, fontFamily: MONO }}>
              {p.win_rate_before != null ? `${Math.round(p.win_rate_before * 100)}%` : "—"}
            </div>
          </div>
          {p.win_rate_after != null && (
            <div>
              <div style={{ fontSize: 8, color: C.dim, textTransform: "uppercase", letterSpacing: 0.8 }}>Win rate na</div>
              <div style={{ fontSize: 13, fontWeight: 700, color: C.green, fontFamily: MONO }}>
                {Math.round(p.win_rate_after * 100)}%
              </div>
            </div>
          )}
          {p.proposed_value != null && (
            <div>
              <div style={{ fontSize: 8, color: C.dim, textTransform: "uppercase", letterSpacing: 0.8 }}>Huidig → Nieuw</div>
              <div style={{ fontSize: 12, fontWeight: 700, color: C.blue, fontFamily: MONO }}>
                {JSON.stringify(p.current_value)} → {JSON.stringify(p.proposed_value)}
              </div>
            </div>
          )}
        </div>

        {isPending && (
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={() => decide(p.id, "accept")} style={{
              flex: 1, padding: "8px 0", borderRadius: 7, border: "none",
              background: C.green, color: "#07130e", fontWeight: 700, fontSize: 12,
              cursor: "pointer", fontFamily: "inherit",
            }}>✓ Toepassen</button>
            <button onClick={() => decide(p.id, "reject")} style={{
              flex: 1, padding: "8px 0", borderRadius: 7, border: `1px solid ${C.borderLt}`,
              background: "transparent", color: C.muted, fontWeight: 700, fontSize: 12,
              cursor: "pointer", fontFamily: "inherit",
            }}>✗ Negeren</button>
          </div>
        )}
      </div>
    );
  };

  return (
    <Card
      title="🧠 Bot leerrapport"
      pad={18}
      right={
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          <button onClick={runAnalysis} disabled={analyzing} className="btn-chip" style={{
            background: analyzing ? C.card2 : C.blue, color: analyzing ? C.muted : "#fff",
          }}>{analyzing ? "Analyseren…" : "🔍 Analyseer nu"}</button>
          <button onClick={runAutotune} disabled={tune?.running} className="btn-chip" style={{
            background: tune?.running ? C.card2 : C.purple, color: tune?.running ? C.muted : "#fff",
          }}>{tune?.running ? `Tuning… ${tune.progress || 0}%` : "🔬 Backtest-tuning"}</button>
        </div>
      }
    >
      <div style={{ fontSize: 11, color: C.muted, marginBottom: 14, marginTop: -4 }}>
        Na 30+ gesloten trades analyseert de bot zichzelf en stelt aanpassingen voor.
      </div>

      {msg && (
        <div style={{
          fontSize: 11, color: C.blue, background: C.blueBg, borderRadius: 6,
          padding: "8px 12px", marginBottom: 14, border: `1px solid ${C.blue}33`,
        }}>{msg}</div>
      )}

      {tune?.running && (
        <div style={{
          fontSize: 11, color: C.purple, background: C.purpleBg, borderRadius: 6,
          padding: "8px 12px", marginBottom: 14, border: `1px solid ${C.purple}44`,
        }}>
          🔬 Backtest-tuning bezig ({tune.progress || 0}%){tune.step ? ` — ${tune.step}` : ""}.
          Voorstellen verschijnen hieronder zodra de run klaar is.
        </div>
      )}
      {!tune?.running && tune?.error && (
        <div style={{
          fontSize: 11, color: C.red, background: C.redBg, borderRadius: 6,
          padding: "8px 12px", marginBottom: 14, border: `1px solid ${C.red}33`,
        }}>⚠ Laatste tuning mislukt: {tune.error}</div>
      )}
      {!tune?.running && tune?.last_run && (
        <div style={{ fontSize: 10, color: C.dim, marginBottom: 10 }}>
          Laatste backtest-tuning: {tune.last_run.slice(0, 16).replace("T", " ")} UTC · draait automatisch elke 30 dagen
        </div>
      )}

      {suppressed > 0 && (
        <div style={{
          display: "flex", alignItems: "center", justifyContent: "space-between",
          gap: 10, flexWrap: "wrap",
          fontSize: 11, color: C.muted, background: C.card2, borderRadius: 6,
          padding: "8px 12px", marginBottom: 14, border: `1px solid ${C.border}`,
        }}>
          <span>
            🚫 {suppressed} eerder afgewezen advies{suppressed > 1 ? "en worden" : " wordt"} niet
            opnieuw voorgesteld. Een advies met een <i>andere</i> waarde komt wel gewoon langs.
          </span>
          <button onClick={allowRejected} className="btn-chip" style={{ background: "transparent", color: C.muted, border: `1px solid ${C.borderLt}` }}>
            Weer toestaan
          </button>
        </div>
      )}

      {pending.length === 0 ? (
        <div style={{
          textAlign: "center", padding: "24px 0", color: C.muted, fontSize: 12,
          background: C.card2, borderRadius: 10, border: `1px dashed ${C.borderLt}`,
        }}>
          {data === null ? "Laden…" : "Geen nieuwe voorstellen — klik 'Analyseer nu' om te starten."}
        </div>
      ) : (
        <>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.yellow, textTransform: "uppercase", letterSpacing: 0.8, marginBottom: 10 }}>
            ⚠ {pending.length} voorstel{pending.length > 1 ? "len" : ""} wacht{pending.length === 1 ? "" : "en"} op jouw beslissing
          </div>
          {pending.map(p => <ProposalCard key={p.id} p={p} />)}
        </>
      )}

      {decided.length > 0 && (
        <div>
          <button onClick={() => setShowHistory(h => !h)} style={{
            background: "none", border: "none", cursor: "pointer",
            fontSize: 11, color: C.muted, padding: "6px 0", fontFamily: "inherit",
          }}>
            {showHistory ? "▲" : "▼"} Geschiedenis ({decided.length})
          </button>
          {showHistory && decided.map(p => <ProposalCard key={p.id} p={p} />)}
        </div>
      )}
    </Card>
  );
}

// ─── Navigatie ────────────────────────────────────────────────────────────────

const NAV = [
  { id: "dashboard", label: "Dashboard", icon: "▦" },
  { id: "journaal",  label: "Journaal",  icon: "▤" },
  { id: "analyse",   label: "Analyse",   icon: "◫" },
  { id: "leren",     label: "Leren",     icon: "◆" },
  { id: "bot",       label: "Bot",       icon: "⚙" },
];

function Sidebar({ page, onSelect, counts, isRunning }) {
  return (
    <nav className="sidebar">
      <div className="brand">
        Trade<span style={{ color: C.blue }}>Bot</span>
      </div>

      <div className="nav-items">
        {NAV.map(n => (
          <button key={n.id} onClick={() => onSelect(n.id)}
            className={`nav-item${page === n.id ? " nav-on" : ""}`}>
            <span style={{ width: 16, textAlign: "center", opacity: 0.85 }}>{n.icon}</span>
            <span>{n.label}</span>
            {counts[n.id] > 0 && (
              <span className="nav-badge" style={{
                background: n.id === "leren" ? C.yellow : C.borderLt,
                color: n.id === "leren" ? "#1a1403" : C.muted,
              }}>{counts[n.id]}</span>
            )}
          </button>
        ))}
      </div>

      <div className="nav-foot">
        <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
          <span className={isRunning ? "dot pulse" : "dot"} style={{
            background: isRunning ? C.green : C.dim,
            boxShadow: isRunning ? `0 0 0 3px ${C.green}33` : "none",
          }} />
          <span style={{ fontSize: 11, color: isRunning ? C.green : C.muted, fontWeight: 600 }}>
            {isRunning ? "Live" : "Offline"}
          </span>
        </div>
        <div style={{ fontSize: 9, color: C.dim, marginTop: 6, letterSpacing: 0.6 }}>
          OKX · 15M / 1H / 4H
        </div>
      </div>
    </nav>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [stats,  setStats]  = useState(null);
  const [config, setConfig] = useState({ symbol: "BTC/USDT:USDT", risk_per_trade: 0.01 });
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState(null);

  const [page, setPage] = useState(() => {
    try { return localStorage.getItem("tradebot.page") || "dashboard"; }
    catch { return "dashboard"; }
  });
  const selectPage = (id) => {
    setPage(id);
    try { localStorage.setItem("tradebot.page", id); } catch { /* private mode */ }
  };

  const fetchAll = useCallback(async () => {
    try {
      const [sRes, stRes] = await Promise.all([
        fetch(`${API_URL}/status`),
        fetch(`${API_URL}/stats`),
      ]);
      setStatus(await sRes.json());
      setStats(await stRes.json());
      setError(null);
    } catch {
      setError("Kan bot API niet bereiken — draait hij?");
    }
  }, []);

  useEffect(() => {
    fetchAll();
    const id = setInterval(fetchAll, 5000);
    return () => clearInterval(id);
  }, [fetchAll]);

  const [pendingProposals, setPendingProposals] = useState(0);
  useEffect(() => {
    const load = () => fetch(`${API_URL}/learning/proposals`)
      .then(r => r.json())
      .then(d => setPendingProposals(d.pending || 0))
      .catch(() => {});
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, []);

  async function startBot() {
    setLoading(true);
    try {
      await fetch(`${API_URL}/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...config, timeframe: "15m" }),
      });
      await fetchAll();
    } catch { setError("Start mislukt"); }
    setLoading(false);
  }

  async function stopBot() {
    setLoading(true);
    try {
      await fetch(`${API_URL}/stop`, { method: "POST" });
      await fetchAll();
    } catch { setError("Stop mislukt"); }
    setLoading(false);
  }

  const isRunning    = status?.running;
  const allTrades    = status?.trades || [];
  const activeTrades = allTrades.filter(t => t.status !== "closed");
  const closedTrades = allTrades.filter(t => t.status === "closed");

  const days = useMemo(() => byDay(closedTrades), [closedTrades]);
  const cum  = useMemo(() => cumulativeByDay(days), [days]);
  const dd   = useMemo(() => drawdownSeries(stats?.equity_history), [stats?.equity_history]);
  const netPnl = closedTrades.reduce((s, t) => s + (t.realized_pnl || 0), 0);

  const now = new Date();
  const dateLine = `${DAYS_NL[now.getDay()]} ${now.getDate()} ${MONTHS_NL[now.getMonth()]} · ${closedTrades.length} trades geregistreerd`;

  return (
    <div className="shell">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600;700&display=swap');
        * { box-sizing: border-box; }
        body { margin: 0; background: ${C.bg}; }
        ::-webkit-scrollbar { width: 8px; height: 8px; background: ${C.bg}; }
        ::-webkit-scrollbar-thumb { background: ${C.borderLt}; border-radius: 4px; }

        .shell {
          display: flex; min-height: 100vh; background: ${C.bg}; color: ${C.text};
          font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
          font-variant-numeric: tabular-nums;
        }
        .sidebar {
          width: 196px; flex-shrink: 0; background: ${C.sidebar};
          border-right: 1px solid ${C.border}; padding: 18px 12px;
          display: flex; flex-direction: column; position: sticky; top: 0;
          height: 100vh;
        }
        .brand {
          font-size: 16px; font-weight: 800; letter-spacing: -0.3px;
          padding: 0 10px 18px; color: ${C.text};
        }
        .nav-items { display: flex; flex-direction: column; gap: 2px; }
        .nav-item {
          display: flex; align-items: center; gap: 10px; width: 100%;
          padding: 9px 10px; border-radius: 8px; border: none; cursor: pointer;
          background: transparent; color: ${C.muted};
          font-family: inherit; font-size: 12.5px; font-weight: 600; text-align: left;
          transition: background 0.14s, color 0.14s;
        }
        .nav-item:hover { background: ${C.card}; color: ${C.text}; }
        .nav-on { background: ${C.card}; color: ${C.text}; box-shadow: inset 2px 0 0 ${C.blue}; }
        .nav-badge {
          margin-left: auto; font-size: 9px; font-weight: 800;
          border-radius: 99px; padding: 1px 6px;
        }
        .nav-foot { margin-top: auto; padding: 12px 10px 0; border-top: 1px solid ${C.border}; }
        .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

        .content { flex: 1; min-width: 0; padding: 20px 24px 40px; max-width: 1560px; }

        .topbar {
          display: flex; align-items: flex-end; justify-content: space-between;
          gap: 14px; flex-wrap: wrap; margin-bottom: 16px;
        }
        .kpi-row {
          display: grid; grid-template-columns: repeat(5, 1fr);
          gap: 10px; margin-bottom: 12px;
        }
        .grid-3 { display: grid; grid-template-columns: 290px 1fr 1fr; gap: 12px; margin-bottom: 12px; }
        .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
        .grid-cal { display: grid; grid-template-columns: 1fr 440px; gap: 12px; margin-bottom: 12px; }

        .btn-primary {
          background: ${C.blue}; color: #fff; border: none; border-radius: 8px;
          padding: 10px 20px; font-family: inherit; font-size: 11px; font-weight: 700;
          letter-spacing: 0.5px; cursor: pointer; text-transform: uppercase;
          transition: opacity 0.15s; width: 100%;
        }
        .btn-primary:hover:not(:disabled) { opacity: 0.88; }
        .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-danger {
          background: ${C.redBg}; color: ${C.red}; border: 1px solid ${C.red}55;
          border-radius: 8px; padding: 10px 20px; font-family: inherit; font-size: 11px;
          font-weight: 700; letter-spacing: 0.5px; cursor: pointer; text-transform: uppercase;
          width: 100%;
        }
        .btn-danger:hover:not(:disabled) { background: ${C.red}28; }
        .btn-chip {
          padding: 6px 12px; border-radius: 7px; border: 1px solid transparent;
          font-family: inherit; font-weight: 700; font-size: 11px; cursor: pointer;
        }
        .btn-chip:disabled { cursor: not-allowed; }
        .icon-btn {
          background: ${C.card2}; border: 1px solid ${C.border}; color: ${C.muted};
          width: 24px; height: 24px; border-radius: 6px; cursor: pointer;
          font-size: 14px; line-height: 1; font-family: inherit;
        }
        .icon-btn:hover { color: ${C.text}; border-color: ${C.borderLt}; }

        .seg { display: flex; gap: 2px; background: ${C.card2}; padding: 2px; border-radius: 7px; }
        .seg button {
          border: none; background: transparent; color: ${C.muted};
          font-family: inherit; font-size: 11px; font-weight: 700;
          padding: 5px 11px; border-radius: 5px; cursor: pointer;
        }
        .seg .seg-on { background: ${C.blue}; color: #fff; }

        select, input[type="number"], input[type="text"] {
          background: ${C.card2}; border: 1px solid ${C.border}; color: ${C.text};
          font-family: inherit; font-size: 12px; padding: 8px 11px;
          border-radius: 8px; outline: none; width: 100%;
        }
        select:focus, input:focus { border-color: ${C.blue}; }
        option { background: ${C.card2}; color: ${C.text}; }

        .closed-row:hover { background: ${C.card2}; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.35} }
        .pulse { animation: pulse 2s infinite; }

        @media (max-width: 1240px) {
          .kpi-row { grid-template-columns: repeat(3, 1fr); }
          .grid-3  { grid-template-columns: 1fr 1fr; }
          .grid-cal { grid-template-columns: 1fr; }
        }
        @media (max-width: 860px) {
          .shell { flex-direction: column; }
          /* Grid in plaats van flex: alleen de middelste kolom mag scrollen.
             Als flexrij duwde de navigatie de hele pagina breder dan het scherm. */
          .sidebar {
            width: 100%; height: auto; position: static;
            display: grid; grid-template-columns: auto minmax(0,1fr) auto;
            align-items: center; gap: 10px; padding: 10px 12px;
            border-right: none; border-bottom: 1px solid ${C.border};
          }
          .brand { padding: 0 8px 0 0; font-size: 14px; }
          .nav-items { flex-direction: row; gap: 4px; overflow-x: auto; min-width: 0; padding-bottom: 2px; }
          .nav-item { padding: 7px 11px; font-size: 12px; white-space: nowrap; flex-shrink: 0; }
          .nav-on { box-shadow: inset 0 -2px 0 ${C.blue}; }
          .nav-foot { margin-top: 0; border-top: none; padding: 0; }
          .nav-foot div:last-child { display: none; }
          .content { padding: 14px 12px 32px; }
          .kpi-row { grid-template-columns: repeat(2, 1fr); }
          .grid-3, .grid-2 { grid-template-columns: 1fr; }
          .price-grid-6 { grid-template-columns: repeat(3,1fr) !important; }
          .closed-table-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
        }
        @media (max-width: 520px) {
          .kpi-row { grid-template-columns: 1fr; }
          /* Twee setup-kaarten naast elkaar worden op deze breedte onleesbaar smal. */
          .setup-grid { grid-template-columns: 1fr !important; }
        }
      `}</style>

      <Sidebar
        page={page} onSelect={selectPage} isRunning={isRunning}
        counts={{ journaal: 0, leren: pendingProposals }}
      />

      <main className="content">
        <CircuitBreakerBanner status={status} />

        <div className="topbar">
          <div>
            <div style={{ fontSize: 19, fontWeight: 800, letterSpacing: -0.3 }}>
              {NAV.find(n => n.id === page)?.label}
            </div>
            <div style={{ fontSize: 11.5, color: C.muted, marginTop: 3 }}>{dateLine}</div>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
            {status?.daily_loss_pct < -1 && (
              <Tag color={C.yellow} bg={C.yellowBg}>DAG {fmt(status.daily_loss_pct, 1)}%</Tag>
            )}
            {status?.trade_mode && (
              <Tag color={status.trade_mode === "both" ? C.green : C.blue} bg={status.trade_mode === "both" ? C.greenBg : C.blueBg}>
                {status.trade_mode === "both" ? "DAYTRADE + SCALP" : status.trade_mode.toUpperCase()}
              </Tag>
            )}
            <div style={{ textAlign: "right" }}>
              <div style={{ fontSize: 9.5, color: C.muted, letterSpacing: 1, textTransform: "uppercase", fontWeight: 700 }}>
                Equity
              </div>
              <div style={{ fontFamily: MONO, fontSize: 17, fontWeight: 700, color: C.text }}>
                {status ? money(status.equity, 2) : "—"}
              </div>
            </div>
          </div>
        </div>

        {error && (
          <div style={{
            background: C.redBg, border: `1px solid ${C.red}44`, borderRadius: 10,
            padding: "10px 16px", marginBottom: 14, color: C.red, fontSize: 11, fontWeight: 600,
          }}>⚠ {error}</div>
        )}

        {/* ── Dashboard ──────────────────────────────────────────────────────── */}
        {page === "dashboard" && (
          <>
            {activeTrades.length > 0 && (
              <div style={{ marginBottom: 12 }}>
                <SectionLabel badge={activeTrades.length}>Actieve trades</SectionLabel>
                {activeTrades.map((t, i) => <ActiveTradeCard key={t.id || i} trade={t} />)}
              </div>
            )}

            <HeroKpis closed={closedTrades} days={days} status={status} />

            <div className="grid-3">
              <EdgeScoreCard closed={closedTrades} maxDd={stats?.max_drawdown_pct} />

              <Card title="Cumulatieve P&L"
                right={<span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 700, color: netPnl >= 0 ? C.green : C.red }}>{fmtSign(netPnl)}</span>}>
                {cum.length > 1
                  ? <AreaSeries data={cum} xKey="date" yKey="cum" color={netPnl >= 0 ? C.green : C.red}
                      zeroRef={0} tip={moneyTip(l => fmtDate(l))} fmtY={v => `$${v}`} height={176} />
                  : <EmptyState icon="◠" text="Nog te weinig handelsdagen" sub="Verschijnt vanaf de tweede dag" height={176} />}
              </Card>

              <Card title="Netto P&L per dag"
                right={<span style={{ fontSize: 10, color: C.dim }}>{days.length} dagen</span>}>
                <DailyNetBars days={days} height={176} />
              </Card>
            </div>

            <div className="grid-cal">
              <RecentTradesCard closed={closedTrades} />
              <CalendarCard days={days} />
            </div>

            <div className="grid-2">
              <Card title="Saldo"
                right={<span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 700, color: C.text }}>{status ? money(status.balance, 2) : "—"}</span>}>
                {stats?.equity_history?.length > 1
                  ? <AreaSeries data={stats.equity_history} xKey="ts" yKey="equity" color={C.blue}
                      zeroRef={stats.equity_history[0]?.equity} tip={absMoneyTip} fmtY={v => `$${(v / 1000).toFixed(1)}k`} />
                  : <EmptyState icon="◠" text="Nog geen equity-historie" height={150} />}
              </Card>
              <Card title="Drawdown"
                right={<span style={{ fontFamily: MONO, fontSize: 12, fontWeight: 700, color: stats?.max_drawdown_pct > 10 ? C.red : C.muted }}>
                  {stats?.max_drawdown_pct != null ? `max −${stats.max_drawdown_pct}%` : "—"}
                </span>}>
                <DrawdownChart series={dd} />
              </Card>
            </div>

            <div className="grid-2">
              <Card title="Setup-statistieken">
                <SetupStatsGrid stats={stats?.setup_stats} />
              </Card>
              <AnalysisPanel lastAnalysis={status?.last_analysis} />
            </div>
          </>
        )}

        {/* ── Journaal ───────────────────────────────────────────────────────── */}
        {page === "journaal" && <JournalPage closed={closedTrades} />}

        {/* ── Analyse ────────────────────────────────────────────────────────── */}
        {page === "analyse" && (
          <>
            <div style={{ marginBottom: 14 }}>
              <LiveCandleChart trades={allTrades} />
            </div>
            <BacktestPanel />
            <MonteCarloPanel />
          </>
        )}

        {/* ── Leren ──────────────────────────────────────────────────────────── */}
        {page === "leren" && (
          <>
            <AutoLearnPanel />
            <ActiveSettingsPanel />
            <LearningPanel />
          </>
        )}

        {/* ── Bot ────────────────────────────────────────────────────────────── */}
        {page === "bot" && (
          <div className="grid-2">
            <ConfigPanel
              config={config} setConfig={setConfig}
              isRunning={isRunning} loading={loading}
              onStart={startBot} onStop={stopBot}
            />
            <SetupHealthPanel setupHealth={status?.setup_health} />
          </div>
        )}

        <div style={{ marginTop: 26, textAlign: "center", color: C.dim, fontSize: 9, letterSpacing: 1.4 }}>
          VERVERST ELKE 5S · {new Date().toLocaleTimeString("nl-NL")} · USE AT YOUR OWN RISK
        </div>
      </main>
    </div>
  );
}
