"""
Generate an LSOA-like polygon dataset for testing vector-tile rendering.

Produces `count` polygons packed edge-to-edge across a rough Great Britain
outline — mimicking how real Lower Super Output Areas tile the country with
no gaps between neighbours — rather than scattering isolated blobs around a
few sparse centres. Two outputs come from one generation pass so they can
never drift apart:

  - <name>.parquet  — id/attributes + geometry as WKT (queryable like any
    other dataset; engine.refresh() auto-registers it as a view).
  - <name>.geojson  — same id/attributes as feature properties, tippecanoe
    input. Build intermediate, not meant to be queried directly.

After running this, tile it:
    tippecanoe -o tiles/<name>.mbtiles -zg --extend-zooms-if-still-dropping \
        -l <name> -f data/<name>.geojson

Usage:
    python generate_polygons.py                          # 50,000 polygons, 100 vertices
    python generate_polygons.py --count 5000 --vertices 40   # smaller/faster
"""
import argparse
import json
import math
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from generate_data import REGION_CENTROIDS

DATA_DIR = Path(__file__).parent / "data"

CATEGORIES = ["Woodland", "Farmland", "Urban", "Wetland", "Grassland"]

# Rough Great Britain mainland outline (lon, lat), clockwise from the north
# coast — approximate, for demo scatter/clipping only. Ignores islands,
# firths and fine coastal detail; not survey-accurate boundary data.
GB_OUTLINE = [
    (-3.0, 58.6), (-2.0, 57.7), (-1.8, 57.2), (-2.1, 56.5), (-2.6, 56.0),
    (-3.2, 56.0), (-2.0, 55.8), (-1.6, 55.0), (-1.4, 54.5), (-0.4, 54.0),
    (0.2, 53.6), (1.7, 52.9), (1.5, 52.2), (1.3, 51.9), (1.4, 51.4),
    (0.9, 51.0), (0.0, 50.8), (-1.4, 50.7), (-3.5, 50.3), (-5.6, 50.1),
    (-4.9, 51.2), (-3.0, 51.5), (-4.7, 51.7), (-5.3, 52.0), (-4.2, 52.9),
    (-3.1, 53.4), (-3.0, 54.0), (-4.9, 54.9), (-4.9, 55.9), (-5.3, 56.5),
    (-5.0, 57.3), (-5.5, 58.0), (-4.3, 58.6),
]


def _point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _outline_area_km2(poly: list[tuple[float, float]], mean_lat: float) -> float:
    """Shoelace area in deg^2, converted to km^2 at this latitude's scale."""
    area_deg2 = 0.0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        area_deg2 += x1 * y2 - x2 * y1
    area_deg2 = abs(area_deg2) / 2.0
    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(mean_lat))
    return area_deg2 * km_per_deg_lat * km_per_deg_lon


def _generate_centroids(count: int, seed: int):
    """Pack `count` centroids across GB_OUTLINE on a jittered, brick-offset
    grid sized so the resulting polygons sit edge-to-edge (LSOA-like tiling)
    rather than scattered. Returns (centroids[count,2] lon/lat, spacing_km).
    """
    rng = np.random.default_rng(seed)
    lons = [p[0] for p in GB_OUTLINE]
    lats = [p[1] for p in GB_OUTLINE]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    mean_lat = (min_lat + max_lat) / 2

    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(mean_lat))
    area_km2 = _outline_area_km2(GB_OUTLINE, mean_lat)
    spacing_km = math.sqrt(area_km2 / count)

    # Overshoot the grid slightly so the scan reliably yields >= count
    # candidates in one pass — avoids falling into the retry branch below for
    # what's usually just a few points' shortfall from area-estimate error.
    accepted: list[tuple[float, float]] = []
    step_km = spacing_km * 0.93
    for _ in range(6):
        dlat = step_km / km_per_deg_lat
        dlon = step_km / km_per_deg_lon
        pts = []
        lat = min_lat
        row = 0
        while lat <= max_lat:
            offset = (dlon / 2) if row % 2 else 0.0  # brick-offset rows, not a bare grid
            lon = min_lon - offset
            while lon <= max_lon:
                jlat = lat + rng.normal(0, dlat * 0.2)
                jlon = lon + rng.normal(0, dlon * 0.2)
                if _point_in_polygon(jlon, jlat, GB_OUTLINE):
                    pts.append((jlon, jlat))
                lon += dlon
            lat += dlat
            row += 1
        accepted = pts
        if len(accepted) >= count:
            break
        step_km *= 0.85  # still short: tighten the grid and retry

    if len(accepted) > count:
        # Thin evenly across scan order (not a fully random subsample) so we
        # keep the tessellation intact — random removal rips neighbours out
        # independently and leaves gaps throughout the packing.
        idx = np.round(np.linspace(0, len(accepted) - 1, count)).astype(int)
        accepted = [accepted[i] for i in idx]
    elif len(accepted) < count:
        # Shouldn't normally trigger given the overshoot above; pad with
        # small jittered duplicates so `count` comes out exact.
        deficit = count - len(accepted)
        pad_from = rng.integers(0, len(accepted), size=deficit)
        for i in pad_from:
            lon, lat = accepted[i]
            accepted.append((lon + rng.normal(0, dlon * 0.1), lat + rng.normal(0, dlat * 0.1)))

    arr = np.array(accepted, dtype=float)
    rng.shuffle(arr)  # so id order doesn't correlate with location (LIMIT N stays representative)
    # Radius sizing uses the *target* spacing (area/count), not whatever
    # finer scan spacing was needed internally — after thinning back to
    # `count` points the true average spacing returns to the target.
    return arr, spacing_km


def _assign_region(centroid_lat: np.ndarray, centroid_lon: np.ndarray) -> np.ndarray:
    names = list(REGION_CENTROIDS)
    pts = np.array([REGION_CENTROIDS[n] for n in names])  # (5, 2) lat/lon
    d2 = (centroid_lat[:, None] - pts[:, 0][None, :]) ** 2 + (
        centroid_lon[:, None] - pts[:, 1][None, :]
    ) ** 2
    return np.array(names)[d2.argmin(axis=1)]


def build_polygons(count: int, vertices: int, seed: int = 42):
    rng = np.random.default_rng(seed + 1)  # offset from the centroid-grid seed

    centroids, step_km = _generate_centroids(count, seed)
    centroid_lon = np.round(centroids[:, 0], 5)
    centroid_lat = np.round(centroids[:, 1], 5)

    ids = np.arange(1, count + 1, dtype=np.int64)
    region = _assign_region(centroid_lat, centroid_lon)
    category = rng.choice(CATEGORIES, size=count)

    # Radius ~55-70% of grid spacing so neighbouring cells sit edge-to-edge
    # with a touch of overlap — a packed, LSOA-like tiling instead of gaps.
    radius_km = step_km * rng.uniform(0.55, 0.70, count)
    area_sqkm = np.round(np.pi * radius_km**2, 3)

    angles = np.linspace(0, 2 * np.pi, vertices, endpoint=False)
    radius_deg_lat = radius_km / 111.0
    jitter = rng.normal(0, 1, size=(count, vertices)) * (radius_deg_lat[:, None] * 0.12)
    radius = radius_deg_lat[:, None] + jitter
    radius = np.clip(radius, radius_deg_lat[:, None] * 0.5, None)

    lon_scale = 1.0 / np.cos(np.radians(centroid_lat))[:, None]
    poly_lon = centroid_lon[:, None] + radius * np.cos(angles)[None, :] * lon_scale
    poly_lat = centroid_lat[:, None] + radius * np.sin(angles)[None, :]

    table = pa.table(
        {
            "id": ids,
            "region": region,
            "category": category,
            "area_sqkm": area_sqkm,
            "centroid_lat": centroid_lat,
            "centroid_lon": centroid_lon,
            "geometry": [_to_wkt(poly_lon[i], poly_lat[i]) for i in range(count)],
        }
    )
    return table, poly_lon, poly_lat, ids, region, category, area_sqkm


def _to_wkt(lons: np.ndarray, lats: np.ndarray) -> str:
    pts = [f"{lon:.6f} {lat:.6f}" for lon, lat in zip(lons, lats)]
    pts.append(pts[0])  # close the ring
    return "POLYGON ((" + ", ".join(pts) + "))"


def write_geojson(path: Path, poly_lon, poly_lat, ids, region, category, area_sqkm):
    count = len(ids)
    with path.open("w") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for i in range(count):
            ring = [[round(float(lo), 6), round(float(la), 6)] for lo, la in zip(poly_lon[i], poly_lat[i])]
            ring.append(ring[0])
            feature = {
                "type": "Feature",
                "properties": {
                    "id": int(ids[i]),
                    "region": str(region[i]),
                    "category": str(category[i]),
                    "area_sqkm": float(area_sqkm[i]),
                },
                "geometry": {"type": "Polygon", "coordinates": [ring]},
            }
            f.write(json.dumps(feature))
            f.write(",\n" if i < count - 1 else "\n")
        f.write("]}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=50_000)
    ap.add_argument("--vertices", type=int, default=100)
    ap.add_argument("--name", type=str, default="land_parcels")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    parquet_out = DATA_DIR / f"{args.name}.parquet"
    geojson_out = DATA_DIR / f"{args.name}.geojson"

    print(f"Generating {args.count:,} polygons x {args.vertices} vertices, tiled across GB -> "
          f"{parquet_out.name} / {geojson_out.name}")
    table, poly_lon, poly_lat, ids, region, category, area_sqkm = build_polygons(
        args.count, args.vertices, seed=args.seed
    )

    pq.write_table(table, parquet_out, compression="zstd")
    write_geojson(geojson_out, poly_lon, poly_lat, ids, region, category, area_sqkm)

    size_mb = parquet_out.stat().st_size / 1e6
    geo_mb = geojson_out.stat().st_size / 1e6
    print(f"Done. {parquet_out.name} is {size_mb:.1f} MB ({table.num_rows:,} rows). "
          f"{geojson_out.name} is {geo_mb:.1f} MB.")
    print(f"Next: tippecanoe -o tiles/{args.name}.mbtiles -zg "
          f"--extend-zooms-if-still-dropping -l {args.name} -f data/{args.name}.geojson")


if __name__ == "__main__":
    main()
