"""
Fetch real UK Local Authority District boundaries from the ONS/OS ArcGIS
FeatureServer and turn them into the same parquet + tippecanoe-tiles shape
used by generate_polygons.py, so they show up as a normal tiled dataset.

Source: Local_Authority_Districts_DEC_2025_Boundaries_UK_BFE (full resolution,
extent of the realm). Contains both Ordnance Survey and ONS Intellectual
Property Rights.

Pulled once, not fetched live per-request — matches every other dataset in
backend/data/, which are static files DuckDB reads, not a live API proxy.

Usage:
    python fetch_uk_districts.py
    python fetch_uk_districts.py --name districts   # different view name

After running this, tile it:
    tippecanoe -o tiles/uk_districts.mbtiles -zg --extend-zooms-if-still-dropping \
        -l uk_districts -f data/uk_districts.geojson
"""
import argparse
import json
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests

DATA_DIR = Path(__file__).parent / "data"

FEATURE_SERVER = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Local_Authority_Districts_DEC_2025_Boundaries_UK_BFE/FeatureServer/0/query"
)
# The layer's maxRecordCount is 2000, but that's a record-count cap, not a
# payload-size one — full BFE resolution geometry (detailed coastlines) for
# a couple hundred districts in one response is enough to hit the server's
# own request timeout (504), independent of being under 2000 records. Small
# pages keep each request's payload light.
PAGE_SIZE = 25
MAX_RETRIES = 4


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


def fetch_all_features() -> list[dict]:
    """Pull every feature as WGS84 GeoJSON, paginating in small pages (see
    PAGE_SIZE) with retries — a couple hundred districts at full BFE
    resolution is too large a payload for one big request."""
    features = []
    offset = 0
    while True:
        data = _fetch_page(offset)
        if "error" in data:
            raise RuntimeError(f"ArcGIS error: {data['error']}")
        page = data.get("features", [])
        features.extend(page)
        print(f"  fetched {len(features):,} features so far...")
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)
    return features


def _ring_to_wkt(ring: list[list[float]]) -> str:
    pts = [f"{lon:.6f} {lat:.6f}" for lon, lat in ring]
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return "(" + ", ".join(pts) + ")"


def geometry_to_wkt(geom: dict) -> str:
    """GeoJSON Polygon/MultiPolygon -> WKT. Real LAD boundaries include
    MultiPolygon geometries (island/exclave authorities), unlike the
    synthetic single-ring polygons in generate_polygons.py."""
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
    ids, codes, names, names_welsh, areas, perimeters, wkts = [], [], [], [], [], [], []
    geojson_features = []
    for i, f in enumerate(features, start=1):
        props = f["properties"]
        ids.append(i)
        codes.append(props.get("LAD25CD"))
        names.append(props.get("LAD25NM"))
        names_welsh.append(props.get("LAD25NMW"))
        areas.append(props.get("Shape__Area"))
        perimeters.append(props.get("Shape__Length"))
        wkts.append(geometry_to_wkt(f["geometry"]))
        geojson_features.append(
            {
                "type": "Feature",
                "properties": {
                    "id": i,
                    "lad_code": props.get("LAD25CD"),
                    "name": props.get("LAD25NM"),
                },
                "geometry": f["geometry"],
            }
        )

    table = pa.table(
        {
            "id": pa.array(ids, type=pa.int64()),
            "lad_code": codes,
            "name": names,
            "name_welsh": names_welsh,
            "area_sqm": pa.array(areas, type=pa.float64()),
            "perimeter_m": pa.array(perimeters, type=pa.float64()),
            "geometry": wkts,
        }
    )
    return table, {"type": "FeatureCollection", "features": geojson_features}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", type=str, default="uk_districts")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    parquet_out = DATA_DIR / f"{args.name}.parquet"
    geojson_out = DATA_DIR / f"{args.name}.geojson"

    print(f"Fetching UK Local Authority District boundaries from {FEATURE_SERVER} ...")
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
