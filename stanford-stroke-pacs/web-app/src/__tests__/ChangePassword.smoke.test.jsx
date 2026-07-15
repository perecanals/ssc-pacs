import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

const apiPostMock = vi.fn();
const checkAuthMock = vi.fn().mockResolvedValue(undefined);

vi.mock("../api/client", () => ({
  apiPost: (...args) => apiPostMock(...args),
}));

let authState = {
  currentUser: "alice",
  mustChangePassword: true,
  checkAuth: checkAuthMock,
};

vi.mock("../context/AuthContext", () => ({
  useAuth: () => authState,
}));

import ChangePassword from "../pages/ChangePassword";

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/change-password"]}>
      <Routes>
        <Route path="/change-password" element={<ChangePassword />} />
        <Route path="/" element={<div>Home</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ChangePassword page", () => {
  beforeEach(() => {
    apiPostMock.mockReset();
    apiPostMock.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({}),
    });
    checkAuthMock.mockClear();
  });

  it("hides the current-password field on the forced first-login change", () => {
    authState = {
      currentUser: "alice",
      mustChangePassword: true,
      checkAuth: checkAuthMock,
    };
    renderPage();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /must change your password/i,
    );
    expect(screen.queryByText("Current password")).not.toBeInTheDocument();
    expect(screen.getByText("New password")).toBeInTheDocument();
  });

  it("submits without current_password on the forced change", async () => {
    authState = {
      currentUser: "alice",
      mustChangePassword: true,
      checkAuth: checkAuthMock,
    };
    renderPage();
    const inputs = document.querySelectorAll('input[type="password"]');
    // Only two fields when the current-password input is hidden: new + confirm.
    expect(inputs).toHaveLength(2);
    fireEvent.change(inputs[0], { target: { value: "brandNewPass456" } });
    fireEvent.change(inputs[1], { target: { value: "brandNewPass456" } });
    fireEvent.click(screen.getByRole("button", { name: /set new password/i }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalled());
    const [url, payload] = apiPostMock.mock.calls[0];
    expect(url).toBe("/api/auth/change-password");
    expect(payload).toEqual({ new_password: "brandNewPass456" });
    expect(payload).not.toHaveProperty("current_password");
  });

  it("shows the current-password field for a voluntary change and sends it", async () => {
    authState = {
      currentUser: "alice",
      mustChangePassword: false,
      checkAuth: checkAuthMock,
    };
    renderPage();
    expect(screen.getByText("Current password")).toBeInTheDocument();
    const inputs = document.querySelectorAll('input[type="password"]');
    expect(inputs).toHaveLength(3);
    fireEvent.change(inputs[0], { target: { value: "oldPass123" } });
    fireEvent.change(inputs[1], { target: { value: "brandNewPass456" } });
    fireEvent.change(inputs[2], { target: { value: "brandNewPass456" } });
    fireEvent.click(screen.getByRole("button", { name: /set new password/i }));

    await waitFor(() => expect(apiPostMock).toHaveBeenCalled());
    const [, payload] = apiPostMock.mock.calls[0];
    expect(payload).toEqual({
      new_password: "brandNewPass456",
      current_password: "oldPass123",
    });
  });
});
