import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import { AuthProvider } from "../context/AuthContext";
import ProtectedRoute from "../components/ProtectedRoute";
import Login from "../pages/Login";
import { apiFetch } from "../api/client";

// These tests exercise the real api/client + AuthContext idle watchdog
// (no client mock), so fetch is stubbed and timers are faked. The
// /api/me payload carries a 5-minute session_timeout_seconds, matching
// the production config.toml default.
const ME_BODY = {
  username: "alice",
  is_admin: false,
  must_change_password: false,
  session_timeout_seconds: 300,
};

function jsonResponse(body) {
  return { status: 200, ok: true, json: () => Promise.resolve(body) };
}

function renderApp() {
  return render(
    <MemoryRouter initialEntries={["/app"]}>
      <AuthProvider>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route
            path="/app"
            element={
              <ProtectedRoute>
                <div>SECRET APP</div>
              </ProtectedRoute>
            }
          />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
}

// Flush the mount's chained promises (apiGet → fetch → res.json) under
// fake timers without RTL's waitFor (which polls on faked timers).
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

describe("AuthContext idle logout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-01-01T00:00:00Z"));
    vi.stubGlobal(
      "fetch",
      vi.fn((url) => {
        const path = typeof url === "string" ? url : url.url;
        if (path === "/api/me") return Promise.resolve(jsonResponse(ME_BODY));
        return Promise.resolve(jsonResponse({}));
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("redirects to the expired login page after the idle timeout with no API activity", async () => {
    renderApp();
    await settle();
    expect(screen.getByText("SECRET APP")).toBeInTheDocument();

    // No API activity for >5 min → the periodic watchdog trips.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6 * 60 * 1000);
    });

    expect(screen.queryByText("SECRET APP")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/session expired/i);
    // Defense-in-depth: the server cookie is dropped explicitly too.
    expect(fetch).toHaveBeenCalledWith(
      "/api/logout",
      expect.objectContaining({ method: "POST" }),
    );
  });

  it("does not log out while real API activity keeps the session sliding", async () => {
    renderApp();
    await settle();
    expect(screen.getByText("SECRET APP")).toBeInTheDocument();

    // 4 min in, a real (session-sliding) request resets the idle clock.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4 * 60 * 1000);
      await apiFetch("/api/studies");
    });

    // 4 more min: 8 since mount but only 4 since activity → still in.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4 * 60 * 1000);
    });
    expect(screen.getByText("SECRET APP")).toBeInTheDocument();

    // 2 more min: now 6 min since the last activity → trips.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2 * 60 * 1000);
    });
    expect(screen.getByRole("alert")).toHaveTextContent(/session expired/i);
  });

  it("re-checks immediately on tab visibility regain (covers laptop-sleep)", async () => {
    renderApp();
    await settle();
    expect(screen.getByText("SECRET APP")).toBeInTheDocument();

    // Jump the clock forward as if the machine slept (timers suspended,
    // so simulate by moving time without letting the interval fire).
    vi.setSystemTime(new Date("2026-01-01T00:10:00Z"));

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(screen.queryByText("SECRET APP")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/session expired/i);
  });
});
