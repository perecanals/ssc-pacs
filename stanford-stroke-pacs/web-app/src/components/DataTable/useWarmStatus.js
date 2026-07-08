import { useCallback, useEffect, useRef, useState } from "react";
import {
  getBatchCacheStatus,
  queueWarmStudy,
  queueWarmPatient,
  queueWarmSeries,
} from "../../api/warmOhif";

const POLL_MS = 4000;

// Shallow object equality — used to keep state identity stable across polls
// that report no change, so an idle table doesn't re-render every tick.
function shallowEqual(a, b) {
  if (a === b) return true;
  if (!a || !b) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  return ka.every((k) => a[k] === b[k]);
}

// Tracks cold/queued/warming/hot/error for the study and patient rows currently
// on screen, so raters can pre-warm a queue and see at a glance what's ready.
// One batched cache-status request per tick covers every visible row (no
// per-row N+1).
//
// Queue gap: a click queues the study into the bounded warm_executor, but a
// worker may not pick it up (and write status='warming') for a while. During
// that gap the server still reports 'cold'. We hold a local "requested" set so
// such rows render "Queued…" instead of snapping back to "Decompress" — the
// guard is dropped once the server confirms warming/hot/error.
export default function useWarmStatus({ enabled, studyUids, patientIds, seriesUids = [] }) {
  const [studyStatus, setStudyStatus] = useState({});
  const [patientStatus, setPatientStatus] = useState({});
  const [seriesStatus, setSeriesStatus] = useState({});

  // Latest visible ids, read by the loop without restarting the interval.
  const idsRef = useRef({ studyUids: [], patientIds: [], seriesUids: [] });
  idsRef.current = { studyUids, patientIds, seriesUids };

  // Studies/patients/series the user just asked to warm, still awaiting a worker.
  const requestedStudies = useRef(new Set());
  const requestedPatients = useRef(new Set());
  const requestedSeries = useRef(new Set());

  const poll = useCallback(async () => {
    const { studyUids: su, patientIds: pi, seriesUids: se } = idsRef.current;
    if (su.length === 0 && pi.length === 0 && se.length === 0) return;
    try {
      const data = await getBatchCacheStatus(su, pi, se);
      if (data.studies) {
        setStudyStatus((prev) => {
          const next = { ...prev };
          for (const [uid, st] of Object.entries(data.studies)) {
            if (st === "warming" || st === "hot" || st === "error") {
              requestedStudies.current.delete(uid);
              next[uid] = st;
            } else if (requestedStudies.current.has(uid)) {
              next[uid] = "queued"; // requested but no worker yet
            } else {
              next[uid] = st;
            }
          }
          return shallowEqual(next, prev) ? prev : next;
        });
      }
      if (data.series) {
        setSeriesStatus((prev) => {
          const next = { ...prev };
          for (const [uid, st] of Object.entries(data.series)) {
            if (st === "warming" || st === "hot" || st === "error") {
              requestedSeries.current.delete(uid);
              next[uid] = st;
            } else if (requestedSeries.current.has(uid)) {
              next[uid] = "queued"; // requested but no worker yet
            } else {
              next[uid] = st;
            }
          }
          return shallowEqual(next, prev) ? prev : next;
        });
      }
      if (data.patients) {
        setPatientStatus((prev) => {
          const next = { ...prev };
          for (const [pid, summary] of Object.entries(data.patients)) {
            const total = summary.total || 0;
            // The server now tracks 'queued' itself, so its summary is
            // authoritative once it reflects any activity. The requested guard
            // only bridges the click -> mark_queued-commit gap.
            const active = summary.warming > 0 || summary.queued > 0
              || (total > 0 && summary.hot >= total) || summary.error > 0;
            if (active) requestedPatients.current.delete(pid);
            let candidate = summary;
            if (!active && requestedPatients.current.has(pid)) {
              candidate = { ...summary, queued: Math.max(total - (summary.hot || 0), 1) };
            }
            // Summaries are fresh objects every poll — keep the previous
            // object when nothing changed so state identity stays stable.
            next[pid] = shallowEqual(candidate, prev[pid]) ? prev[pid] : candidate;
          }
          return shallowEqual(next, prev) ? prev : next;
        });
      }
    } catch {
      /* transient — the next tick retries */
    }
  }, []);

  useEffect(() => {
    if (!enabled) return undefined;
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => clearInterval(id);
  }, [enabled, poll]);

  // Refetch promptly when the visible id set changes (scroll/expand) so newly
  // shown rows resolve their badge without waiting a full interval.
  const sig = `${studyUids.join(",")}|${patientIds.join(",")}|${seriesUids.join(",")}`;
  useEffect(() => {
    if (enabled) poll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig]);

  const warmStudy = useCallback(async (uid) => {
    requestedStudies.current.add(uid);
    setStudyStatus((prev) => ({ ...prev, [uid]: "queued" }));
    try {
      await queueWarmStudy(uid);
    } catch (e) {
      requestedStudies.current.delete(uid);
      setStudyStatus((prev) => ({ ...prev, [uid]: "error" }));
      alert(e?.message || "Could not decompress study");
    }
  }, []);

  const warmSeries = useCallback(async (uid) => {
    requestedSeries.current.add(uid);
    setSeriesStatus((prev) => ({ ...prev, [uid]: "queued" }));
    try {
      await queueWarmSeries(uid);
    } catch (e) {
      requestedSeries.current.delete(uid);
      setSeriesStatus((prev) => ({ ...prev, [uid]: "error" }));
      alert(e?.message || "Could not decompress series");
    }
  }, []);

  const warmPatient = useCallback(async (patientId) => {
    requestedPatients.current.add(patientId);
    setPatientStatus((prev) => {
      const cur = prev[patientId] || {};
      const total = cur.total || 0;
      // total||1 so the badge reads "Queued…" even before this patient's
      // aggregate has been polled (deriveState treats total===0 as cold).
      const queued = Math.max(total - (cur.hot || 0), 1);
      return {
        ...prev,
        [patientId]: { ...cur, total: total || 1, queued, warming: 0, error: 0, cold: 0 },
      };
    });
    try {
      await queueWarmPatient(patientId);
      poll();
    } catch (e) {
      requestedPatients.current.delete(patientId);
      alert(e?.message || "Could not decompress patient studies");
      poll();
    }
  }, [poll]);

  return { studyStatus, patientStatus, seriesStatus, warmStudy, warmPatient, warmSeries };
}
