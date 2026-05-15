const API = "";

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
