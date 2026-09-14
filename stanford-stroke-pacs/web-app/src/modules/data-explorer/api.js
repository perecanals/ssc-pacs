import { apiFetch } from "../../api/client";

export const ROOT = "/api/data-explorer";

export async function request(path, method = "GET", body, background = false) {
  const res = await apiFetch(ROOT + path, {
    method,
    trackActivity: !background,
    ...(background ? { headers: { "X-Explorer-Poll": "1" } } : {}),
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const data = await res.json();
  if (!res.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Please check the query settings.",
    );
  return data;
}
