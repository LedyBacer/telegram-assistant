/* Minimal same-origin API client. Every request carries the raw Telegram
 * initData in the X-Telegram-Init-Data header; the backend verifies its HMAC
 * before trusting the identity. (In Playwright the backend runs with the
 * test-only auth override, so the header content is irrelevant there.)
 */

import { INIT_DATA } from "./telegram.js";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function authHeaders() {
  const headers = { "X-Telegram-Init-Data": INIT_DATA };
  return headers;
}

/**
 * JSON request helper.
 * @param {string} path  e.g. "/api/v1/items"
 * @param {"GET"|"POST"|"PATCH"|"PUT"|"DELETE"} [method]
 * @param {object} [body]   JSON body (POST/PATCH)
 * @returns {Promise<any|null>} parsed JSON, or null on 204
 */
export async function api(path, method = "GET", body) {
  const options = {
    method,
    headers: authHeaders(),
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const res = await fetch(path, options);
  if (res.status === 401) {
    throw new ApiError("auth", 401);
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && typeof data.detail === "string" && data.detail) detail = data.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return null;
  return res.json();
}

/**
 * Multipart upload helper (Files screen).
 * @param {string} path
 * @param {File} file
 */
export async function apiUpload(path, file) {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(path, {
    method: "POST",
    headers: authHeaders(),
    body: form,
  });
  if (res.status === 401) throw new ApiError("auth", 401);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && typeof data.detail === "string" && data.detail) detail = data.detail;
    } catch {
      /* non-JSON */
    }
    throw new ApiError(detail, res.status);
  }
  if (res.status === 204) return null;
  return res.json();
}
