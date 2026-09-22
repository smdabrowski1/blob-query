import { useEffect, useRef, useState } from "react";

// Replaces the browser's native textarea resize grip (the small diagonal
// lines in the bottom-right corner) with a full-width drag handle along the
// bottom edge — the same interaction as the sidebar's own resize handle
// (Sidebar.jsx), rather than each resizable text box in the app having its
// own inconsistent, easy-to-miss native affordance.
export default function ResizableTextarea({
  className,
  wrapClassName,
  defaultHeight = 150,
  minHeight = 60,
  maxHeight = 600,
  ...textareaProps
}) {
  const [height, setHeight] = useState(defaultHeight);
  const draggingRef = useRef(false);
  const startRef = useRef({ y: 0, height: 0 });

  const startDrag = (e) => {
    e.preventDefault();
    draggingRef.current = true;
    startRef.current = { y: e.clientY, height };
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
  };

  useEffect(() => {
    const onMove = (e) => {
      if (!draggingRef.current) return;
      const delta = e.clientY - startRef.current.y;
      setHeight(Math.min(maxHeight, Math.max(minHeight, startRef.current.height + delta)));
    };
    const onUp = () => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
    return () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
  }, [minHeight, maxHeight]);

  return (
    <div className={`resizable-textarea-wrap ${wrapClassName || ""}`} style={{ height }}>
      <textarea className={className} {...textareaProps} />
      <div className="resizable-textarea-handle" onMouseDown={startDrag} title="Drag to resize" />
    </div>
  );
}
