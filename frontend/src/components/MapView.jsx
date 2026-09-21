import { useEffect, useRef, useState } from "react";
import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { runQuery, MAP_LIMIT, POLYGON_ID_LIMIT, TILE_BASE } from "../api";

// CARTO's hosted Positron style: a light basemap with borders/labels, served
// over the network. Points layer below is unchanged and basemap-agnostic.
const BASE_STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";

// Turn a {key: value} object into the feature-panel's row shape, dropping the
// raw WKT geometry blob (both polygon datasets name it literally "geometry")
// — that's storage, not a display attribute.
function toRows(entries) {
  return entries
    .filter(([k]) => k.toLowerCase() !== "geometry")
    .map(([key, value]) => ({
      key,
      value: value === null || value === undefined ? "—" : String(value),
    }));
}

// Pre-built vector tiles (see backend/generate_polygons.py + tippecanoe) are
// static — the query never changes their geometry. What changes per query is
// *which* already-tiled polygon ids are visible, applied as a MapLibre filter.
// Queries that don't keep `id` in their projection just show nothing (logged
// to the console, not surfaced as UI chrome), rather than erroring visibly.
async function loadPolygonLayer(map, query, setSelectedFeature, highlightRef, isCancelled) {
  const name = query.polygonSource;
  // Strip a trailing ";" before embedding — it's fine as the outermost
  // statement terminator but breaks once nested inside `(...)`.
  const innerSql = query.sql.trim().replace(/;\s*$/, "");

  // minzoom/maxzoom must match what's actually in the .mbtiles (tippecanoe's
  // -zg picks this per run) — declaring a higher maxzoom than exists makes
  // MapLibre request tiles beyond what was tiled (404s, nothing renders)
  // instead of overzooming the deepest available tile as it normally would.
  const [minzoom, maxzoom] = query.polygonZoom || [0, 14];
  map.addSource("polygons", {
    type: "vector",
    tiles: [`${TILE_BASE}/tiles/${name}/{z}/{x}/{y}.pbf`],
    minzoom,
    maxzoom,
  });
  const DEFAULT_FILL_OPACITY = 0.3;
  const DIMMED_FILL_OPACITY = 0.1;
  const DEFAULT_LINE_WIDTH = ["interpolate", ["linear"], ["zoom"], 4, 0.8, 12, 1.6];
  const SELECTED_LINE_WIDTH = ["interpolate", ["linear"], ["zoom"], 4, 2, 12, 4];
  const DEFAULT_LINE_COLOR = "rgba(20, 24, 28, 0.55)";
  map.addLayer({
    id: "polygons-fill",
    type: "fill",
    source: "polygons",
    "source-layer": name,
    paint: {
      "fill-color": "#2f9e44",
      "fill-opacity": DEFAULT_FILL_OPACITY,
    },
  });
  // Separate line layer for borders — fill-outline-color has no width control,
  // so a visible/thicker border needs its own layer.
  map.addLayer({
    id: "polygons-outline",
    type: "line",
    source: "polygons",
    "source-layer": name,
    paint: {
      "line-color": DEFAULT_LINE_COLOR,
      "line-width": DEFAULT_LINE_WIDTH,
    },
  });

  // Selecting a polygon compares each feature's own `id` (from the tile,
  // via ["get","id"]) against the current selection, baked in as a literal
  // each time setPaintProperty runs below — no feature-state/promoteId
  // needed, and it's what lets every *other* polygon dim to 10% opacity so
  // the selected one reads clearly against basemap linework (river/road
  // lines were getting confused with plain polygon borders otherwise).
  const applySelection = (id) => {
    if (id == null) {
      map.setPaintProperty("polygons-fill", "fill-opacity", DEFAULT_FILL_OPACITY);
      map.setPaintProperty("polygons-outline", "line-color", DEFAULT_LINE_COLOR);
      map.setPaintProperty("polygons-outline", "line-width", DEFAULT_LINE_WIDTH);
      return;
    }
    const isSelected = ["==", ["get", "id"], id];
    map.setPaintProperty("polygons-fill", "fill-opacity", ["case", isSelected, DEFAULT_FILL_OPACITY, DIMMED_FILL_OPACITY]);
    map.setPaintProperty("polygons-outline", "line-color", ["case", isSelected, "#ffb703", DEFAULT_LINE_COLOR]);
    map.setPaintProperty("polygons-outline", "line-width", ["case", isSelected, SELECTED_LINE_WIDTH, DEFAULT_LINE_WIDTH]);
  };
  highlightRef.clear = () => applySelection(null);

  // Identify: the tile only carries the small property set baked in at
  // generation time (e.g. id/lad_code/name) — "all attributes" needs a real
  // row fetch by id on click. That fetch wraps the *query's own SQL*, not
  // `SELECT * FROM "<name>"` — so a query with its own aliases/column list
  // (e.g. a join renaming columns to `la_name`/`msoa_name`) shows exactly
  // those names in the panel, not the raw base-table schema.
  map.on("click", "polygons-fill", async (e) => {
    const f = e.features[0];
    const id = f.properties?.id;
    applySelection(id);
    const fallbackTitle = f.properties?.name || f.properties?.lad_code || (id != null ? `#${id}` : "Feature");
    setSelectedFeature({ title: fallbackTitle, rows: [], loading: true });
    if (id == null) {
      setSelectedFeature({ title: fallbackTitle, rows: [], loading: false });
      return;
    }
    try {
      const res = await runQuery({ sql: `SELECT * FROM (${innerSql}) AS _sub WHERE id = ${id}`, limit: 1 });
      const row = res.rows[0];
      if (!row) {
        setSelectedFeature({ title: fallbackTitle, rows: [{ key: "error", value: "Row not found" }], loading: false });
        return;
      }
      const rows = toRows(res.columns.map((c, i) => [c.name, row[i]]));
      const nameRow = rows.find((r) => r.key.toLowerCase() === "name");
      setSelectedFeature({ title: nameRow?.value || fallbackTitle, rows, loading: false });
    } catch (err) {
      setSelectedFeature({
        title: fallbackTitle,
        rows: [{ key: "error", value: String(err.message || err) }],
        loading: false,
      });
    }
  });
  map.on("mouseenter", "polygons-fill", () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", "polygons-fill", () => (map.getCanvas().style.cursor = ""));

  let ids = [];
  try {
    const idRes = await runQuery({
      sql: `SELECT id FROM (${innerSql}) AS _sub`,
      limit: POLYGON_ID_LIMIT,
    });
    ids = idRes.rows.map((r) => r[0]);
  } catch (err) {
    // Most often: the query's SELECT list dropped the `id` column, so there's
    // nothing to filter the pre-built tiles by.
    console.warn("Map: query has no `id` column to filter polygons by", err);
    return;
  }
  if (isCancelled()) return;

  // Both layers need the filter — they're two separate layers over the same
  // vector source (fill has no width-controllable border, so outline is its
  // own layer), and a filter set on one doesn't apply to the other. Leaving
  // "polygons-outline" unfiltered was exactly the bug where every polygon's
  // border still drew regardless of the query, alongside the correctly
  // filtered (and so invisible) fill — reading as "empty" outlined shapes
  // for every excluded polygon rather than nothing at all.
  const idFilter = ["in", ["get", "id"], ["literal", ids]];
  map.setFilter("polygons-fill", idFilter);
  map.setFilter("polygons-outline", idFilter);

  if (ids.length) {
    try {
      const boundsRes = await runQuery({
        sql:
          `SELECT min(centroid_lat), max(centroid_lat), min(centroid_lon), max(centroid_lon) ` +
          `FROM "${name}" WHERE id IN (${ids.join(",")})`,
        limit: 1,
      });
      const [minLat, maxLat, minLon, maxLon] = boundsRes.rows[0] || [];
      if (!isCancelled() && [minLat, maxLat, minLon, maxLon].every((v) => typeof v === "number")) {
        map.fitBounds(
          [
            [minLon, minLat],
            [maxLon, maxLat],
          ],
          { padding: 56, maxZoom: 11, duration: 0 }
        );
      }
    } catch {
      // Framing is a nicety; keep whatever view the map already had.
    }
  }
}

export default function MapView({ query }) {
  const containerRef = useRef(null);
  const [selectedFeature, setSelectedFeature] = useState(null);
  // loadPolygonLayer attaches a .clear() here to reset the selection dim/highlight
  // — called from outside its closure (background click, panel's × button). A
  // ref since it drives MapLibre paint directly, not something React re-renders on.
  const highlightRef = useRef(null);

  // Keyed by query.id at the parent, so this component remounts per query and
  // this effect runs once: build the map, then load points/polygons.
  useEffect(() => {
    const map = new maplibregl.Map({
      container: containerRef.current,
      style: BASE_STYLE,
      center: [-1.7, 52.9],
      zoom: 4.6,
      attributionControl: true,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }));

    let cancelled = false;

    map.on("load", async () => {
      map.addSource("points", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.addLayer({
        id: "points",
        type: "circle",
        source: "points",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 2.5, 7, 4.5, 11, 8],
          "circle-color": "#0d9488",
          "circle-opacity": 0.7,
          "circle-stroke-color": "rgba(0,0,0,0.35)",
          "circle-stroke-width": 1,
        },
      });

      map.on("click", "points", (e) => {
        const f = e.features[0];
        setSelectedFeature({ title: "Point details", rows: toRows(Object.entries(f.properties || {})), loading: false });
      });
      map.on("mouseenter", "points", () => (map.getCanvas().style.cursor = "pointer"));
      map.on("mouseleave", "points", () => (map.getCanvas().style.cursor = ""));

      // Clicking empty map (no feature under the cursor, on any identify-able
      // layer) closes the panel. Layer-specific click handlers above fire for
      // feature clicks; this re-checks the same point independently, so it's
      // correct regardless of handler firing order.
      map.on("click", (e) => {
        const layers = ["points"];
        if (map.getLayer("polygons-fill")) layers.push("polygons-fill", "polygons-outline");
        const hits = map.queryRenderedFeatures(e.point, { layers });
        if (hits.length === 0) {
          setSelectedFeature(null);
          highlightRef.clear?.();
        }
      });

      if (query.polygonSource) {
        await loadPolygonLayer(map, query, setSelectedFeature, highlightRef, () => cancelled);
        return;
      }

      try {
        const res = await runQuery({ sql: query.sql, limit: MAP_LIMIT });
        if (cancelled) return;

        const li = res.columns.findIndex((c) => c.name === query.geometry.lat);
        const oi = res.columns.findIndex((c) => c.name === query.geometry.lon);

        const features = [];
        const bounds = new maplibregl.LngLatBounds();
        for (const row of res.rows) {
          const lat = row[li];
          const lon = row[oi];
          if (typeof lat === "number" && typeof lon === "number" && isFinite(lat) && isFinite(lon)) {
            const props = {};
            res.columns.forEach((c, i) => {
              if (i !== li && i !== oi) props[c.name] = row[i];
            });
            features.push({
              type: "Feature",
              geometry: { type: "Point", coordinates: [lon, lat] },
              properties: props,
            });
            bounds.extend([lon, lat]);
          }
        }

        map.getSource("points").setData({ type: "FeatureCollection", features });
        if (features.length) map.fitBounds(bounds, { padding: 56, maxZoom: 11, duration: 0 });
      } catch (e) {
        if (!cancelled) console.error("Map: couldn't load points", e);
      }
    });

    return () => {
      cancelled = true;
      map.remove();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="mapview">
      <div className="map-container" ref={containerRef} />
      <div className={`feature-panel ${selectedFeature ? "open" : ""}`}>
        {selectedFeature && (
          <>
            <div className="fp-header">
              <span className="fp-title">{selectedFeature.title}</span>
              <button
                className="fp-close"
                onClick={() => {
                  setSelectedFeature(null);
                  highlightRef.clear?.();
                }}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <div className="fp-body">
              {selectedFeature.loading ? (
                <div className="fp-loading">Loading attributes…</div>
              ) : (
                selectedFeature.rows.map((r) => (
                  <div className="fp-row" key={r.key}>
                    <span className="fp-key">{r.key}</span>
                    <span className="fp-val">{r.value}</span>
                  </div>
                ))
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
