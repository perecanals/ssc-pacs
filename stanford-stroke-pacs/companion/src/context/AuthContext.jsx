import { createContext, useContext, useState, useEffect, useRef, useCallback } from "react";
import { apiGet, apiPost, getLastApiActivityAt, markApiActivity } from "../api/client";

const AuthContext = createContext(null);

// Fallback if /api/me doesn't carry session_timeout_seconds (older
// backend / unexpected shape). Matches the documented 5-minute default.
const DEFAULT_SESSION_TIMEOUT_MS = 5 * 60 * 1000;
// How often the idle watchdog re-checks while the tab is foregrounded.
const IDLE_CHECK_INTERVAL_MS = 20 * 1000;

export function AuthProvider({ children }) {
  const [currentUser, setCurrentUser] = useState(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [mustChangePassword, setMustChangePassword] = useState(false);
  const [loading, setLoading] = useState(true);
  const [sessionTimeoutMs, setSessionTimeoutMs] = useState(DEFAULT_SESSION_TIMEOUT_MS);
  // Tracks whether this tab ever had an authenticated user, so ProtectedRoute
  // can show the "expired" banner only on involuntary session loss. Cleared
  // synchronously inside `logout()` so intentional logouts skip the banner.
  const wasAuthedRef = useRef(false);

  const checkAuth = useCallback(async () => {
    try {
      const data = await apiGet("/api/me");
      setCurrentUser(data.username || null);
      setIsAdmin(Boolean(data.is_admin));
      setMustChangePassword(Boolean(data.must_change_password));
      if (Number(data.session_timeout_seconds) > 0) {
        setSessionTimeoutMs(data.session_timeout_seconds * 1000);
      }
      // A successful /api/me right after login/refresh means the user is
      // present now — seed the idle clock so the watchdog starts fresh.
      if (data.username) markApiActivity();
    } catch {
      setCurrentUser(null);
      setIsAdmin(false);
      setMustChangePassword(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

  useEffect(() => {
    if (currentUser) wasAuthedRef.current = true;
  }, [currentUser]);

  useEffect(() => {
    const onExpired = () => {
      setCurrentUser(null);
      setIsAdmin(false);
      setMustChangePassword(false);
    };
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, []);

  // Proactive idle logout. The backend session is a sliding token that
  // dies after `sessionTimeoutMs` with no session-sliding request. The
  // SPA can otherwise keep rendering the app shell against a dead session
  // until the next API call 401s — leave the office, come back, still see
  // the app. This watchdog redirects the instant the server session is
  // truly gone, reusing the existing involuntary-expiry path (dispatch
  // "auth:expired" → onExpired clears the user → ProtectedRoute sends to
  // /login?expired=1 with the banner). It tracks real API activity only
  // (client.js), so it is exactly in lockstep with the backend token.
  useEffect(() => {
    if (!currentUser) return undefined;
    let fired = false;

    const expireIfIdle = () => {
      if (fired) return;
      if (Date.now() - getLastApiActivityAt() < sessionTimeoutMs) return;
      fired = true;
      // Defense-in-depth: the cookie is already expired once we're idle
      // past the timeout, but explicitly drop it server-side too.
      // suppressAuthEvent so its own 401, if any, can't double-fire.
      apiPost("/api/logout", {}, { suppressAuthEvent: true }).catch(() => {});
      window.dispatchEvent(new CustomEvent("auth:expired"));
    };

    const interval = setInterval(expireIfIdle, IDLE_CHECK_INTERVAL_MS);
    // Timers are throttled/suspended while the tab is hidden or the
    // machine sleeps, so the interval alone can miss the laptop-closed /
    // came-back-from-home case. Re-check the moment the tab is seen again.
    const onVisible = () => {
      if (document.visibilityState === "visible") expireIfIdle();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", expireIfIdle);

    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", expireIfIdle);
    };
  }, [currentUser, sessionTimeoutMs]);

  const login = async (username, password) => {
    const res = await apiPost("/api/login", { username, password });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Login failed");
    }
    setCurrentUser(username);
    await checkAuth();
  };

  const logout = async () => {
    try {
      await apiPost("/api/logout", {});
    } finally {
      wasAuthedRef.current = false;
      setCurrentUser(null);
      setIsAdmin(false);
      setMustChangePassword(false);
    }
  };

  return (
    <AuthContext.Provider
      value={{
        currentUser,
        isAdmin,
        mustChangePassword,
        loading,
        login,
        logout,
        checkAuth,
        wasAuthedRef,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
