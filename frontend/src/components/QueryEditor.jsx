import { useState } from "react";
import { generateSql } from "../api";

export default function QueryEditor({ sql, setSql, onRun, status, aiEnabled }) {
  const [mode, setMode] = useState("sql"); // sql | ai
  const [prompt, setPrompt] = useState("");
  const [aiStatus, setAiStatus] = useState("idle"); // idle | running | error
  const [aiError, setAiError] = useState(null);

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
          <textarea
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
          <div className="toolbar">
            <button className="run-btn" onClick={generate} disabled={!aiEnabled || aiStatus === "running"}>
              {aiStatus === "running" ? "Generating…" : "Generate"}
            </button>
            <span className="kbd">⌘ / Ctrl + Enter</span>
            {!aiEnabled && (
              <span className="ai-note">Not configured — set ANTHROPIC_API_KEY in backend/.env</span>
            )}
          </div>
          <textarea
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
