# Parquet Query Engine

**[Live demo](https://blobquery.sebdabrowski.com)** · **[Walkthrough](https://sebdabrowski.com/blobquery)**

A minimal SQL query workbench over parquet files. **DuckDB** does the querying,
**FastAPI** serves it, and a **React + Glide Data Grid** frontend renders results
with a windowed grid that scrolls millions of rows without loading them all.

Since the initial version, this has grown AI-assisted natural-language SQL
and a discovery chat, dataset refresh/versioning against live UK government
geography APIs, and map choropleths — see `CHANGELOG.md` for the history and
`CLAUDE.md` for how it all fits together. `CLAUDE.md` also has a `Security`
section worth reading before pointing this at anything other than localhost:
no authentication, by design, for a local single-user demo.

Each parquet file in `backend/data/` is auto-registered as a queryable view named
after the file, so you write `SELECT * FROM sales_events` just like a table in SSMS.

```
parquet-query-engine/
├── backend/
│   ├── engine.py          # DuckDB engine: registers views, runs paginated queries
│   ├── main.py            # FastAPI app (/api/datasets, /api/query)
│   ├── generate_data.py   # makes generic test parquet files
│   ├── requirements.txt
│   └── data/              # drop .parquet files here (two samples included)
└── frontend/
    ├── src/
    │   ├── App.jsx
    │   ├── api.js
    │   └── components/
    │       ├── Sidebar.jsx      # dataset + column tree
    │       ├── QueryEditor.jsx  # SQL editor (Cmd/Ctrl+Enter to run)
    │       └── ResultsGrid.jsx  # Glide grid with lazy chunk loading
    └── package.json
```

## Quickstart

### 1. Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Two sample datasets are already in backend/data. To (re)generate:
python generate_data.py --rows 1000000              # sales_events.parquet
python generate_data.py --rows 120000 --name inventory_snapshot --seed 7

uvicorn main:app --reload --port 8000
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

Open the app, click a dataset on the left, or hit **Run** on the default query.

## Run with Docker

One container builds the React app and serves it alongside the API from FastAPI,
so the whole thing runs on a single port with no CORS to think about.

```bash
docker compose up --build
# open http://localhost:8000
```

Or without compose:

```bash
docker build -t parquet-query-engine .
docker run --rm -p 8000:8000 parquet-query-engine
```

The sample parquet files are baked into the image, so it works out of the box.
To query your own data instead, mount a folder over `/app/data` (compose already
mounts `./backend/data`):

```bash
docker run --rm -p 8000:8000 -v "$(pwd)/backend/data:/app/data" parquet-query-engine
```

How it fits together: a multi-stage build compiles the frontend with
`VITE_API_BASE=""` (so the UI calls `/api/*` on its own origin), then copies the
`dist/` output into the Python image at `/app/static`. `main.py` mounts that
directory only when it exists, so local dev (Vite on :5173) is unaffected.

## Adding your own data

Drop any `.parquet` file into `backend/data/` and click the ⟳ button in the
sidebar (or `POST /api/datasets/refresh`). The filename becomes the view name.

To point the data folder somewhere else without moving files:

```bash
DATA_DIR=/path/to/parquet uvicorn main:app --port 8000
```

## Moving to Azure Blob / S3 later

DuckDB reads remote parquet natively — only `engine.py`'s view registration
changes, nothing in the frontend. Load the extension once and register a glob:

```python
# Azure
con.execute("INSTALL azure; LOAD azure;")
con.execute("SET azure_storage_connection_string = '<conn-string>';")
con.execute("CREATE VIEW sales_events AS "
            "SELECT * FROM read_parquet('az://container/sales/*.parquet');")

# S3
con.execute("INSTALL httpfs; LOAD httpfs;")
con.execute("SET s3_region='eu-west-1';")
con.execute("SET s3_access_key_id='...'; SET s3_secret_access_key='...';")
con.execute("CREATE VIEW sales_events AS "
            "SELECT * FROM read_parquet('s3://bucket/sales/*.parquet');")
```

The `read_parquet(glob)` pattern also lets one view span many partitioned files.

## How the million-row scroll works

The grid never holds the whole result set. On query, the backend returns the
first window plus a `count(*)` total, which sets the grid's row count. As you
scroll, `ResultsGrid` watches the visible region and fetches only the row
windows on screen (250 rows per chunk) via `SELECT * FROM (<your sql>) LIMIT ? OFFSET ?`,
caching each chunk. DuckDB serves deep offsets over parquet in single-digit ms.

## Sensible next steps

- **Editor:** swap the `<textarea>` for CodeMirror 6 with `@codemirror/lang-sql`
  for syntax highlighting and completion driven by the dataset schema.
- **Total count:** caching it per query (rather than recomputing on the first
  page) matters once queries get expensive; a keyset/cursor scheme beats large
  `OFFSET`s on huge ordered scans.
- **Polars:** the same parquet files load via `pl.scan_parquet(...)` if you want
  a DataFrame path alongside SQL for transforms.
- **Export:** add a `/api/export` that streams the full result as parquet/CSV
  straight from DuckDB (`COPY (<sql>) TO 'out.parquet'`).
