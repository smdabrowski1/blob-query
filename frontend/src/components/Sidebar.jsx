import { useCallback, useEffect, useRef, useState } from "react";
import { activateVersion, getVersions, setColumnDescription } from "../api";
import { formatVersionUK } from "../formatTime";
import ResizableTextarea from "./ResizableTextarea.jsx";

const DEFAULT_SIDEBAR_WIDTH = 300;
const MIN_SIDEBAR_WIDTH = 240;
const MAX_SIDEBAR_WIDTH = 640;

// A plain chevron glyph (▸/▾) rendered close to invisible in the self-hosted
// Inter build this app uses — an SVG stroke icon renders identically
// regardless of font/glyph coverage, and reads as "light" (thin stroke)
// rather than a filled triangle glyph the way a font character would.
function ChevronIcon({ open }) {
  return (
    <svg
      className={`chevron-icon ${open ? "open" : ""}`}
      width="11"
      height="11"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M5.5 3.5l5 4.5-5 4.5" />
    </svg>
  );
}

// Brand mark: an abstract "blob" — literal nod to the name (BlobQuery), and
// distinct from a generic database-cylinder icon that'd read as "just any
// SQL tool" rather than this app's actual point (querying data that lives
// in blob storage — S3/Azure Blob — not just local files; see engine.py's
// storage-agnostic design).
function BrandIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M12 3c4.5 0 9 2.1 9 7.2S16.8 18 12 18s-9-3-9-7.8S7.5 3 12 3z"
        fill="var(--accent)"
      />
    </svg>
  );
}

// The standard Material Design "refresh" glyph — a filled shape (fill, no
// stroke), not built from arcs/chevrons by hand like the earlier attempts
// here, which kept coming out mis-shaped or mis-sized. It naturally fills
// roughly the same 16×15px footprint of its 24x24 grid as HistoryIcon's own
// r=8 circle does, so rendering both at the same width/height keeps them
// visually matched without needing hand-tuned geometry to line up.
function RefreshIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
      <path d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-8 3.58-8 8s3.58 8 8 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z" />
    </svg>
  );
}

function HistoryIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="8" />
      <polyline points="12 7.5 12 12 15 13.8" />
    </svg>
  );
}

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
        <span className={`refresh-icon-wrap ${running ? "spin" : ""}`}>
          <RefreshIcon />
        </span>
      </button>
      <button className="icon-btn" title="Version history" onClick={toggleVersions}>
        <HistoryIcon />
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

// Descriptions feed the "Ask AI" NL->SQL prompt (see main.py's
// _schema_prompt) — this is the editor for them. Keyed by (dataset, column)
// server-side, not by version, so a value set here survives every future
// refresh of this dataset automatically; it only changes when someone edits
// it again here.
function ColumnRow({ datasetName, column, onInsertColumn, onDatasetsUpdated }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(column.description || "");
  const [saving, setSaving] = useState(false);

  const startEdit = (e) => {
    e.stopPropagation();
    setDraft(column.description || "");
    setEditing(true);
  };

  const cancel = (e) => {
    e.stopPropagation();
    setEditing(false);
  };

  const save = async (e) => {
    e.stopPropagation();
    setSaving(true);
    try {
      const datasets = await setColumnDescription(datasetName, column.name, draft);
      onDatasetsUpdated(datasets);
      setEditing(false);
    } catch (err) {
      window.alert(`Failed to save description: ${err.message || err}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="col-row">
      <div className="col-row-main">
        <button
          className="col-row-insert"
          onClick={() => onInsertColumn(column.name)}
          title="Insert this column into the editor"
        >
          <span className="col-name">{column.name}</span>
          <span className="col-type">{column.type}</span>
        </button>
        <button className="icon-btn col-edit-btn" title="Edit description" onClick={startEdit}>
          ✎
        </button>
      </div>

      {editing ? (
        <div className="col-desc-edit" onClick={(e) => e.stopPropagation()}>
          <ResizableTextarea
            className="col-desc-input"
            defaultHeight={52}
            minHeight={36}
            maxHeight={300}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="What does this column mean? Helps Ask AI generate more accurate SQL."
            autoFocus
          />
          <div className="col-desc-actions">
            <button className="col-desc-btn" onClick={cancel} disabled={saving}>
              Cancel
            </button>
            <button className="col-desc-btn primary" onClick={save} disabled={saving}>
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      ) : (
        column.description && (
          <div className="col-desc" title={column.description} onClick={(e) => e.stopPropagation()}>
            {column.description}
          </div>
        )
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
  onDatasetsUpdated,
}) {
  const [open, setOpen] = useState({});
  const [width, setWidth] = useState(DEFAULT_SIDEBAR_WIDTH);
  const draggingRef = useRef(false);

  const toggle = (name) => setOpen((o) => ({ ...o, [name]: !o[name] }));

  // Drag-to-resize from the sidebar's right edge. Width is plain component
  // state, not persisted (localStorage) — consistent with the rest of the
  // frontend not depending on browser storage; it just resets to the
  // default on reload, same as every other view preference here (open/
  // closed dataset rows, active result tab, etc.).
  const startResize = useCallback((e) => {
    e.preventDefault();
    draggingRef.current = true;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  useEffect(() => {
    const onMove = (e) => {
      if (!draggingRef.current) return;
      setWidth(Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, e.clientX)));
    };
    const onUp = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, []);

  return (
    <aside className="sidebar" style={{ width, flexBasis: width }}>
      <div
        className="sidebar-resize-handle"
        onMouseDown={startResize}
        title="Drag to resize"
      />
      <div className="sidebar-head">
        <span className="brand">
          <BrandIcon />
          Blob<span className="brand-accent">Query</span>
        </span>
        <button className="icon-btn" title="Rescan the data folder" onClick={onRefresh}>
          <RefreshIcon />
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
                <ChevronIcon open={!!open[ds.name]} />
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
                  <ColumnRow
                    key={c.name}
                    datasetName={ds.name}
                    column={c}
                    onInsertColumn={onInsertColumn}
                    onDatasetsUpdated={onDatasetsUpdated}
                  />
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </aside>
  );
}
