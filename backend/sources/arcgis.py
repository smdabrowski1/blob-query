"""
Generic ArcGIS FeatureServer fetcher — one paginated, retrying, optionally
concurrent implementation driven entirely by a registry.py config entry.
Replaces what used to be two near-duplicate scripts (fetch_uk_districts.py,
fetch_msoa.py) that differed only in URL/page-size/field-names.

Page size and concurrency are tuned per-endpoint in registry.py, not
guessable from a general rule: a FeatureServer's advertised `maxRecordCount`
is a record-count cap, not a payload-size one — full-BFE-resolution
geometry (detailed coastlines) can hit the server's own request timeout well
under that cap. uk_districts (heavier per-feature geometry) uses a small
page_size=25 for that reason; msoa/oa (lighter per-feature) tolerate much
bigger pages. All three currently run at max_workers=4 — a uniform value
chosen for simplicity over the originally more conservative per-endpoint
concurrency (uk_districts started sequential); if a given endpoint starts
timing out or erroring under concurrent load, page_size is the first thing
to shrink, since that's what actually governs per-request payload weight.
Pulling data this way is I/O-bound waiting on the server, not CPU-bound, so
a thread pool is the right concurrency tool, not multiprocessing.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pyarrow as pa
import requests

MAX_RETRIES = 4


def _fetch_page(url: str, page_size: int, offset: int) -> dict:
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
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
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
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
    url: str, page_size: int, max_workers: int = 1, on_progress=None, on_log=None
) -> list[dict]:
    """Pull every feature as WGS84 GeoJSON, paginating with retries, then
    verify the fetched count matches what the server itself reported at the
    start — a page silently coming back short (a transient hiccup that
    wasn't a hard HTTP error) must not be written to disk as if it were a
    complete, trustworthy dataset.

    on_progress(pages_done, pages_total), if given, is called as each page
    completes — works the same whether max_workers is 1 or more, since it's
    driven by as_completed() either way. on_log(message), if given, mirrors
    what's already printed to stdout into the refresh job's persisted log
    (see db.append_log) — same events, just also visible in the UI modal,
    not only container logs."""
    total = _get_total_count(url)
    offsets = list(range(0, total, page_size))
    start_msg = f"Fetching from {url} — {total:,} features in {len(offsets)} pages ({page_size}/page, {max_workers} worker(s))"
    print(start_msg)
    if on_log:
        on_log(start_msg)

    pages: dict[int, list[dict]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_page, url, page_size, off): off for off in offsets}
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


def _ring_to_wkt(ring: list[list[float]]) -> str:
    pts = [f"{lon:.6f} {lat:.6f}" for lon, lat in ring]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return "(" + ", ".join(pts) + ")"


def geometry_to_wkt(geom: dict) -> str:
    """GeoJSON Polygon/MultiPolygon -> WKT. Real boundary data includes
    MultiPolygon geometries (island/exclave authorities)."""
    if geom["type"] == "Polygon":
        rings = ", ".join(_ring_to_wkt(r) for r in geom["coordinates"])
        return f"POLYGON ({rings})"
    if geom["type"] == "MultiPolygon":
        polys = []
        for poly in geom["coordinates"]:
            rings = ", ".join(_ring_to_wkt(r) for r in poly)
            polys.append(f"({rings})")
        return "MULTIPOLYGON (" + ", ".join(polys) + ")"
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
    Returns (pyarrow.Table, geojson_dict) — same shape generate_polygons.py's
    build_polygons returns, so refresh.py can treat both kinds uniformly."""
    features = fetch_all_features(
        config["feature_server"],
        config["page_size"],
        config.get("max_workers", 1),
        on_progress=on_progress,
        on_log=on_log,
    )
    return build_table(features, config["field_mapping"], config["float_fields"], config["tile_fields"])
