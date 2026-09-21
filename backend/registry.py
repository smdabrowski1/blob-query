"""
Generic dataset registry — one entry per dataset describing how to fetch or
generate it, replacing constants that used to be hardcoded per-script
(fetch_uk_districts.py, fetch_msoa.py). Adding a dataset that reuses an
existing `kind` means adding an entry here, not a new Python file.

Fields:
  kind: "arcgis" | "generated_table" | "generated_polygons"
  polygon: whether this dataset needs a tileset (.mbtiles) alongside its parquet

arcgis-specific:
  feature_server: the FeatureServer layer's /query URL
  page_size / max_workers: tuned per-endpoint (see backend/sources/arcgis.py's
    module docstring for how these were picked — payload size, not just
    record count, is what determines a safe page size)
  field_mapping: {output_column: arcgis_property_name} — every output column
    except `id` (synthetic) and `geometry` (WKT, derived from the feature's
    geometry, not a property)
  float_fields: subset of field_mapping's keys that are numeric
  tile_fields: subset of field_mapping's keys (plus "id") to bake into the
    vector tile's properties — kept small on purpose; the rest is available
    via the parquet, fetched by id on click (see MapView.jsx's identify panel)

generated_table-specific (backend/generate_data.py):
  rows, seed, geo

generated_polygons-specific (backend/generate_polygons.py):
  count, vertices, seed
"""

DATASETS = {
    "uk_districts": {
        "kind": "arcgis",
        "polygon": True,
        "feature_server": (
            "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
            "Local_Authority_Districts_DEC_2025_Boundaries_UK_BFE/FeatureServer/0/query"
        ),
        "page_size": 25,
        "max_workers": 1,
        "field_mapping": {
            "lad_code": "LAD25CD",
            "name": "LAD25NM",
            "name_welsh": "LAD25NMW",
            "area_sqm": "Shape__Area",
            "perimeter_m": "Shape__Length",
        },
        "float_fields": ["area_sqm", "perimeter_m"],
        "tile_fields": ["lad_code", "name"],
    },
    "msoa": {
        "kind": "arcgis",
        "polygon": True,
        "feature_server": (
            "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
            "MSOA_2021_EW_BFE_V8_RUC/FeatureServer/3/query"
        ),
        "page_size": 150,
        "max_workers": 5,
        "field_mapping": {
            "msoa_code": "MSOA21CD",
            "name": "MSOA21NM",
            "name_welsh": "MSOA21NMW",
            "ruc_code": "RUC21CD",
            "ruc_name": "RUC21NM",
            "area_sqm": "Shape__Area",
            "perimeter_m": "Shape__Length",
        },
        "float_fields": ["area_sqm", "perimeter_m"],
        "tile_fields": ["msoa_code", "name"],
    },
    "land_parcels": {
        "kind": "generated_polygons",
        "polygon": True,
        "count": 50_000,
        "vertices": 100,
        "seed": 42,
    },
    "sales_events": {
        "kind": "generated_table",
        "polygon": False,
        "rows": 1_000_000,
        "seed": 42,
        "geo": True,
    },
    "inventory_snapshot": {
        "kind": "generated_table",
        "polygon": False,
        "rows": 120_000,
        "seed": 7,
        "geo": False,
    },
}
