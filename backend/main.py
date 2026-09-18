"""
FastAPI wrapper around the DuckDB query engine.

Run:
    uvicorn main:app --reload --port 8000
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from engine import QueryEngine

load_dotenv()  # picks up backend/.env in local dev; a no-op if it's absent

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
TILES_DIR = Path(os.environ.get("TILES_DIR", Path(__file__).parent / "tiles"))

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

engine = QueryEngine(DATA_DIR, TILES_DIR)


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
    lines = []
    for ds in engine.list_datasets():
        cols = ", ".join(f"{c['name']} {c['type']}" for c in ds["columns"])
        lines.append(f"- {ds['name']}({cols})")
    return "\n".join(lines)


@app.get("/api/datasets")
def datasets():
    return {"datasets": engine.list_datasets()}


@app.post("/api/datasets/refresh")
def refresh():
    engine.refresh()
    return {"datasets": engine.list_datasets()}


@app.post("/api/query")
def query(req: QueryRequest):
    try:
        return engine.run_query(
            req.sql,
            offset=max(0, req.offset),
            limit=max(1, min(req.limit, 20000)),
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
    mbtiles = TILES_DIR / f"{name}.mbtiles"
    if not mbtiles.is_file():
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
