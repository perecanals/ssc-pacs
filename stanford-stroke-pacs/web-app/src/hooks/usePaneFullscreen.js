import { useCallback, useEffect, useState } from "react";

// Native fullscreen for a pane. The point is efficiency, not just presentation:
// requestFullscreen renders the element fullscreen *without* moving it in the
// DOM, so the cross-origin OHIF iframe inside it is never unmounted and never
// reloads. A new tab, by contrast, is a fresh browsing context that must refetch
// every frame (18-540 MiB for a typical-to-large study).
//
// The caller must keep the pane in the same React tree position — a portal would
// move the node and force the reload this exists to avoid.
export default function usePaneFullscreen({ paneRef }) {
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    // Fullscreen can also be exited by Esc or the browser's own UI, so the
    // event is the source of truth rather than our enter/exit calls.
    const onChange = () => {
      setIsFullscreen(document.fullscreenElement === paneRef.current);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, [paneRef]);

  // requestFullscreen rejects unless it's driven by a user gesture, and is
  // absent in jsdom — swallow both rather than surfacing an unhandled rejection.
  const enter = useCallback(() => {
    paneRef.current?.requestFullscreen?.()?.catch(() => {});
  }, [paneRef]);

  const exit = useCallback(() => {
    if (document.fullscreenElement) {
      document.exitFullscreen?.()?.catch(() => {});
    }
  }, []);

  return { isFullscreen, enter, exit };
}
