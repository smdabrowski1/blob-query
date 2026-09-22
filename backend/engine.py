"""
DuckDB-backed query engine.

Each parquet file in the data directory is registered as a view named after the
file stem, so the frontend can query datasets by name exactly like tables in a
SQL editor:

    SELECT region, sum(revenue) AS total
    FROM sales_events
    GROUP BY region
    ORDER BY total DESC

The engine is deliberately storage-agnostic. Point DATA_DIR at a local folder
for development; see `register_remote_source` / the README for pointing DuckDB
at Azure Blob (az://) or S3 (s3://) with no other code changes.
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

import db as registry_db
from registry import DATASETS as DATASET_CONFIG


def _sanitize(name: str) -> str:
    """Turn a filename stem into a safe SQL identifier."""
    ident = re.sub(r"\W+", "_", name).strip("_")
    if not ident:
        ident = "dataset"
    if ident[0].isdigit():
        ident = f"ds_{ident}"
    return ident


def _json_safe(v: Any) -> Any:
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat(sep=" ")
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v).hex()
    return v


# Column names that identify a mappable (point) result.
_LAT_NAMES = ("lat", "latitude")
_LON_NAMES = ("lon", "lng", "long", "longitude")


def _detect_geometry(columns: list[dict]) -> dict | None:
    """Return {'lat': <col>, 'lon': <col>} if the result has coordinate columns."""
    lower = {c["name"].lower(): c["name"] for c in columns}
    lat = next((lower[n] for n in _LAT_NAMES if n in lower), None)
    lon = next((lower[n] for n in _LON_NAMES if n in lower), None)
    return {"lat": lat, "lon": lon} if lat and lon else None


def _find_referenced_table(sql: str, names) -> str | None:
    """Return the one name in `names` that appears as a FROM/JOIN target in
    `sql` — or None if zero or more-than-one do (a join/subquery spanning
    multiple known tables is ambiguous, not "pick the first match"). Name-based,
    like _detect_geometry above — not a SQL parser."""
    matches = [
        name
        for name in names
        if re.search(rf"\b(from|join)\s+\"?{re.escape(name)}\"?\b", sql, re.IGNORECASE)
    ]
    return matches[0] if len(matches) == 1 else None


def _primary_from_table(sql: str) -> str | None:
    """The first FROM target in `sql` — the "driving" table whose shape the
    result usually takes, even when other tables are JOINed in purely to
    filter (e.g. a spatial join: `FROM msoa m JOIN uk_districts d ON
    ST_Intersects(...)` is still an msoa-shaped result). Name-based, like
    _detect_geometry above — not a SQL parser, so a subquery/CTE as the
    primary FROM target just won't match anything, same as before."""
    m = re.search(r'\bfrom\s+"?(\w+)"?', sql, re.IGNORECASE)
    return m.group(1) if m else None


def _detect_polygon_source(sql: str, columns: list[dict], tiled_names: set[str]) -> str | None:
    """Return the tiled dataset name a query targets, if any.

    A query "targets" a tiled dataset when its result still carries an `id`
    column (so the frontend can filter the pre-built tiles by id) and the
    dataset is the query's primary FROM table — not just any table it
    happens to JOIN against (a spatial join used only to filter shouldn't
    make an otherwise-clear result "ambiguous").
    """
    if not tiled_names:
        return None
    if not any(c["name"].lower() == "id" for c in columns):
        return None
    primary = _primary_from_table(sql)
    return primary if primary in tiled_names else None


class QueryEngine:
    def __init__(
        self,
        data_dir: str | Path,
        tiles_dir: str | Path | None = None,
        registry_db_path: str | Path | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.tiles_dir = Path(tiles_dir) if tiles_dir else None
        self.registry_db_path = Path(registry_db_path) if registry_db_path else Path(__file__).parent / "registry.db"
        self.con = duckdb.connect(database=":memory:")
        # Handy defaults; httpfs/azure loaded lazily only if a remote path is used.
        self.con.execute("SET enable_progress_bar = false;")
        # Spatial extension: ST_Intersects, ST_Distance, etc. against the WKT
        # `geometry` columns land_parcels/uk_districts/msoa already store —
        # DuckDB doesn't ship this loaded by default. INSTALL fetches it once
        # per environment (cached under ~/.duckdb after that, so this is only
        # a real network call on a cold container/machine); LOAD attaches it
        # to this connection every time. Best-effort: an offline first boot
        # shouldn't take the whole app down over one missing extension — the
        # rest of the app (and every non-spatial query) works fine without it.
        try:
            self.con.execute("INSTALL spatial; LOAD spatial;")
        except Exception as e:
            print(f"Spatial extension unavailable ({e}) — ST_* functions won't work until this can install.")
        self._views: dict[str, Path] = {}
        self._mbtiles: dict[str, Path] = {}
        self._tile_zoom_cache: dict[str, tuple[int, int]] = {}
        self.refresh()

    # ---- dataset registration -------------------------------------------------
    def refresh(self) -> None:
        """(Re)register the *current* version of every dataset in the
        registry (registry.py's static config + registry.db's per-version
        metadata) as a DuckDB view. A dataset with no active version yet
        (never fetched/generated) is simply absent, same as a missing flat
        file used to be before versioning existed."""
        cur = self.con.cursor()
        self._views.clear()
        self._mbtiles.clear()
        self._tile_zoom_cache.clear()

        con = registry_db.connect(self.registry_db_path)
        try:
            for name in DATASET_CONFIG:
                row = registry_db.get_current(con, name)
                if row is None or not row["parquet_path"]:
                    continue
                parquet_path = Path(row["parquet_path"])
                if not parquet_path.is_file():
                    continue
                view = _sanitize(name)
                safe_path = str(parquet_path).replace("'", "''")
                cur.execute(
                    f'CREATE OR REPLACE VIEW "{view}" AS '
                    f"SELECT * FROM read_parquet('{safe_path}')"
                )
                self._views[view] = parquet_path
                if row["mbtiles_path"] and Path(row["mbtiles_path"]).is_file():
                    self._mbtiles[view] = Path(row["mbtiles_path"])
        finally:
            con.close()

    def _has_tiles(self, view: str) -> bool:
        return view in self._mbtiles

    def mbtiles_path(self, view: str) -> Path | None:
        return self._mbtiles.get(view)

    def tiled_dataset_names(self) -> set[str]:
        return set(self._mbtiles.keys())

    def tile_zoom_range(self, view: str) -> tuple[int, int] | None:
        """(minzoom, maxzoom) actually present in the current version's
        .mbtiles, from its metadata table — tippecanoe's -zg picks these
        automatically per run, so this must be read rather than assumed;
        otherwise the frontend would request zooms beyond what's tiled and
        get 404s instead of MapLibre's normal overzoom-from-maxzoom behaviour."""
        if view in self._tile_zoom_cache:
            return self._tile_zoom_cache[view]
        mbtiles = self._mbtiles.get(view)
        if mbtiles is None:
            return None
        con = sqlite3.connect(str(mbtiles))
        try:
            rows = dict(
                con.execute(
                    "SELECT name, value FROM metadata WHERE name IN ('minzoom', 'maxzoom')"
                ).fetchall()
            )
        finally:
            con.close()
        if "minzoom" not in rows or "maxzoom" not in rows:
            return None
        zoom = (int(rows["minzoom"]), int(rows["maxzoom"]))
        self._tile_zoom_cache[view] = zoom
        return zoom

    def list_datasets(self) -> list[dict]:
        # A fresh cursor per call, not self.con directly: FastAPI runs sync
        # routes on a thread pool, and DuckDB connections aren't safe for
        # concurrent execute() calls from multiple threads — two requests
        # racing on the shared connection was corrupting cursor state and
        # surfacing as `fetchone()` returning None (backend looked "down" to
        # the frontend when it was actually a 500 from this race).
        cur = self.con.cursor()
        reg_con = registry_db.connect(self.registry_db_path)
        try:
            out = []
            for view, path in self._views.items():
                descriptions = registry_db.get_column_descriptions(reg_con, view)
                cols = [
                    {"name": r[0], "type": r[1], "description": descriptions.get(r[0], "")}
                    for r in cur.execute(f'DESCRIBE "{view}"').fetchall()
                ]
                rows = cur.execute(f'SELECT count(*) FROM "{view}"').fetchone()[0]
                out.append(
                    {
                        "name": view,
                        "file": path.name,
                        "rows": rows,
                        "columns": cols,
                        "hasTiles": self._has_tiles(view),
                        "tileZoom": self.tile_zoom_range(view),
                    }
                )
        finally:
            reg_con.close()
        return out

    # ---- querying -------------------------------------------------------------
    def run_query(
        self,
        sql: str,
        offset: int = 0,
        limit: int = 200,
        include_total: bool = False,
        sample: bool = False,
    ) -> dict:
        sql = sql.strip().rstrip(";")
        if not sql:
            raise ValueError("Empty query.")

        # Fresh cursor per call — see the comment in list_datasets(); the
        # shared connection isn't safe across FastAPI's threadpool-concurrent
        # requests otherwise.
        cur = self.con.cursor()
        total = None
        if sample:
            # Representative random spread across the whole result — used by the
            # map so a million-row scan plots an even sample, not the first N rows.
            cur = cur.execute(
                f"SELECT * FROM ({sql}) AS _sub USING SAMPLE reservoir({int(limit)} ROWS)"
            )
        else:
            if include_total:
                total = cur.execute(
                    f"SELECT count(*) FROM ({sql}) AS _sub"
                ).fetchone()[0]
            cur = cur.execute(
                f"SELECT * FROM ({sql}) AS _sub LIMIT ? OFFSET ?", [limit, offset]
            )

        columns = [{"name": d[0], "type": str(d[1])} for d in cur.description]
        raw = cur.fetchall()
        rows = [[_json_safe(v) for v in row] for row in raw]
        polygon_source = _detect_polygon_source(sql, columns, self.tiled_dataset_names())
        return {
            "columns": columns,
            "rows": rows,
            "total": total,
            "geometry": _detect_geometry(columns),
            "polygonSource": polygon_source,
            "polygonZoom": self.tile_zoom_range(polygon_source) if polygon_source else None,
            # Which known dataset (if exactly one, unambiguously) this query
            # targets — lets the frontend offer a "full table" peek alongside
            # the query's own result. None for joins/subqueries spanning
            # multiple known tables, or SQL that names none of them.
            "baseTable": _find_referenced_table(sql, self._views.keys()),
        }


if __name__ == "__main__":
    # Quick self-test against the local data dir.
    eng = QueryEngine(Path(__file__).parent / "data")
    print("Datasets:")
    for d in eng.list_datasets():
        print(f"  {d['name']:<20} {d['rows']:>10,} rows  ({d['file']})")

    print("\nAggregate query:")
    res = eng.run_query(
        "SELECT region, count(*) AS n, round(sum(revenue),2) AS revenue "
        "FROM sales_events GROUP BY region ORDER BY revenue DESC",
        include_total=True,
    )
    print("  columns:", [c["name"] for c in res["columns"]])
    for r in res["rows"]:
        print("  ", r)
    print("  total groups:", res["total"])

    print("\nPagination check (rows 500000-500003 of raw scan):")
    res = eng.run_query("SELECT event_id, region, revenue FROM sales_events ORDER BY event_id",
                        offset=500_000, limit=4)
    for r in res["rows"]:
        print("  ", r)
