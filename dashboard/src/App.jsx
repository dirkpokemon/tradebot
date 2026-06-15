import { useState, useEffect, useCallback } from "react";
import LiveCandleChart from "./LiveCandleChart";
import SnapshotChart from "./SnapshotChart";
import {
  AreaChart, Area,
  BarChart, Bar,
  XAxis, YAxis, CartesianGrid,
  ResponsiveContainer, Cell, ReferenceLine, Tooltip,
} from "recharts";

const API_URL = import.meta.env.VITE_API_URL || "";
// Perpetual futures symbolen (long én short) — spot verwijderd
const SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"];

// ─── Theme ────────────────────────────────────────────────────────────────────

const C = {
  bg:        "#f0f2f7",
  card:      "#ffffff",
  border:    "#e2e5ef",
  text:      "#1a1d2e",
  muted:     "#8b92a5",
  dim:       "#c5c9d6",
  green:     "#00b37e",
  greenBg:   "#e8f7f2",
  greenLine: "#00c896",
  red:       "#e63946",
  redBg:     "#fdeaeb",
  blue:      "#3b5bdb",
  blueBg:    "#eef1fd",
  yellow:    "#f59f00",
  yellowBg:  "#fff8e6",
  orange:    "#e8590c",
  shadow:    "0 1px 4px rgba(30,40,80,0.07), 0 1px 2px rgba(30,40,80,0.04)",
  shadowMd:  "0 4px 12px rgba(30,40,80,0.09)",
};

const PHASES = {
  open:      { label: "OPEN",    color: C.green,  bg: C.greenBg, pct: 100 },
  partial_1: { label: "TP1 ✓",  color: C.yellow, bg: C.yellowBg, pct: 75 },
  partial_2: { label: "TP2 ✓",  color: C.orange, bg: "#fff3ec",  pct: 50 },
  partial_3: { label: "RUNNER", color: C.red,     bg: C.redBg,   pct: 25 },
  closed:    { label: "CLOSED", color: C.muted,   bg: "#f5f6fa",  pct: 0  },
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
const fmtSign = (n, d = 2) => n == null ? "—" : `${n >= 0 ? "+" : ""}$${fmt(n, d)}`;
const getPhase = (t) => PHASES[t.status] || PHASES.open;

function getSession() {
  const h = new Date().getUTCHours();
  if (h >= 8  && h < 12) return { name: "London",   color: C.blue,   active: true  };
  if (h >= 13 && h < 17) return { name: "New York",  color: C.green,  active: true  };
  return                         { name: "Off-hours", color: C.muted,  active: false };
}

// ─── Atoms ────────────────────────────────────────────────────────────────────

function PnlBadge({ value, size = 13 }) {
  if (value == null) return <span style={{ color: C.muted }}>—</span>;
  const pos = value >= 0;
  return (
    <span style={{ color: pos ? C.green : C.red, fontWeight: 700, fontSize: size }}>
      {pos ? "+" : ""}{fmt(value)} USDT
    </span>
  );
}

function Tag({ children, color, bg }) {
  return (
    <span style={{
      fontSize: 9, padding: "2px 8px", borderRadius: 99, fontWeight: 700,
      letterSpacing: 0.5, textTransform: "uppercase",
      color: color || C.muted, background: bg || C.border,
    }}>
      {children}
    </span>
  );
}

function SectionLabel({ children, badge }) {
  return (
    <div style={{
      fontSize: 10, letterSpacing: 1.5, color: C.muted, marginBottom: 14,
      textTransform: "uppercase", fontWeight: 600,
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
      justifyContent: "center", height, gap: 6, color: C.dim,
    }}>
      <div style={{ fontSize: 24, opacity: 0.35 }}>{icon}</div>
      <div style={{ fontSize: 12, color: C.muted }}>{text}</div>
      {sub && <div style={{ fontSize: 10, color: C.dim }}>{sub}</div>}
    </div>
  );
}

function StatCard({ label, value, sub, accent, small }) {
  return (
    <div style={{
      background: C.card, borderRadius: 12, padding: small ? "12px 16px" : "16px 20px",
      boxShadow: C.shadow, flex: 1, minWidth: 100,
      borderLeft: accent ? `3px solid ${accent}` : "3px solid transparent",
    }}>
      <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1.5, textTransform: "uppercase", marginBottom: 6 }}>{label}</div>
      <div style={{ fontSize: small ? 16 : 20, fontWeight: 700, color: accent || C.text, lineHeight: 1 }}>{value}</div>
      {sub && <div style={{ fontSize: 10, color: C.dim, marginTop: 4 }}>{sub}</div>}
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
      background: C.redBg, border: `1px solid ${C.red}33`,
      borderRadius: 12, padding: "14px 20px", marginBottom: 20,
      display: "flex", alignItems: "center", gap: 14,
    }}>
      <span style={{ fontSize: 20 }}>🚨</span>
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

// ─── Session Indicator ────────────────────────────────────────────────────────

function SessionIndicator() {
  const [sess, setSess] = useState(getSession());
  useEffect(() => {
    const id = setInterval(() => setSess(getSession()), 30000);
    return () => clearInterval(id);
  }, []);
  return (
    <div style={{
      display: "flex", alignItems: "center", gap: 6,
      background: sess.active ? C.greenBg : "#f5f6fa",
      border: `1px solid ${sess.active ? C.green + "44" : C.border}`,
      borderRadius: 99, padding: "4px 12px",
    }}>
      <div style={{
        width: 7, height: 7, borderRadius: "50%",
        background: sess.active ? C.green : C.dim,
        boxShadow: sess.active ? `0 0 0 3px ${C.green}33` : "none",
      }} />
      <span style={{ fontSize: 10, color: sess.active ? C.green : C.muted, fontWeight: 600, letterSpacing: 0.5 }}>
        {sess.name}
      </span>
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
        <span style={{ fontSize: small ? 10 : 12, fontWeight: 700, color }}>{value}%</span>
      </div>
      <div style={{ height: 5, background: C.border, borderRadius: 99, overflow: "hidden" }}>
        <div style={{
          height: "100%", width: `${value}%`,
          background: color, borderRadius: 99,
          transition: "width 0.4s ease",
        }} />
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
    { from: trade.stop_loss,   to: trade.entry_price, color: C.red + "20"   },
    { from: trade.entry_price, to: trade.tp1,          color: C.green + "20" },
    { from: trade.tp1,         to: trade.tp2,          color: C.green + "35" },
    { from: trade.tp2,         to: trade.tp3,          color: C.green + "50" },
  ] : [
    { from: trade.entry_price, to: trade.stop_loss,   color: C.red + "20"   },
    { from: trade.tp1,         to: trade.entry_price, color: C.green + "20" },
    { from: trade.tp2,         to: trade.tp1,         color: C.green + "35" },
    { from: trade.tp3,         to: trade.tp2,         color: C.green + "50" },
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
          <div style={{ fontSize: 8, color: m.hit ? m.color : C.dim, whiteSpace: "nowrap", marginBottom: 1, fontWeight: 600 }}>
            {m.label}
          </div>
          <div style={{ width: 1, height: 9, background: m.hit ? m.color : C.border }} />
          <div style={{
            width: 7, height: 7, borderRadius: "50%",
            background: m.hit ? m.color + "22" : "#f5f6fa",
            border: `2px solid ${m.hit ? m.color : C.border}`,
          }} />
          <div style={{ fontSize: 8, color: C.muted, whiteSpace: "nowrap", marginTop: 3 }}>
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
          fontSize: 9, padding: "2px 8px", borderRadius: 99, fontWeight: 700,
          background: trade[tp.key] ? C.greenBg : C.border,
          color: trade[tp.key] ? C.green : C.muted,
        }}>
          {trade[tp.key] ? "✓ " : ""}{tp.label}
        </span>
      ))}
      <span style={{
        fontSize: 9, padding: "2px 8px", borderRadius: 99, fontWeight: 700,
        background: trade.status === "partial_3" ? C.redBg : C.border,
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
      background: C.card, borderRadius: 14, padding: "18px 20px", marginBottom: 12,
      boxShadow: C.shadowMd,
      borderLeft: `4px solid ${isLong ? C.green : C.red}`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span style={{ fontSize: 13, fontWeight: 800, color: isLong ? C.green : C.red }}>
            {isLong ? "▲ LONG" : "▼ SHORT"}
          </span>
          <span style={{ fontSize: 11, color: C.muted }}>{trade.symbol}</span>
          <Tag color={C.blue} bg={C.blueBg}>{trade.setup_type.replace("_", " ")}</Tag>
          {trade.trade_mode && (
            <Tag color={trade.trade_mode === "scalp" ? C.orange : C.muted} bg={trade.trade_mode === "scalp" ? "#fff3ec" : "#f5f6fa"}>
              {trade.trade_mode.toUpperCase()}
            </Tag>
          )}
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Tag color={phase.color} bg={phase.bg}>{phase.label}</Tag>
          <span style={{ fontSize: 10, color: C.muted }}>{phase.pct}% open</span>
        </div>
      </div>

      {/* Price grid */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(6,1fr)", gap: 8, marginBottom: 4 }}>
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
            <div style={{ fontSize: 12, color: item.color, fontWeight: 700 }}>{item.value}</div>
          </div>
        ))}
      </div>

      <PriceLadder trade={trade} />

      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 10 }}>
        <TpProgress trade={trade} />
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, marginBottom: 2 }}>REALIZED PNL</div>
          <PnlBadge value={trade.realized_pnl} size={14} />
        </div>
      </div>

      {trade.reason && (
        <div style={{ marginTop: 8, fontSize: 10, color: C.dim, fontStyle: "italic" }}>{trade.reason}</div>
      )}

      {trade.context_score > 0 && (
        <div style={{ marginTop: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 3 }}>
            <span style={{ fontSize: 9, color: C.muted, letterSpacing: 1 }}>CONTEXT SCORE</span>
            <span style={{ fontSize: 10, fontWeight: 700, color: trade.context_score >= 70 ? C.green : C.yellow }}>{trade.context_score}/100</span>
          </div>
          <div style={{ height: 4, background: C.border, borderRadius: 99, overflow: "hidden" }}>
            <div style={{
              height: "100%", borderRadius: 99,
              width: `${trade.context_score}%`,
              background: trade.context_score >= 70 ? C.green : C.yellow,
            }} />
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Chart Tooltips ───────────────────────────────────────────────────────────

function EquityTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, padding: "8px 12px", boxShadow: C.shadow, fontSize: 11 }}>
      <div style={{ color: C.muted, fontSize: 9, marginBottom: 3 }}>{label}</div>
      <div style={{ color: C.green, fontWeight: 700 }}>${fmt(payload[0].value, 0)}</div>
    </div>
  );
}

function PnlTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  const v = payload[0].value;
  return (
    <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, padding: "8px 12px", boxShadow: C.shadow, fontSize: 11 }}>
      <div style={{ color: C.muted, fontSize: 9, marginBottom: 3 }}>{label}</div>
      <div style={{ color: v >= 0 ? C.green : C.red, fontWeight: 700 }}>{fmtSign(v)}</div>
    </div>
  );
}

// ─── Equity Curve ─────────────────────────────────────────────────────────────

function EquityCurve({ history }) {
  if (!history?.length) {
    return <EmptyState icon="📈" text="Nog geen equity data" sub="Verschijnt na de eerste gesloten trade" />;
  }
  const isPos = history[history.length - 1]?.equity >= history[0]?.equity;
  const color = isPos ? C.greenLine : C.red;
  return (
    <ResponsiveContainer width="100%" height={160}>
      <AreaChart data={history} margin={{ top: 4, right: 6, left: -28, bottom: 0 }}>
        <defs>
          <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor={color} stopOpacity={0.18} />
            <stop offset="95%" stopColor={color} stopOpacity={0}    />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
        <XAxis dataKey="ts" stroke="transparent" tick={{ fill: C.dim, fontSize: 9 }} tickLine={false} interval="preserveStartEnd" />
        <YAxis stroke="transparent" tick={{ fill: C.dim, fontSize: 9 }} tickLine={false} axisLine={false}
          tickFormatter={v => `$${(v / 1000).toFixed(1)}k`} />
        <Tooltip content={<EquityTooltip />} />
        <ReferenceLine y={history[0]?.equity} stroke={C.border} strokeDasharray="4 4" />
        <Area type="monotone" dataKey="equity" stroke={color} strokeWidth={2}
          fill="url(#equityGrad)" dot={false} animationDuration={400} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ─── Daily PnL Chart ──────────────────────────────────────────────────────────

function DailyPnlChart({ data }) {
  if (!data?.length) {
    return <EmptyState icon="📊" text="Nog geen dagdata" sub="Verschijnt na de eerste handelsdag" height={130} />;
  }
  return (
    <ResponsiveContainer width="100%" height={130}>
      <BarChart data={data} margin={{ top: 4, right: 6, left: -28, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
        <XAxis dataKey="date" stroke="transparent" tick={{ fill: C.dim, fontSize: 9 }} tickLine={false} />
        <YAxis stroke="transparent" tick={{ fill: C.dim, fontSize: 9 }} tickLine={false} axisLine={false}
          tickFormatter={v => `$${v}`} />
        <Tooltip content={<PnlTooltip />} />
        <ReferenceLine y={0} stroke={C.border} />
        <Bar dataKey="pnl" radius={[3, 3, 0, 0]} maxBarSize={44} animationDuration={400}>
          {data.map((entry, i) => (
            <Cell key={i} fill={entry.pnl >= 0 ? C.green : C.red} fillOpacity={0.8} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

// ─── Setup Health Panel (sidebar) ────────────────────────────────────────────

function SetupHealthPanel({ setupHealth }) {
  return (
    <div style={{ background: C.card, borderRadius: 14, padding: 18, boxShadow: C.shadow }}>
      <SectionLabel>Setups</SectionLabel>
      <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
        {SETUP_META.map(s => {
          const h = setupHealth?.[s.key];
          const health = HEALTH[h?.status || 'healthy'];
          const isDisabled = h?.status === 'disabled';
          return (
            <div key={s.key} style={{ opacity: isDisabled ? 0.55 : 1 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                <div>
                  <span style={{ fontSize: 11, fontWeight: 700, color: isDisabled ? C.red : C.text }}>
                    {s.label}
                  </span>
                  <span style={{ fontSize: 9, color: C.muted, marginLeft: 6 }}>{s.desc}</span>
                </div>
                <Tag color={health.color} bg={health.bg}>{health.label}</Tag>
              </div>
              <WinRateBar value={h?.win_rate != null ? Math.round(h.win_rate * 100) : null} small />
              {h?.trades > 0 && (
                <div style={{ fontSize: 9, color: C.dim, marginTop: 3 }}>
                  {h.trades} recente trades
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Analyse Panel: wat ziet de bot, en waarom (nog) geen trade ──────────────

function TrendBadge({ value }) {
  if (!value) return <span style={{ color: C.muted, fontSize: 11 }}>—</span>;
  const map = {
    uptrend:   { label: "↑ Stijgend",  color: C.green  },
    downtrend: { label: "↓ Dalend",    color: C.red    },
    ranging:   { label: "↔ Zijwaarts", color: C.muted  },
    onbekend:  { label: "?",            color: C.dim    },
  };
  const t = map[value] || { label: value, color: C.muted };
  return (
    <span style={{
      fontSize: 11, fontWeight: 700, color: t.color,
      background: t.color + "18", borderRadius: 6, padding: "1px 8px",
    }}>{t.label}</span>
  );
}

function AnalysisMode({ a, mode }) {
  if (!a) return null;
  const isSignal   = (a.result || "").startsWith("SIGNAAL");
  const isCooldown = (a.result || "").includes("cooldown");
  const ts = a.ts ? a.ts.replace("T", " ").replace("Z", " UTC") : null;

  // Resultaatkleur en icoon
  let resultColor = C.text, resultIcon = "○";
  if (isSignal)   { resultColor = C.green;  resultIcon = "●"; }
  if (isCooldown) { resultColor = C.yellow; resultIcon = "⏸"; }

  // Score uit breakdown als beschikbaar
  const bd = a.score_breakdown;

  return (
    <div style={{
      border: `1px solid ${isSignal ? C.green + "44" : C.border}`,
      borderRadius: 10, padding: "12px 14px",
      background: isSignal ? C.greenBg : "#fafbfd",
    }}>
      {/* Header: mode + timestamp */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
        <span style={{ fontSize: 10, fontWeight: 700, letterSpacing: 1.2, textTransform: "uppercase", color: C.muted }}>
          {mode}
        </span>
        {ts && <span style={{ fontSize: 9, color: C.dim }}>{ts}</span>}
      </div>

      {/* Marktstructuur */}
      {a.structuur && (
        <div style={{ display: "grid", gridTemplateColumns: "40px 1fr", gap: "4px 8px", marginBottom: 10, alignItems: "center" }}>
          <span style={{ fontSize: 9, color: C.muted, letterSpacing: 0.8, textTransform: "uppercase" }}>4H</span>
          <TrendBadge value={a.structuur["4h"]} />
          <span style={{ fontSize: 9, color: C.muted, letterSpacing: 0.8, textTransform: "uppercase" }}>1H</span>
          <TrendBadge value={a.structuur.context} />
          <span style={{ fontSize: 9, color: C.muted, letterSpacing: 0.8, textTransform: "uppercase" }}>15M</span>
          <TrendBadge value={a.structuur.entry} />
        </div>
      )}

      {/* Resultaat */}
      <div style={{
        display: "flex", alignItems: "flex-start", gap: 6,
        padding: "8px 10px", borderRadius: 8,
        background: isSignal ? C.green + "22" : C.border + "55",
        marginBottom: (a.notes?.length || bd) ? 8 : 0,
      }}>
        <span style={{ fontSize: 14, lineHeight: 1, color: resultColor, flexShrink: 0 }}>{resultIcon}</span>
        <span style={{ fontSize: 11, color: resultColor, fontWeight: isSignal ? 700 : 400, lineHeight: 1.4 }}>
          {a.result || "—"}
        </span>
      </div>

      {/* Score breakdown als een signal bijna haalde */}
      {bd && !isSignal && (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 4 }}>
            Score breakdown
          </div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "3px 8px" }}>
            {Object.entries(bd).map(([k, v]) => (
              <span key={k} style={{ fontSize: 9, color: v > 0 ? C.green : C.dim }}>
                {k}: {v > 0 ? "+" : ""}{v}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Diagnostische notes: WAAROM geen setup */}
      {(a.notes || []).length > 0 && (
        <div style={{ borderTop: `1px solid ${C.border}`, paddingTop: 6 }}>
          <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 4 }}>
            Wat de bot zag
          </div>
          {(a.notes || []).map((n, i) => (
            <div key={i} style={{
              fontSize: 10, color: C.text, marginBottom: 3, lineHeight: 1.4,
              paddingLeft: 8, borderLeft: `2px solid ${C.border}`,
            }}>
              {n}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AnalysisPanel({ lastAnalysis }) {
  const modes = ["daytrade", "scalp"];
  const hasData = modes.some(m => lastAnalysis?.[m]);
  return (
    <div style={{ background: C.card, borderRadius: 14, padding: 18, boxShadow: C.shadow, marginTop: 16 }}>
      <SectionLabel>Wat doet de bot?</SectionLabel>
      {!hasData ? (
        <div style={{ fontSize: 11, color: C.muted, padding: "12px 0" }}>
          Wacht op de eerste candle — start de bot om analyse te zien.
        </div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {modes.map(m => <AnalysisMode key={m} a={lastAnalysis?.[m]} mode={m} />)}
        </div>
      )}
    </div>
  );
}

// ─── Config Panel ─────────────────────────────────────────────────────────────

function ConfigPanel({ config, setConfig, isRunning, loading, onStart, onStop }) {
  return (
    <div style={{ background: C.card, borderRadius: 14, padding: 18, boxShadow: C.shadow, marginBottom: 16 }}>
      <SectionLabel>Configuratie</SectionLabel>
      <label style={{ display: "block", marginBottom: 14 }}>
        <div style={{ fontSize: 9, color: C.muted, marginBottom: 5, letterSpacing: 1, textTransform: "uppercase" }}>Symbol</div>
        <select value={config.symbol} onChange={e => setConfig({ ...config, symbol: e.target.value })} disabled={isRunning}>
          {SYMBOLS.map(s => <option key={s}>{s}</option>)}
        </select>
      </label>
      <label style={{ display: "block", marginBottom: 20 }}>
        <div style={{ fontSize: 9, color: C.muted, marginBottom: 5, letterSpacing: 1, textTransform: "uppercase" }}>Risico per trade</div>
        <input
          type="number" min="0.001" max="0.05" step="0.001"
          value={config.risk_per_trade}
          onChange={e => setConfig({ ...config, risk_per_trade: parseFloat(e.target.value) })}
          disabled={isRunning}
        />
        <div style={{ fontSize: 10, color: C.muted, marginTop: 4 }}>
          {(config.risk_per_trade * 100).toFixed(1)}% per trade
        </div>
      </label>
      {!isRunning
        ? <button className="btn-primary" onClick={onStart} disabled={loading}>{loading ? "Starten…" : "▶  Start Bot"}</button>
        : <button className="btn-danger"  onClick={onStop}  disabled={loading}>{loading ? "Stoppen…" : "■  Stop Bot"}</button>
      }
    </div>
  );
}

// ─── Closed Trades Table ──────────────────────────────────────────────────────

function ClosedTradesTable({ trades }) {
  if (!trades.length) {
    return (
      <div style={{ background: C.card, borderRadius: 14, padding: "32px 0", textAlign: "center", boxShadow: C.shadow }}>
        <EmptyState icon="📋" text="Nog geen gesloten trades" height={80} />
      </div>
    );
  }
  return (
    <div style={{ background: C.card, borderRadius: 14, boxShadow: C.shadow, overflow: "hidden" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
        <thead>
          <tr style={{ borderBottom: `1px solid ${C.border}`, background: "#fafbfd" }}>
            {["Tijd", "Setup", "Mode", "Side", "Entry", "Exit", "TP's", "PnL"].map(h => (
              <th key={h} style={{ padding: "10px 16px", textAlign: "left", color: C.muted, fontWeight: 600, fontSize: 9, letterSpacing: 1, textTransform: "uppercase" }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {[...trades].reverse().map((t, i) => (
            <tr key={t.id || i} className="closed-row" style={{ borderBottom: `1px solid ${C.border}` }}>
              <td style={{ padding: "10px 16px", color: C.muted }}>{t.timestamp?.slice(11, 16)}</td>
              <td style={{ padding: "10px 16px" }}>
                <Tag color={C.blue} bg={C.blueBg}>{t.setup_type?.replace("_", " ")}</Tag>
              </td>
              <td style={{ padding: "10px 16px" }}>
                <Tag color={t.trade_mode === "scalp" ? C.orange : C.muted} bg={t.trade_mode === "scalp" ? "#fff3ec" : "#f5f6fa"}>
                  {(t.trade_mode || "daytrade").toUpperCase()}
                </Tag>
              </td>
              <td style={{ padding: "10px 16px" }}>
                <span style={{ color: t.side === "buy" ? C.green : C.red, fontWeight: 700, textTransform: "uppercase", fontSize: 11 }}>
                  {t.side === "buy" ? "▲" : "▼"} {t.side}
                </span>
              </td>
              <td style={{ padding: "10px 16px", fontWeight: 600 }}>{fmtP(t.entry_price)}</td>
              <td style={{ padding: "10px 16px", color: C.muted }}>{fmtP(t.exit_price)}</td>
              <td style={{ padding: "10px 16px" }}>
                <div style={{ display: "flex", gap: 3 }}>
                  {["tp1_hit", "tp2_hit", "tp3_hit"].map((k, idx) => (
                    <span key={k} style={{
                      fontSize: 9, padding: "1px 6px", borderRadius: 99, fontWeight: 700,
                      background: t[k] ? C.greenBg : C.border,
                      color:      t[k] ? C.green   : C.dim,
                    }}>TP{idx + 1}</span>
                  ))}
                </div>
              </td>
              <td style={{ padding: "10px 16px" }}><PnlBadge value={t.realized_pnl} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Setup Stats Grid (analytics) ────────────────────────────────────────────

function SetupStatsGrid({ stats }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
      {SETUP_META.map(s => {
        const d = stats?.[s.key];
        const health = HEALTH[d?.health || 'healthy'];
        const isDisabled = d?.health === 'disabled';
        return (
          <div key={s.key} style={{
            background: "#fafbfd", border: `1px solid ${C.border}`,
            borderLeft: `3px solid ${health.color}`,
            borderRadius: 10, padding: "12px 14px",
            opacity: isDisabled ? 0.6 : 1,
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
              <div>
                <div style={{ fontSize: 10, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5 }}>{s.label}</div>
                <div style={{ fontSize: 9, color: C.dim, marginTop: 2 }}>{s.desc}</div>
              </div>
              <Tag color={health.color} bg={health.bg}>{health.label}</Tag>
            </div>
            <WinRateBar value={d?.win_rate ?? null} />
            {d?.count > 0 && (
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 10 }}>
                <div>
                  <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1 }}>TRADES</div>
                  <div style={{ fontSize: 11, fontWeight: 700, color: C.text, marginTop: 2 }}>{d.count}</div>
                </div>
                <div>
                  <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1 }}>GEM. PNL</div>
                  <div style={{ fontSize: 11, fontWeight: 700, color: d.avg_pnl >= 0 ? C.green : C.red, marginTop: 2 }}>
                    {fmtSign(d.avg_pnl)}
                  </div>
                </div>
                <div>
                  <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1 }}>WINST</div>
                  <div style={{ fontSize: 11, fontWeight: 700, color: C.text, marginTop: 2 }}>{d.wins}/{d.count}</div>
                </div>
                <div>
                  <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1 }}>PROF. FACTOR</div>
                  <div style={{ fontSize: 11, fontWeight: 700, color: d.profit_factor > 1 ? C.green : d.profit_factor != null ? C.red : C.muted, marginTop: 2 }}>
                    {d.profit_factor ?? "—"}
                  </div>
                </div>
              </div>
            )}
            {(!d || d.count === 0) && (
              <div style={{ fontSize: 10, color: C.dim, fontStyle: "italic", marginTop: 6 }}>geen trades</div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Candle Chart (SVG) ───────────────────────────────────────────────────────

function CandleChart({ candles, entry, sl, tp1, tp2, tp3, side, entryTs }) {
  if (!candles || candles.length === 0) return <EmptyState icon="📊" text="Geen candle data opgeslagen" sub="Trades die na deze update zijn geopend bevatten een snapshot" height={140} />;

  const W = 680, H = 360;
  const PAD = { top: 24, right: 66, bottom: 20, left: 8 };
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;

  // Laatste 50 candles (breder dan 100 op mobiel)
  const visible = candles.slice(-50);
  const entryIdx = entryTs != null ? visible.findIndex(c => c[0] === entryTs) : -1;

  // Y-range: begin altijd vanuit de candlerange zodat er sowieso iets te zien is
  const candleLows  = visible.map(c => c[3]);
  const candleHighs = visible.map(c => c[2]);
  let minP = Math.min(...candleLows);
  let maxP = Math.max(...candleHighs);
  // Voeg handels-levels toe als ze binnen 10% van de candlerange vallen
  const span0 = maxP - minP || 1;
  const tradeLevels = [entry, sl, tp1, tp2, tp3]
    .filter(p => p > 0 && p > minP - span0 && p < maxP + span0);
  if (tradeLevels.length) {
    minP = Math.min(minP, ...tradeLevels);
    maxP = Math.max(maxP, ...tradeLevels);
  }
  const pad5 = (maxP - minP) * 0.05;
  minP -= pad5; maxP += pad5;
  const span = maxP - minP || 1;

  const xScale = (i) => PAD.left + (i + 0.5) * (plotW / visible.length);
  const yScale = (p) => PAD.top + plotH - ((p - minP) / span) * plotH;
  const cw = Math.max(2, plotW / visible.length - 2);

  const lines = [
    { p: entry, color: "#ffffff", label: "ENTRY", dash: "none" },
    { p: sl,    color: "#ef5350", label: "SL",    dash: "5 3"  },
    { p: tp1,   color: "#26a69a", label: "TP1",   dash: "4 3"  },
    { p: tp2,   color: "#26a69a", label: "TP2",   dash: "4 3"  },
    { p: tp3,   color: "#26a69a", label: "TP3",   dash: "4 3"  },
  ].filter(l => l.p > 0);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto", background: "#131722", borderRadius: 8, display: "block" }}>

      {/* Candles éérst — zodat prijslijnen er bovenop staan */}
      {visible.map((c, i) => {
        const [, open, high, low, close] = c;
        const bull = close >= open;
        const col  = bull ? "#26a69a" : "#ef5350";
        const x    = xScale(i);
        const bodyTop = Math.min(yScale(open), yScale(close));
        const bodyH   = Math.max(1.5, Math.abs(yScale(open) - yScale(close)));
        return (
          <g key={i}>
            <line x1={x} x2={x} y1={yScale(high)} y2={yScale(low)}
              stroke={col} strokeWidth={1} />
            <rect x={x - cw / 2} y={bodyTop} width={cw} height={bodyH}
              fill={col} />
          </g>
        );
      })}

      {/* Prijslijnen: ENTRY, SL, TP1-3 */}
      {lines.map(({ p, color, label, dash }) => {
        const y = yScale(p);
        return (
          <g key={label}>
            <line x1={PAD.left} x2={W - PAD.right} y1={y} y2={y}
              stroke={color} strokeWidth={label === "ENTRY" ? 1.4 : 1}
              strokeDasharray={dash} opacity={0.95} />
            <text x={W - PAD.right + 4} y={y + 4} fill={color} fontSize={8}
              fontFamily="monospace" fontWeight="700">
              {label} {Math.round(p)}
            </text>
          </g>
        );
      })}

      {/* Entry candle verticale markering */}
      {entryIdx >= 0 && (
        <>
          <line x1={xScale(entryIdx)} x2={xScale(entryIdx)}
            y1={PAD.top} y2={PAD.top + plotH}
            stroke="#ffffff" strokeWidth={0.8} strokeDasharray="3 3" opacity={0.35} />
          <text x={xScale(entryIdx)} y={PAD.top - 7} fill="#ffffff" fontSize={9}
            fontFamily="monospace" textAnchor="middle" opacity={0.7}>▼</text>
        </>
      )}
    </svg>
  );
}

// ─── Trade Review Panel ───────────────────────────────────────────────────────

const RATINGS = [
  { key: "good",     emoji: "✅", label: "Goed",  color: C.green,  bg: C.greenBg  },
  { key: "marginal", emoji: "⚠️", label: "Matig", color: C.yellow, bg: C.yellowBg },
  { key: "bad",      emoji: "❌", label: "Slecht", color: C.red,    bg: C.redBg   },
];

const REASONS = {
  good:     ["Duidelijke setup", "Goed niveau", "Met de trend", "Perfecte timing", "Goede R:R", "Sterke bevestiging"],
  marginal: ["Te vroeg", "Te laat", "Niveau net niet", "R:R kon beter", "Slechte timing", "Onduidelijke setup"],
  bad:      ["Geen duidelijke setup", "Niveau klopte niet", "Slechte R:R", "Tegen de trend", "Vals signaal", "Te klein SL"],
};

function getLabelDisplay(key) {
  const map = {
    good:         { emoji: "✅", label: "Goed",            color: C.green  },
    marginal:     { emoji: "⚠️", label: "Matig",           color: C.yellow },
    bad:          { emoji: "❌", label: "Slecht",           color: C.red    },
    good_entry:   { emoji: "✅", label: "Goede entry",      color: C.green  },
    too_early:    { emoji: "⚠️", label: "Te vroeg",         color: C.yellow },
    wrong_setup:  { emoji: "❌", label: "Verkeerde setup",  color: C.red    },
    bad_rr:       { emoji: "📊", label: "Slechte R:R",      color: C.orange },
    false_signal: { emoji: "🚫", label: "Vals signaal",     color: C.muted  },
  };
  return map[key] || { emoji: "?", label: key, color: C.muted };
}

function TradeReviewModal({ trade, onClose, onSaved }) {
  const existingRating = ["good","marginal","bad"].includes(trade.review_label) ? trade.review_label : null;
  const [rating,    setRating]    = useState(existingRating);
  const [reasons,   setReasons]   = useState(new Set());
  const [noteText,  setNoteText]  = useState(trade.review_note || "");
  const [saving,    setSaving]    = useState(false);

  function toggleReason(r) {
    setReasons(prev => {
      const next = new Set(prev);
      next.has(r) ? next.delete(r) : next.add(r);
      return next;
    });
  }

  async function saveReview() {
    if (!rating) return;
    setSaving(true);
    const parts = [];
    if (reasons.size > 0) parts.push([...reasons].join(", "));
    if (noteText.trim()) parts.push(noteText.trim());
    const note = parts.join("\n");
    await fetch(`${API_URL}/trades/${trade.id}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: rating, note }),
    });
    setSaving(false);
    onSaved(trade.id, rating, note);
    onClose();
  }

  const isLong  = trade.side === "buy";
  const pnlPos  = trade.realized_pnl >= 0;
  const rr      = trade.entry_price && trade.stop_loss && trade.tp3
    ? Math.abs((trade.tp3 - trade.entry_price) / (trade.entry_price - trade.stop_loss)).toFixed(1)
    : null;

  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.7)", zIndex: 1000,
      display: "flex", alignItems: "flex-start", justifyContent: "center",
      padding: "12px 12px", overflowY: "auto",
    }} onClick={onClose}>
      <div style={{
        background: C.card, borderRadius: 16, padding: 20, maxWidth: 780, width: "100%",
        boxShadow: "0 24px 64px rgba(0,0,0,0.35)", marginTop: 8,
      }} onClick={e => e.stopPropagation()}>

        {/* Header row */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontSize: 15, fontWeight: 800, color: isLong ? C.green : C.red }}>
              {isLong ? "▲ LONG" : "▼ SHORT"}
            </span>
            <Tag color={C.blue} bg={C.blueBg}>{trade.setup_type?.replace("_", " ")}</Tag>
            {trade.trade_mode && (
              <Tag color={trade.trade_mode === "scalp" ? C.orange : C.muted} bg={trade.trade_mode === "scalp" ? "#fff3ec" : "#f5f6fa"}>
                {trade.trade_mode.toUpperCase()}
              </Tag>
            )}
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", fontSize: 20, cursor: "pointer", color: C.muted, lineHeight: 1, padding: "0 4px" }}>✕</button>
        </div>

        {/* Key numbers row */}
        <div style={{
          display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 8, marginBottom: 14,
          background: C.bg, borderRadius: 10, padding: "10px 12px",
        }}>
          {[
            { label: "Entry",  value: fmtP(trade.entry_price) },
            { label: "Exit",   value: fmtP(trade.exit_price) || "—" },
            { label: "PnL",    value: `${pnlPos ? "+" : ""}${fmt(trade.realized_pnl)} $`, color: pnlPos ? C.green : C.red },
            { label: "R:R",    value: rr ? `${rr}R` : "—" },
          ].map(({ label, value, color }) => (
            <div key={label} style={{ textAlign: "center" }}>
              <div style={{ fontSize: 9, color: C.muted, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 2 }}>{label}</div>
              <div style={{ fontSize: 13, fontWeight: 800, color: color || C.text }}>{value}</div>
            </div>
          ))}
        </div>

        {/* Chart */}
        <div style={{ marginBottom: 14 }}>
          <SnapshotChart trade={trade} />
        </div>

        {/* TP progress */}
        <div style={{ marginBottom: 18 }}>
          <TpProgress trade={trade} />
        </div>

        {/* ── Step 1: Rating ── */}
        <div style={{ fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 10 }}>
          Hoe was deze trade?
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, marginBottom: 20 }}>
          {RATINGS.map(r => {
            const active = rating === r.key;
            return (
              <button key={r.key} onClick={() => { setRating(r.key); setReasons(new Set()); }} style={{
                padding: "14px 8px", borderRadius: 12, cursor: "pointer", fontFamily: "inherit",
                border: `2px solid ${active ? r.color : C.border}`,
                background: active ? r.bg : C.card,
                display: "flex", flexDirection: "column", alignItems: "center", gap: 4,
                transition: "all 0.15s",
              }}>
                <span style={{ fontSize: 22 }}>{r.emoji}</span>
                <span style={{ fontSize: 12, fontWeight: 800, color: active ? r.color : C.muted }}>{r.label}</span>
              </button>
            );
          })}
        </div>

        {/* ── Step 2: Reasons (only after rating selected) ── */}
        {rating && (
          <>
            <div style={{ fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 8 }}>
              Wat was de reden? <span style={{ fontWeight: 400, textTransform: "none" }}>(optioneel, meerdere mogelijk)</span>
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 18 }}>
              {REASONS[rating].map(r => {
                const active = reasons.has(r);
                const rc = RATINGS.find(x => x.key === rating);
                return (
                  <button key={r} onClick={() => toggleReason(r)} style={{
                    padding: "6px 12px", borderRadius: 20, fontSize: 11, fontWeight: 600, cursor: "pointer",
                    fontFamily: "inherit", transition: "all 0.12s",
                    border: `1.5px solid ${active ? rc.color : C.border}`,
                    background: active ? rc.color + "18" : "#fafbfd",
                    color: active ? rc.color : C.muted,
                  }}>
                    {r}
                  </button>
                );
              })}
            </div>

            {/* ── Step 3: Note ── */}
            <textarea
              placeholder="Extra notitie (optioneel) — wat zag je in de chart?"
              value={noteText}
              onChange={e => setNoteText(e.target.value)}
              style={{
                width: "100%", minHeight: 56, padding: "8px 12px", borderRadius: 8,
                border: `1px solid ${C.border}`, background: "#fafbfd", color: C.text,
                fontFamily: "inherit", fontSize: 11, resize: "vertical", marginBottom: 14,
                boxSizing: "border-box",
              }}
            />

            <button className="btn-primary" onClick={saveReview} disabled={saving} style={{ width: "100%", padding: "12px", fontSize: 13, borderRadius: 10 }}>
              {saving ? "Opslaan…" : "Opslaan"}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function TradeReviewPanel({ closedTrades }) {
  const [reviewTrade, setReviewTrade] = useState(null);
  const [reviews, setReviews] = useState({});  // trade_id → { label, note }

  // Load existing reviews from the trades list
  useEffect(() => {
    const existing = {};
    closedTrades.forEach(t => {
      if (t.review_label) existing[t.id] = { label: t.review_label, note: t.review_note || "" };
    });
    setReviews(existing);
  }, [closedTrades]);

  function handleSaved(id, label, note) {
    setReviews(prev => ({ ...prev, [id]: { label, note } }));
  }

  const pending   = closedTrades.filter(t => !reviews[t.id]);
  const done      = closedTrades.filter(t =>  reviews[t.id]);

  // Summary: per label, count and win/loss
  const summary = {};
  done.forEach(t => {
    const lbl = reviews[t.id]?.label;
    if (!lbl) return;
    if (!summary[lbl]) summary[lbl] = { total: 0, wins: 0 };
    summary[lbl].total++;
    if (t.realized_pnl > 0) summary[lbl].wins++;
  });

  return (
    <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow, marginBottom: 20 }}>
      <SectionLabel badge={pending.length}>Trade Review{pending.length > 0 ? ` — ${pending.length} te beoordelen` : ""}</SectionLabel>

      {closedTrades.length === 0 && (
        <EmptyState icon="🔍" text="Nog geen gesloten trades om te beoordelen" height={80} />
      )}

      {/* Label summary */}
      {done.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 18 }}>
          {Object.entries(summary).map(([key, s]) => {
            const l  = getLabelDisplay(key);
            const wr = Math.round(s.wins / s.total * 100);
            return (
              <div key={key} style={{
                background: l.color + "12", border: `1px solid ${l.color}33`,
                borderRadius: 10, padding: "8px 12px", fontSize: 10,
              }}>
                <div style={{ fontWeight: 700, color: l.color, marginBottom: 2 }}>{l.emoji} {l.label}</div>
                <div style={{ color: C.muted }}>{s.total} trades · {wr}% win</div>
              </div>
            );
          })}
        </div>
      )}

      {/* Trade list */}
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {[...closedTrades].reverse().map(t => {
          const rv = reviews[t.id];
          const pnlPos = t.realized_pnl >= 0;
          return (
            <div key={t.id} onClick={() => setReviewTrade(t)} style={{
              display: "flex", alignItems: "center", gap: 10,
              padding: "14px 16px", borderRadius: 10, cursor: "pointer",
              background: rv ? "#fafbfd" : "#fffdf5",
              border: `1px solid ${rv ? C.border : C.yellow + "44"}`,
              WebkitTapHighlightColor: "transparent",
            }}>
              <span style={{ fontSize: 13, fontWeight: 700, color: t.side === "buy" ? C.green : C.red }}>
                {t.side === "buy" ? "▲" : "▼"}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 3 }}>
                  <Tag color={C.blue} bg={C.blueBg}>{t.setup_type?.replace("_", " ")}</Tag>
                  <span style={{ fontSize: 11, fontWeight: 700, color: pnlPos ? C.green : C.red }}>
                    {pnlPos ? "+" : ""}{fmt(t.realized_pnl)} USDT
                  </span>
                </div>
                <div style={{ fontSize: 10, color: C.muted }}>{t.timestamp?.slice(0, 16).replace("T", " ")}</div>
              </div>
              <div style={{ textAlign: "right", flexShrink: 0 }}>
                {rv ? (
                  <span style={{ fontSize: 11, color: getLabelDisplay(rv.label).color, fontWeight: 700 }}>{getLabelDisplay(rv.label).emoji} {getLabelDisplay(rv.label).label}</span>
                ) : (
                  <span style={{
                    fontSize: 10, padding: "4px 10px", borderRadius: 6, fontWeight: 700,
                    background: C.blueBg, color: C.blue, border: `1px solid ${C.blue}33`,
                  }}>Beoordeel →</span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {reviewTrade && (
        <TradeReviewModal
          trade={reviewTrade}
          onClose={() => setReviewTrade(null)}
          onSaved={handleSaved}
        />
      )}
    </div>
  );
}

// ─── Backtest Panel ───────────────────────────────────────────────────────────

function BacktestPanel() {
  const [btConfig, setBtConfig] = useState({ symbol: "BTC/USDT:USDT", days: 90, test_pct: 0.30, trade_mode: "daytrade" });
  const [bt, setBt]     = useState(null);
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
      body: JSON.stringify({
        ...btConfig,
        days: Math.min(365, Math.max(30, Number(btConfig.days) || 90)),
      }),
    });
    setLoading(false);
    pollBacktest();
  }

  const r = bt?.result;
  const isPos = r?.total_pnl >= 0;

  return (
    <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow, marginBottom: 20 }}>
      <SectionLabel>Backtest</SectionLabel>

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
          <select value={btConfig.trade_mode}
            onChange={e => setBtConfig({ ...btConfig, trade_mode: e.target.value })}
            disabled={bt?.running}>
            <option value="daytrade">Daytrade (15m)</option>
            <option value="scalp">Scalp (5m)</option>
            <option value="both">Beide (multi-tf)</option>
          </select>
        </label>
        <button className="btn-primary" onClick={startBt} disabled={bt?.running || loading}
          style={{ flex: 1, minWidth: 130 }}>
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
          {/* Summary */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
            {[
              { label: "Trades",    value: r.total_trades,   color: C.text  },
              { label: "Win Rate",  value: `${r.win_rate}%`, color: r.win_rate >= 50 ? C.green : C.red },
              { label: "Prof. Factor", value: r.profit_factor ?? "—", color: r.profit_factor > 1 ? C.green : C.red },
              { label: "Sharpe",    value: r.sharpe ?? "—",  color: r.sharpe > 1 ? C.green : C.yellow },
              { label: "Max DD",    value: r.max_drawdown_pct != null ? `-${r.max_drawdown_pct}%` : "—", color: r.max_drawdown_pct > 15 ? C.red : C.muted },
              { label: "PnL",       value: fmtSign(r.total_pnl), color: isPos ? C.green : C.red },
              { label: "Expectancy",value: fmtSign(r.expectancy), color: r.expectancy >= 0 ? C.green : C.red },
            ].map(s => (
              <div key={s.label} style={{ background: "#fafbfd", border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 14px", flex: 1, minWidth: 80 }}>
                <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 4 }}>{s.label}</div>
                <div style={{ fontSize: 14, fontWeight: 700, color: s.color }}>{s.value}</div>
              </div>
            ))}
          </div>

          <div style={{ fontSize: 9, color: C.dim, marginBottom: 12 }}>
            Train: {r.train_period} · Test: {r.test_period} · {r.duration_s}s
          </div>

          {r.equity_curve?.length > 1 && (
            <div style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 8 }}>Equity Curve — Test Window</div>
              <ResponsiveContainer width="100%" height={130}>
                <AreaChart data={r.equity_curve} margin={{ top: 4, right: 6, left: -28, bottom: 0 }}>
                  <defs>
                    <linearGradient id="btGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="5%"  stopColor={isPos ? C.greenLine : C.red} stopOpacity={0.2} />
                      <stop offset="95%" stopColor={isPos ? C.greenLine : C.red} stopOpacity={0}   />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
                  <XAxis dataKey="ts" stroke="transparent" tick={{ fill: C.dim, fontSize: 9 }} tickLine={false} interval="preserveStartEnd" />
                  <YAxis stroke="transparent" tick={{ fill: C.dim, fontSize: 9 }} tickLine={false}
                    tickFormatter={v => `$${(v/1000).toFixed(1)}k`} />
                  <Tooltip content={<EquityTooltip />} />
                  <ReferenceLine y={r.equity_curve[0]?.equity} stroke={C.border} strokeDasharray="4 4" />
                  <Area type="monotone" dataKey="equity" stroke={isPos ? C.greenLine : C.red} strokeWidth={2}
                    fill="url(#btGrad)" dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          )}

          <div style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 8 }}>
            {Object.entries(r.setup_stats || {}).map(([setup, d]) => (
              <div key={setup} style={{ background: "#fafbfd", border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 12px" }}>
                <div style={{ fontSize: 9, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
                  {setup.replace("_", " ")}
                </div>
                {d.trades === 0
                  ? <div style={{ fontSize: 9, color: C.dim }}>geen trades</div>
                  : <>
                      <div style={{ fontSize: 13, fontWeight: 800, color: d.win_rate >= 50 ? C.green : C.red }}>{d.win_rate}%</div>
                      <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>{d.trades} trades · PF {d.profit_factor ?? "—"}</div>
                    </>
                }
              </div>
            ))}
          </div>

          {r.counter_trend_stats && (Object.keys(r.counter_trend_stats).length > 0) && (
            <>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", margin: "16px 0 8px" }}>
                Met-trend vs. tegen-trend
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 8 }}>
                {[
                  { key: "with_trend",    label: "Mét trend" },
                  { key: "counter_trend", label: "⚖️ Tegen-trend" },
                ].map(({ key, label }) => {
                  const d = r.counter_trend_stats[key];
                  return (
                    <div key={key} style={{ background: "#fafbfd", border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 12px" }}>
                      <div style={{ fontSize: 9, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
                        {label}
                      </div>
                      {!d || d.trades === 0
                        ? <div style={{ fontSize: 9, color: C.dim }}>geen trades</div>
                        : <>
                            <div style={{ fontSize: 13, fontWeight: 800, color: d.win_rate >= 50 ? C.green : C.red }}>{d.win_rate}%</div>
                            <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>
                              {d.trades} trades · PF {d.profit_factor ?? "—"} · gem. {fmtSign(d.avg_pnl)}
                            </div>
                          </>
                      }
                    </div>
                  );
                })}
              </div>
            </>
          )}

          {r.config?.trade_mode === "both" && r.mode_stats && (Object.keys(r.mode_stats).length > 0) && (
            <>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", margin: "16px 0 8px" }}>
                Daytrade vs. scalp (multi-tf)
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2,1fr)", gap: 8 }}>
                {[
                  { key: "daytrade", label: "Daytrade (15m)" },
                  { key: "scalp",    label: "Scalp (5m)" },
                ].map(({ key, label }) => {
                  const d = r.mode_stats[key];
                  return (
                    <div key={key} style={{ background: "#fafbfd", border: `1px solid ${C.border}`, borderRadius: 10, padding: "10px 12px" }}>
                      <div style={{ fontSize: 9, color: C.blue, fontWeight: 700, textTransform: "uppercase", letterSpacing: 0.5, marginBottom: 6 }}>
                        {label}
                      </div>
                      {!d || d.trades === 0
                        ? <div style={{ fontSize: 9, color: C.dim }}>geen trades</div>
                        : <>
                            <div style={{ fontSize: 13, fontWeight: 800, color: d.win_rate >= 50 ? C.green : C.red }}>{d.win_rate}%</div>
                            <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>
                              {d.trades} trades · PF {d.profit_factor ?? "—"} · gem. {fmtSign(d.avg_pnl)}
                            </div>
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
    </div>
  );
}

// ─── Monte Carlo Panel ────────────────────────────────────────────────────────

function MonteCarloPanel() {
  const [mc,      setMc]      = useState(null);
  const [loading, setLoading] = useState(false);
  const [sims,    setSims]    = useState(1000);

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

  const verdictStyle = r ? (
    r.has_edge
      ? { color: C.green, bg: C.greenBg }
      : r.percentile_vs_random >= 80
        ? { color: C.yellow, bg: C.yellowBg }
        : { color: C.red,    bg: C.redBg    }
  ) : null;

  return (
    <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow, marginBottom: 20 }}>
      <SectionLabel>Monte Carlo Validatie</SectionLabel>

      <div style={{ display: "flex", gap: 10, alignItems: "flex-end", marginBottom: 14, flexWrap: "wrap" }}>
        <label style={{ flex: 1, minWidth: 90 }}>
          <div style={{ fontSize: 9, color: C.muted, marginBottom: 4, letterSpacing: 1, textTransform: "uppercase" }}>Simulaties</div>
          <select value={sims} onChange={e => setSims(+e.target.value)} disabled={mc?.running}>
            {[500, 1000, 2000, 5000].map(n => <option key={n} value={n}>{n.toLocaleString()}</option>)}
          </select>
        </label>
        <button className="btn-primary" onClick={startMc}
          disabled={mc?.running || loading}
          style={{ flex: 2, minWidth: 180 }}>
          {mc?.running ? `${mc.progress}% bezig…` : "▶  Run Monte Carlo"}
        </button>
      </div>

      {!r && !mc?.running && (
        <div style={{ fontSize: 10, color: C.muted, fontStyle: "italic" }}>
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
          {/* Verdict */}
          <div style={{
            display: "flex", alignItems: "center", gap: 12, marginBottom: 18,
            background: verdictStyle.bg, borderRadius: 10, padding: "12px 16px",
            border: `1px solid ${verdictStyle.color}33`,
          }}>
            <div style={{ fontSize: 20 }}>
              {r.has_edge ? "✅" : r.percentile_vs_random >= 80 ? "⚠️" : "❌"}
            </div>
            <div>
              <div style={{ fontSize: 12, fontWeight: 700, color: verdictStyle.color }}>{r.verdict}</div>
              <div style={{ fontSize: 10, color: C.muted, marginTop: 2 }}>
                {r.n_trades} trades · {r.n_simulations.toLocaleString()} simulaties · {r.duration_s}s
              </div>
            </div>
          </div>

          {/* Key metrics row */}
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
            {[
              {
                label:  "Actuele PnL",
                value:  fmtSign(r.actual_pnl),
                color:  r.actual_pnl >= 0 ? C.green : C.red,
                sub:    "backtest resultaat",
              },
              {
                label:  "Bootstrap CI (90%)",
                value:  `${fmtSign(r.bootstrap_pnl_p5)} – ${fmtSign(r.bootstrap_pnl_p95)}`,
                color:  r.bootstrap_pnl_p5 >= 0 ? C.green : C.yellow,
                sub:    "bandbreedte bij hersampling",
              },
              {
                label:  "Positieve kans",
                value:  `${r.bootstrap_positive_pct}%`,
                color:  r.bootstrap_positive_pct >= 70 ? C.green : r.bootstrap_positive_pct >= 50 ? C.yellow : C.red,
                sub:    "bootstrap samples > $0",
              },
              {
                label:  "Rang vs. willekeurig",
                value:  `${r.percentile_vs_random}e percentiel`,
                color:  r.percentile_vs_random >= 95 ? C.green : r.percentile_vs_random >= 80 ? C.yellow : C.red,
                sub:    "vs. coin-flip strategie",
              },
              {
                label:  "Max DD (actueel)",
                value:  `-${r.actual_max_dd}%`,
                color:  r.actual_max_dd > 20 ? C.red : C.muted,
                sub:    `mediaan random: -${r.bootstrap_dd_p50}%`,
              },
            ].map(s => (
              <div key={s.label} style={{
                flex: 1, minWidth: 120,
                background: "#fafbfd", border: `1px solid ${C.border}`,
                borderRadius: 10, padding: "10px 14px",
              }}>
                <div style={{ fontSize: 8, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 4 }}>{s.label}</div>
                <div style={{ fontSize: 13, fontWeight: 700, color: s.color }}>{s.value}</div>
                {s.sub && <div style={{ fontSize: 9, color: C.dim, marginTop: 3 }}>{s.sub}</div>}
              </div>
            ))}
          </div>

          {/* Histogram */}
          {r.pnl_histogram?.length > 0 && (
            <div>
              <div style={{ fontSize: 9, color: C.muted, letterSpacing: 1, textTransform: "uppercase", marginBottom: 8 }}>
                PnL-verdeling willekeurige strategie — blauwe balk = actueel resultaat
              </div>
              <ResponsiveContainer width="100%" height={120}>
                <BarChart data={r.pnl_histogram} margin={{ top: 2, right: 4, left: -28, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke={C.border} vertical={false} />
                  <XAxis dataKey="bucket_center" stroke="transparent"
                    tick={{ fill: C.dim, fontSize: 8 }} tickLine={false}
                    tickFormatter={v => `$${Math.round(v / 100) * 100}`}
                    interval={3} />
                  <YAxis stroke="transparent" tick={{ fill: C.dim, fontSize: 8 }} tickLine={false} axisLine={false} />
                  <Tooltip
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null;
                      const d = payload[0].payload;
                      return (
                        <div style={{ background: C.card, border: `1px solid ${C.border}`, borderRadius: 8, padding: "6px 10px", fontSize: 10 }}>
                          <div style={{ color: C.muted, fontSize: 9 }}>~${Math.round(d.bucket_center)}</div>
                          <div style={{ fontWeight: 700, color: d.is_actual ? C.blue : C.text }}>{d.count} simulaties{d.is_actual ? " ← actueel" : ""}</div>
                        </div>
                      );
                    }}
                  />
                  <Bar dataKey="count" radius={[3, 3, 0, 0]} maxBarSize={40} animationDuration={400}>
                    {r.pnl_histogram.map((entry, i) => (
                      <Cell key={i}
                        fill={entry.is_actual ? C.blue : C.border}
                        fillOpacity={entry.is_actual ? 1 : 0.75}
                      />
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
    </div>
  );
}

// ─── Learning Stats Panel ─────────────────────────────────────────────────────

const FACTOR_LABELS = {
  score_atr_sl:        "ATR SL",
  score_trend_4h:      "4H Trend",
  score_trend_1h:      "1H Trend",
  score_volume:        "Volume",
  score_level_clean:   "Level kwaliteit",
  score_round_number:  "Round number",
  score_inside_doji:   "Inside/Doji",
};

const REJECTION_LABELS = {
  level_weak:    "Level zwak",
  no_confirm:    "Geen bevestiging",
  wrong_trend:   "Verkeerde trend",
  bad_timing:    "Slecht moment",
  other:         "Anders",
  unknown:       "Onbekend",
};

function LearningStatsPanel() {
  const [data, setData] = useState(null);

  useEffect(() => {
    fetch(`${API_URL}/learning_stats`).then(r => r.json()).then(setData).catch(() => {});
  }, []);

  if (!data) return null;
  if (data.reviews !== undefined && data.reviews < 5) {
    return (
      <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow, marginBottom: 20 }}>
        <SectionLabel>Leermodel</SectionLabel>
        <div style={{ color: C.muted, fontSize: 12 }}>{data.message || `Nog niet genoeg beoordelingen (${data.reviews}/30)`}</div>
      </div>
    );
  }
  if (!data.factor_comparison) return null;

  const factors = Object.entries(data.factor_comparison);
  const topFactors = new Set(data.top_differentiating_factors || []);

  return (
    <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow, marginBottom: 20 }}>
      <SectionLabel>Leermodel</SectionLabel>

      {/* Summary row */}
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginBottom: 18 }}>
        <div style={{ background: C.greenBg, borderRadius: 10, padding: "8px 16px", minWidth: 90, textAlign: "center" }}>
          <div style={{ fontSize: 20, fontWeight: 800, color: C.green }}>{data.approval_rate}%</div>
          <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>Goedkeuring</div>
        </div>
        <div style={{ background: C.blueBg, borderRadius: 10, padding: "8px 16px", minWidth: 90, textAlign: "center" }}>
          <div style={{ fontSize: 20, fontWeight: 800, color: C.blue }}>{data.approved}</div>
          <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>Goedgekeurd</div>
        </div>
        <div style={{ background: C.redBg, borderRadius: 10, padding: "8px 16px", minWidth: 90, textAlign: "center" }}>
          <div style={{ fontSize: 20, fontWeight: 800, color: C.red }}>{data.skipped}</div>
          <div style={{ fontSize: 9, color: C.muted, marginTop: 2 }}>Overgeslagen</div>
        </div>
        {!data.ready_for_learning && (
          <div style={{ background: C.yellowBg, borderRadius: 10, padding: "8px 16px", alignSelf: "center" }}>
            <div style={{ fontSize: 10, color: C.yellow, fontWeight: 600 }}>{data.total_reviews}/30 beoordelingen voor volledige analyse</div>
          </div>
        )}
      </div>

      {/* Factor comparison */}
      <div style={{ marginBottom: 16 }}>
        <div style={{ fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: 1, marginBottom: 8 }}>FACTOR VERGELIJKING (gem. score)</div>
        {factors.map(([key, val]) => (
          <div key={key} style={{ marginBottom: 6 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11, marginBottom: 3 }}>
              <span style={{ color: topFactors.has(key) ? C.blue : C.text, fontWeight: topFactors.has(key) ? 700 : 400 }}>
                {FACTOR_LABELS[key] || key}{topFactors.has(key) ? " ★" : ""}
              </span>
              <span style={{ color: C.muted, fontSize: 10 }}>
                ✅ {val.avg_approved} · ❌ {val.avg_skipped}
              </span>
            </div>
            <div style={{ height: 4, background: C.border, borderRadius: 99, overflow: "hidden", display: "flex" }}>
              <div style={{ height: "100%", background: C.green, borderRadius: "99px 0 0 99px", flex: val.avg_approved }} />
              <div style={{ height: "100%", background: C.red, flex: val.avg_skipped }} />
            </div>
          </div>
        ))}
      </div>

      {/* Rejection reasons */}
      {data.rejection_reasons && Object.keys(data.rejection_reasons).length > 0 && (
        <div>
          <div style={{ fontSize: 10, fontWeight: 700, color: C.muted, letterSpacing: 1, marginBottom: 8 }}>AFWIJZINGSREDENEN</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {Object.entries(data.rejection_reasons).sort((a, b) => b[1] - a[1]).map(([reason, count]) => (
              <div key={reason} style={{ background: C.redBg, borderRadius: 8, padding: "4px 10px", fontSize: 10 }}>
                <span style={{ color: C.red, fontWeight: 700 }}>{count}×</span>
                <span style={{ color: C.muted, marginLeft: 4 }}>{REJECTION_LABELS[reason] || reason}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────

export default function Dashboard() {
  const [status, setStatus] = useState(null);
  const [stats,  setStats]  = useState(null);
  const [config, setConfig] = useState({ symbol: "BTC/USDT:USDT", risk_per_trade: 0.01 });
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState(null);

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
  const winRate      = closedTrades.length > 0
    ? Math.round((status.winning_trades / closedTrades.length) * 100)
    : null;

  return (
    <div style={{
      minHeight: "100vh", background: C.bg, color: C.text,
      fontFamily: "'Inter', -apple-system, BlinkMacSystemFont, sans-serif",
      padding: "24px 28px", maxWidth: 1440, margin: "0 auto",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        * { box-sizing: border-box; }
        ::-webkit-scrollbar { width: 5px; background: ${C.bg}; }
        ::-webkit-scrollbar-thumb { background: ${C.border}; border-radius: 3px; }
        .btn-primary {
          background: ${C.blue}; color: #fff; border: none; border-radius: 8px;
          padding: 10px 20px; font-family: inherit; font-size: 11px; font-weight: 700;
          letter-spacing: 0.5px; cursor: pointer; text-transform: uppercase;
          transition: opacity 0.15s; width: 100%; box-shadow: 0 2px 8px ${C.blue}44;
        }
        .btn-primary:hover:not(:disabled) { opacity: 0.88; }
        .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
        .btn-danger {
          background: ${C.redBg}; color: ${C.red}; border: 1px solid ${C.red}44;
          border-radius: 8px; padding: 10px 20px; font-family: inherit; font-size: 11px;
          font-weight: 700; letter-spacing: 0.5px; cursor: pointer; text-transform: uppercase;
          transition: background 0.15s; width: 100%;
        }
        .btn-danger:hover:not(:disabled) { background: ${C.red}18; }
        select, input[type="number"], input[type="text"] {
          background: #fafbfd; border: 1px solid ${C.border}; color: ${C.text};
          font-family: inherit; font-size: 12px; padding: 8px 12px;
          border-radius: 8px; outline: none; width: 100%; transition: border-color 0.15s;
        }
        select:focus, input:focus { border-color: ${C.blue}88; box-shadow: 0 0 0 3px ${C.blue}15; }
        .closed-row:hover { background: #fafbfd; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
        .pulse { animation: pulse 2s infinite; }
        @media (max-width: 960px) {
          .main-grid { grid-template-columns: 1fr !important; }
          .analytics-grid { grid-template-columns: 1fr !important; }
        }
      `}</style>

      <CircuitBreakerBanner status={status} />

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 24 }}>
        <div>
          <div style={{ fontSize: 22, fontWeight: 800, letterSpacing: -0.5, color: C.text }}>
            Trade<span style={{ color: C.blue }}>Bot</span>
          </div>
          <div style={{ fontSize: 10, color: C.muted, marginTop: 2, letterSpacing: 1 }}>
            OKX · DoopieCash Method · 15M / 1H / 4H
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          {status?.daily_loss_pct < -1 && (
            <Tag color={C.yellow} bg={C.yellowBg}>DAY {fmt(status.daily_loss_pct, 1)}%</Tag>
          )}
          {status?.trade_mode && (
            <Tag color={status.trade_mode === 'both' ? C.green : C.blue} bg={status.trade_mode === 'both' ? C.greenBg : C.blueBg}>
              {status.trade_mode === 'both' ? 'DAYTRADE + SCALP' : status.trade_mode.toUpperCase()}
            </Tag>
          )}
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <div className={isRunning ? "pulse" : ""} style={{
              width: 8, height: 8, borderRadius: "50%",
              background: isRunning ? C.green : C.dim,
              boxShadow: isRunning ? `0 0 0 3px ${C.green}33` : "none",
            }} />
            <span style={{ fontSize: 11, color: isRunning ? C.green : C.muted, fontWeight: 600 }}>
              {isRunning ? "Live" : "Offline"}
            </span>
          </div>
        </div>
      </div>

      {error && (
        <div style={{
          background: C.redBg, border: `1px solid ${C.red}33`, borderRadius: 10,
          padding: "10px 16px", marginBottom: 18, color: C.red, fontSize: 11, fontWeight: 600,
        }}>⚠ {error}</div>
      )}

      {/* ── Stats row ──────────────────────────────────────────────────────── */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginBottom: 24 }}>
        <StatCard label="Balance"     value={status ? `$${fmt(status.balance, 0)}` : "—"}     sub="USDT vrij"     accent={C.blue}  />
        <StatCard label="Equity"      value={status ? `$${fmt(status.equity,  0)}` : "—"}     sub="USDT totaal"                    />
        <StatCard label="Total PnL"   value={<PnlBadge value={status?.total_pnl} size={18} />}                                     />
        <StatCard label="Win Rate"    value={winRate != null ? `${winRate}%` : "—"}            sub={`${status?.winning_trades || 0}/${closedTrades.length}`} accent={winRate >= 50 ? C.green : winRate != null ? C.red : undefined} />
        <StatCard label="Sharpe"      value={stats?.sharpe_ratio ?? "—"}                       sub="annualized"   accent={stats?.sharpe_ratio > 1 ? C.green : undefined} />
        <StatCard label="Max DD"      value={stats?.max_drawdown_pct != null ? `-${stats.max_drawdown_pct}%` : "—"} sub="van piek" accent={stats?.max_drawdown_pct > 10 ? C.red : undefined} />
        <StatCard label="Actief"      value={activeTrades.length}                              sub={activeTrades.length > 0 ? activeTrades.map(t => t.setup_type).join(", ") : "geen"} accent={activeTrades.length > 0 ? C.yellow : undefined} />
      </div>

      {/* ── Main grid ──────────────────────────────────────────────────────── */}
      <div className="main-grid" style={{ display: "grid", gridTemplateColumns: "260px 1fr", gap: 20, marginBottom: 20 }}>

        {/* Sidebar */}
        <div>
          <ConfigPanel
            config={config} setConfig={setConfig}
            isRunning={isRunning} loading={loading}
            onStart={startBot} onStop={stopBot}
          />
          <SetupHealthPanel setupHealth={status?.setup_health} />
          <AnalysisPanel lastAnalysis={status?.last_analysis} />
        </div>

        {/* Main */}
        <div>
          {/* Active trades */}
          <div style={{ marginBottom: 20 }}>
            <SectionLabel badge={activeTrades.length}>Actieve trades</SectionLabel>
            {activeTrades.length === 0 ? (
              <div style={{ background: C.card, borderRadius: 14, boxShadow: C.shadow }}>
                <EmptyState icon="🔍" text="Geen actieve trades" sub="Bot zoekt naar een setup…" height={100} />
              </div>
            ) : (
              activeTrades.map((t, i) => <ActiveTradeCard key={t.id || i} trade={t} />)
            )}
          </div>

          {/* Equity curve */}
          <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow }}>
            <SectionLabel>Equity Curve</SectionLabel>
            <EquityCurve history={stats?.equity_history} />
          </div>
        </div>
      </div>

      {/* ── Live Candle Chart ──────────────────────────────────────────────── */}
      <div style={{ marginBottom: 20 }}>
        <LiveCandleChart trades={allTrades} />
      </div>

      {/* ── Analytics ──────────────────────────────────────────────────────── */}
      <div className="analytics-grid" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, marginBottom: 20 }}>
        <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow }}>
          <SectionLabel>Dagelijks PnL</SectionLabel>
          <DailyPnlChart data={stats?.daily_pnl} />
        </div>
        <div style={{ background: C.card, borderRadius: 14, padding: 20, boxShadow: C.shadow }}>
          <SectionLabel>Setup statistieken</SectionLabel>
          <SetupStatsGrid stats={stats?.setup_stats} />
        </div>
      </div>

      {/* ── Backtest ───────────────────────────────────────────────────────── */}
      <BacktestPanel />

      {/* ── Monte Carlo ────────────────────────────────────────────────────── */}
      <MonteCarloPanel />

      {/* ── Trade Review ────────────────────────────────────────────────────── */}
      <TradeReviewPanel closedTrades={closedTrades} />

      {/* ── Learning Stats ──────────────────────────────────────────────────── */}
      <LearningStatsPanel />

      {/* ── Closed trades ──────────────────────────────────────────────────── */}
      <SectionLabel badge={closedTrades.length}>Gesloten trades</SectionLabel>
      <ClosedTradesTable trades={closedTrades} />

      <div style={{ marginTop: 28, textAlign: "center", color: C.dim, fontSize: 9, letterSpacing: 1.5 }}>
        VERVERST ELKE 5S · {new Date().toLocaleTimeString("nl-NL")} · USE AT YOUR OWN RISK
      </div>
    </div>
  );
}
