import { useEffect, useRef, useState } from "react";

// Quick-start prompts shown before the first real message — one click sends
// them exactly like typing and hitting Send (onSend), not a canned reply;
// they're a demo of what to ask, not a static list of capabilities.
const QUICK_START_PROMPTS = [
  "What's in the UK geography data?",
  "Which local authorities have the most MSOAs?",
  "What can I explore in the sales data?",
];

// One conversation, two homes: the landing screen (variant="landing") and
// the persistent side panel (variant="panel") both render this against the
// same lifted state (App.jsx) — same messages array, same in-flight
// request — so picking up the chat in the panel after dismissing the
// landing screen continues the exact conversation, not a fresh one.
export default function DiscoverChat({
  variant,
  messages,
  sending,
  error,
  aiEnabled,
  onSend,
  onOpenDataset,
  onUseQuery,
}) {
  const [draft, setDraft] = useState("");
  const listRef = useRef(null);

  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, sending]);

  const send = () => {
    const text = draft.trim();
    if (!text || sending) return;
    setDraft("");
    onSend(text);
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  if (!aiEnabled) {
    return (
      <div className={`discover-chat discover-chat-${variant}`}>
        <div className="discover-chat-disabled">
          Discovery chat needs an AI key — set ANTHROPIC_API_KEY in backend/.env to enable it.
        </div>
      </div>
    );
  }

  return (
    <div className={`discover-chat discover-chat-${variant}`}>
      <div className="discover-chat-messages" ref={listRef}>
        {/* The intro used to be static copy above the chat — now it's the
            assistant's own first message, with the quick-start prompts as
            real, one-click sends (onSend) rather than a features list. */}
        {messages.length === 0 && !sending && (
          <div className="chat-msg chat-msg-assistant chat-welcome">
            <div className="chat-msg-bubble">
              Hi — I'm your data assistant. Tell me what you're curious about and I'll point
              you at which datasets are relevant, and how they fit together. Or try one of
              these:
            </div>
            <div className="chat-suggestions">
              {QUICK_START_PROMPTS.map((p, i) => (
                <button key={i} className="chat-suggestion-chip chat-suggestion-demo" onClick={() => onSend(p)}>
                  {p}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg chat-msg-${m.role}`}>
            <div className="chat-msg-bubble">{m.content}</div>
            {m.suggestions?.length > 0 && (
              <div className="chat-suggestions">
                {m.suggestions.map((s, j) => (
                  <button
                    key={j}
                    className={`chat-suggestion-chip chat-suggestion-${s.type}`}
                    onClick={() =>
                      s.type === "query" ? onUseQuery(s.sql, s.datasets) : onOpenDataset(s.datasets[0])
                    }
                    title={s.type === "query" ? s.sql : `Open ${s.datasets[0]}`}
                  >
                    {s.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        ))}
        {sending && (
          <div className="chat-msg chat-msg-assistant">
            <div className="chat-msg-bubble chat-msg-loading">Thinking…</div>
          </div>
        )}
        {error && <div className="chat-error">{error}</div>}
      </div>
      <div className="discover-chat-input-row">
        <textarea
          className="discover-chat-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask about the data…"
          rows={1}
        />
        <button className="run-btn discover-chat-send" onClick={send} disabled={sending || !draft.trim()}>
          Send
        </button>
      </div>
    </div>
  );
}
