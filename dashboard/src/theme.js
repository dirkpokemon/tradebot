// Eén bron voor de kleuren. De dashboardstijl is donker: lange sessies naar een
// scherm met cijfers zijn rustiger op een donkere ondergrond, en winst/verlies
// springen er in kleur veel sterker uit dan op wit.
export const C = {
  bg:        "#0a0b0f",   // pagina
  sidebar:   "#0d0e14",   // navigatiekolom
  card:      "#121319",   // kaart
  card2:     "#171922",   // verdiepte vlakken binnen een kaart
  border:    "#20222c",
  borderLt:  "#2a2d39",

  text:      "#e6e9f2",
  muted:     "#7b8296",
  dim:       "#4c5262",

  green:     "#24c98a",
  greenBg:   "rgba(36,201,138,0.12)",
  greenLine: "#24c98a",
  red:       "#f05469",
  redBg:     "rgba(240,84,105,0.12)",
  blue:      "#5b7cfa",
  blueBg:    "rgba(91,124,250,0.14)",
  yellow:    "#f0b429",
  yellowBg:  "rgba(240,180,41,0.13)",
  orange:    "#f08a3c",
  orangeBg:  "rgba(240,138,60,0.13)",
  purple:    "#8b6ef0",
  purpleBg:  "rgba(139,110,240,0.14)",

  shadow:    "0 1px 2px rgba(0,0,0,0.4)",
  shadowMd:  "0 8px 24px rgba(0,0,0,0.45)",
};

// Cijfers krijgen een smalle mono: kolommen lijnen uit en bedragen zijn in één
// oogopslag te vergelijken, zoals in een handelsjournaal hoort.
export const MONO = "'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, monospace";
