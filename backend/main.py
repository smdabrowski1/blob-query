"""
FastAPI wrapper around the DuckDB query engine.

Run:
    uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import db as registry_db
from engine import QueryEngine
from refresh import run_refresh
from registry import DATASETS as DATASET_CONFIG

load_dotenv()  # picks up backend/.env in local dev; a no-op if it's absent

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
TILES_DIR = Path(os.environ.get("TILES_DIR", Path(__file__).parent / "tiles"))
REGISTRY_DB = Path(os.environ.get("REGISTRY_DB", Path(__file__).parent / "registry.db"))

# Natural-language SQL generation is optional: without a key, /api/nl-query
# reports itself unconfigured (503) instead of crashing the app.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
ai_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

app = FastAPI(title="Parquet Query Engine")

# Vite dev server runs on 5173 by default.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = QueryEngine(DATA_DIR, TILES_DIR, REGISTRY_DB)


class QueryRequest(BaseModel):
    sql: str
    offset: int = 0
    limit: int = 200
    include_total: bool = False
    sample: bool = False


class NLQueryRequest(BaseModel):
    prompt: str


class SQLGeneration(BaseModel):
    sql: str


def _schema_prompt() -> str:
    # Column descriptions (registry.db, user-editable via the sidebar) are
    # what actually let this do better than guessing from a bare column
    # name — e.g. knowing `ruc_code` means "rural-urban classification"
    # rather than inferring it from four letters. Kept on separate lines
    # from the compact name/type list rather than inlined as SQL `--`
    # comments per column — mid-line "--" reads as "everything after this
    # is a comment" to a model that's seen a lot of real SQL, which would
    # misleadingly swallow the columns listed after it on the same line.
    lines = []
    for ds in engine.list_datasets():
        cols = ", ".join(f"{c['name']} {c['type']}" for c in ds["columns"])
        lines.append(f"- {ds['name']}({cols})")
        for c in ds["columns"]:
            if c["description"]:
                lines.append(f"    {ds['name']}.{c['name']}: {c['description']}")
    return "\n".join(lines)


@app.get("/api/datasets")
def datasets():
    return {"datasets": engine.list_datasets()}


@app.post("/api/datasets/refresh")
def refresh_all():
    """Re-scan: re-point every view at whatever registry.db currently marks
    as each dataset's current version. Does not fetch/generate anything new
    — that's the per-dataset endpoints below. Kept for "I changed something
    on disk/in the DB directly, reload" use."""
    engine.refresh()
    return {"datasets": engine.list_datasets()}


def _dataset_or_404(name: str) -> dict:
    if name not in DATASET_CONFIG:
        raise HTTPException(status_code=404, detail=f"Unknown dataset '{name}'")
    return DATASET_CONFIG[name]


class ColumnDescriptionRequest(BaseModel):
    description: str


@app.put("/api/datasets/{name}/columns/{column}/description")
def set_column_description(name: str, column: str, req: ColumnDescriptionRequest):
    _dataset_or_404(name)
    known_columns = {c["name"] for ds in engine.list_datasets() if ds["name"] == name for c in ds["columns"]}
    if not known_columns:
        raise HTTPException(status_code=404, detail=f"'{name}' has no active version to have columns yet")
    if column not in known_columns:
        raise HTTPException(status_code=404, detail=f"'{name}' has no column '{column}'")
    con = registry_db.connect(REGISTRY_DB)
    try:
        registry_db.set_column_description(con, name, column, req.description)
    finally:
        con.close()
    return {"datasets": engine.list_datasets()}


@app.post("/api/datasets/{name}/refresh")
def refresh_dataset(name: str):
    _dataset_or_404(name)
    con = registry_db.connect(REGISTRY_DB)
    try:
        registry_db.reap_stale_jobs(con, name)
        if registry_db.has_running_job(con, name):
            raise HTTPException(status_code=409, detail=f"A refresh for '{name}' is already running")
    finally:
        con.close()

    def _job():
        run_refresh(name, data_dir=DATA_DIR, tiles_dir=TILES_DIR, registry_db_path=REGISTRY_DB)
        engine.refresh()  # re-point views at the new current version once the job lands it

    threading.Thread(target=_job, daemon=True).start()
    return {"status": "started"}


def _split_log(raw: str | None) -> list[str]:
    return raw.split("\n") if raw else []


@app.get("/api/datasets/{name}/refresh-status")
def refresh_status(name: str):
    _dataset_or_404(name)
    con = registry_db.connect(REGISTRY_DB)
    try:
        row = registry_db.get_latest_status(con, name)
    finally:
        con.close()
    if row is None:
        return {"status": "idle"}
    return {
        "status": row["status"],
        "version": row["version"],
        "error": row["error"],
        "startedAt": row["created_at"],
        "completedAt": row["completed_at"],
        "progressCurrent": row["progress_current"],
        "progressTotal": row["progress_total"],
        "log": _split_log(row["log"]),
    }


@app.get("/api/refresh-jobs")
def refresh_jobs():
    """Every dataset with a job currently in flight, in one call — lets the
    frontend discover on page load what's still running without polling each
    dataset individually just to find out."""
    con = registry_db.connect(REGISTRY_DB)
    try:
        rows = registry_db.list_running_jobs(con)
    finally:
        con.close()
    return {
        "jobs": [
            {
                "name": r["name"],
                "status": r["status"],
                "version": r["version"],
                "error": r["error"],
                "startedAt": r["created_at"],
                "completedAt": r["completed_at"],
                "progressCurrent": r["progress_current"],
                "progressTotal": r["progress_total"],
                "log": _split_log(r["log"]),
            }
            for r in rows
        ]
    }


@app.get("/api/datasets/{name}/versions")
def dataset_versions(name: str):
    _dataset_or_404(name)
    con = registry_db.connect(REGISTRY_DB)
    try:
        rows = registry_db.list_versions(con, name)
    finally:
        con.close()
    return {"versions": [dict(r) for r in rows]}


class ActivateVersionRequest(BaseModel):
    version: str


@app.post("/api/datasets/{name}/activate")
def activate_dataset_version(name: str, req: ActivateVersionRequest):
    _dataset_or_404(name)
    con = registry_db.connect(REGISTRY_DB)
    try:
        rows = {r["version"]: r for r in registry_db.list_versions(con, name)}
        target = rows.get(req.version)
        if target is None:
            raise HTTPException(status_code=404, detail=f"No version '{req.version}' for '{name}'")
        if target["status"] not in ("active", "superseded"):
            raise HTTPException(status_code=400, detail=f"Version '{req.version}' is not activatable (status: {target['status']})")
        registry_db.activate_version(
            con, name, req.version,
            row_count=target["row_count"],
            parquet_path=target["parquet_path"],
            mbtiles_path=target["mbtiles_path"],
        )
    finally:
        con.close()
    engine.refresh()
    return {"datasets": engine.list_datasets()}


@app.post("/api/query")
def query(req: QueryRequest):
    try:
        return engine.run_query(
            req.sql,
            offset=max(0, req.offset),
            # No upper cap — whatever LIMIT the submitted SQL has (or doesn't
            # have) is what governs the result. The sidebar's default query
            # already bakes in `LIMIT 1000`; a user who explicitly asks for
            # more (or for everything) gets exactly that, not a second,
            # hidden ceiling underneath their own.
            limit=max(1, req.limit),
            include_total=req.include_total,
            sample=req.sample,
        )
    except Exception as e:  # surface the DuckDB error message to the editor
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/nl-query")
def nl_query(req: NLQueryRequest):
    if ai_client is None:
        raise HTTPException(
            status_code=503,
            detail="AI query generation is not configured (set ANTHROPIC_API_KEY in backend/.env)",
        )
    try:
        response = ai_client.messages.parse(
            model=ANTHROPIC_MODEL,
            # Sonnet 5 runs with adaptive thinking on by default, and thinking
            # tokens share this budget with the output — a hard multi-step
            # prompt (window functions, HAVING, date bucketing, ...) can burn
            # through a small budget and get cut off mid-JSON before finishing
            # (surfaces as a pydantic "Invalid JSON: EOF while parsing" error,
            # or parsed_output=None). 16000 gives real headroom for both the
            # thinking and the answer; still a fraction of a cent even if used.
            max_tokens=16000,
            system=(
                "You generate a single DuckDB SELECT query for the user's request.\n\n"
                f"Schema:\n{_schema_prompt()}\n\n"
                "Rules: output one SELECT statement only, no DDL/DML, no comments outside "
                "the SQL, and use only the tables/columns listed above."
            ),
            messages=[{"role": "user", "content": req.prompt}],
            output_format=SQLGeneration,
        )
    except Exception as e:  # bad key, rate limit, network error, etc.
        raise HTTPException(status_code=400, detail=str(e))

    if response.parsed_output is None:
        # Model didn't return schema-valid output — ran out of budget mid-thought,
        # refused, or similar. Surface why rather than crashing on .sql access.
        raise HTTPException(
            status_code=400,
            detail=f"The model didn't return a usable query (stop_reason: {response.stop_reason}). "
            "Try rephrasing or simplifying the request.",
        )
    return {"sql": response.parsed_output.sql}


@app.get("/api/health")
def health():
    return {"ok": True, "datasets": len(engine.list_datasets()), "aiEnabled": ai_client is not None}


# ---- Vector tiles (pre-built .mbtiles, one per tiled dataset) ---------------
# .mbtiles is a SQLite db (tiles table: zoom_level, tile_column, tile_row, gzip
# tile_data blobs). Read directly with stdlib sqlite3 — no tile-server process,
# no new dependency. Registered before the SPA catch-all mount below so /tiles/*
# matches first.
@app.get("/tiles/{name}/{z}/{x}/{y}.pbf")
def tile(name: str, z: int, x: int, y: int):
    mbtiles = engine.mbtiles_path(name)
    if mbtiles is None or not mbtiles.is_file():
        raise HTTPException(status_code=404, detail=f"No tileset for '{name}'")
    tms_y = (1 << z) - 1 - y  # mbtiles uses TMS row order, MapLibre requests XYZ
    con = sqlite3.connect(str(mbtiles))
    try:
        row = con.execute(
            "SELECT tile_data FROM tiles WHERE zoom_level = ? AND tile_column = ? AND tile_row = ?",
            (z, x, tms_y),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="No tile at this coordinate")
    return Response(row[0], media_type="application/x-protobuf", headers={"Content-Encoding": "gzip"})


# ---- Serve the built frontend (only when static assets are present) ---------
# In the Docker image the compiled React app is copied to /app/static and
# STATIC_DIR points at it, so the API and UI share one origin on port 8000.
# In local dev there is no static dir, so this is skipped and Vite serves the
# frontend on :5173 instead. Mounted last so /api/* routes match first.
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "static"))
if STATIC_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="spa")
