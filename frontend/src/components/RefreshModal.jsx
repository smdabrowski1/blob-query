import { formatVersionUK } from "../formatTime";

const PHASE_LABEL = {
  fetching: "Fetching…",
  validating: "Validating…",
  tiling: "Tiling…",
  active: "Done",
  failed: "Failed",
};

const RUNNING_STATUSES = new Set(["fetching", "validating", "tiling"]);

function progressFraction(job) {
  const { progressCurrent, progressTotal } = job;
  if (progressTotal == null || progressCurrent == null || progressTotal <= 0) return null;
  return Math.min(1, Math.max(0, progressCurrent / progressTotal));
}

function progressLabel(job) {
  const frac = progressFraction(job);
  if (frac == null) return null;
  return `${Math.round(frac * 100)}%`;
}

function JobModal({ name, job, onMinimize, onDismiss }) {
  const running = RUNNING_STATUSES.has(job.status);
  const frac = progressFraction(job);
  const pct = progressLabel(job);
  // No progress number yet (job just started, or mid "validating", which
  // never reports one) — a static full bar would read as already done.
  const indeterminate = running && frac == null;

  return (
    <div className="refresh-modal-backdrop">
      <div className="refresh-modal" role="dialog" aria-label={`Refresh progress for ${name}`}>
        <div className="refresh-modal-head">
          <span className="refresh-modal-title">{name}</span>
          {running && (
            <button className="icon-btn" title="Minimize" onClick={() => onMinimize(name)}>
              —
            </button>
          )}
        </div>

        <div className="refresh-modal-phase">
          {PHASE_LABEL[job.status] || job.status}
          {pct != null && running ? ` — ${pct}` : ""}
        </div>

        <div className="refresh-modal-bar-track">
          <div
            className={`refresh-modal-bar-fill ${job.status} ${indeterminate ? "indeterminate" : ""}`}
            style={indeterminate ? undefined : { width: `${frac != null ? frac * 100 : running ? 100 : 0}%` }}
          />
        </div>

        {job.status === "failed" && (
          <div className="refresh-modal-error">{job.error || "Refresh failed."}</div>
        )}
        {job.status === "active" && (
          <div className="refresh-modal-success">
            Refresh complete{job.version ? ` — ${formatVersionUK(job.version)}` : ""}.
          </div>
        )}

        <div className="refresh-modal-actions">
          {running ? (
            <button className="refresh-modal-btn" onClick={() => onMinimize(name)}>
              Minimize
            </button>
          ) : (
            <button className="refresh-modal-btn primary" onClick={() => onDismiss(name)}>
              Dismiss
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function JobPill({ name, job, onReopen }) {
  const pct = progressLabel(job);
  const label = PHASE_LABEL[job.status] || job.status;
  return (
    <button
      className={`refresh-pill ${job.status}`}
      onClick={() => onReopen(name)}
      title={job.error || undefined}
    >
      <span className="refresh-pill-name">{name}</span>
      <span className="refresh-pill-phase">
        {label}
        {pct != null && RUNNING_STATUSES.has(job.status) ? ` ${pct}` : ""}
      </span>
    </button>
  );
}

export default function RefreshModal({ jobs, onMinimize, onReopen, onDismiss }) {
  const entries = Object.entries(jobs);
  if (entries.length === 0) return null;

  const open = entries.filter(([, job]) => !job.minimized);
  const minimized = entries.filter(([, job]) => job.minimized);

  return (
    <>
      {open.map(([name, job]) => (
        <JobModal key={name} name={name} job={job} onMinimize={onMinimize} onDismiss={onDismiss} />
      ))}
      {minimized.length > 0 && (
        <div className="refresh-pill-stack">
          {minimized.map(([name, job]) => (
            <JobPill key={name} name={name} job={job} onReopen={onReopen} />
          ))}
        </div>
      )}
    </>
  );
}
