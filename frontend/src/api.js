const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

// Vector tiles are fetched directly by URL template (not through the JSON
// fetch helpers below) inside a MapLibre worker, which has no page context to
// resolve a relative URL against — unlike `fetch()`, it needs an absolute
// origin. In the Docker build BASE is deliberately "" (same-origin relative
// paths for everything else); fall back to the browser's own origin here.
export const TILE_BASE = BASE || window.location.origin;

// Rows fetched per scroll window. Keep in sync with the grid's paging.
export const CHUNK = 250;

// Max points pulled for the map (reservoir-sampled across the whole result).
export const MAP_LIMIT = 5000;

// Max polygon ids pulled to filter the pre-built tile layer (reservoir-sampled
// beyond this, same idea as MAP_LIMIT). Matches the backend's /api/query cap.
export const POLYGON_ID_LIMIT = 20000;

export async function getDatasets() {
  const r = await fetch(`${BASE}/api/datasets`);
  if (!r.ok) throw new Error("Failed to load datasets");
  return (await r.json()).datasets;
}

export async function getHealth() {
  const r = await fetch(`${BASE}/api/health`);
  if (!r.ok) throw new Error("Failed to reach backend");
  return r.json();
}

export async function generateSql({ prompt }) {
  const r = await fetch(`${BASE}/api/nl-query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(body.detail || "AI query generation failed");
  }
  return (await r.json()).sql;
}

export async function refreshDatasets() {
  const r = await fetch(`${BASE}/api/datasets/refresh`, { method: "POST" });
  if (!r.ok) throw new Error("Failed to refresh datasets");
  return (await r.json()).datasets;
}

export async function runQuery({
  sql,
  offset = 0,
  limit = CHUNK,
  include_total = false,
  sample = false,
}) {
  const r = await fetch(`${BASE}/api/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sql, offset, limit, include_total, sample }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(body.detail || "Query failed");
  }
  return r.json();
}
