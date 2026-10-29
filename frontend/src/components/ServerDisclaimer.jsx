export default function ServerDisclaimer({ onDismiss }) {
  return (
    <div className="server-disclaimer" role="note">
      <span className="server-disclaimer-text">
        Please note that this is a demonstration running on a small server
        (&lt;4GB RAM); as a result, a heavy query or a large AI-generated join
        may cause it to crash.
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
