const API = "";

// Timestamp of the last real API request, used by AuthContext's idle
// watchdog to mirror the backend sliding session. Only requests that
// actually slide the server token count — so we skip exactly the paths
// the backend's `sliding_jwt` middleware skips (app.py): static assets
// and the session-probe/auth endpoints that own their own cookie.
let lastApiActivityAt = Date.now();

function slidesSession(path) {
  return !(
    path.startsWith("/assets/") ||
    path === "/api/me" ||
    path === "/api/login" ||
    path === "/api/logout"
  );
}

export function markApiActivity() {
  lastApiActivityAt = Date.now();
}

export function getLastApiActivityAt() {
  return lastApiActivityAt;
}

export async function apiFetch(path, options = {}) {
  const { suppressAuthEvent, ...rest } = options;
  const res = await fetch(`${API}${path}`, {
    credentials: "same-origin",
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...rest.headers,
    },
  });
  // A completed request (even a failed one) proves the user is active and
  // the backend token just slid — keep the idle clock in lockstep with it.
  if (slidesSession(path)) {
    markApiActivity();
  }
  // Wrong-credential 401s on endpoints like /api/auth/change-password are
  // not session expiries — opt them out so the user stays on the page.
  if (res.status === 401 && !suppressAuthEvent) {
    window.dispatchEvent(new CustomEvent("auth:expired"));
  }
  return res;
}

export async function apiGet(path) {
  const res = await apiFetch(path);
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
  return res.json();
}

export async function apiPost(path, body, options = {}) {
  return apiFetch(path, {
    method: "POST",
    body: JSON.stringify(body),
    ...options,
  });
}

export async function apiPatch(path, body) {
  return apiFetch(path, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function apiDelete(path) {
  return apiFetch(path, { method: "DELETE" });
}
