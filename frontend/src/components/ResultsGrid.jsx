import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataEditor, GridCellKind } from "@glideapps/glide-data-grid";
import "@glideapps/glide-data-grid/dist/index.css";
import { runQuery, CHUNK } from "../api";

const NUMERIC = /INT|DECIMAL|DOUBLE|FLOAT|REAL|NUMERIC|HUGEINT/i;

// Light theme tuned to match the app shell. Selection/link accents use the
// status teal rather than the brand coral (styles.css's --accent) — a data
// grid's own selection outline is a different kind of "accent" than a CTA
// button, and every selected cell edge being hot pink would be a lot.
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

  // Reset everything whenever a new query runs.
  useEffect(() => {
    chunksRef.current = new Map();
    loadingRef.current = new Set();
    if (query.initialRows) chunksRef.current.set(0, query.initialRows);
    setGridColumns(
      query.columns.map((c) => ({ title: c.name, id: c.name, width: 170 }))
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
      return {
        kind: GridCellKind.Text,
        data: isNull ? "" : display,
        displayData: display,
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
