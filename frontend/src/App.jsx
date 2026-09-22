import { useCallback, useEffect, useRef, useState } from "react";
import Sidebar from "./components/Sidebar.jsx";
import QueryEditor from "./components/QueryEditor.jsx";
import ResultsGrid from "./components/ResultsGrid.jsx";
import MapView from "./components/MapView.jsx";
import RefreshModal from "./components/RefreshModal.jsx";
import {
  getDatasets,
  getHealth,
  refreshDatasets,
  refreshDataset,
  getRefreshStatus,
  getRefreshJobs,
  runQuery,
  CHUNK,
} from "./api";

const DEFAULT_SQL = `SELECT region, category, revenue, lat, lon
FROM sales_events
WHERE revenue > 500
LIMIT 5000;`;

// A job legitimately in flight lives in this state — anything else (idle,
// active, failed) is terminal and stops polling.
const RUNNING_STATUSES = new Set(["fetching", "validating", "tiling"]);

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

  // Refresh-job tracking lives here, not per-Sidebar-row, so a job's progress
  // modal can be minimized/reopened independent of the row that started it,
  // and survives a page reload (the job itself runs server-side — see
  // getRefreshJobs() below, which rediscovers anything still in flight).
  const [refreshJobs, setRefreshJobs] = useState({});
  const pollRefs = useRef({});

  const stopPollingJob = useCallback((name) => {
    if (pollRefs.current[name]) {
      clearInterval(pollRefs.current[name]);
      delete pollRefs.current[name];
    }
  }, []);

  const pollJob = useCallback(
    async (name) => {
      try {
        const s = await getRefreshStatus(name);
        setRefreshJobs((jobs) => {
          if (!(name in jobs)) return jobs; // dismissed while this request was in flight
          return { ...jobs, [name]: { ...jobs[name], ...s } };
        });
        if (!RUNNING_STATUSES.has(s.status)) {
          stopPollingJob(name);
          if (s.status === "active") loadDatasets();
        }
      } catch {
        stopPollingJob(name);
      }
    },
    [stopPollingJob] // eslint-disable-line react-hooks/exhaustive-deps
  );

  const startPollingJob = useCallback(
    (name) => {
      stopPollingJob(name);
      pollRefs.current[name] = setInterval(() => pollJob(name), 2000);
    },
    [pollJob, stopPollingJob]
  );

  const startRefresh = useCallback(
    async (name) => {
      try {
        await refreshDataset(name);
        setRefreshJobs((jobs) => ({
          ...jobs,
          [name]: { status: "fetching", minimized: false },
        }));
        startPollingJob(name);
      } catch (err) {
        setRefreshJobs((jobs) => ({
          ...jobs,
          [name]: { status: "failed", error: String(err.message || err), minimized: false },
        }));
      }
    },
    [startPollingJob]
  );

  const minimizeJob = useCallback((name) => {
    setRefreshJobs((jobs) => (name in jobs ? { ...jobs, [name]: { ...jobs[name], minimized: true } } : jobs));
  }, []);

  const reopenJob = useCallback((name) => {
    setRefreshJobs((jobs) => (name in jobs ? { ...jobs, [name]: { ...jobs[name], minimized: false } } : jobs));
  }, []);

  const dismissJob = useCallback(
    (name) => {
      stopPollingJob(name);
      setRefreshJobs((jobs) => {
        const { [name]: _drop, ...rest } = jobs;
        return rest;
      });
    },
    [stopPollingJob]
  );

  // Discover jobs already running on the server (survives a page reload or
  // the app having crashed mid-job) — start them minimized, since a job the
  // user didn't just click shouldn't pop an intrusive modal on every load.
  useEffect(() => {
    getRefreshJobs()
      .then((jobs) => {
        for (const j of jobs) {
          setRefreshJobs((prev) => ({ ...prev, [j.name]: { ...j, minimized: true } }));
          startPollingJob(j.name);
        }
      })
      .catch(() => {});
    return () => {
      Object.keys(pollRefs.current).forEach((name) => clearInterval(pollRefs.current[name]));
      pollRefs.current = {};
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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
      const text = `SELECT *\nFROM ${name}\nLIMIT 1000;`;
      setSql(text);
      execute(text);
    },
    [execute]
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
        refreshJobs={refreshJobs}
        onStartRefresh={startRefresh}
        onReopenRefresh={reopenJob}
        onDatasetsUpdated={setDatasets}
      />

      <RefreshModal jobs={refreshJobs} onMinimize={minimizeJob} onDismiss={dismissJob} />

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
            <div className="seg" role="tablist" aria-label="View">
              <button
                className={`seg-btn ${resultTab === "table" ? "active" : ""}`}
                onClick={() => setResultTab("table")}
              >
                Table
              </button>
              {hasGeometry && (
                <button
                  className={`seg-btn ${resultTab === "map" ? "active" : ""}`}
                  onClick={() => setResultTab("map")}
                >
                  Map
                </button>
              )}
            </div>

            {/* This whole trailing group (source-segment + row count) is one
                right-anchored unit — margin-left:auto lives only on it, not
                also on the caption, so the group's position doesn't shift
                depending on whether the caption happens to be rendered
                (previously: both had their own auto margin, splitting the
                free space between them and making the segment drift toward
                center on the table tab, flush-right only on the map tab
                where the caption is absent). */}
            <div className="res-tabbar-trailing">
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
            </div>
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
          </div>
        </div>
      </div>
    </div>
  );
}
