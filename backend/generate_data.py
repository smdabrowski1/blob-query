"""
Generate a generic parquet dataset for testing the query engine.

Produces a wide-ish table with a mix of types (ints, floats, strings, booleans,
timestamps) plus optional geographic coordinates (lat/lon) so the map view has
real spatial data to plot. Points are clustered around each region's centre so
the map shows recognisable regional groupings.

Usage:
    python generate_data.py                        # 1,000,000 rows, with lat/lon
    python generate_data.py --rows 5000000         # bigger
    python generate_data.py --name inv --no-geo    # no coordinates (no map tab)
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

DATA_DIR = Path(__file__).parent / "data"

REGIONS = ["North", "South", "East", "West", "Central"]
CATEGORIES = ["Electronics", "Grocery", "Apparel", "Home", "Toys", "Automotive", "Garden"]
CHANNELS = ["Online", "Retail", "Wholesale", "Partner"]
STATUSES = ["completed", "pending", "refunded", "cancelled"]

# Rough GB region centres (lat, lon); events scatter around these.
REGION_CENTROIDS = {
    "North":   (54.05, -1.52),
    "South":   (51.05, -0.55),
    "East":    (52.45,  0.60),
    "West":    (53.45, -2.60),
    "Central": (52.55, -1.50),
}


def _norm(weights):
    a = np.array(weights, dtype=float)
    return a / a.sum()


def build_table(rows: int, seed: int = 42, geo: bool = True) -> pa.Table:
    rng = np.random.default_rng(seed)

    ids = np.arange(1, rows + 1, dtype=np.int64)

    start = datetime(2023, 1, 1)
    span_seconds = int(timedelta(days=730).total_seconds())
    offsets = rng.integers(0, span_seconds, size=rows)
    ts = np.array([start], dtype="datetime64[s]") + offsets.astype("timedelta64[s]")

    region = rng.choice(REGIONS, size=rows)
    category = rng.choice(CATEGORIES, size=rows, p=_norm([5, 4, 3, 3, 2, 1, 1]))
    channel = rng.choice(CHANNELS, size=rows)
    status = rng.choice(STATUSES, size=rows, p=_norm([80, 10, 5, 5]))

    quantity = rng.integers(1, 25, size=rows).astype(np.int32)
    unit_price = np.round(rng.gamma(shape=2.0, scale=18.0, size=rows) + 1.0, 2)
    discount = np.round(rng.beta(2, 8, size=rows), 3)
    revenue = np.round(quantity * unit_price * (1 - discount), 2)

    is_priority = rng.random(rows) < 0.15
    customer_id = rng.integers(1, 50_000, size=rows).astype(np.int64)

    cols = {
        "event_id": ids,
        "event_ts": pa.array(ts),
        "customer_id": customer_id,
        "region": region,
        "category": category,
        "channel": channel,
        "status": status,
        "quantity": quantity,
        "unit_price": unit_price,
        "discount": discount,
        "revenue": revenue,
        "is_priority": is_priority,
    }

    if geo:
        lat0 = np.empty(rows)
        lon0 = np.empty(rows)
        for name, (la, lo) in REGION_CENTROIDS.items():
            mask = region == name
            lat0[mask] = la
            lon0[mask] = lo
        lat = np.round(lat0 + rng.normal(0, 0.35, rows), 5)
        lon = np.round(lon0 + rng.normal(0, 0.45, rows), 5)
        cols["lat"] = lat
        cols["lon"] = lon

    return pa.table(cols)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1_000_000)
    ap.add_argument("--name", type=str, default="sales_events")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--geo", action=argparse.BooleanOptionalAction, default=True,
                    help="include lat/lon coordinates (use --no-geo to omit)")
    args = ap.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / f"{args.name}.parquet"

    print(f"Generating {args.rows:,} rows (geo={args.geo}) -> {out}")
    table = build_table(args.rows, seed=args.seed, geo=args.geo)
    pq.write_table(table, out, compression="zstd")

    size_mb = out.stat().st_size / 1e6
    print(f"Done. {out.name} is {size_mb:.1f} MB, {table.num_rows:,} rows, "
          f"{table.num_columns} columns.")


if __name__ == "__main__":
    main()
