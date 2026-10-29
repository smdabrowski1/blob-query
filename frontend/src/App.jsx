import { useCallback, useEffect, useRef, useState } from "react";
import Sidebar from "./components/Sidebar.jsx";
import QueryEditor from "./components/QueryEditor.jsx";
import ResultsGrid from "./components/ResultsGrid.jsx";
import MapView from "./components/MapView.jsx";
import RefreshModal from "./components/RefreshModal.jsx";
import DiscoverChat from "./components/DiscoverChat.jsx";
import ServerDisclaimer from "./components/ServerDisclaimer.jsx";
import { isServerDisclaimerDismissed, dismissServerDisclaimer } from "./serverDisclaimer.js";
import {
  getDatasets,
  getHealth,
  refreshDatasets,
  refreshDataset,
  getRefreshStatus,
  getRefreshJobs,
  runQuery,
  discoverChat,
  CHUNK,
} from "./api";

const DEFAULT_SQL = `SELECT region, category, revenue, lat, lon
FROM sales_events
WHERE revenue > 500
LIMIT 5000;`;

// A job legitimately in flight lives in this state — anything else (idle,
// active, failed) is terminal and stops polling.
const RUNNING_STATUSES = new Set(["fetching", "validating", "tiling"]);

function ChatIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
    </svg>
  );
}

export default function App() {
  // Shared with QueryEditor.jsx's copy of this same banner — dismissing
  // either one dismisses both, for the current browser tab session.
  const [disclaimerDismissed, setDisclaimerDismissed] = useState(isServerDisclaimerDismissed);
  const dismissDisclaimer = () => {
    dismissServerDisclaimer();
    setDisclaimerDismissed(true);
  };

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

  // Lives here, not per-Sidebar-row, so the progress modal can be
  // minimized/reopened independent of the row that started it, and
  // survives a page reload (getRefreshJobs() below rediscovers in-flight jobs).
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

  // Discover jobs already running server-side and start them minimized —
  // a job the user didn't just click shouldn't pop a modal on every load.
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
          choropleth: res.choropleth,
          choroplethZoom: res.choroplethZoom,
          choroplethSecondary: res.choroplethSecondary,
          baseTable: res.baseTable,
          initialRows: res.rows,
        });
        // Fall back to the table if the new result isn't mappable.
        if (!res.geometry && !res.polygonSource && !res.choropleth) setResultTab("table");
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
        choropleth: res.choropleth,
        choroplethZoom: res.choroplethZoom,
        choroplethSecondary: res.choroplethSecondary,
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

  // One conversation shared by the landing screen and the persistent side
  // panel (same DiscoverChat component, same state), so dismissing the
  // landing screen doesn't lose or restart it. Stateless server-side, like
  // the "Ask AI" tab — this array is the history, resent in full each turn.
  const [chatMessages, setChatMessages] = useState([]);
  const [chatSending, setChatSending] = useState(false);
  const [chatError, setChatError] = useState(null);
  const [showLanding, setShowLanding] = useState(true);
  const [chatPanelOpen, setChatPanelOpen] = useState(false);
  // Datasets the chat most recently pointed at — Sidebar glows these rows
  // so a suggestion in the chat has a visible landing spot in the tree, not
  // just prose. Replaced wholesale by each new reply, not accumulated.
  const [highlightedDatasets, setHighlightedDatasets] = useState([]);

  const sendChatMessage = useCallback(
    async (text) => {
      const nextHistory = [...chatMessages, { role: "user", content: text }];
      setChatMessages(nextHistory);
      setChatSending(true);
      setChatError(null);
      try {
        const { reply, suggestions } = await discoverChat(
          nextHistory.map((m) => ({ role: m.role, content: m.content }))
        );
        setChatMessages((prev) => [...prev, { role: "assistant", content: reply, suggestions }]);
        const mentioned = [...new Set(suggestions.flatMap((s) => s.datasets))];
        if (mentioned.length) setHighlightedDatasets(mentioned);
      } catch (e) {
        setChatError(String(e.message || e));
      } finally {
        setChatSending(false);
      }
    },
    [chatMessages]
  );

  // Reuses the same openDataset path a sidebar click does — the chat is a
  // second entry point into it, not a parallel one — plus dismisses the
  // landing screen and keeps the dataset highlighted.
  const useSuggestedDataset = useCallback(
    (name) => {
      openDataset(name);
      setHighlightedDatasets([name]);
      setShowLanding(false);
    },
    [openDataset]
  );

  // Lands in the editor for review, same as "Ask AI" generated SQL —
  // never auto-run (see CLAUDE.md's natural-language SQL section).
  const useSuggestedQuery = useCallback((sql, datasets) => {
    setSql(sql);
    setHighlightedDatasets(datasets || []);
    setShowLanding(false);
  }, []);

  // Shared by the toggle button and the panel's own × so both close the
  // same way and clear the highlight.
  const closeChatPanel = useCallback(() => {
    setChatPanelOpen(false);
    setHighlightedDatasets([]);
  }, []);

  const displayQuery =
    viewMode === "full" && fullDatasetResult
      ? { ...activeQuery, ...fullDatasetResult, id: `${activeQuery.id}-full` }
      : activeQuery;
  const hasGeometry = !!(displayQuery?.geometry || displayQuery?.polygonSource || displayQuery?.choropleth);

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
        highlightedDatasets={highlightedDatasets}
      />

      <RefreshModal jobs={refreshJobs} onMinimize={minimizeJob} onDismiss={dismissJob} />

      {showLanding && (
        <div className="landing-overlay">
          <div className="landing-panel">
            <div className="landing-head">
              <span className="landing-badge">AI Assistant</span>
              <h1>Meet your data assistant</h1>
            </div>
            {!disclaimerDismissed && <ServerDisclaimer onDismiss={dismissDisclaimer} />}
            <DiscoverChat
              variant="landing"
              messages={chatMessages}
              sending={chatSending}
              error={chatError}
              aiEnabled={aiEnabled}
              onSend={sendChatMessage}
              onOpenDataset={useSuggestedDataset}
              onUseQuery={useSuggestedQuery}
            />
            <button className="landing-skip" onClick={() => setShowLanding(false)}>
              Skip and go straight to the workbench
            </button>
          </div>
        </div>
      )}

      {!showLanding && (
        <>
          <div className={`chat-panel ${chatPanelOpen ? "open" : ""}`}>
            <div className="chat-panel-head">
              <span className="chat-panel-title">Discover</span>
              <button className="icon-btn" onClick={closeChatPanel} aria-label="Close">
                ×
              </button>
            </div>
            <DiscoverChat
              variant="panel"
              messages={chatMessages}
              sending={chatSending}
              error={chatError}
              aiEnabled={aiEnabled}
              onSend={sendChatMessage}
              onOpenDataset={useSuggestedDataset}
              onUseQuery={useSuggestedQuery}
            />
          </div>
        </>
      )}

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

            {/* margin-left:auto lives only on this group, not also on the
                caption, so its position doesn't shift depending on whether
                the caption is rendered. */}
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
            {/* Lives in the statusbar rather than floating over the map —
                a fixed-position button kept colliding with MapLibre's own
                bottom-right attribution control. */}
            <button
              className={`chat-toggle-btn ${chatPanelOpen ? "active" : ""}`}
              onClick={() => (chatPanelOpen ? closeChatPanel() : setChatPanelOpen(true))}
              title={chatPanelOpen ? "Turn off AI guide" : "Turn on AI guide"}
            >
              <ChatIcon />
              <span className="chat-toggle-label">{chatPanelOpen ? "AI Guide: On" : "AI Guide"}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
