"""
One-time migration: move today's flat backend/data/<name>.parquet (and
backend/tiles/<name>.mbtiles) into the versioned layout
backend/data/<name>/<version>.parquet, and register each as the active
version in registry.db.

Not general migration tooling — this runs once, for the 5 datasets that
existed under the old flat-file scheme, then is done. Re-running it is safe
(skips any dataset that already has a current version registered).

Usage: python migrate_to_registry.py
"""
import datetime as _dt
from pathlib import Path

import pyarrow.parquet as pq

import db
from registry import DATASETS

DATA_DIR = Path(__file__).parent / "data"
TILES_DIR = Path(__file__).parent / "tiles"
REGISTRY_DB = Path(__file__).parent / "registry.db"


def main():
    con = db.connect(REGISTRY_DB)
    try:
        for name, config in DATASETS.items():
            if db.get_current(con, name) is not None:
                print(f"[{name}] already has a current version — skipping")
                continue

            flat_parquet = DATA_DIR / f"{name}.parquet"
            if not flat_parquet.is_file():
                print(f"[{name}] no existing {flat_parquet.name} — nothing to migrate")
                continue

            version = _dt.datetime.fromtimestamp(flat_parquet.stat().st_mtime, _dt.timezone.utc).strftime(
                "%Y%m%d-%H%M%S"
            )

            dataset_dir = DATA_DIR / name
            dataset_dir.mkdir(parents=True, exist_ok=True)
            new_parquet = dataset_dir / f"{version}.parquet"
            flat_parquet.rename(new_parquet)
            row_count = pq.ParquetFile(new_parquet).metadata.num_rows

            new_mbtiles = None
            if config["polygon"]:
                flat_mbtiles = TILES_DIR / f"{name}.mbtiles"
                if flat_mbtiles.is_file():
                    tiles_dataset_dir = TILES_DIR / name
                    tiles_dataset_dir.mkdir(parents=True, exist_ok=True)
                    new_mbtiles = tiles_dataset_dir / f"{version}.mbtiles"
                    flat_mbtiles.rename(new_mbtiles)

            db.start_job(con, name, version, has_geometry=config["polygon"])
            db.activate_version(
                con, name, version,
                row_count=row_count,
                parquet_path=str(new_parquet),
                mbtiles_path=str(new_mbtiles) if new_mbtiles else None,
            )
            print(f"[{name}] migrated to version {version} ({row_count:,} rows)"
                  + (f" + {new_mbtiles.name}" if new_mbtiles else ""))

            # Flat .geojson build intermediates are already tiled into the
            # .mbtiles just migrated and aren't referenced going forward —
            # just reclaim the space.
            flat_geojson = DATA_DIR / f"{name}.geojson"
            if flat_geojson.is_file():
                size_mb = flat_geojson.stat().st_size / 1e6
                flat_geojson.unlink()
                print(f"[{name}] removed stale build intermediate {flat_geojson.name} ({size_mb:.0f} MB)")
    finally:
        con.close()


if __name__ == "__main__":
    main()
