const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

// Vector tiles are fetched directly by URL template (not through the JSON
// fetch helpers below) inside a MapLibre worker, which has no page context to
// resolve a relative URL against — unlike `fetch()`, it needs an absolute
// origin. In the Docker build BASE is deliberately "" (same-origin relative
// paths for everything else); fall back to the browser's own origin here.
export const TILE_BASE = BASE || window.location.origin;

// Rows fetched per scroll window. Keep in sync with the grid's paging.
export const CHUNK = 250;

// The API has no "no limit" value, so this is a sentinel well above any
// dataset in the app (largest today: sales_events, 1M rows) — the query's
// own LIMIT is what actually governs the result.
export const MAP_LIMIT = 10_000_000;

// Same idea as MAP_LIMIT, for the polygon id-filter fetch.
export const POLYGON_ID_LIMIT = 10_000_000;

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

// Multi-turn discovery chat — distinct from generateSql above (one prompt ->
// one SQL statement). Stateless server-side, same as generateSql: the full
// conversation (messages: [{role, content}, ...]) is sent every call, and
// the caller (App.jsx) is what accumulates history in React state.
export async function discoverChat(messages) {
  const r = await fetch(`${BASE}/api/discover-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ messages }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(body.detail || "Discovery chat failed");
  }
  return r.json(); // { reply, suggestions: [{type, label, datasets, sql}] }
}

export async function refreshDatasets() {
  const r = await fetch(`${BASE}/api/datasets/refresh`, { method: "POST" });
  if (!r.ok) throw new Error("Failed to refresh datasets");
  return (await r.json()).datasets;
}

// ---- Per-dataset refresh/versioning ----------------------------------------
// Triggers a real re-fetch/re-generate (+ re-tile for polygon datasets),
// not just a re-scan of what's already on disk (that's refreshDatasets()
// above). Runs as a background job on the server; poll getRefreshStatus.

export async function refreshDataset(name) {
  const r = await fetch(`${BASE}/api/datasets/${name}/refresh`, { method: "POST" });
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(body.detail || "Refresh failed to start");
  }
  return r.json();
}

export async function getRefreshStatus(name) {
  const r = await fetch(`${BASE}/api/datasets/${name}/refresh-status`);
  if (!r.ok) throw new Error("Failed to get refresh status");
  return r.json();
}

// Every dataset with a job currently running, in one call — used on app
// mount to discover in-flight jobs without polling each dataset by hand.
export async function getRefreshJobs() {
  const r = await fetch(`${BASE}/api/refresh-jobs`);
  if (!r.ok) throw new Error("Failed to load refresh jobs");
  return (await r.json()).jobs;
}

// Column descriptions are keyed by (dataset, column) only — not by version —
// so they persist across refreshes automatically; see backend/db.py.
export async function setColumnDescription(name, column, description) {
  const r = await fetch(`${BASE}/api/datasets/${name}/columns/${column}/description`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ description }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(body.detail || "Failed to save description");
  }
  return (await r.json()).datasets;
}

export async function getVersions(name) {
  const r = await fetch(`${BASE}/api/datasets/${name}/versions`);
  if (!r.ok) throw new Error("Failed to load versions");
  return (await r.json()).versions;
}

export async function activateVersion(name, version) {
  const r = await fetch(`${BASE}/api/datasets/${name}/activate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ version }),
  });
  if (!r.ok) {
    const body = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(body.detail || "Failed to activate version");
  }
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
