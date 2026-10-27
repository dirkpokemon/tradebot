// Afgeleide cijfers voor het dashboard. Alles wordt uit de trades en de
// equity-historie berekend die /status en /stats al teruggeven — geen extra
// endpoints, dus geen tweede waarheid die uit de pas kan gaan lopen.

const pnl = (t) => Number(t?.realized_pnl) || 0;

/**
 * R-multiple: de uitkomst uitgedrukt in het risico dat de trade liep.
 * Het risico is de afstand entry→SL maal de positiegrootte — precies het bedrag
 * dat `calculate_position_size` op 1% van het saldo mikt. +2R betekent dus twee
 * keer de inzet verdiend, ongeacht hoe groot die inzet in dollars was.
 */
export function tradeR(t) {
  const dist = Math.abs((t?.entry_price || 0) - (t?.stop_loss || 0));
  const risk = dist * (t?.quantity || 0);
  if (!risk) return null;
  return pnl(t) / risk;
}

/** Context score → rapportcijfer, zoals een journaal een setup beoordeelt. */
export function gradeOf(score) {
  if (score == null || score === 0) return null;
  if (score >= 85) return { label: "A+", color: "#24c98a" };
  if (score >= 75) return { label: "A",  color: "#24c98a" };
  if (score >= 65) return { label: "B",  color: "#5b7cfa" };
  if (score >= 55) return { label: "C",  color: "#f0b429" };
  return                 { label: "D",  color: "#f05469" };
}

export function winRateOf(trades) {
  if (!trades.length) return null;
  return trades.filter(t => pnl(t) > 0).length / trades.length * 100;
}

export function profitFactorOf(trades) {
  const gp = trades.filter(t => pnl(t) > 0).reduce((s, t) => s + pnl(t), 0);
  const gl = Math.abs(trades.filter(t => pnl(t) < 0).reduce((s, t) => s + pnl(t), 0));
  if (!gl) return gp > 0 ? Infinity : null;
  return gp / gl;
}

/** Gemiddelde winst vs. gemiddeld verlies, en hun verhouding. */
export function avgWinLossOf(trades) {
  const wins   = trades.filter(t => pnl(t) > 0);
  const losses = trades.filter(t => pnl(t) < 0);
  const avgWin  = wins.length   ? wins.reduce((s, t) => s + pnl(t), 0) / wins.length : 0;
  const avgLoss = losses.length ? Math.abs(losses.reduce((s, t) => s + pnl(t), 0) / losses.length) : 0;
  return { avgWin, avgLoss, ratio: avgLoss ? avgWin / avgLoss : null };
}

/** Dagen gegroepeerd op UTC-datum — dezelfde indeling die /stats aanhoudt. */
export function byDay(trades) {
  const map = new Map();
  for (const t of trades) {
    const day = (t.timestamp || "").slice(0, 10);
    if (!day) continue;
    const cur = map.get(day) || { date: day, pnl: 0, trades: 0, wins: 0 };
    cur.pnl += pnl(t);
    cur.trades += 1;
    if (pnl(t) > 0) cur.wins += 1;
    map.set(day, cur);
  }
  return [...map.values()].sort((a, b) => a.date.localeCompare(b.date));
}

/** Cumulatieve P&L per dag — de curve die laat zien of het de goede kant op gaat. */
export function cumulativeByDay(days) {
  let run = 0;
  return days.map(d => {
    run += d.pnl;
    return { date: d.date, cum: Math.round(run * 100) / 100, pnl: d.pnl };
  });
}

/**
 * Drawdown-reeks uit de equity-historie: hoeveel staat het saldo onder zijn
 * hoogste punt tot dan toe. Altijd ≤ 0, zodat de grafiek onder de nullijn hangt.
 */
export function drawdownSeries(equityHistory) {
  if (!equityHistory?.length) return [];
  let peak = equityHistory[0].equity;
  return equityHistory.map(e => {
    if (e.equity > peak) peak = e.equity;
    return { ts: e.ts, dd: Math.round((e.equity - peak) * 100) / 100 };
  });
}

/**
 * Edge score: zes eigenschappen van een handelsreeks, elk op 0-100 geschaald,
 * samen één rapportcijfer. Het nut zit in de vorm van de zeshoek, niet in het
 * getal: een diepe deuk laat zien wáár het lek zit.
 *
 * De schalen zijn bewust ruim ("100 = uitstekend", niet "100 = haalbaar"), zodat
 * een nette strategie rond de 60-75 uitkomt en er boven nog ruimte over is.
 */
export function edgeScore(trades, maxDrawdownPct) {
  if (!trades.length) return null;

  const wr   = winRateOf(trades) ?? 0;
  const pf   = profitFactorOf(trades);
  const { ratio } = avgWinLossOf(trades);
  const net  = trades.reduce((s, t) => s + pnl(t), 0);
  const days = byDay(trades);
  const profitableDays = days.length ? days.filter(d => d.pnl > 0).length / days.length * 100 : 0;

  // Herstelvermogen: netto winst afgezet tegen de diepste terugval.
  const ddPct = maxDrawdownPct ?? 0;
  const worstLoss = Math.abs(Math.min(0, ...trades.map(pnl)));
  const recovery = ddPct > 0 ? net / (ddPct / 100 * Math.max(1, Math.abs(net) + worstLoss)) : (net > 0 ? 3 : 0);

  const clamp = (v) => Math.max(0, Math.min(100, Math.round(v)));
  const axes = [
    { key: "win_rate",  label: "Win %",        value: clamp(wr / 65 * 100) },
    { key: "pf",        label: "Profit factor", value: clamp((pf === Infinity ? 3 : (pf ?? 0)) / 3 * 100) },
    { key: "wl",        label: "Gem. W/V",      value: clamp((ratio ?? 0) / 3 * 100) },
    { key: "recovery",  label: "Herstel",       value: clamp(recovery / 5 * 100) },
    { key: "dd",        label: "Drawdown",      value: clamp(100 - ddPct * 6) },
    { key: "consist",   label: "Consistentie",  value: clamp(profitableDays / 70 * 100) },
  ];
  const score = axes.reduce((s, a) => s + a.value, 0) / axes.length;
  return { axes, score: Math.round(score * 10) / 10 };
}

/**
 * Maandrooster voor de kalender-heatmap. Weken beginnen op maandag; dagen van
 * buiten de maand blijven leeg in plaats van dat ze doorlopen uit de vorige
 * maand — anders tel je per ongeluk een dag dubbel bij het scannen.
 */
export function monthGrid(year, month, dayMap) {
  const first = new Date(Date.UTC(year, month, 1));
  const daysInMonth = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  const lead = (first.getUTCDay() + 6) % 7;  // maandag = 0

  const cells = [];
  for (let i = 0; i < lead; i++) cells.push(null);
  for (let d = 1; d <= daysInMonth; d++) {
    const iso = `${year}-${String(month + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    cells.push({ day: d, iso, ...(dayMap.get(iso) || null) });
  }
  while (cells.length % 7 !== 0) cells.push(null);
  return cells;
}
