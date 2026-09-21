// Everything server-side is UTC (registry.db timestamps, and refresh.py's
// version strings, which are literally a UTC clock reading formatted as
// YYYYMMDD-HHMMSS). The user is in the UK, so display converts to
// Europe/London — which also means the offset flips correctly across the
// BST/GMT boundary instead of being hardcoded as +0/+1.

const VERSION_RE = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/;

export function formatVersionUK(version) {
  const m = VERSION_RE.exec(version);
  if (!m) return version;
  const [, y, mo, d, h, mi, s] = m;
  return formatUK(new Date(Date.UTC(+y, +mo - 1, +d, +h, +mi, +s)));
}

export function formatTimestampUK(isoString) {
  if (!isoString) return null;
  const d = new Date(isoString);
  if (Number.isNaN(d.getTime())) return isoString;
  return formatUK(d);
}

function formatUK(date) {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "Europe/London",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}
