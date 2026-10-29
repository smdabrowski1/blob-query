import { useState } from "react";
import { generateSql } from "../api";
import ResizableTextarea from "./ResizableTextarea.jsx";

const DISCLAIMER_KEY = "blobquery-server-disclaimer-dismissed";

export default function QueryEditor({ sql, setSql, onRun, status, aiEnabled }) {
  const [mode, setMode] = useState("sql"); // sql | ai
  const [prompt, setPrompt] = useState("");
  const [aiStatus, setAiStatus] = useState("idle"); // idle | running | error
  const [aiError, setAiError] = useState(null);
  // sessionStorage, not localStorage — dismissed for this browser tab's
  // session only, not forever. A deliberate, narrow exception to the
  // no-browser-storage rule (see CLAUDE.md), scoped to one UI dismissal.
  const [disclaimerDismissed, setDisclaimerDismissed] = useState(
    () => sessionStorage.getItem(DISCLAIMER_KEY) === "1"
  );
  const dismissDisclaimer = () => {
    sessionStorage.setItem(DISCLAIMER_KEY, "1");
    setDisclaimerDismissed(true);
  };

  const onKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      onRun();
    }
  };

  const generate = async () => {
    const p = prompt.trim();
    if (!p || aiStatus === "running") return;
    setAiStatus("running");
    setAiError(null);
    try {
      const generatedSql = await generateSql({ prompt: p });
      // Land the result in the SQL tab for review — never auto-run.
      setSql(generatedSql);
      setMode("sql");
      setAiStatus("idle");
    } catch (e) {
      setAiError(String(e.message || e));
      setAiStatus("error");
    }
  };

  const onPromptKeyDown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      generate();
    }
  };

  return (
    <div className="editor-pane">
      <div className="res-tabbar editor-tabbar">
        <button className={`res-tab ${mode === "sql" ? "active" : ""}`} onClick={() => setMode("sql")}>
          SQL
        </button>
        <button className={`res-tab ${mode === "ai" ? "active" : ""}`} onClick={() => setMode("ai")}>
          Ask AI
        </button>
      </div>

      {mode === "sql" ? (
        <>
          <div className="toolbar">
            <button className="run-btn" onClick={onRun} disabled={status === "running"}>
              {status === "running" ? "Running…" : "Run"}
            </button>
            <span className="kbd">⌘ / Ctrl + Enter</span>
          </div>
          <ResizableTextarea
            className="editor"
            value={sql}
            onChange={(e) => setSql(e.target.value)}
            onKeyDown={onKeyDown}
            spellCheck={false}
            placeholder="Write SQL against your datasets…  e.g.  SELECT * FROM sales_events LIMIT 1000"
          />
        </>
      ) : (
        <>
          {!disclaimerDismissed && (
            <div className="server-disclaimer" role="note">
              <span className="server-disclaimer-text">
                This is a demo running on a small server (&lt;4GB RAM) — a heavy query
                or a large AI-generated join can crash it. Please keep queries reasonable.
              </span>
              <button
                type="button"
                className="server-disclaimer-dismiss"
                onClick={dismissDisclaimer}
                aria-label="Dismiss"
              >
                ×
              </button>
            </div>
          )}
          <div className="toolbar">
            <button className="run-btn" onClick={generate} disabled={!aiEnabled || aiStatus === "running"}>
              {aiStatus === "running" ? "Generating…" : "Generate"}
            </button>
            <span className="kbd">⌘ / Ctrl + Enter</span>
            {!aiEnabled && (
              <span className="ai-note">Not configured — set ANTHROPIC_API_KEY in backend/.env</span>
            )}
          </div>
          <ResizableTextarea
            className="editor"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={onPromptKeyDown}
            spellCheck={false}
            disabled={!aiEnabled}
            placeholder="Describe the query in plain English…  e.g.  top 5 regions by total revenue"
          />
          {aiError && <div className="ai-note ai-error">{aiError}</div>}
        </>
      )}
    </div>
  );
}
