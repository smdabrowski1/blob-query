"""
Generic ArcGIS FeatureServer fetcher — one paginated, retrying, optionally
concurrent implementation driven entirely by a registry.py config entry.

Page size and concurrency are tuned per-endpoint in registry.py: a
FeatureServer's advertised `maxRecordCount` is a record-count cap, not a
payload-size one — full-resolution coastline geometry can hit the server's
own request timeout well under that cap. uk_districts uses a small
page_size=25 for that reason; msoa/oa tolerate much bigger pages. All three
run at max_workers=4; if an endpoint starts timing out under concurrent
load, page_size is the first thing to shrink. I/O-bound waiting on the
server, so threads, not multiprocessing.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests

MAX_RETRIES = 4

# Hard cap on a single page's response body. `timeout=` below only bounds
# the gap between chunks, not total size — a slow trickle (or a server bug
# echoing back far more than page_size rows) would otherwise accumulate in
# memory forever. Hit this for real fetching ONSPD: a worker stalled
# mid-response and memory climbed past 6.9GB before the OOM killer stepped
# in. 50MB is ~20x a normal ONSPD page, turning that hang into a clean,
# retried failure.
MAX_PAGE_BYTES = 50 * 1024 * 1024


def _fetch_page(url: str, page_size: int, offset: int, max_page_bytes: int = MAX_PAGE_BYTES) -> dict:
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(
                url,
                params={
                    "where": "1=1",
                    "outFields": "*",
                    "f": "geojson",
                    "outSR": "4326",
                    "resultOffset": offset,
                    "resultRecordCount": page_size,
                },
                timeout=90,
                stream=True,
            ) as resp:
                resp.raise_for_status()
                chunks = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    chunks.extend(chunk)
                    if len(chunks) > max_page_bytes:
                        raise RuntimeError(
                            f"offset {offset}: response exceeded {max_page_bytes:,} bytes — aborting"
                        )
                data = json.loads(chunks)
                if "error" in data:
                    # ArcGIS returns these as HTTP 200 with an error body —
                    # usually a transient blip, so retry rather than fail fast.
                    raise RuntimeError(f"offset {offset}: ArcGIS error: {data['error']}")
                return data
        except (requests.exceptions.RequestException, RuntimeError, json.JSONDecodeError) as e:
            last_err = e
            wait = 2**attempt
            print(f"  offset {offset}: {e} — retrying in {wait}s ({attempt + 1}/{MAX_RETRIES})")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch offset {offset} after {MAX_RETRIES} attempts: {last_err}")


def _get_total_count(url: str) -> int:
    resp = requests.get(url, params={"where": "1=1", "returnCountOnly": "true", "f": "json"}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return data["count"]


def fetch_all_features(
    url: str, page_size: int, max_workers: int = 1, on_progress=None, on_log=None,
    max_page_bytes: int = MAX_PAGE_BYTES,
) -> list[dict]:
    """Pull every feature as WGS84 GeoJSON, paginating with retries, then
    verify the fetched count matches what the server reported up front — a
    page silently coming back short shouldn't be written to disk as if it
    were complete.

    on_progress(pages_done, pages_total) fires as each page completes,
    whether max_workers is 1 or more. on_log(message) mirrors stdout into
    the refresh job's persisted log (db.append_log) so it's visible in the
    UI modal too, not just container logs."""
    total = _get_total_count(url)
    offsets = list(range(0, total, page_size))
    start_msg = f"Fetching from {url} — {total:,} features in {len(offsets)} pages ({page_size}/page, {max_workers} worker(s))"
    print(start_msg)
    if on_log:
        on_log(start_msg)

    pages: dict[int, list[dict]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_page, url, page_size, off, max_page_bytes): off for off in offsets}
        for fut in as_completed(futures):
            off = futures[fut]
            data = fut.result()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error at offset {off}: {data['error']}")
            pages[off] = data.get("features", [])
            done += 1
            page_msg = f"Page {done}/{len(offsets)} fetched (offset {off}, {len(pages[off])} features)"
            print(f"  page done (offset {off}, {len(pages[off])} features)")
            if on_log:
                on_log(page_msg)
            if on_progress:
                on_progress(done, len(offsets))

    features = []
    for off in offsets:  # stitch back into a stable, offset-ordered sequence
        features.extend(pages[off])

    if len(features) != total:
        raise RuntimeError(
            f"Fetched {len(features):,} features but the server reported {total:,} — "
            "refusing to treat this as a complete dataset"
        )
    return features


def fetch_pages_streaming(
    url: str, page_size: int, max_workers: int = 1, on_page=None, on_progress=None, on_log=None,
    max_page_bytes: int = MAX_PAGE_BYTES,
) -> int:
    """Like fetch_all_features, but never holds the whole dataset in memory
    at once — calls on_page(offset, features) as each page arrives instead
    of accumulating into one big list. Needed for ONSPD's ~1.8M rows, which
    blew past 7GB and got OOM-killed even while writing to disk every few
    pages.

    The leak wasn't the write side — it was submitting every offset to the
    executor upfront ({ex.submit(...): off for off in offsets}), which keeps
    every Future (and its parsed JSON) alive in one unpruned dict regardless
    of how fast the consumer drains them. max_workers throttles concurrent
    fetches, not how far ahead of the consumer they can get. Fixed with a
    bounded in-flight window — only ~2x max_workers futures exist at once,
    so a slow consumer throttles the fetch side too.

    Returns the total feature count received; the caller checks it against
    _get_total_count() since there's no final list to measure here."""
    total = _get_total_count(url)
    offsets = list(range(0, total, page_size))
    start_msg = f"Fetching from {url} - {total:,} features in {len(offsets)} pages ({page_size}/page, {max_workers} worker(s))"
    print(start_msg)
    if on_log:
        on_log(start_msg)

    done = 0
    received = 0
    window = max(max_workers * 2, 1)
    offset_iter = iter(offsets)

    def _submit_next(ex, in_flight):
        try:
            off = next(offset_iter)
        except StopIteration:
            return
        in_flight[ex.submit(_fetch_page, url, page_size, off, max_page_bytes)] = off

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        in_flight: dict = {}
        for _ in range(window):
            _submit_next(ex, in_flight)

        while in_flight:
            fut = next(as_completed(in_flight))
            off = in_flight.pop(fut)
            data = fut.result()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error at offset {off}: {data['error']}")
            features = data.get("features", [])
            received += len(features)
            if on_page:
                on_page(off, features)
            done += 1
            page_msg = f"Page {done}/{len(offsets)} fetched (offset {off}, {len(features)} features)"
            print(f"  page done (offset {off}, {len(features)} features)")
            if on_log:
                on_log(page_msg)
            if on_progress:
                on_progress(done, len(offsets))
            _submit_next(ex, in_flight)

    if received != total:
        raise RuntimeError(
            f"Fetched {received:,} features but the server reported {total:,} — "
            "refusing to treat this as a complete dataset"
        )
    return received


def _ring_to_wkt(ring: list[list[float]]) -> str:
    pts = [f"{lon:.6f} {lat:.6f}" for lon, lat in ring]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return "(" + ", ".join(pts) + ")"


def geometry_to_wkt(geom: dict | None) -> str | None:
    """GeoJSON Polygon/MultiPolygon/Point -> WKT, or None -> None.
    MultiPolygon covers island/exclave authorities; Point is for postcodes.
    A small number of "non-geographic" postcodes (PO boxes etc.) carry no
    grid reference and come back as `geometry: null` — polygon datasets
    never hit this case since they're always geometry-complete."""
    if geom is None:
        return None
    if geom["type"] == "Polygon":
        rings = ", ".join(_ring_to_wkt(r) for r in geom["coordinates"])
        return f"POLYGON ({rings})"
    if geom["type"] == "MultiPolygon":
        polys = []
        for poly in geom["coordinates"]:
            rings = ", ".join(_ring_to_wkt(r) for r in poly)
            polys.append(f"({rings})")
        return "MULTIPOLYGON (" + ", ".join(polys) + ")"
    if geom["type"] == "Point":
        lon, lat = geom["coordinates"]
        return f"POINT ({lon:.6f} {lat:.6f})"
    raise ValueError(f"Unexpected geometry type: {geom['type']}")


def build_table(features: list[dict], field_mapping: dict[str, str], float_fields: list[str], tile_fields: list[str]):
    """field_mapping: {output_column: arcgis_property_name}. Returns
    (pyarrow.Table, geojson-FeatureCollection-dict) — the parquet gets every
    mapped column + id + geometry; the geojson (tippecanoe input, and so the
    vector tile's baked-in properties) only gets id + tile_fields, matching
    the existing convention of keeping tiles small and fetching the rest by
    id on click (see MapView.jsx's identify panel)."""
    ids = list(range(1, len(features) + 1))
    raw_cols: dict[str, list] = {out_col: [] for out_col in field_mapping}
    wkts = []
    geojson_features = []
    for i, f in zip(ids, features):
        props = f["properties"]
        for out_col, arcgis_prop in field_mapping.items():
            raw_cols[out_col].append(props.get(arcgis_prop))
        wkts.append(geometry_to_wkt(f["geometry"]))
        tile_props = {"id": i, **{col: props.get(field_mapping[col]) for col in tile_fields}}
        geojson_features.append({"type": "Feature", "properties": tile_props, "geometry": f["geometry"]})

    table_cols: dict = {"id": pa.array(ids, type=pa.int64())}
    for out_col, values in raw_cols.items():
        table_cols[out_col] = pa.array(values, type=pa.float64()) if out_col in float_fields else values
    table_cols["geometry"] = wkts

    table = pa.table(table_cols)
    return table, {"type": "FeatureCollection", "features": geojson_features}


def fetch_arcgis_dataset(config: dict, on_progress=None, on_log=None):
    """config: a registry.py DATASETS[name] entry with kind == 'arcgis'.
    Returns (pyarrow.Table, geojson_dict) — same shape as
    generate_polygons.build_polygons, so refresh.py can treat both kinds
    uniformly.

    Holds the whole dataset in memory twice (fetched JSON, then a Table) —
    fine for the polygon datasets this is used for, which also need the
    full geojson in memory to hand tippecanoe as one file. For a large
    non-polygon dataset, see fetch_arcgis_table_to_parquet below."""
    features = fetch_all_features(
        config["feature_server"],
        config["page_size"],
        config.get("max_workers", 1),
        on_progress=on_progress,
        on_log=on_log,
        max_page_bytes=config.get("max_page_bytes", MAX_PAGE_BYTES),
    )
    return build_table(features, config["field_mapping"], config["float_fields"], config["tile_fields"])


def _page_raw_rows(features: list[dict], field_mapping: dict[str, str], id_start: int):
    """Same per-feature transform as build_table, for one page's worth of
    features, but returns plain Python lists rather than a pyarrow Table.
    `id_start` makes ids offset-based (page at offset O gets ids
    O+1..O+len(features)), so ids stay deterministic regardless of arrival
    order. No geojson output — this path is only for non-polygon datasets."""
    ids = list(range(id_start + 1, id_start + 1 + len(features)))
    raw_cols: dict[str, list] = {out_col: [] for out_col in field_mapping}
    wkts = []
    for f in features:
        props = f["properties"]
        for out_col, arcgis_prop in field_mapping.items():
            raw_cols[out_col].append(props.get(arcgis_prop))
        wkts.append(geometry_to_wkt(f["geometry"]))
    return ids, raw_cols, wkts


def fetch_arcgis_table_to_parquet(
    config: dict, out_path: str | Path, on_progress=None, on_log=None, flush_every_pages: int = 20
) -> int:
    """The memory-bounded sibling of fetch_arcgis_dataset — writes pages to
    `out_path` incrementally via a single ParquetWriter instead of holding
    the full dataset as one in-memory Table. `flush_every_pages` trades peak
    memory for write overhead; resident memory stays bounded to roughly one
    flush batch plus whatever's in-flight, regardless of total row count.
    Returns the row count written, for the caller's own validation.

    Accumulates plain Python lists per flush batch and builds exactly one
    pyarrow Table right before each write, rather than a Table per page
    concatenated via pa.concat_tables() on every flush — that first version
    leaked measurably (ONSPD climbed past 7GB), since pyarrow's memory pool
    doesn't release concat's freed intermediate buffers promptly. Plain
    list.extend() doesn't have that problem."""
    field_mapping = config["field_mapping"]
    float_fields = config["float_fields"]
    out_cols = list(field_mapping.keys())
    state: dict = {
        "writer": None,
        "ids": [],
        "cols": {c: [] for c in out_cols},
        "wkts": [],
        "buffered_pages": 0,
        "row_count": 0,
    }

    def flush():
        if not state["ids"]:
            return
        table_cols: dict = {"id": pa.array(state["ids"], type=pa.int64())}
        for c in out_cols:
            values = state["cols"][c]
            table_cols[c] = pa.array(values, type=pa.float64()) if c in float_fields else values
        table_cols["geometry"] = state["wkts"]
        batch = pa.table(table_cols)
        if state["writer"] is None:
            # use_dictionary=False: parquet's per-column dictionary
            # accumulates across the whole file, not per flush batch — for
            # a high-cardinality column like postcode (~1.8M near-unique
            # values) that grows unboundedly regardless of flush_every_pages.
            # Cheaper to skip dictionary encoding than hand-split which
            # columns are low-cardinality enough to bother.
            state["writer"] = pq.ParquetWriter(
                str(out_path), batch.schema, compression="zstd", use_dictionary=False
            )
        state["writer"].write_table(batch)
        state["ids"] = []
        state["cols"] = {c: [] for c in out_cols}
        state["wkts"] = []
        state["buffered_pages"] = 0

    def on_page(offset, features):
        if not features:
            return
        ids, raw_cols, wkts = _page_raw_rows(features, field_mapping, id_start=offset)
        state["ids"].extend(ids)
        for c in out_cols:
            state["cols"][c].extend(raw_cols[c])
        state["wkts"].extend(wkts)
        state["buffered_pages"] += 1
        state["row_count"] += len(features)
        if state["buffered_pages"] >= flush_every_pages:
            flush()

    try:
        fetch_pages_streaming(
            config["feature_server"],
            config["page_size"],
            config.get("max_workers", 1),
            on_page=on_page,
            on_progress=on_progress,
            on_log=on_log,
            max_page_bytes=config.get("max_page_bytes", MAX_PAGE_BYTES),
        )
        flush()
    finally:
        if state["writer"] is not None:
            state["writer"].close()
    return state["row_count"]
