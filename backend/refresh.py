"""
Refresh orchestrator. Looks up a dataset in registry.py, dispatches to the
right builder for its `kind`, writes everything to `.tmp-*` names, validates
what was written, and only then makes it live via db.activate_version().

A refresh that fails or is interrupted before that final call leaves the
previously-active version untouched.
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
    seconds — msoa's concurrent page fetches (and tippecanoe's rapid percent
    updates) would otherwise hammer SQLite with writes for a purely
    informational value."""
    last = [0.0]

    def wrapped(current, total):
        now = time.monotonic()
        if now - last[0] >= min_interval or current >= total:
            last[0] = now
            fn(current, total)

    return wrapped


def _run_tippecanoe(cmd: list[str], on_progress, on_log=None) -> None:
    """Runs tippecanoe as a streaming subprocess so its progress meter
    (single-line, carriage-return-updated, like ` 50.0%  7/64/41`) can be
    parsed as it happens. Stdout+stderr merged since it's unclear which
    stream the meter uses. Reads one character at a time to split on '\\r'
    as well as '\\n'.

    Non-percentage lines (tippecanoe's real status output — what it
    inferred about the input, per-layer counts) get forwarded to on_log
    verbatim, so the job log reads like tippecanoe's own terminal output."""
    if on_log:
        on_log(f"Running: {' '.join(cmd)}")
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
                    elif on_log:
                        on_log(buf.strip())
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


def _build(config: dict, on_progress=None, on_log=None):
    """Dispatch on kind. Each builder predates this orchestrator with its
    own return shape, normalized here. Returns (pyarrow.Table,
    geojson_write_fn | None); geojson_write_fn(path) writes the tippecanoe
    input for polygon datasets.

    on_progress only matters for kind == "arcgis" — the generated kinds run
    in-process and finish too fast to report meaningful progress, but still
    log one line so the modal isn't blank."""
    kind = config["kind"]
    if kind == "arcgis":
        table, geojson_dict = arcgis.fetch_arcgis_dataset(config, on_progress=on_progress, on_log=on_log)
        return table, lambda path: path.write_text(json.dumps(geojson_dict))
    if kind == "generated_polygons":
        if on_log:
            on_log(f"Generating {config['count']:,} synthetic polygons (seed={config['seed']})")
        table, poly_lon, poly_lat, ids, region, category, area_sqkm = generate_polygons.build_polygons(
            config["count"], config["vertices"], seed=config["seed"]
        )
        return table, lambda path: generate_polygons.write_geojson(
            path, poly_lon, poly_lat, ids, region, category, area_sqkm
        )
    if kind == "generated_table":
        if on_log:
            on_log(f"Generating {config['rows']:,} synthetic rows (seed={config['seed']}, geo={config['geo']})")
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
    (fine for standalone/CLI use); main.py passes its own env-configured
    paths explicitly so a refresh lands where the running app actually reads
    from."""
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

    log_cb = lambda msg: db.append_log(con, name, version, msg)  # noqa: E731
    fetch_progress = _throttled(lambda cur, tot: db.set_progress(con, name, version, cur, tot))
    # Tiling's progress ticks come from tippecanoe's own percentage line,
    # which can update many times a second — piggyback the log line on the
    # same once-a-second throttle rather than firing it separately, so the
    # log doesn't fill up with 200 near-identical "62.3%… 62.4%… 62.5%" rows.
    def _tiling_tick(cur, tot):
        db.set_progress(con, name, version, cur, tot)
        log_cb(f"Tiling: {cur:.0f}% complete")

    tiling_progress = _throttled(_tiling_tick)

    try:
        log_cb(f"Starting refresh (kind={config['kind']}, version={version})")
        print(f"[{name}] fetching/generating version {version}...")

        if config["kind"] == "arcgis" and not is_polygon:
            # Large non-polygon sources (ONSPD postcodes: ~1.8M rows) stream
            # straight to parquet instead of building one big in-memory
            # Table — see fetch_arcgis_table_to_parquet's docstring.
            arcgis.fetch_arcgis_table_to_parquet(
                config, tmp_parquet, on_progress=fetch_progress, on_log=log_cb
            )
            row_count = _validate_parquet(tmp_parquet)
        else:
            table, write_geojson = _build(
                config,
                on_progress=fetch_progress if config["kind"] == "arcgis" else None,
                on_log=log_cb,
            )
            pq.write_table(table, tmp_parquet, compression="zstd")
            row_count = _validate_parquet(tmp_parquet)
        log_cb(f"Parquet written and validated: {row_count:,} rows")

        if is_polygon:
            db.set_status(con, name, version, "tiling")
            write_geojson(tmp_geojson)
            print(f"[{name}] tiling with tippecanoe...")
            # -zg guesses maxzoom from feature count/density, not physical
            # feature size — wrong for a dataset of many small polygons (see
            # registry.py's "max_zoom" docstring: it picked 9 for OA, four
            # zooms too coarse, so tippecanoe substituted placeholder squares
            # for every sub-pixel polygon). Datasets can override it.
            zoom_flag = f"-z{config['max_zoom']}" if config.get("max_zoom") else "-zg"
            _run_tippecanoe(
                [
                    "tippecanoe", "-o", str(tmp_mbtiles), zoom_flag,
                    "--extend-zooms-if-still-dropping", "-l", name, "-f", str(tmp_geojson),
                ],
                on_progress=tiling_progress,
                on_log=log_cb,
            )
            _validate_mbtiles(tmp_mbtiles)
            log_cb("Tiling complete, .mbtiles validated")

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
        log_cb(f"Version {version} is now active ({row_count:,} rows)")
        print(f"[{name}] version {version} is now active ({row_count:,} rows).")

    except subprocess.CalledProcessError as e:
        log_cb(f"FAILED: tippecanoe error: {e.output[:500]}")
        db.fail_job(con, name, version, f"tippecanoe failed: {e.output[:2000]}")
        print(f"[{name}] FAILED: tippecanoe error: {e.output}")
    except Exception as e:
        log_cb(f"FAILED: {e}")
        db.fail_job(con, name, version, str(e)[:2000])
        print(f"[{name}] FAILED: {e}")
    finally:
        con.close()
