# CLAUDE.md

Project context for Claude Code. Read this before making changes.

## What this is

A SQL query workbench over parquet files. **DuckDB** is the query engine,
**FastAPI** serves it, and a **React + Glide Data Grid** frontend renders results
in a windowed grid that scrolls millions of rows without loading them all.

## Layout

- `backend/engine.py` — `QueryEngine`: registers each parquet in `backend/data/`
  as a DuckDB **view** named after the file stem; runs paginated queries.
- `backend/main.py` — FastAPI app: `/api/datasets`, `/api/datasets/refresh`,
  `/api/query`, `/api/nl-query`, `/api/health`, `/tiles/{name}/{z}/{x}/{y}.pbf`.
  CORS is open to the Vite dev origin only.
- `backend/generate_data.py` — generates generic test parquet files.
- `backend/generate_polygons.py` — generates the `land_parcels` polygon dataset
  (parquet + a `.geojson` build intermediate for tippecanoe). See "Polygon
  tiles" below.
- `backend/fetch_uk_districts.py` — pulls the real `uk_districts` boundary
  dataset from an ONS/OS ArcGIS FeatureServer (parquet + `.geojson`, same
  tippecanoe flow). See "Polygon tiles" below.
- `frontend/src/api.js` — fetch client. Exports `CHUNK` (rows per scroll window).
- `frontend/src/components/ResultsGrid.jsx` — Glide grid + lazy chunk cache.
- `frontend/src/components/MapView.jsx` — MapLibre map; renders either a
  reservoir sample of the current query's lat/lon points, or (for a tiled
  polygon dataset) the pre-built vector tiles filtered to the current query's
  matching ids. Uses CARTO's hosted Positron basemap style (requires network
  access; no longer offline). Clicking a point or polygon opens a slide-in
  "identify" panel (`.feature-panel`) with every attribute — see "Map identify"
  below.
- `frontend/src/components/Sidebar.jsx` — dataset/column tree.
- `frontend/src/components/QueryEditor.jsx` — SQL tab (the manual `<textarea>`)
  and the "Ask AI" tab (natural-language → SQL via `/api/nl-query`). See
  "Natural-language SQL" below.
- `frontend/src/App.jsx` — wires editor, sidebar, results; owns query + result-tab state.

## Run it

```bash
# backend
cd backend && pip install -r requirements.txt && uvicorn main:app --reload --port 8000
# frontend (separate terminal)
cd frontend && npm install && npm run dev    # http://localhost:5173
```

Backend must be on :8000 for the frontend's default `VITE_API_BASE`.
`DATA_DIR` env var overrides the parquet folder read by `backend/engine.py`;
`TILES_DIR` (default `backend/tiles/`) overrides where `.mbtiles` are read from.

Regenerate sample parquet with `python generate_data.py --rows N [--name X --seed S]`
(needs `requirements-dev.txt`, which adds numpy/pyarrow/polars — not in the runtime image).

Docker builds the frontend and serves it from FastAPI on one port (no CORS):
`docker compose up --build` → http://localhost:8000. `./backend/data` is bind-mounted
so dropping parquet there and hitting refresh works without a rebuild.

There are no automated tests or linters configured in this repo yet.

### Natural-language SQL

The "Ask AI" tab (`QueryEditor.jsx`) turns a plain-English request into SQL via
the Claude API, called server-side from `POST /api/nl-query` (`main.py`). Setup:

```bash
cd backend
cp .env.example .env   # or just edit the .env already there
# paste an API key (console.anthropic.com/settings/keys) into ANTHROPIC_API_KEY
```

`main.py` loads `backend/.env` via `python-dotenv` at startup. Without a key set,
`ai_client` stays `None`, `/api/health` reports `aiEnabled: false`, and the tab
shows a "not configured" message instead of failing on click — this is a fully
optional feature, not a hard dependency. Model is `ANTHROPIC_MODEL` (default
`claude-sonnet-5`), also read from `.env`.

**Generated SQL always lands in the SQL tab for review — it never auto-runs.**
There's also no extra validation/sandboxing on AI-generated SQL: `run_query`
already wraps *any* submitted SQL (human- or AI-written) as `SELECT * FROM
(<sql>) AS _sub LIMIT ? OFFSET ?`, which structurally rejects anything that
isn't a single SELECT-shaped expression. Don't add a second safety layer here —
it'd be redundant with a gate that already exists for every query path.

`backend/.env` is gitignored (real key stays local) and excluded from the Docker
build context via `.dockerignore` (never baked into an image layer). In Docker,
`docker-compose.yml`'s `env_file: ./backend/.env` reads it into the running
container instead — the file must exist, but an empty one is fine (feature just
stays disabled).

### Polygon tiles

`land_parcels` is a second sample dataset (~50k polygons) rendered as MapLibre
vector tiles instead of the point/circle path. Regenerating it is a two-command,
dev-only flow (not part of `uvicorn`/Docker):

```bash
cd backend
python generate_polygons.py                 # -> data/land_parcels.parquet + .geojson
tippecanoe -o tiles/land_parcels.mbtiles -zg --extend-zooms-if-still-dropping \
    -l land_parcels -f data/land_parcels.geojson
```

`tippecanoe` (install via `brew install tippecanoe` or build from source) is a
local/dev-time tool only — it is **not** installed in the Docker image, the same
way `requirements-dev.txt`'s numpy/pyarrow/polars aren't. `tiles/*.mbtiles` is
committed as a build artifact, like the sample `.parquet` files, so a fresh
clone or `docker compose up` works with no local tippecanoe install.

`uk_districts` is a third tiled dataset — real UK Local Authority District
boundaries (361 districts, Dec 2025, full BFE resolution) pulled once from the
ONS/OS ArcGIS FeatureServer, not fetched live per-request. Contains both
Ordnance Survey and ONS Intellectual Property Rights. Same two-command flow:

```bash
cd backend
python fetch_uk_districts.py                # -> data/uk_districts.parquet + .geojson
tippecanoe -o tiles/uk_districts.mbtiles -zg --extend-zooms-if-still-dropping \
    -l uk_districts -f data/uk_districts.geojson
```

`fetch_uk_districts.py` needs `requests` (in `requirements-dev.txt`). It paginates
the FeatureServer at **25 records per request** — the layer's `maxRecordCount` is
2000, but that's a record-count cap, not a payload-size one: full BFE geometry
for this many districts in one big request is enough to hit Esri's own ~60s
gateway timeout (confirmed: 500 records → 504; 25 reliably completes in
single-digit seconds). Don't raise `PAGE_SIZE` without re-testing against the
live service. `id` is a synthetic sequential integer (not the real `LAD25CD`
code) — `MapView.jsx`'s bounds query builds a bare `IN (${ids.join(",")})` SQL
list, which only works unquoted for numeric ids; the real code lives in the
separate `lad_code` column. Some districts are `MultiPolygon` (island/exclave
authorities) — `fetch_uk_districts.py`'s WKT writer handles both, unlike
`generate_polygons.py`'s (single-ring `Polygon` only, since it's synthetic data).

`msoa` (`fetch_msoa.py`) is a fourth tiled dataset — England & Wales Middle
Super Output Area boundaries (7,264 of them, same ONS ArcGIS org, layer index
**3** not 0). Same shape as `fetch_uk_districts.py` but pages fetched
concurrently (`ThreadPoolExecutor`, 5 workers) since 7,264 records at a safe
page size would take 6-7 minutes sequentially — this is I/O-bound waiting on
the ArcGIS server, not CPU-bound, so threads are the right tool, not
multiprocessing. Page size (150) was picked empirically per-endpoint, same as
districts' 25 — don't assume one page size transfers to a new ONS dataset
without testing it first (`maxRecordCount` is a record-count cap, not a
payload-size one, and heavier full-BFE geometry can still time out under it).

**Known gap:** `loadPolygonLayer`'s bounds-fit query selects `centroid_lat`/
`centroid_lon` — columns `land_parcels` has but `uk_districts`/`msoa` don't. It
fails silently (wrapped in a "framing is a nicety" try/catch), so selecting
their results doesn't auto-zoom to fit them; the map stays at whatever view it
had. Not yet fixed — either add centroid columns to those fetch scripts'
parquet, or compute bounds a different way.

### Map identify

Clicking a point or polygon on the map opens a slide-in panel
(`.feature-panel` in `MapView.jsx`) listing every attribute — not just a
truncated popup. Points already carry their full row as GeoJSON properties
(built client-side from the query result), so identify is free. Polygons only
carry the handful of properties baked into the tile at generation time (e.g.
`id`/`lad_code`/`name`), so a click fetches the rest — but by re-running
**the query's own SQL**, wrapped and filtered to the clicked id
(`SELECT * FROM (${innerSql}) AS _sub WHERE id = <id>`), not `SELECT * FROM
"<name>" WHERE id = <id>` against the raw table. That distinction matters as
soon as the query aliases/renames columns (e.g. a join like `SELECT d.name AS
la_name, m.name AS msoa_name FROM msoa m JOIN uk_districts d ON
ST_Intersects(...)`) — the panel shows `la_name`/`msoa_name`, matching what
you actually selected, not `msoa`'s base schema. The raw `geometry` WKT column
is still filtered out of the display (generation-time convention, not schema
introspection). One identify mechanism for both geometry types — no separate
`maplibregl.Popup` code path anymore.

Polygon borders are a separate `line` layer (`polygons-outline`), not
`fill-outline-color` — that paint property has no width control, so a visible
border needs its own layer. Selecting a polygon calls `map.setPaintProperty`
on both `polygons-fill` and `polygons-outline` with a fresh `["case", ["==",
["get","id"], selectedId], ...]` expression — comparing each feature's own
`id` (straight from the tile) against the clicked id, no `feature-state`/
`promoteId` needed. This drives two things at once: the selected polygon gets
a thicker amber border, and — since a plain thicker border alone was getting
visually confused with basemap linework like rivers — every *other* polygon's
fill drops to 10% opacity, so the selection reads clearly regardless of border
styling. `MapView.jsx` exposes this as a `.clear()` on a ref (`highlightRef`,
not React state — it drives MapLibre paint directly) attached once the
polygon layer loads; the background-click-to-close handler and the panel's ×
button both call it, so the dim/highlight and the identify panel always
open/close in lockstep.

### Query / Full Dataset toggle

`engine.run_query` reports `baseTable`: the single known dataset name a query's
SQL references as a FROM/JOIN target, or `null` for joins/subqueries spanning
more than one known table (or none at all) — same name-based approach as
`_detect_polygon_source`, generalized via `_find_referenced_table`. When
`baseTable` is set, the results header shows a "Results / Full Dataset"
segmented control (`App.jsx`); switching to "Full Dataset" runs and caches
`SELECT * FROM "<baseTable>"` as a second result set, so toggling back and
forth after the first fetch is instant. **The SQL editor's text is never
touched by this** — it's a display-only swap of which cached result feeds
`ResultsGrid`/`MapView`, not a re-run of the user's query.

This dataset needed zero changes anywhere else in the stack (`engine.py`,
`main.py`, `MapView.jsx`) — the polygon-tile machinery was already generic
over dataset name, which was the point of not hardcoding `land_parcels`.

## How querying works

- Datasets are queried by view name, e.g. `SELECT * FROM sales_events`.
- `run_query` wraps the user's SQL: `SELECT * FROM (<sql>) LIMIT ? OFFSET ?`, and
  computes `count(*)` only when `include_total=true` (first page of a new query).
- Rows are returned as arrays-of-values (column order matches `columns`), with
  datetimes/Decimals coerced to JSON-safe types in `engine._json_safe`.

## Conventions & gotchas

- **`CHUNK` must match** between `api.js` and any paging logic. The grid fetches
  one 250-row window per visible region and caches it by chunk index.
- DuckDB **cannot bind parameters in DDL** (`CREATE VIEW`), so the parquet path is
  inlined with `'`-escaping in `engine.refresh()`. Keep it that way; don't switch
  view creation to bound params.
- Errors from DuckDB surface to the editor as HTTP 400 with the message intact —
  preserve that behaviour when touching `main.query`.
- **Geometry / map:** `_detect_geometry` flags a result as mappable when its
  columns include a lat (`lat`/`latitude`) and lon (`lon`/`lng`/`long`/`longitude`)
  pair; the response carries `geometry: {lat, lon} | null`. The frontend shows the
  Map tab only when `geometry` is non-null. The map fetches points with
  `sample: true` (DuckDB `USING SAMPLE reservoir(N ROWS)`) so a huge scan plots an
  even spread, capped at `MAP_LIMIT`. Detection is name-based on purpose — a real
  `GEOMETRY`/WKB column would need the DuckDB `spatial` extension and a WKB→GeoJSON
  step; add that here, not in the frontend.
- **Polygon tiles:** `TILES_DIR` (default `backend/tiles/`) holds pre-built
  `.mbtiles` per dataset (SQLite; `tiles` table keyed by `zoom_level`/`tile_column`/
  `tile_row`). `main.tile()` reads tiles directly with stdlib `sqlite3` — no tile
  server process, no new dependency. `/api/datasets` exposes `hasTiles`; a query
  result carries `polygonSource: "<name>" | null`, set by `engine._detect_polygon_source`
  when the result still has an `id` column *and* the dataset is the query's
  **primary FROM table** (`_primary_from_table` — just the first FROM target,
  ignoring JOINs). Deliberately not "any tiled name anywhere in the SQL": a
  spatial join like `FROM msoa m JOIN uk_districts d ON ST_Intersects(...)` is
  still an msoa-shaped result even though `uk_districts` is also tiled — it's
  only there to filter. `baseTable` (the Query/Full Dataset toggle's field)
  keeps the stricter "exactly one tiled-or-not table, anywhere" rule via
  `_find_referenced_table` — offering a "full table" peek is genuinely
  ambiguous for a real multi-table join in a way "which tiles do I filter"
  isn't. The map never re-tiles: geometry is always the pre-built version, and
  a query only changes *which* ids are visible (`MapView`'s `loadPolygonLayer`
  re-queries `SELECT id FROM (<sql>)` and applies a MapLibre `filter`). A query
  that drops `id` from its projection can't be filtered to — that's an
  expected, surfaced-in-status limit, not a bug to fix by parsing SQL further.
- **Spatial extension:** DuckDB's `spatial` extension (`ST_Intersects`,
  `ST_GeomFromText`, etc.) is installed/loaded once at `QueryEngine.__init__`
  against the WKT `geometry` columns the polygon datasets already store —
  best-effort (wrapped in try/except; an offline first boot just runs without
  spatial functions rather than crashing the whole app).
- **Concurrency:** `main.py`'s routes are sync functions, which FastAPI runs
  on a thread pool — but a single shared `duckdb.Connection` isn't safe for
  concurrent `execute()` calls from multiple threads (two requests racing on
  it corrupted cursor state, surfacing as `fetchone()` returning `None` and
  the frontend misreporting "can't reach the backend" for what was actually a
  crashed request, not a dead server). `list_datasets()`, `run_query()`, and
  `refresh()` each grab `self.con.cursor()` at the top rather than using
  `self.con` directly — cheap, and the fix DuckDB's own docs recommend for
  this exact multi-threaded-single-connection pattern.
- Frontend uses **no browser storage** and plain fetch; keep it dependency-light.
  The editor is a `<textarea>` by design (CodeMirror is a deliberate future step).
- Backend is storage-agnostic. To go remote, only view registration in
  `engine.py` changes (load `azure`/`httpfs`, register `read_parquet('az://…')`
  or `'s3://…'` globs) — the frontend is untouched.

## Deferred / good next tasks

- CodeMirror 6 editor with `@codemirror/lang-sql` + schema-driven completion.
- Cache the per-query total; move deep `OFFSET` to a keyset/cursor scheme.
- `/api/export` streaming parquet/CSV via `COPY (<sql>) TO …`.
- Azure Blob / S3 view registration (single-file views vs partitioned globs —
  decide which before building the sidebar tree for partitioned datasets).
