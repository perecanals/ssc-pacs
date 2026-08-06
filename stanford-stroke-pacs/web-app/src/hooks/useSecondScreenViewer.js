import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { openOnSecondScreen } from "../utils/secondScreen";

// Owns the second-screen popup window for the OHIF viewer: opening it,
// re-pointing it at new studies, and noticing when the user closes it. While
// the popup is live, Navigator routes row-click previews here instead of the
// preview pane, so the data table stays interactive on the main monitor.
export default function useSecondScreenViewer() {
  const winRef = useRef(null);
  // Last URL the popup was pointed at — lets navigate() skip the reload when a
  // re-click resolves to the same study (an OHIF reload re-downloads it all).
  const urlRef = useRef("");
  const [active, setActive] = useState(false);
  // Transient footer-chip text: warming/resolve progress, or an error message.
  const [status, setStatus] = useState("");

  const isLive = useCallback(
    () => Boolean(winRef.current && !winRef.current.closed),
    [],
  );

  // Closing the popup fires no event in the opener, so poll while active.
  useEffect(() => {
    if (!active) return undefined;
    const timer = setInterval(() => {
      if (!winRef.current || winRef.current.closed) {
        winRef.current = null;
        urlRef.current = "";
        setActive(false);
        setStatus("");
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [active]);

  // Opens the popup on the other display, or just focuses it when one is
  // already live. Resolves true when a live popup exists afterwards.
  const open = useCallback(
    async (url) => {
      if (isLive()) {
        winRef.current.focus();
        return true;
      }
      if (!url) return false;
      const win = await openOnSecondScreen(url);
      if (!win) return false;
      winRef.current = win;
      urlRef.current = url;
      setActive(true);
      setStatus("");
      return true;
    },
    [isLive],
  );

  // Re-points the live popup at a new viewer URL. replace() keeps its history
  // flat; a same-URL call only focuses, so re-clicking the current row does
  // not force OHIF to reload the study. Returns false when there is no live
  // popup to navigate (caller decides the fallback).
  const navigate = useCallback(
    (url) => {
      if (!isLive() || !url) return false;
      if (url !== urlRef.current) {
        winRef.current.location.replace(url);
        urlRef.current = url;
      }
      winRef.current.focus();
      return true;
    },
    [isLive],
  );

  return useMemo(
    () => ({ active, status, setStatus, isLive, open, navigate }),
    [active, status, isLive, open, navigate],
  );
}
