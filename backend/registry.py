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
  max_zoom (optional): forces tippecanoe's `-z<N>` instead of letting `-zg`
    guess it. `-zg` guesses from overall feature *count/density*, not how
    physically small each feature is — fine for uk_districts/msoa (both
    landed on 13 on their own), but a dataset of many genuinely tiny
    polygons (OA: ~770m across, 26x msoa's feature count) can get guessed
    a much lower maxzoom, under which each polygon is sub-pixel and
    tippecanoe silently replaces it with a placeholder square (its "tiny
    polygon" fallback) — real shape only reappears once you zoom in past
    what the tiles actually contain, since MapLibre just overzooms/stretches
    the coarse placeholder rather than requesting more tiles. If a new
    dataset's polygons render as jagged/self-intersecting blobs at close
    zoom, check the .mbtiles' maxzoom (backend/tiles/<name>/<version>.mbtiles,
    sqlite `metadata` table) against a known-good one before assuming a code
    bug — this is very likely why.
  field_mapping: {output_column: arcgis_property_name} — every output column
    except `id` (synthetic) and `geometry` (WKT, derived from the feature's
    geometry, not a property)
  float_fields: subset of field_mapping's keys that are numeric
  tile_fields: subset of field_mapping's keys (plus "id") to bake into the
    vector tile's properties — kept small on purpose; the rest is available
    via the parquet, fetched by id on click (see MapView.jsx's identify panel)
    Meaningless (leave as []) when `polygon` is False — there's no tileset
    for a non-polygon dataset to bake tile-only properties into.
  code_column (optional): this dataset's natural, unique-per-row code column
    (e.g. "lad_code"). Drives choropleth detection (engine.py's
    _detect_choropleth): a query result that keeps a column with exactly
    this name — typically a GROUP BY on it, e.g. "SELECT lad_code,
    count(*) ... GROUP BY lad_code" — gets offered as a colour-by-value map
    even though it has no `id` column and isn't the polygon-filter case
    _detect_polygon_source handles. Deliberately an *exact* column-name
    match, not a fuzzy one on e.g. "name" — every tiled dataset has a
    "name"-ish column, so matching on that couldn't tell which dataset a
    join's result should color against. Omit for a dataset with no natural
    code (land_parcels' polygons are synthetic, id is all there is).

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
        "max_workers": 4,
        # Full-BFE-resolution coastline geometry makes some pages far heavier
        # than page_size alone suggests — a real 25-feature page at offset 300
        # measured 75MB. sources/arcgis.py's MAX_PAGE_BYTES default (50MB,
        # sized for postcodes' ~2.5MB pages) was rejecting these as if they
        # were a runaway/hung response, failing every refresh at that offset.
        # Override per-dataset here rather than raising the global default,
        # which would blunt that cap's ability to catch an actual hang on
        # lighter endpoints.
        "max_page_bytes": 150 * 1024 * 1024,
        "field_mapping": {
            "lad_code": "LAD25CD",
            "name": "LAD25NM",
            "name_welsh": "LAD25NMW",
            "area_sqm": "Shape__Area",
            "perimeter_m": "Shape__Length",
        },
        "float_fields": ["area_sqm", "perimeter_m"],
        "tile_fields": ["lad_code", "name"],
        "code_column": "lad_code",
    },
    "msoa": {
        "kind": "arcgis",
        "polygon": True,
        "feature_server": (
            "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
            "MSOA_2021_EW_BFE_V8_RUC/FeatureServer/3/query"
        ),
        "page_size": 150,
        "max_workers": 4,
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
        "code_column": "msoa_code",
    },
    "oa": {
        "kind": "arcgis",
        "polygon": True,
        # BGC ("Boundaries Generalised, Clipped") variant, not the full-
        # resolution BFE used for uk_districts/msoa — deliberately: at
        # 188,880 features (~26x msoa's count), full-resolution coastline
        # detail per polygon would make both the fetch and the tippecanoe
        # run impractically heavy for what's the UK census's smallest,
        # unnamed geography (an Output Area has no place name, just a code
        # — see field_mapping below, there's no "name" field to map).
        "feature_server": (
            "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
            "OA_2021_EW_BGC_V2_RUC/FeatureServer/0/query"
        ),
        # Benchmarked directly against this endpoint before picking these
        # (see the "don't assume a page size transfers to a new endpoint"
        # rule in sources/arcgis.py's docstring): maxRecordCount is 2000,
        # and each 2000-feature page is only ~1KB/feature here (BGC is far
        # lighter than uk_districts/msoa's full-resolution boundaries), so
        # unlike those two, the ceiling here is server-side pagination, not
        # payload weight — page_size is set to the server's own max.
        "page_size": 2000,
        "max_workers": 4,
        # See "max_zoom (optional)" above — `-zg` guessed 9 for this dataset
        # (vs. 13 for uk_districts/msoa), far too coarse for ~770m-wide
        # polygons: below zoom ~13 each OA is under a pixel, so tippecanoe
        # substituted placeholder squares instead of real shape. 14 keeps an
        # average OA comfortably multi-pixel-wide at full zoom.
        "max_zoom": 14,
        "field_mapping": {
            "oa_code": "OA21CD",
            "lsoa_code": "LSOA21CD",
            "lsoa_name": "LSOA21NM",
            "lsoa_name_welsh": "LSOA21NMW",
            "ruc_code": "RUC21CD",
            "ruc_name": "RUC21NM",
            "area_sqm": "Shape__Area",
            "perimeter_m": "Shape__Length",
        },
        "float_fields": ["area_sqm", "perimeter_m"],
        # No "name" field exists for an Output Area (see above) — lsoa_name
        # (its parent geography, which *is* named) is the closest
        # human-readable label available when identifying one on the map.
        "tile_fields": ["oa_code", "lsoa_name"],
        "code_column": "oa_code",
    },
    "postcodes": {
        "kind": "arcgis",
        "polygon": False,
        # The "Live" table (current, non-terminated postcodes only —
        # 1,810,364 of them, confirmed via returnCountOnly) rather than a
        # dated quarterly snapshot (~2.7M, includes historical/terminated
        # postcodes) — a current lookup table is what's actually useful
        # here, not postcode history.
        "feature_server": (
            "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
            "Online_ONS_Postcode_Directory_Live/FeatureServer/0/query"
        ),
        # Benchmarked directly (same rule as oa/msoa): maxRecordCount is
        # 2000 and each 2000-row page is a lean ~1.2KB/row despite 57
        # available source fields (most are short fixed-width codes) — the
        # server's own page-size ceiling is the limit here, not payload
        # weight, same situation as oa.
        "page_size": 2000,
        "max_workers": 4,
        "field_mapping": {
            "postcode": "PCDS",  # single-space "standard" form, e.g. "AB1 2CD" — not the fixed-width PCD7/PCD8
            "oa_code": "OA21CD",  # matches this app's own `oa` dataset's oa_code exactly — a direct equi-join, no spatial join needed
            "lsoa_code": "LSOA21CD",
            "msoa_code": "MSOA21CD",  # matches `msoa`'s msoa_code exactly, same reasoning as oa_code above
            "lad_code": "LAD26CD",  # Dec 2026 vintage — one year newer than uk_districts' LAD25CD; still useful, just not a guaranteed exact-code join against that dataset
            "region_code": "RGN26CD",
            "country_code": "CTRY26CD",
            "constituency_code": "PCON24CD",
            "ruc_code": "RUC21IND",
            "imd_rank": "IMD25IND",  # Index of Multiple Deprivation rank — the actual "explore deprivation" data this app didn't have before
            "date_introduced": "DOINTR",
            "lat": "LAT",
            "lon": "LONG",
        },
        "float_fields": ["lat", "lon", "imd_rank"],
        # No tileset for a point dataset this large — see "polygon" above.
        "tile_fields": [],
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
