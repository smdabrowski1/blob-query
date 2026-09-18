import { useState } from "react";

export default function Sidebar({ datasets, onOpenDataset, onInsertColumn, onRefresh }) {
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

      <div className="sidebar-foot">DuckDB · local parquet</div>
    </aside>
  );
}
