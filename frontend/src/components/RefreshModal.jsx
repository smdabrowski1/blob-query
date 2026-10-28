import { useEffect, useRef } from "react";
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

// Each log line is stored server-side as "<ISO timestamp>  <message>" — split
// that back apart so the timestamp can render as a de-emphasized prefix
// instead of competing with the actual message for attention.
function splitLogLine(line) {
  const m = /^(\S+)\s\s(.*)$/.exec(line);
  return m ? { ts: m[1], message: m[2] } : { ts: null, message: line };
}

function LogPanel({ log }) {
  const scrollRef = useRef(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [log]);

  if (!log || log.length === 0) return null;

  return (
    <div className="refresh-modal-log" ref={scrollRef}>
      {log.map((line, i) => {
        const { ts, message } = splitLogLine(line);
        return (
          <div className="refresh-modal-log-line" key={i}>
            {ts && <span className="refresh-modal-log-ts">{ts.slice(11, 19)}</span>}
            <span>{message}</span>
          </div>
        );
      })}
    </div>
  );
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

        <LogPanel log={job.log} />

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

// Minimized jobs render nothing here — Sidebar.jsx's DatasetRefreshControl
// already shows live status next to the dataset and reopens this modal on
// click.
export default function RefreshModal({ jobs, onMinimize, onDismiss }) {
  const open = Object.entries(jobs).filter(([, job]) => !job.minimized);
  if (open.length === 0) return null;

  return (
    <>
      {open.map(([name, job]) => (
        <JobModal key={name} name={name} job={job} onMinimize={onMinimize} onDismiss={onDismiss} />
      ))}
    </>
  );
}
