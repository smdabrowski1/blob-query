import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataEditor, GridCellKind } from "@glideapps/glide-data-grid";
import "@glideapps/glide-data-grid/dist/index.css";
import { runQuery, CHUNK } from "../api";

const NUMERIC = /INT|DECIMAL|DOUBLE|FLOAT|REAL|NUMERIC|HUGEINT/i;

// Column sizing: fit the content instead of every column getting the same
// fixed width regardless of whether it holds "id" or a multi-thousand-
// character geometry WKT string. Capped at MAX_COL_CHARS so one long-text
// column (typically `geometry`) can't dominate the grid and push every
// column after it off-screen — horizontal scroll reaches the rest instead.
const MIN_COL_CHARS = 4;
const MAX_COL_CHARS = 30;
// JetBrains Mono @ 13px (this grid's font — see gridTheme below): a rough
// but consistent per-character advance width, since a canvas grid has no
// DOM text to actually measure against.
const CHAR_WIDTH_PX = 7.8;
const CELL_PADDING_PX = 28; // cellHorizontalPadding (10 each side) + margin

function charsToWidth(chars) {
  return Math.round(chars * CHAR_WIDTH_PX + CELL_PADDING_PX);
}

// Sample the rows already in hand (the query's first chunk) to size a
// column against what it actually contains, not a guess — capped at
// MAX_COL_CHARS either way, so a column doesn't need every row scanned to
// know it should be truncated.
function computeColumnCharCaps(columns, sampleRows) {
  const caps = columns.map((c) => Math.min(MAX_COL_CHARS, Math.max(MIN_COL_CHARS, c.name.length)));
  for (const row of sampleRows || []) {
    for (let i = 0; i < columns.length; i++) {
      if (caps[i] >= MAX_COL_CHARS) continue; // already at the ceiling, no point checking more
      const v = row[i];
      const len = v === null || v === undefined ? 4 : String(v).length; // "NULL"
      if (len > caps[i]) caps[i] = Math.min(MAX_COL_CHARS, len);
    }
  }
  return caps;
}

// Light theme tuned to match the app shell. Selection/link accents use the
// status teal, not the brand coral (styles.css's --accent) — every selected
// cell edge in hot pink would be a lot.
const gridTheme = {
  accentColor: "#0d9488",
  accentLight: "rgba(13,148,136,0.12)",
  textDark: "#222222",
  textMedium: "#6a6a6a",
  textLight: "#adadad",
  textHeader: "#404040",
  textHeaderSelected: "#111111",
  bgCell: "#ffffff",
  bgCellMedium: "#fafafa",
  bgHeader: "#f7f7f7",
  bgHeaderHovered: "#eeeeee",
  bgHeaderHasFocus: "#eeeeee",
  bgBubble: "#f7f7f7",
  bgBubbleSelected: "#eeeeee",
  borderColor: "rgba(0,0,0,0.08)",
  horizontalBorderColor: "rgba(0,0,0,0.06)",
  drilldownBorder: "rgba(0,0,0,0.15)",
  linkColor: "#2563eb",
  cellHorizontalPadding: 10,
  headerFontStyle: "600 13px",
  baseFontStyle: "13px",
  fontFamily: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
};

// Measure a container so the grid canvas gets exact pixel dimensions.
function useSize() {
  const ref = useRef(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver((entries) => {
      const cr = entries[0].contentRect;
      setSize({ w: Math.floor(cr.width), h: Math.floor(cr.height) });
    });
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, size];
}

export default function ResultsGrid({ query }) {
  const [wrapRef, size] = useSize();

  // Sparse cache: chunkIndex -> array of rows (each row is an array of values).
  const chunksRef = useRef(new Map());
  const loadingRef = useRef(new Set());
  const [version, setVersion] = useState(0); // bump to force a redraw

  const [gridColumns, setGridColumns] = useState([]);
  // Per-column character cap used to shorten over-length cell text (a long
  // `geometry` WKT string, mainly) with a visible "…" rather than letting
  // canvas rendering just clip it mid-character at the cell boundary —
  // computed once per query, alongside column widths, from the same sample.
  const charCapsRef = useRef([]);

  // Reset everything whenever a new query runs.
  useEffect(() => {
    chunksRef.current = new Map();
    loadingRef.current = new Set();
    if (query.initialRows) chunksRef.current.set(0, query.initialRows);

    const caps = computeColumnCharCaps(query.columns, query.initialRows);
    charCapsRef.current = caps;
    setGridColumns(
      query.columns.map((c, i) => ({ title: c.name, id: c.name, width: charsToWidth(caps[i]) }))
    );
    setVersion((v) => v + 1);
  }, [query.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const loadChunk = useCallback(
    async (c) => {
      if (c < 0) return;
      const maxChunk = Math.floor(Math.max(0, (query.total ?? 1) - 1) / CHUNK);
      if (c > maxChunk) return;
      if (chunksRef.current.has(c) || loadingRef.current.has(c)) return;

      loadingRef.current.add(c);
      try {
        const res = await runQuery({
          sql: query.sql,
          offset: c * CHUNK,
          limit: CHUNK,
        });
        chunksRef.current.set(c, res.rows);
      } catch {
        chunksRef.current.set(c, []); // don't hammer the server on a failed page
      } finally {
        loadingRef.current.delete(c);
        setVersion((v) => v + 1);
      }
    },
    [query.sql, query.total]
  );

  // Drive loading from what's visible, prefetching one chunk either side.
  const onVisibleRegionChanged = useCallback(
    (range) => {
      const first = Math.floor(range.y / CHUNK) - 1;
      const last = Math.floor((range.y + range.height) / CHUNK) + 1;
      for (let c = first; c <= last; c++) loadChunk(c);
    },
    [loadChunk]
  );

  const getCellContent = useCallback(
    ([col, row]) => {
      const chunk = chunksRef.current.get(Math.floor(row / CHUNK));
      const colType = query.columns[col]?.type ?? "";
      const numeric = NUMERIC.test(colType);

      if (!chunk) {
        return { kind: GridCellKind.Loading, allowOverlay: false };
      }
      const raw = chunk[row % CHUNK]?.[col];
      const isNull = raw === null || raw === undefined;
      const display = isNull ? "NULL" : String(raw);
      // `data` always carries the full value (so a cell's overlay still
      // shows/copies the complete geometry WKT) — only `displayData` gets
      // truncated to the column's char cap, with an explicit "…" instead
      // of letting the canvas clip it mid-character.
      const cap = charCapsRef.current[col] ?? MAX_COL_CHARS;
      const displayData = display.length > cap ? `${display.slice(0, cap)}…` : display;
      return {
        kind: GridCellKind.Text,
        data: isNull ? "" : display,
        displayData,
        allowOverlay: true,
        readonly: true,
        contentAlign: numeric ? "right" : "left",
        themeOverride: isNull ? { textDark: "#adadad" } : undefined,
      };
    },
    [version, query.columns] // eslint-disable-line react-hooks/exhaustive-deps
  );

  const onColumnResize = useCallback((column, newSize) => {
    setGridColumns((prev) =>
      prev.map((c) => (c.id === column.id ? { ...c, width: newSize } : c))
    );
  }, []);

  const rows = query.total ?? 0;
  const ready = size.w > 0 && size.h > 0 && gridColumns.length > 0;

  const grid = useMemo(
    () =>
      ready ? (
        <DataEditor
          width={size.w}
          height={size.h}
          columns={gridColumns}
          rows={rows}
          getCellContent={getCellContent}
          onVisibleRegionChanged={onVisibleRegionChanged}
          onColumnResize={onColumnResize}
          getCellsForSelection={true}
          rowMarkers="number"
          smoothScrollX
          smoothScrollY
          theme={gridTheme}
          // Fills whatever's left after the last real column with a plain
          // bordered strip instead of blank canvas — otherwise there's no
          // visual line marking "this is where the dataset actually ends"
          // when total column width is narrower than the grid's viewport.
          rightElement={<div className="grid-right-filler" />}
          rightElementProps={{ sticky: true, fill: true }}
        />
      ) : null,
    [ready, size.w, size.h, gridColumns, rows, getCellContent, onVisibleRegionChanged, onColumnResize]
  );

  return (
    <div className="grid-wrap" ref={wrapRef}>
      {grid}
    </div>
  );
}
