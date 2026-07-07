import { useCallback, useRef, useState } from "react";

// Vertical drag-resize for a bottom-docked pane. Pointer capture keeps the
// drag alive over the cross-origin OHIF iframe; the caller also disables
// pointer events on the frame while `resizing` as a belt-and-suspenders
// guard. `onResize(null)` (double-click) restores the CSS default height.
export default function usePaneResize({ paneRef, onResize, minHeight = 320, reservedAbove = 256 }) {
  const [resizing, setResizing] = useState(false);
  const drag = useRef(null); // { startY, startHeight }

  const onPointerDown = useCallback((e) => {
    if (!paneRef.current) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = {
      startY: e.clientY,
      startHeight: paneRef.current.getBoundingClientRect().height,
    };
    setResizing(true);
  }, [paneRef]);

  const onPointerMove = useCallback((e) => {
    if (!drag.current) return;
    // Dragging up (clientY decreases) grows the pane.
    const raw = drag.current.startHeight + (drag.current.startY - e.clientY);
    const max = Math.max(minHeight, window.innerHeight - reservedAbove);
    onResize(Math.round(Math.min(max, Math.max(minHeight, raw))));
  }, [onResize, minHeight, reservedAbove]);

  const endDrag = useCallback((e) => {
    if (!drag.current) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    drag.current = null;
    setResizing(false);
  }, []);

  const onDoubleClick = useCallback(() => {
    onResize(null);
  }, [onResize]);

  return {
    resizing,
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: endDrag,
      onPointerCancel: endDrag,
      onDoubleClick,
    },
  };
}
