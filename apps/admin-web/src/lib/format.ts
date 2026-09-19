// Small formatting helpers shared across pages.

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function int(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toLocaleString();
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return `${value.toLocaleString()} ms`;
}

/** Epoch seconds (the API stores integer seconds) to a local datetime. */
export function dateFromEpochSeconds(epoch: number | null | undefined): string {
  if (epoch === null || epoch === undefined) return "—";
  const d = new Date(epoch * 1000);
  return d.toLocaleString();
}
