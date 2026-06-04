import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

const loginMock = vi.fn();

// Mock the API client so AuthProvider's initial /api/me resolves to "no user".
vi.mock("../api/client", () => ({
  apiFetch: vi.fn().mockResolvedValue({ ok: false, status: 401 }),
  apiGet: vi.fn().mockRejectedValue(new Error("GET /api/me failed: 401")),
  apiPost: vi.fn().mockResolvedValue({ ok: false, json: () => Promise.resolve({ detail: "bad" }) }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true }),
}));

// Replace useAuth() with our spy so we control login() outcomes deterministically.
vi.mock("../context/AuthContext", async () => {
  const actual = await vi.importActual("../context/AuthContext");
  return {
    ...actual,
    useAuth: () => ({ login: loginMock, logout: vi.fn(), currentUser: null, isAdmin: false, loading: false }),
  };
});

import Login from "../pages/Login";

function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<div>Home</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Login page", () => {
  it("renders the title, subtitle, and form fields", () => {
    loginMock.mockReset();
    renderAt("/login");
    expect(screen.getByText("Stanford Stroke Center PACS")).toBeInTheDocument();
    expect(screen.getByText("Sign in to continue")).toBeInTheDocument();
    expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/password/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /sign in/i })).toBeInTheDocument();
  });

  it("does NOT render the expired banner on a fresh visit", () => {
    loginMock.mockReset();
    renderAt("/login");
    expect(screen.queryByText(/session expired/i)).not.toBeInTheDocument();
  });

  it("renders the expired banner when ?expired=1 is present", () => {
    loginMock.mockReset();
    renderAt("/login?expired=1");
    expect(screen.getByRole("alert")).toHaveTextContent(/session expired/i);
  });

  it("shows an error message when login() throws", async () => {
    loginMock.mockReset();
    loginMock.mockRejectedValueOnce(new Error("Invalid credentials"));
    renderAt("/login");

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "wrong" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByText("Invalid credentials")).toBeInTheDocument();
    });
  });

  it("navigates to / on successful login", async () => {
    loginMock.mockReset();
    loginMock.mockResolvedValueOnce(undefined);
    renderAt("/login");

    fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "ok" } });
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));

    await waitFor(() => {
      expect(screen.getByText("Home")).toBeInTheDocument();
    });
    expect(loginMock).toHaveBeenCalledWith("alice", "ok");
  });
});
