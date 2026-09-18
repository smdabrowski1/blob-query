import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataEditor, GridCellKind } from "@glideapps/glide-data-grid";
import "@glideapps/glide-data-grid/dist/index.css";
import { runQuery, CHUNK } from "../api";

const NUMERIC = /INT|DECIMAL|DOUBLE|FLOAT|REAL|NUMERIC|HUGEINT/i;

// Dark theme tuned to match the app shell.
const gridTheme = {
  accentColor: "#46b3a6",
  accentLight: "rgba(70,179,166,0.14)",
  textDark: "#d7dee8",
  textMedium: "#aab3c0",
  textLight: "#707a89",
  textHeader: "#c3ccd8",
  textHeaderSelected: "#ffffff",
  bgCell: "#0e1116",
  bgCellMedium: "#12171f",
  bgHeader: "#151a21",
  bgHeaderHovered: "#1b222b",
  bgHeaderHasFocus: "#1b222b",
  bgBubble: "#1b222b",
  bgBubbleSelected: "#1b222b",
  borderColor: "rgba(255,255,255,0.06)",
  horizontalBorderColor: "rgba(255,255,255,0.04)",
  drilldownBorder: "rgba(255,255,255,0.2)",
  linkColor: "#5eb0ef",
  cellHorizontalPadding: 10,
  headerFontStyle: "600 13px",
  baseFontStyle: "13px",
  fontFamily:
    "ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace",
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
        themeOverride: isNull ? { textDark: "#5b6472" } : undefined,
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
