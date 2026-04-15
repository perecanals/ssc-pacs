const API = "";

export async function apiFetch(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (res.status === 401) {
    window.dispatchEvent(new CustomEvent("auth:expired"));
  }
  return res;
}

export async function apiGet(path) {
  const res = await apiFetch(path);
  if (!res.ok) throw new Error(`GET ${path} failed: ${res.status}`);
  return res.json();
}

export async function apiPost(path, body) {
  return apiFetch(path, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function apiDelete(path) {
  return apiFetch(path, { method: "DELETE" });
}
