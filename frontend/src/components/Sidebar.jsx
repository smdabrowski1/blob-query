import { useEffect, useState } from "react";
import { activateVersion, getVersions } from "../api";
import { formatVersionUK } from "../formatTime";

const PHASE_LABEL = {
  fetching: "Fetching…",
  validating: "Validating…",
  tiling: "Tiling…",
  active: "Done",
  failed: "Failed",
};

const RUNNING_STATUSES = new Set(["fetching", "validating", "tiling"]);

// Job status/progress now lives in App.jsx (refreshJobs), not here — it has
// to outlive this row (minimized modal, page reload) which a per-row poll
// can't do. This component just renders whatever slice App hands it.
function DatasetRefreshControl({ name, hasTiles, status, onStartRefresh, onReopenRefresh, onRefreshed }) {
  const [showVersions, setShowVersions] = useState(false);
  const [versions, setVersions] = useState(null);

  useEffect(() => {
    if (!showVersions) return;
    const onDocClick = () => setShowVersions(false);
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [showVersions]);

  const startRefresh = (e) => {
    e.stopPropagation();
    onStartRefresh(name);
  };

  const toggleVersions = async (e) => {
    e.stopPropagation();
    if (showVersions) {
      setShowVersions(false);
      return;
    }
    setShowVersions(true);
    try {
      setVersions(await getVersions(name));
    } catch {
      setVersions([]);
    }
  };

  const activate = async (version) => {
    try {
      await activateVersion(name, version);
      setVersions(await getVersions(name));
      onRefreshed();
    } catch (err) {
      window.alert(`Failed to activate version ${version}: ${err.message || err}`);
    }
  };

  const running = status && RUNNING_STATUSES.has(status.status);

  return (
    <div className="ds-refresh">
      {status && (
        <button
          className={`ds-refresh-phase ${status.status === "failed" ? "failed" : ""}`}
          title={status.error || "Click to view refresh progress"}
          onClick={(e) => {
            e.stopPropagation();
            onReopenRefresh(name);
          }}
        >
          {PHASE_LABEL[status.status] || status.status}
        </button>
      )}
      <button
        className="icon-btn ds-refresh-btn"
        title={hasTiles ? "Refresh (re-fetch + re-tile)" : "Refresh (re-fetch)"}
        onClick={startRefresh}
        disabled={running}
      >
        <span className={running ? "spin" : ""}>⟳</span>
      </button>
      <button className="icon-btn" title="Version history" onClick={toggleVersions}>
        🕘
      </button>
      {showVersions && (
        <div className="version-list" onClick={(e) => e.stopPropagation()}>
          {versions === null ? (
            <div className="version-empty">Loading…</div>
          ) : versions.length === 0 ? (
            <div className="version-empty">No versions yet</div>
          ) : (
            versions.map((v) => (
              <button
                key={v.version}
                className={`version-row ${v.is_current ? "current" : ""}`}
                onClick={() => activate(v.version)}
                disabled={!!v.is_current || (v.status !== "active" && v.status !== "superseded")}
                title={v.error || undefined}
              >
                <span className="version-check">{v.is_current ? "✓" : ""}</span>
                <span className="version-ts">{formatVersionUK(v.version)}</span>
                <span className="version-rows">{v.row_count?.toLocaleString() ?? "—"}</span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

export default function Sidebar({
  datasets,
  onOpenDataset,
  onInsertColumn,
  onRefresh,
  refreshJobs,
  onStartRefresh,
  onReopenRefresh,
}) {
  const [open, setOpen] = useState({});

  const toggle = (name) => setOpen((o) => ({ ...o, [name]: !o[name] }));

  return (
    <aside className="sidebar">
      <div className="sidebar-head">
        <span className="brand">
          parquet<span className="brand-dot">·</span>engine
        </span>
        <button className="icon-btn" title="Rescan the data folder" onClick={onRefresh}>
          ⟳
        </button>
      </div>

      <div className="sidebar-label">Datasets</div>

      <div className="tree">
        {datasets.length === 0 && (
          <div className="tree-empty">
            No parquet files found. Drop some into <code>backend/data</code> and refresh.
          </div>
        )}

        {datasets.map((ds) => (
          <div key={ds.name} className="tree-group">
            <div className="tree-node">
              <button className="chevron" onClick={() => toggle(ds.name)}>
                {open[ds.name] ? "▾" : "▸"}
              </button>
              <button
                className="ds-name"
                onClick={() => onOpenDataset(ds.name)}
                title="Open — SELECT * … LIMIT 1000"
              >
                {ds.name}
              </button>
              <span className="ds-rows">{ds.rows.toLocaleString()}</span>
              <DatasetRefreshControl
                name={ds.name}
                hasTiles={ds.hasTiles}
                status={refreshJobs[ds.name] || null}
                onStartRefresh={onStartRefresh}
                onReopenRefresh={onReopenRefresh}
                onRefreshed={onRefresh}
              />
            </div>

            {open[ds.name] && (
              <div className="cols">
                {ds.columns.map((c) => (
                  <button
                    key={c.name}
                    className="col-row"
                    onClick={() => onInsertColumn(c.name)}
                    title="Insert this column into the editor"
                  >
                    <span className="col-name">{c.name}</span>
                    <span className="col-type">{c.type}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </aside>
  );
}
