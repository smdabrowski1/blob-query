"""
Fetch real England & Wales Middle Super Output Area (MSOA) boundaries from
the ONS ArcGIS FeatureServer and turn them into the same parquet +
tippecanoe-tiles shape used by fetch_uk_districts.py, so they show up as a
normal tiled dataset.

Source: MSOA_2021_EW_BFE_V8_RUC (full resolution, extent of the realm, with
Rural-Urban Classification attached). England & Wales only — MSOA is an E&W
statistical geography; Scotland/NI aren't covered (unlike uk_districts, which
spans the whole UK). Contains both Ordnance Survey and ONS Intellectual
Property Rights.

Pulled once, not fetched live per-request — matches every other dataset in
backend/data/, which are static files DuckDB reads, not a live API proxy.

7,264 MSOAs total — a page-per-request approach like fetch_uk_districts.py's
would take 6-7 minutes sequentially, so pages are fetched concurrently (this
is I/O-bound waiting on the ArcGIS server, not CPU-bound work — a thread pool
is the right tool, not multiprocessing). Empirically: 100-300 records/request
is fine (5-15s), but 800+ starts creeping toward the server's own timeout, so
PAGE_SIZE stays well under that with retries per page regardless of the
larger MAX_WORKERS concurrency.

Usage:
    python fetch_msoa.py
    python fetch_msoa.py --name msoa   # different view name

After running this, tile it:
    tippecanoe -o tiles/msoa.mbtiles -zg --extend-zooms-if-still-dropping \
        -l msoa -f data/msoa.geojson
"""
import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests

DATA_DIR = Path(__file__).parent / "data"

FEATURE_SERVER = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "MSOA_2021_EW_BFE_V8_RUC/FeatureServer/3/query"
)
PAGE_SIZE = 150
MAX_RETRIES = 4
MAX_WORKERS = 5  # concurrent requests — polite to the shared public service


def _fetch_page(offset: int) -> dict:
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(
                FEATURE_SERVER,
                params={
                    "where": "1=1",
                    "outFields": "*",
                    "f": "geojson",
                    "outSR": "4326",
                    "resultOffset": offset,
                    "resultRecordCount": PAGE_SIZE,
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


def _get_total_count() -> int:
    resp = requests.get(
        FEATURE_SERVER, params={"where": "1=1", "returnCountOnly": "true", "f": "json"}, timeout=30
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS error: {data['error']}")
    return data["count"]


def fetch_all_features() -> list[dict]:
    total = _get_total_count()
    offsets = list(range(0, total, PAGE_SIZE))
    print(f"Fetching {total:,} MSOAs in {len(offsets)} pages "
          f"({PAGE_SIZE}/page, {MAX_WORKERS} concurrent workers)...")

    pages: dict[int, list[dict]] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(_fetch_page, off): off for off in offsets}
        for fut in as_completed(futures):
            off = futures[fut]
            data = fut.result()
            if "error" in data:
                raise RuntimeError(f"ArcGIS error at offset {off}: {data['error']}")
            pages[off] = data.get("features", [])
            done += 1
            print(f"  page {done}/{len(offsets)} done (offset {off}, {len(pages[off])} features)")

    features = []
    for off in offsets:  # stitch back into a stable, offset-ordered sequence
        features.extend(pages[off])
    return features


def _ring_to_wkt(ring: list[list[float]]) -> str:
    pts = [f"{lon:.6f} {lat:.6f}" for lon, lat in ring]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return "(" + ", ".join(pts) + ")"


def geometry_to_wkt(geom: dict) -> str:
    """GeoJSON Polygon/MultiPolygon -> WKT — same handling as
    fetch_uk_districts.py, since real boundary data can be either."""
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


def build_table(features: list[dict]):
    ids, codes, names, names_welsh = [], [], [], []
    ruc_codes, ruc_names, areas, perimeters, wkts = [], [], [], [], []
    geojson_features = []
    for i, f in enumerate(features, start=1):
        props = f["properties"]
        ids.append(i)
        codes.append(props.get("MSOA21CD"))
        names.append(props.get("MSOA21NM"))
        names_welsh.append(props.get("MSOA21NMW"))
        ruc_codes.append(props.get("RUC21CD"))
        ruc_names.append(props.get("RUC21NM"))
        areas.append(props.get("Shape__Area"))
        perimeters.append(props.get("Shape__Length"))
        wkts.append(geometry_to_wkt(f["geometry"]))
        geojson_features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": i,
                    "msoa_code": props.get("MSOA21CD"),
                    "name": props.get("MSOA21NM"),
                },
                "geometry": f["geometry"],
            }
        )

    table = pa.table(
        {
            "id": pa.array(ids, type=pa.int64()),
            "msoa_code": codes,
            "name": names,
            "name_welsh": names_welsh,
            "ruc_code": ruc_codes,
            "ruc_name": ruc_names,
            "area_sqm": pa.array(areas, type=pa.float64()),
            "perimeter_m": pa.array(perimeters, type=pa.float64()),
            "geometry": wkts,
        }
    )
    return table, {"type": "FeatureCollection", "features": geojson_features}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", type=str, default="msoa")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    parquet_out = DATA_DIR / f"{args.name}.parquet"
    geojson_out = DATA_DIR / f"{args.name}.geojson"

    print(f"Fetching MSOA (Middle Super Output Area) boundaries from {FEATURE_SERVER} ...")
    features = fetch_all_features()
    print(f"Got {len(features):,} features.")

    table, geojson = build_table(features)
    pq.write_table(table, parquet_out, compression="zstd")
    with geojson_out.open("w") as f:
        json.dump(geojson, f)

    size_mb = parquet_out.stat().st_size / 1e6
    geo_mb = geojson_out.stat().st_size / 1e6
    print(f"Done. {parquet_out.name} is {size_mb:.1f} MB ({table.num_rows:,} rows). "
          f"{geojson_out.name} is {geo_mb:.1f} MB.")
    print(f"Next: tippecanoe -o tiles/{args.name}.mbtiles -zg "
          f"--extend-zooms-if-still-dropping -l {args.name} -f data/{args.name}.geojson")


if __name__ == "__main__":
    main()
