import { useCallback, useEffect, useState } from "react";
import Sidebar from "./components/Sidebar.jsx";
import QueryEditor from "./components/QueryEditor.jsx";
import ResultsGrid from "./components/ResultsGrid.jsx";
import MapView from "./components/MapView.jsx";
import { getDatasets, getHealth, refreshDatasets, runQuery, CHUNK } from "./api";

const DEFAULT_SQL = `SELECT region, category, revenue, lat, lon
FROM sales_events
WHERE revenue > 500
LIMIT 5000;`;

export default function App() {
  const [datasets, setDatasets] = useState([]);
  const [sql, setSql] = useState(DEFAULT_SQL);
  const [activeQuery, setActiveQuery] = useState(null);
  const [status, setStatus] = useState("idle"); // idle | running | ok | error
  const [error, setError] = useState(null);
  const [elapsed, setElapsed] = useState(null);
  const [resultTab, setResultTab] = useState("table"); // table | map
  const [aiEnabled, setAiEnabled] = useState(false);
  const [viewMode, setViewMode] = useState("query"); // query | full
  const [fullDatasetResult, setFullDatasetResult] = useState(null);
  const [fullDatasetLoading, setFullDatasetLoading] = useState(false);

  const loadDatasets = useCallback(async () => {
    try {
      setDatasets(await getDatasets());
    } catch {
      setError("Can't reach the backend on :8000 — is it running?");
      setStatus("error");
    }
  }, []);

  useEffect(() => {
    loadDatasets();
    getHealth()
      .then((h) => setAiEnabled(!!h.aiEnabled))
      .catch(() => {});
  }, [loadDatasets]);

  const execute = useCallback(
    async (text) => {
      const q = (text ?? sql).trim();
      if (!q) return;
      setStatus("running");
      setError(null);
      const t0 = performance.now();
      try {
        const res = await runQuery({ sql: q, offset: 0, limit: CHUNK, include_total: true });
        setElapsed(Math.round(performance.now() - t0));
        setActiveQuery({
          id: Date.now(),
          sql: q,
          columns: res.columns,
          total: res.total,
          geometry: res.geometry,
          polygonSource: res.polygonSource,
          polygonZoom: res.polygonZoom,
          baseTable: res.baseTable,
          initialRows: res.rows,
        });
        // Fall back to the table if the new result isn't mappable.
        if (!res.geometry && !res.polygonSource) setResultTab("table");
        setViewMode("query");
        setFullDatasetResult(null);
        setStatus("ok");
      } catch (e) {
        setError(String(e.message || e));
        setStatus("error");
      }
    },
    [sql]
  );

  const openDataset = useCallback(
    (name) => {
      // A small preview LIMIT is fine for the table view, but it starves the
      // polygon map: land_parcels tiles a packed tessellation, and a 1000-row
      // (2%) slice of it leaves visible gaps almost everywhere. Tiled
      // datasets get a limit that comfortably covers the whole tileset
      // instead (still capped, just not at table-preview size).
      const hasTiles = datasets.find((d) => d.name === name)?.hasTiles;
      const limit = hasTiles ? 50000 : 1000;
      const text = `SELECT *\nFROM ${name}\nLIMIT ${limit};`;
      setSql(text);
      execute(text);
    },
    [execute, datasets]
  );

  const loadFullDataset = useCallback(async () => {
    if (!activeQuery?.baseTable) return;
    setViewMode("full");
    if (fullDatasetResult) return; // already cached for this query
    setFullDatasetLoading(true);
    try {
      const fullSql = `SELECT * FROM "${activeQuery.baseTable}"`;
      const res = await runQuery({ sql: fullSql, offset: 0, limit: CHUNK, include_total: true });
      setFullDatasetResult({
        sql: fullSql,
        columns: res.columns,
        total: res.total,
        geometry: res.geometry,
        polygonSource: res.polygonSource,
        polygonZoom: res.polygonZoom,
        initialRows: res.rows,
      });
    } catch (e) {
      setError(String(e.message || e));
      setViewMode("query");
    } finally {
      setFullDatasetLoading(false);
    }
  }, [activeQuery, fullDatasetResult]);

  const insertColumn = useCallback((col) => {
    setSql((prev) => (prev ? `${prev} ${col}` : col));
  }, []);

  const refresh = useCallback(async () => {
    try {
      setDatasets(await refreshDatasets());
    } catch {
      /* ignore */
    }
  }, []);

  const displayQuery =
    viewMode === "full" && fullDatasetResult
      ? { ...activeQuery, ...fullDatasetResult, id: `${activeQuery.id}-full` }
      : activeQuery;
  const hasGeometry = !!(displayQuery?.geometry || displayQuery?.polygonSource);

  return (
    <div className="app">
      <Sidebar
        datasets={datasets}
        onOpenDataset={openDataset}
        onInsertColumn={insertColumn}
        onRefresh={refresh}
      />

      <div className="main">
        <QueryEditor
          sql={sql}
          setSql={setSql}
          onRun={() => execute()}
          status={status}
          aiEnabled={aiEnabled}
        />

        <div className="results">
          <div className="res-tabbar">
            <button
              className={`res-tab ${resultTab === "table" ? "active" : ""}`}
              onClick={() => setResultTab("table")}
            >
              Table
            </button>
            {hasGeometry && (
              <button
                className={`res-tab ${resultTab === "map" ? "active" : ""}`}
                onClick={() => setResultTab("map")}
              >
                Map
              </button>
            )}
            {activeQuery && status !== "error" && activeQuery.baseTable && (
              <div className="seg" role="tablist" aria-label="Data source">
                <button
                  className={`seg-btn ${viewMode === "query" ? "active" : ""}`}
                  onClick={() => setViewMode("query")}
                >
                  Results
                </button>
                <button
                  className={`seg-btn ${viewMode === "full" ? "active" : ""}`}
                  onClick={loadFullDataset}
                >
                  {fullDatasetLoading ? "Loading…" : "Full Dataset"}
                </button>
              </div>
            )}
            {activeQuery && status !== "error" && (
              <span className="res-caption">
                {resultTab === "map"
                  ? displayQuery.polygonSource
                    ? "polygons synced to query"
                    : "points synced to query"
                  : `${displayQuery.total?.toLocaleString()} rows`}
              </span>
            )}
          </div>

          <div className="results-body">
            {error ? (
              <div className="error-banner">{error}</div>
            ) : !activeQuery ? (
              <div className="empty">
                Run a query to see results, or click a dataset on the left to start.
              </div>
            ) : (
              <>
                <div
                  className="view-layer"
                  style={{ display: resultTab === "table" ? "block" : "none" }}
                >
                  <ResultsGrid query={displayQuery} />
                </div>
                {resultTab === "map" && hasGeometry && (
                  <div className="view-layer">
                    <MapView key={displayQuery.id} query={displayQuery} />
                  </div>
                )}
              </>
            )}
          </div>

          <div className="statusbar">
            <span className={`dot ${status}`} />
            <span>
              {status === "running"
                ? "Running…"
                : status === "ok"
                ? "Ready"
                : status === "error"
                ? "Error"
                : "Idle"}
            </span>
            {activeQuery && status !== "error" && (
              <span className="sep">· {displayQuery.total?.toLocaleString()} rows</span>
            )}
            {elapsed != null && status !== "error" && <span className="sep">· {elapsed} ms</span>}
            {hasGeometry && status !== "error" && (
              <span className="sep">· geometry detected</span>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
