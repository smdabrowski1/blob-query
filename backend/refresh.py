"""
Generic refresh orchestrator. Looks up a dataset in registry.py, dispatches
to the right builder for its `kind`, writes everything to `.tmp-*` names,
validates what was written, and only then makes it live — see the
module-level docstring in db.py for why the "make it live" step is a single
SQLite transaction rather than a hand-rolled file-pointer dance.

A refresh that fails or is interrupted at any point before the final
`db.activate_version()` call leaves the previously-active version completely
untouched — nothing before that call is ever read by the running app.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

import pyarrow.parquet as pq

import db
import generate_data
import generate_polygons
from registry import DATASETS
from sources import arcgis

DATA_DIR = Path(__file__).parent / "data"
TILES_DIR = Path(__file__).parent / "tiles"
REGISTRY_DB = Path(__file__).parent / "registry.db"

_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")


def _new_version() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _throttled(fn, min_interval=1.0):
    """Wrap a progress callback so it fires at most once per min_interval
    seconds — msoa's 49 concurrent page fetches (or tippecanoe's rapid
    percentage updates) would otherwise hit SQLite with near-simultaneous
    writes for a value that's purely informational anyway."""
    last = [0.0]

    def wrapped(current, total):
        now = time.monotonic()
        if now - last[0] >= min_interval or current >= total:
            last[0] = now
            fn(current, total)

    return wrapped


def _run_tippecanoe(cmd: list[str], on_progress) -> None:
    """Runs tippecanoe as a streaming subprocess instead of run-and-wait, so
    its progress meter (single-line, carriage-return-updated output like
    ` 50.0%  7/64/41` — not one line per update) can be parsed as it happens.
    Stdout+stderr are merged: it isn't certain which stream the meter uses,
    and merging sidesteps the question. Reads one character at a time to
    correctly split on '\\r' as well as '\\n'."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    buf = ""
    tail = ""  # last ~2000 chars seen, for the error message on failure
    try:
        while True:
            ch = proc.stdout.read(1)
            if ch == "" and proc.poll() is not None:
                break
            if ch in ("\r", "\n"):
                if buf:
                    tail = (tail + buf + "\n")[-2000:]
                    m = _PERCENT_RE.search(buf)
                    if m:
                        on_progress(float(m.group(1)), 100)
                buf = ""
            else:
                buf += ch
    finally:
        proc.stdout.close()
    returncode = proc.wait()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output=tail)


def _clear_stale_tmp(dataset_dir: Path, tiles_dataset_dir: Path) -> None:
    for d in (dataset_dir, tiles_dataset_dir):
        if d.exists():
            for f in d.glob(".tmp-*"):
                f.unlink()


def _build(config: dict, on_progress=None):
    """Dispatch on kind. Each builder has a genuinely different return shape
    (they predate this orchestrator and are proven working) — normalized
    here rather than forced into a fake common interface. Returns
    (pyarrow.Table, geojson_write_fn | None), where geojson_write_fn(path)
    writes the tippecanoe input if this is a polygon dataset.

    on_progress is only meaningful for kind == "arcgis" — the generated
    kinds run entirely in-process with no long-running I/O to report on."""
    kind = config["kind"]
    if kind == "arcgis":
        table, geojson_dict = arcgis.fetch_arcgis_dataset(config, on_progress=on_progress)
        return table, lambda path: path.write_text(json.dumps(geojson_dict))
    if kind == "generated_polygons":
        table, poly_lon, poly_lat, ids, region, category, area_sqkm = generate_polygons.build_polygons(
            config["count"], config["vertices"], seed=config["seed"]
        )
        return table, lambda path: generate_polygons.write_geojson(
            path, poly_lon, poly_lat, ids, region, category, area_sqkm
        )
    if kind == "generated_table":
        table = generate_data.build_table(config["rows"], seed=config["seed"], geo=config["geo"])
        return table, None
    raise ValueError(f"Unknown dataset kind: {kind!r}")


def _validate_parquet(path: Path) -> int:
    """Returns row count. Raises if the file is unreadable or empty —
    catches a truncated/corrupt write from a failed job."""
    pf = pq.ParquetFile(path)
    row_count = pf.metadata.num_rows
    if row_count <= 0:
        raise RuntimeError(f"{path.name} has no rows")
    return row_count


def _validate_mbtiles(path: Path) -> None:
    con = sqlite3.connect(str(path))
    try:
        count = con.execute("SELECT count(*) FROM tiles").fetchone()[0]
    finally:
        con.close()
    if count <= 0:
        raise RuntimeError(f"{path.name} has no tiles")


def run_refresh(
    name: str,
    data_dir: Path | None = None,
    tiles_dir: Path | None = None,
    registry_db_path: Path | None = None,
) -> None:
    """data_dir/tiles_dir/registry_db_path default to this module's constants
    (fine for standalone/CLI use) but should be passed explicitly by callers
    that already have their own configured paths (main.py reads these from
    DATA_DIR/TILES_DIR/REGISTRY_DB env vars — a refresh job must land in the
    same place the running app is actually reading from, not silently fall
    back to a different default)."""
    if name not in DATASETS:
        raise ValueError(f"Unknown dataset: {name!r}")
    config = DATASETS[name]
    is_polygon = config["polygon"]

    data_dir = data_dir or DATA_DIR
    tiles_dir = tiles_dir or TILES_DIR
    registry_db_path = registry_db_path or REGISTRY_DB

    dataset_dir = data_dir / name
    tiles_dataset_dir = tiles_dir / name
    dataset_dir.mkdir(parents=True, exist_ok=True)
    if is_polygon:
        tiles_dataset_dir.mkdir(parents=True, exist_ok=True)

    con = db.connect(registry_db_path)
    db.reap_stale_jobs(con, name)
    _clear_stale_tmp(dataset_dir, tiles_dataset_dir)

    version = _new_version()
    db.start_job(con, name, version, has_geometry=is_polygon)

    tmp_parquet = dataset_dir / f".tmp-{version}.parquet"
    final_parquet = dataset_dir / f"{version}.parquet"
    tmp_geojson = dataset_dir / f".tmp-{version}.geojson"
    tmp_mbtiles = tiles_dataset_dir / f".tmp-{version}.mbtiles"
    final_mbtiles = tiles_dataset_dir / f"{version}.mbtiles"

    fetch_progress = _throttled(lambda cur, tot: db.set_progress(con, name, version, cur, tot))
    tiling_progress = _throttled(lambda cur, tot: db.set_progress(con, name, version, cur, tot))

    try:
        print(f"[{name}] fetching/generating version {version}...")
        table, write_geojson = _build(config, on_progress=fetch_progress if config["kind"] == "arcgis" else None)
        pq.write_table(table, tmp_parquet, compression="zstd")
        row_count = _validate_parquet(tmp_parquet)

        if is_polygon:
            db.set_status(con, name, version, "tiling")
            write_geojson(tmp_geojson)
            print(f"[{name}] tiling with tippecanoe...")
            _run_tippecanoe(
                [
                    "tippecanoe", "-o", str(tmp_mbtiles), "-zg",
                    "--extend-zooms-if-still-dropping", "-l", name, "-f", str(tmp_geojson),
                ],
                on_progress=tiling_progress,
            )
            _validate_mbtiles(tmp_mbtiles)

        # Only now: files exist complete at .tmp names, validated. Move them
        # to their final names, then flip the registry pointer — the only
        # two things that can change what the running app serves.
        os.replace(tmp_parquet, final_parquet)
        if is_polygon:
            os.replace(tmp_mbtiles, final_mbtiles)
            tmp_geojson.unlink(missing_ok=True)  # build intermediate, not needed once tiled

        db.activate_version(
            con, name, version,
            row_count=row_count,
            parquet_path=str(final_parquet),
            mbtiles_path=str(final_mbtiles) if is_polygon else None,
        )
        print(f"[{name}] version {version} is now active ({row_count:,} rows).")

    except subprocess.CalledProcessError as e:
        db.fail_job(con, name, version, f"tippecanoe failed: {e.output[:2000]}")
        print(f"[{name}] FAILED: tippecanoe error: {e.output}")
    except Exception as e:
        db.fail_job(con, name, version, str(e)[:2000])
        print(f"[{name}] FAILED: {e}")
    finally:
        con.close()
