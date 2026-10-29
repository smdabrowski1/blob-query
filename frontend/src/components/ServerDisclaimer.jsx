export default function ServerDisclaimer({ onDismiss }) {
  return (
    <div className="server-disclaimer" role="note">
      <span className="server-disclaimer-text">
        This is a demo running on a small server (&lt;4GB RAM); a heavy query
        or a large AI-generated join can crash it. Please keep queries reasonable.
      </span>
      <button
        type="button"
        className="server-disclaimer-dismiss"
        onClick={onDismiss}
        aria-label="Dismiss"
      >
        ×
      </button>
    </div>
  );
}
