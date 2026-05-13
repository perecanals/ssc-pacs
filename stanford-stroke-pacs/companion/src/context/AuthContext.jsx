import { createContext, useContext, useState, useEffect, useRef, useCallback } from "react";
import { apiGet, apiPost } from "../api/client";

const AuthContext = createContext(null);

export function AuthProvider({ children }) {
  const [currentUser, setCurrentUser] = useState(null);
  const [isAdmin, setIsAdmin] = useState(false);
  const [loading, setLoading] = useState(true);
  // Tracks whether this tab ever had an authenticated user, so ProtectedRoute
  // can show the "expired" banner only on involuntary session loss. Cleared
  // synchronously inside `logout()` so intentional logouts skip the banner.
  const wasAuthedRef = useRef(false);

  const checkAuth = useCallback(async () => {
    try {
      const data = await apiGet("/api/me");
      setCurrentUser(data.username || null);
      setIsAdmin(Boolean(data.is_admin));
    } catch {
      setCurrentUser(null);
      setIsAdmin(false);
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
    };
    window.addEventListener("auth:expired", onExpired);
    return () => window.removeEventListener("auth:expired", onExpired);
  }, []);

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
    }
  };

  return (
    <AuthContext.Provider value={{ currentUser, isAdmin, loading, login, logout, wasAuthedRef }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
